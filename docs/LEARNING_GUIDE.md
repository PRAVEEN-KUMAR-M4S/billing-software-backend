# Learning the Bill Reader API — a code-by-code guide for backend interviews

This document teaches **this repository**, from zero to explaining every file out loud in an
interview. It is not API documentation (that is `README.md`) and not a beginner FastAPI tutorial:
it is the "why is this line here" layer, mapped onto the questions a backend interviewer will
actually ask.

**How to use it**

1. **Pass 1 — read §1–§3.** Get the story and the architecture. You should be able to draw the
   four layers and three tables on a whiteboard from memory.
2. **Pass 2 — read §4 with the code open.** Every file is explained in order, smallest first.
   Keep the editor next to this document; each section names the exact lines being discussed.
3. **Pass 3 — quiz yourself with §6.** Cover the answers. Where you stumble, go back to that
   file's section in §4.

Skip nothing on the first read even if it looks familiar. The value here is not "what is FastAPI"
but "why did *this* project choose *this* line", because that is what gets asked.

---

## 1. The project in one page

**What it is:** a backend for an expense app. A user photographs a bill; Gemini reads it into a
strict JSON shape; the backend re-checks the maths like a cashier would; the user confirms; the
bill is saved, the user's balance goes down, and once a month an AI-written "Smart Tips" card is
shown for that month.

**Stack**

| Piece | Choice | Why it is in the story |
| --- | --- | --- |
| Python 3.14, uv | runtime + package manager | `pyproject.toml`, `uv run` everywhere |
| FastAPI | HTTP framework | routers, dependency injection, automatic OpenAPI |
| Pydantic v2 | validation / serialisation | every request and response is a model |
| SQLAlchemy 2.x async + aiosqlite | ORM | async sessions over SQLite; portable SQL for Postgres |
| SQLite | database | a file, zero setup; `DATABASE_URL` makes Postgres a config change |
| bcrypt | password hashing | slow by design |
| PyJWT | access tokens | stateless auth, 30-minute expiry |
| Google GenAI SDK | AI | bill extraction + monthly tips, schema-constrained JSON |
| pytest | tests | 79 tests, no network, no shared database file |

**Endpoints as built**

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/auth/register` | — | create an account (409 if the email exists) |
| POST | `/auth/login` | — | access token (30 min) + refresh token (30 days) |
| POST | `/auth/refresh` | refresh token in body | rotate the pair; the old refresh token dies |
| POST | `/auth/logout` | refresh token in body | revoke that session (always 204) |
| GET | `/auth/me` | Bearer | the user behind the token |
| PUT | `/account/balance` | Bearer | set / correct the money available |
| GET | `/account` | Bearer | balance left + all-time spent |
| POST | `/bills/extract` | Bearer + multipart JPG/PNG | photo → draft + `problems[]`; writes nothing |
| POST | `/bills/confirm` | Bearer | edited draft → saved bill (201) or 422 |
| GET | `/bills` | Bearer | that user's saved bills, newest first |
| GET | `/insights?month=YYYY-MM` | Bearer | Smart Tips: tip + facts + challenge, one AI call per month |

**Tables**

| Table | One row is… | Notable columns |
| --- | --- | --- |
| `users` | an account | `email` (unique), `hashed_password`, `balance_minor` (nullable) |
| `refresh_tokens` | one logged-in session | `token_hash` (unique, never the token), `expires_at` |
| `expenses` | one confirmed bill | `total_minor` (paise), `category`, `icon`, `items` (JSON), `prompt_version` |
| `monthly_insights` | one month's tip for one user | unique `(user_id, month)`, `source` (`ai`/`local`), `last_attempt_at` |

**Run it**

```bash
uv sync
cp .env.example .env      # set SECRET_KEY, GEMINI_API_KEY
uv run main.py            # http://127.0.0.1:8000/docs
uv run pytest             # 79 tests
```

**The one-sentence pitch**

> A FastAPI backend where a bill photo becomes validated expense data without ever trusting the
> model: the schema constrains what the AI can say, deterministic checks re-verify the maths, and a
> human confirm is the only door into the database.

---

## 2. The architecture rule

```
routers/   the web desk      which URL, which status, which shape in/out
schemas/   the form          the exact fields, types, enums, serialisation
services/  the workshop      how it is done: SQL, hashing, JWT, AI, rules
models/    the warehouse     tables and columns
deps.py    the bouncer       who is calling (logged in? which user?)
```

**Arrows only point down.** A model never imports a router; a service never knows HTTP exists;
`checks.py` imports Pydantic schemas but no database and no FastAPI. This is why the maths rules
can be tested with a plain `pytest` call and no server, and why swapping SQLite for Postgres
touches one setting.

When you describe the project in an interview, lead with this rule. It is the difference between
"a FastAPI app" and "an app with a structure I can defend".

---

## 3. Traces: one request, start to finish

Read these before the file-by-file section; they are the map for everything that follows.

### 3.1 Login

```
POST /auth/login {"email","password"}
  router: app/routers/auth.py::login
    → session.scalar(select(User).where(User.email == email.lower().strip()))
    → verify_password(plain, user.hashed_password)   # bcrypt.checkpw
    → _issue_tokens(user, session)
         create_refresh_token() → (raw, sha256(raw), expires_at)
         session.add(RefreshToken(...)); commit
         TokenResponse(access=create_access_token(user.id), refresh=raw)
  response 200 {"access_token","refresh_token","token_type":"bearer"}
```

Two tokens on purpose: the access token is a signed, stateless JWT (cannot be revoked, so it lives
30 minutes); the refresh token is a random string stored *hashed* in the database (so it can be
revoked, and is rotated on every use).

### 3.2 Protected route

```
GET /bills  with "Authorization: Bearer <jwt>"
  deps.get_current_user (a FastAPI dependency — runs before the route body)
    → HTTPBearer(auto_error=False) → credentials or None
    → decode_access_token(token) → int user_id | None   (signature + exp checked by PyJWT)
    → session.get(User, user_id) → User | None
    → any failure: HTTPException 401
  route body receives a real User object, not an id
```

Every protected route ends with `user: User = Depends(get_current_user)`. That is the whole
authorisation story of the app, and the reason no route ever reads `user_id` from the request body.

### 3.3 Photo → draft (writes nothing)

```
POST /bills/extract  (multipart file)
  router: app/routers/bills.py::extract
    → content_type in {image/jpeg,image/png}?  else 415
    → await file.read(MAX+1)                  # 5 MB + 1 byte
    → empty? 400 ; too big? 413
    → run_in_threadpool(extract_bill, photo, mime)   # the SDK call is blocking
         app/services/extraction.py
           get_client() → genai.Client(api_key=...)
           GenerateContentConfig(system_instruction=EXTRACTION_PROMPT,
                                 response_mime_type="application/json",
                                 response_schema=BillDraft, temperature=0)
           per model in [primary, *fallbacks]:
               503 → try the next model
               429 → stop, raise ExtractionError(status=429)
               other → raise ExtractionError(status=502)
           return response.parsed (a BillDraft) or parse response.text as JSON
    → ExtractionError → HTTP 503 (+Retry-After: 60) / 429 / 502, message says nothing was saved
    → ExtractionReport(draft=draft, problems=check_bill(draft))
  response 200 {"draft": {...}, "problems": [{field, message}, ...]}
```

Nothing is stored. The photo lives only in memory for the duration of the call.

### 3.4 Confirm (the only door to the database)

```
POST /bills/confirm {"draft": {...}}
  router: app/routers/bills.py::confirm
    → problems = check_bill(draft)
      any problem → 422 {"message":"Fix the highlighted fields…", "problems":[...]}
    → Expense(user_id=user.id, …, total_minor=int(draft.total*100),
              items=[json.loads(i.model_dump_json()) for i in draft.items],
              prompt_version=PROMPT_VERSION)
    → session.add(row)
    → session.execute(update(User).where(User.id == user.id)
                        .values(balance_minor=User.balance_minor - row.total_minor))
    → commit, refresh(row)            # both writes, or neither
  response 201 ExpenseOut (money back out as "128.00")
```

Two ideas to be able to defend: (1) checks run **again** on the edited draft, because the first
run was on the model's draft, not the user's; (2) the balance decrement is a single SQL statement
in the same transaction as the insert, because read–modify–write in Python loses updates when two
bills are confirmed at once.

### 3.5 Smart Tips

```
GET /insights?month=2026-10
  router: app/routers/insights.py::get_insights
    → month or current; month_bounds("2026-10") → (2026-10-01, 2026-11-01)   # half-open range
      invalid → 422
    → facts_for(session, user.id, start, end)      # one SELECT, no cache
    → facts.bills == 0? return a local "no bills yet" tip, no AI call, nothing stored
    → row = saved tip for (user, month)?
         source == "ai"                        → return it (0 calls)
         source == "local" and < 6 h old       → return it (0 calls)
         otherwise                              → generate
    → generate_tip(facts, "October 2026") in a threadpool
         response_schema=TipDraft (category enum, headline, action_label)
         503 → next model; 429/other → GeminiError
      success → SmartTip(source="ai")
      GeminiError → local_tip(facts)                # still true, just plainer
    → upsert monthly_insights; commit
      IntegrityError (two first-opens raced) → rollback, re-read, return that row
  response 200 {month, month_label, generated_at, tip, facts, challenge}
```

Facts are recomputed every request so a bill added mid-month shows immediately; only the sentence
is frozen. That split is the design idea worth explaining.

---

## 4. Code by code

Order: entry point → settings → database → models → schemas → pure services → dependencies →
routers. Each file: what it is for, the lines that matter, and the interview takeaway.

### 4.1 `main.py` — the launcher (5 lines)

```python
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app="app.app:app", host="0.0.0.0", port=8000, reload=True)
```

- The import string `"app.app:app"` (not the object) is what makes `reload=True` work: uvicorn can
  re-import the module in a child process. Passing the object would defeat reload.
- `0.0.0.0` is required for a phone on the same network to reach a dev laptop. It is a development
  convenience, not a deployment plan.
- No app logic here. That is deliberate: `main.py` is a launcher, so importing the app (tests,
  a WSGI-style host, a script) never starts a server.

**Q: why does `uvicorn` need a string?** Because reload spawns a subprocess that imports the module
by name. Also it avoids double-importing during reload.

### 4.2 `app/app.py` — the application object

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_db_and_table()
    yield

app = FastAPI(title="Bill Reader API", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(account.router)
app.include_router(bills.router)
app.include_router(insights.router)
```

- **`lifespan`** is the modern replacement for `@app.on_event("startup")`. It runs once when the
  process starts (and once per reload), then yields control. Code after `yield` would run on
  shutdown; there is none here, though a production app would `await engine.dispose()` there.
- **`create_db_and_table()`** creates missing tables and adds missing nullable columns; it does not
  seed data, so a fresh install is just an empty database.
- The app file is the *inventory* of routers: if a URL is 404, the answer is usually "that router
  is not included here".

**Q: why not create tables at import time?** Because importing the app should not touch the disk.
Tests import the app to exercise it; they must be able to point the engine at a temp file first
(see §5.7).

### 4.3 `app/config.py` — one owner for settings

Three kinds of values live here: **secrets/settings** (`SECRET_KEY`, `GEMINI_API_KEY`,
`DATABASE_URL`, model ids), **domain constants** (`ALLOWED_CURRENCIES`, `TOTAL_TOLERANCE`,
`CASHIER_ROUNDING`, the category→icon map), and **prompts** (`EXTRACTION_PROMPT`,
`INSIGHTS_PROMPT`).

```python
load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
```

- `load_dotenv()` reads `.env` into `os.environ` at import. `.env` is git-ignored; `.env.example`
  is the template that *is* committed.
- The API key has **no default** on purpose: a missing key must surface as a clear 502 at request
  time, not as a crash at import (the app still serves `/docs`, auth and the checks without it).
- `GEMINI_MODEL` is a string from the environment because model ids change faster than deploys.

**`EXPENSE_CATEGORIES`** is a `dict[str, tuple[str, ...]]` of 10 slugs to **exact hugeicons 1.2.0
member names**, e.g. `"dining": ("strokeRoundedRestaurant01", "strokeRoundedCoffee01",
"strokeRoundedHamburger01")`. The first icon is the category's default.

Why a tuple of real names rather than free text: the Flutter client writes
`HugeIcon(icon: HugeIcons.strokeRoundedHome01)`. A Dart member cannot be looked up by string, so an
invented name is a compile-time hole the app cannot render. Constraining the model to names that
exist removes that whole failure class.

Derived from it:

```python
_CATEGORY_HELP = "\n".join(f"- {category}: {', '.join(icons)}" ...)
```

The prompt is generated **from the same dict** the schema enums use, so the three consumers (the
model's instructions, the Pydantic `Literal`, the Flutter map) cannot drift apart. That is a
"single source of truth" answer you can give with a concrete example.

**`EXPENSE_CATEGORY_LABELS`** is the human half: `"dining": "Food & drink"`. Slugs are for code and
the API; labels are for prompts and the UI.

**Money constants:**

```python
TOTAL_TOLERANCE = Decimal("0.02")   # when a Round Off line is printed
CASHIER_ROUNDING = Decimal("1.00")  # when none is printed
```

`Decimal`, never float: `0.1 + 0.2 == 0.30000000000000004` in binary floating point, and money must
add up to the paise.

**`PROMPT_VERSION = "v5"`** is stored on every confirmed bill (`expenses.prompt_version`), so a
change in extraction quality can be traced to the prompt that produced the row. `INSIGHTS_PROMPT_VERSION
= "v1"` does the same for saved tips. Versioning a prompt is cheap and it is exactly the kind of
production thinking interviewers like.

**`INSIGHTS_RETRY_HOURS = 6`** is the fallback retry window explained in §5.3.

### 4.4 `app/services/db.py` — engine, sessions, startup schema fix

```python
engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
```

Four ideas packed into nine lines:

1. **One engine per process.** The engine owns the connection pool. Creating one per request is a
   common beginner bug and leaks connections.
2. **A session per request**, created by a dependency and always closed by `async with`, even when
   the route raises.
3. **`expire_on_commit=False`.** By default SQLAlchemy expires every attribute after `commit()`,
   so touching `row.id` afterwards triggers a lazy `SELECT` — which in async code is an
   `InvalidRequestError` (or an extra await you did not write). Turning it off keeps the objects
   usable after commit; `await session.refresh(row)` is still used where a server-side default
   (`created_at`) must be read back.
4. **`Base`** is the declarative registry: every model subclasses it, and `Base.metadata` is the
   list of all tables. `create_all` walks it.

**`_add_missing_nullable_columns`** is the hand-rolled migration:

```python
inspector = inspect(connection)
for table in Base.metadata.sorted_tables:
    existing = {c["name"] for c in inspector.get_columns(table.name)}
    for column in table.columns:
        if column.name in existing or not column.nullable:
            continue
        connection.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type}'))
```

`create_all()` creates missing tables but **never alters an existing one**. This project has an
on-disk SQLite file that predates `category`, `icon` and `balance_minor`, so without this, adding a
field breaks every insert until someone deletes the database. The helper adds only **nullable**
columns — nothing is dropped, renamed, retyped or made `NOT NULL`, because those are migration
decisions a startup hook must not guess. Non-nullable additions therefore require a real migration,
which is the honest limitation to state.

It also backfills the one thing whose value is knowable:

```python
UPDATE expenses SET category = :category, icon = :icon WHERE category IS NULL
# category="other", icon=strokeRoundedHelpCircle
```

A bill nobody classified becomes "other" — the one truthful bucket — with that category's
default icon, so old rows still render in the app.

**Q: why not Alembic?** For one developer and a file database, Alembic is more machinery than
change. The honest answer is "this is the point at which I would add Alembic": multiple
environments, non-nullable columns, renames, backfills that need to be reversible.

### 4.5 Models — `app/models/*.py`

SQLAlchemy 2.x typed style, e.g. `app/models/user.py`:

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    balance_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

- `Mapped[T]` + `mapped_column(...)` is the 2.x syntax: the annotation is the Python type the
  attribute carries, the call declares the SQL type and constraints. It gives type checkers and
  dataclass-style construction something to work with, unlike the older `Column(...)` form.
- `unique=True, index=True` on `email` gives both the constraint and the lookup index.
- `balance_minor` is **nullable on purpose**: `NULL` means "the user has not told us their balance
  yet". That is different from `0`, and it makes the spend-down arithmetic correct for free —
  `NULL - 128` is `NULL` (see §5.6).
- `server_default=func.now()` lets the database set `created_at`; the API never trusts a client
  clock.

`RefreshToken` (`app/models/refresh_token.py`) stores `token_hash` (unique, indexed) and
`expires_at`, never the raw token: a leaked database yields unusable hashes.

`Expense` (`app/models/expense.py`) is the money story in one table:

```python
total_minor: Mapped[int] = mapped_column(Integer)          # paise, exact
category: Mapped[str | None] = mapped_column(String(32), nullable=True)
icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
items: Mapped[list[dict]] = mapped_column(JSON)
prompt_version: Mapped[str] = mapped_column(String(16))

@property
def total(self) -> Decimal:
    return (Decimal(self.total_minor) / 100).quantize(Decimal("0.01"))
```

`category`/`icon` are nullable in *storage* even though the API requires them: rows created before
those fields existed are `NULL` until the startup backfill (or a migration) fills them. Model type
≠ API contract is a useful distinction to be able to name.

`MonthlyInsight` (`app/models/insight.py`) is the monthly cache:

```python
__table_args__ = (UniqueConstraint("user_id", "month", name="uq_monthly_insights_user_month"),)
```

That constraint is the mechanism behind "one AI call per month": the database, not a Python `if`,
guarantees a single row.

**Q: why store `items` as JSON instead of an `expense_items` table?** Because nothing queries
individual lines yet: the list screen reads names and amounts. A child table is the right move the
first time a screen needs "all Milk purchases" — and that is the answer, not "JSON is easier".

### 4.6 Schemas — `app/schemas/*.py`

Pydantic models are used for three different jobs in this project, and being able to name them is
half the interview:

1. **request validation** (`UserCreate`, `BalanceSetRequest`, `BillConfirmRequest`);
2. **response filtering** (`UserOut` has no password hash; `ExpenseOut` has no `prompt_version`);
3. **the shape the AI must fill** (`BillDraft`, `TipDraft`) — the same class is handed to Gemini
   as `response_schema`.

`app/schemas/auth.py` is the simplest: `email: str` with `min_length/max_length`, `password` with
`min_length=6, max_length=72`. The 72 is bcrypt's byte limit — a validator that encodes a real
library constraint instead of a made-up rule.

`app/schemas/bill.py` is the heart. Three helpers do the heavy lifting.

**(a) `parse_money` — anything the model might say → `Decimal` rupees, or `None`:**

```python
def parse_money(raw: object) -> Decimal | None:
    ...
    if isinstance(raw, str):
        text = raw.strip()
        for symbol in _SYMBOLS:            # ₹ $ £ € , nbsp
            text = text.replace(symbol, "")
        text = text.upper().replace("RS.", "").replace("RS", "").replace("INR", "").strip()
        amount = Decimal(text)
    ...
    return amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
```

It accepts `128`, `128.0`, `"128"`, `"₹1,234.50"`, `"Rs. 128"`; rejects junk (`"N/A"`, `"-"`,
booleans) by returning `None` so one unreadable cell costs one field, not the whole bill. `Decimal`
is constructed from a normalised string, never from the original float, because `Decimal(128.0)`
carries binary noise while `Decimal("128.0")` does not.

**(b) `Money` — rupees in, a two-place string out, a plain number for the model:**

```python
Money = Annotated[
    Decimal,
    PlainSerializer(format_money, return_type=str, when_used="json"),
    WithJsonSchema({"type": "number"}, mode="validation"),
]
```

Pydantic's default schema for `Decimal` is `number | string`, which Gemini would receive as
`anyOf(NUMBER, STRING)` — an ambiguous instruction that invites the string `"128.00"`. `WithJsonSchema`
pins what the model sees to a plain number; `PlainSerializer` formats what *we* return as
`"128.00"`. The two audiences are different, and this type serves both without a copy of the field
in each schema.

**(c) `Category` and `Icon` — enums generated from config:**

```python
Category = Literal[tuple(EXPENSE_CATEGORIES)]
Icon = Literal[tuple(icon for icons in EXPENSE_CATEGORIES.values() for icon in icons)]
```

`Literal[("a","b")]` is an enum in JSON Schema (`{"type":"string","enum":[...]}`), so the model
*stops being able to* answer `"medicine"` or a misspelled icon. Validation is the guarantee; a
prompt instruction is only a request. This is the single most transferable idea in the project:
**turn "please don't" into "cannot".**

`BillDraft` itself:

```python
class BillDraft(BaseModel):
    shop: str | None = ...
    date: str | None = ...              # ISO string, validated later in checks.py
    currency: str = DEFAULT_CURRENCY
    category: Category | None = ...
    icon: Icon | None = ...
    total: Money | None = ...
    round_off: Money | None = ...
    items: list[BillItem] = []
```

Every field is optional **on purpose**: a blurry bill is a normal outcome, and "unreadable" must be
representable. The `problems[]` list then tells the user which field to fix. A schema that rejected
`None` would turn a bad photo into a 422 from the model's output, which is a worse experience and a
worse error message.

The `model_validator` enforces the icon/category relationship:

```python
@model_validator(mode="after")
def _icon_belongs_to_category(self):
    if self.category is not None and self.icon not in EXPENSE_CATEGORIES[self.category]:
        self.icon = EXPENSE_CATEGORIES[self.category][0]
    return self
```

An icon from another category, or no icon at all, becomes that category's default — never an
invented name. Note `mode="after"`: it runs on validated data, when `category` is already a valid
slug.

`app/schemas/account.py` reuses `Money` and `parse_money` — one money type for the whole API — and
adds `ge=0` on the balance plus a before-validator so `"5000"` and `5000` both work.

`app/schemas/insight.py` defines the response (`InsightsOut`, `SmartTip`, `QuickFacts`,
`CategoryFact`, `ShopFact`, `Challenge`) and `TipDraft`, which is **only** the AI's part:
`category`, `headline`, `action_label`. Its validator collapses whitespace and rejects an empty
string, because "the model returned spaces" is a failed generation, not a tip.

### 4.7 `app/services/auth.py` — passwords and tokens, no HTTP, no database

Pure functions only. That is what makes them testable with no server and reusable by login,
refresh and any future route.

```python
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
```

- **bcrypt, not SHA-256.** A fast hash is brute-forceable at billions of guesses per second; bcrypt
  is deliberately slow and salted (the salt is inside the returned string). The 72-byte limit is
  why `UserCreate.password` caps at 72.
- `bcrypt.hashpw` returns bytes; `.decode()` stores text in SQLite.

```python
def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
```

- `sub` is a **string** because the JWT spec says so; `decode_access_token` converts back with
  `int(payload["sub"])`.
- `exp` is a timezone-aware datetime; PyJWT converts it to a Unix timestamp and validates it on
  decode. No hand-written expiry check.
- `HS256` means one shared secret signs and verifies. Fine for one service; asymmetric `RS256` is
  what you reach for when other services must verify without holding the signing key.

```python
def decode_access_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
```

A signature failure, expiry, wrong algorithm and a malformed value all become the same `None`; the
route turns that into 401. Never leak *which* check failed.

```python
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def create_refresh_token() -> tuple[str, str, datetime]:
    raw = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return raw, hash_token(raw), expires_at
```

A refresh token is a **random secret**, not a JWT: 256 bits from `secrets`, not `random`. It is
stored as a SHA-256 hash, so the database never holds a usable credential. SHA-256 is fine here
(unlike for passwords) because the input is already high-entropy — there is nothing to brute-force.

**Q: why two tokens at all?** The access token is stateless and fast (no DB read to authorise), but
it cannot be revoked before it expires. The refresh token lives in the database, so logout can
revoke it, and rotation on every use limits the damage if one leaks. Short access + revocable
refresh is the standard compromise.

### 4.8 `app/deps.py` — the bouncer

```python
bearer_scheme = HTTPBearer(auto_error=False)

async def get_current_user(credentials=Depends(bearer_scheme), session=Depends(get_async_session)) -> User:
    if credentials is None:
        raise HTTPException(401, "Not authenticated")
    user_id = decode_access_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(401, "Invalid or expired token")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "User no longer exists")
    return user
```

- `auto_error=False` stops FastAPI from returning its own 403 for a missing header; the app controls
  the status (401) and the message.
- **Three distinct failures, three honest messages.** A deleted user is not the same as a bad token,
  and neither is the same as no token at all — useful when debugging a client.
- `session.get(User, id)` is the identity-map lookup: no `SELECT` if the row is already in the
  session, a primary-key fetch otherwise.
- **Dependency caching:** FastAPI caches a dependency's result for the duration of one request by
  default. So the `get_async_session` used here and the one a route declares are the *same*
  `AsyncSession`. That is why `PUT /account/balance` can mutate the `User` this function returned and
  commit it in the route's session.

**Q: why inject the ORM object instead of the user id?** So no request body or query parameter can
name another user's id. Ownership is always the token's subject.

### 4.9 `app/routers/auth.py`

```python
@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: UserCreate, session=Depends(get_async_session)):
    email = data.email.lower().strip()
    if await session.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(email=email, hashed_password=hash_password(data.password))
    session.add(user); await session.commit(); await session.refresh(user)
    return user
```

- Lower-casing and stripping **before** the lookup is what stops `A@x.com` and `a@x.com` becoming
  two accounts. (A production system also normalises Unicode and uses a case-insensitive collation.)
- `409 Conflict` for an existing email; a `500` from a unique-constraint violation would be a
  race-condition bug, and the unique index is still there as the backstop.
- `response_model=UserOut` strips `hashed_password` even though the route returns the whole row. The
  response model is a security boundary, not documentation.
- `refresh(user)` after commit reads back `id` and `created_at` generated by the database.

```python
async def _issue_tokens(user, session) -> TokenResponse:
    raw, token_hash, expires_at = create_refresh_token()
    session.add(RefreshToken(token_hash=token_hash, user_id=user.id, expires_at=expires_at))
    await session.commit()
    return TokenResponse(access_token=create_access_token(user.id), refresh_token=raw)
```

One helper used by both login and refresh, so the two paths cannot drift.

```python
@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, session=...):
    user = await session.scalar(select(User).where(User.email == data.email.lower().strip()))
    if user is None or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, "Incorrect email or password")
    return await _issue_tokens(user, session)
```

The same 401 for unknown email and wrong password: no account enumeration. Note the short-circuit —
`verify_password` only runs if a user was found, so timing does reveal existence slightly; the
strict fix is bcrypt-checking a dummy hash, which is worth mentioning but was not needed here.

```python
@router.post("/refresh")
async def refresh(data: RefreshRequest, session=...):
    row = await session.scalar(select(RefreshToken).where(
        RefreshToken.token_hash == hash_token(data.refresh_token)))
    if row is None or row.expires_at < utcnow():
        raise HTTPException(401, "Invalid or expired refresh token")
    user = await session.get(User, row.user_id)
    if user is None:
        raise HTTPException(401, …)
    await session.delete(row)          # single use
    return await _issue_tokens(user, session)   # commits delete + insert together
```

Rotation: the presented token is deleted and a new pair issued in the same commit. If someone
replays an old refresh token, it is already gone — the classic detection signal for a stolen token.

`logout` deletes the row if found and **always** answers 204: whether a token existed is not
information a caller should be able to probe.

`/auth/me` is the smallest protected route and the template for all others:

```python
@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
```

### 4.10 `app/services/gemini.py` — one client, one error vocabulary

Added when the second AI feature (Smart Tips) arrived. Both features need the same three things, so
none of them is copied:

```python
class GeminiError(Exception):
    def __init__(self, message, *, status_code=502):
        super().__init__(message); self.status_code = status_code

_client: genai.Client | None = None

def get_client():
    global _client
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY is not set. Add it to .env …")
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client
```

- **Lazy client** so importing the app never needs a key; `/docs`, auth and the checks work while
  the key is being set up.
- **A module-level singleton** because the SDK client holds an HTTP connection pool.
- `status_code` on the exception is what a route should answer with when there is no fallback. The
  service decides the status; the router does not sniff strings.

```python
def candidate_models() -> list[str]:
    models = []
    for name in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        name = (name or "").strip()
        if name and name not in models:
            models.append(name)
    return models
```

Order is preserved and duplicates removed, so the primary model is always first and a duplicated
fallback does not cost a second call.

```python
def upstream_code(exc) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    text = str(exc).upper()
    if "UNAVAILABLE" in text: return 503
    if "RESOURCE_EXHAUSTED" in text: return 429
    return None
```

Different SDK versions expose the HTTP status differently (an `int`, or only inside the message), so
this normalises it once. That is the seam that makes the 503-vs-429 policy possible at all.

`extraction.py` keeps its public name with an alias:

```python
ExtractionError = GeminiError
```

So `except ExtractionError` still catches a missing key raised inside the shared client, and no
caller or test had to change. During the refactor that mattered: the tests patched private names in
`extraction`, and they were updated to patch the shared module (which is the correct target).

### 4.11 `app/services/extraction.py` — one photo, one draft

```python
config = types.GenerateContentConfig(
    system_instruction=EXTRACTION_PROMPT,
    response_mime_type="application/json",
    response_schema=BillDraft,
    temperature=0,
)

response = client.models.generate_content(
    model=model,
    contents=["Extract this bill.", types.Part.from_bytes(data=photo_bytes, mime_type=mime_type)],
    config=config,
)
```

- `response_schema=BillDraft` is Gemini's structured-output mechanism (the equivalent of a forced
  tool call). The API constrains generation to the schema; the SDK hands back `.parsed` as a
  Pydantic object.
- `temperature=0` because extraction is transcription, not creative writing. There is one right
  answer and no benefit to variation.
- The image is sent as a `Part` with the real MIME type, not base64 in a prompt string.
- The photo is a local variable: never written to disk, never stored in the database. Personal data
  minimisation, and it also avoids a file-cleanup problem.

Failure policy:

```python
for model in candidate_models():
    try: response = client.models.generate_content(...)
    except Exception as exc:
        code = upstream_code(exc)
        if code == 503: overloaded.append(model); continue    # capacity → try another model
        if code == 429: raise ExtractionError(… "quota" …, status_code=429)  # bucket, not capacity
        raise ExtractionError(f"Gemini request failed: {exc}")                 # 502
    return _draft_from(response)
raise ExtractionError("all models busy …", status_code=503)
```

Why 503 falls over but 429 does not: rate limits are **per project**, so a second model is still the
same quota bucket — retrying would just burn time. Overload (“This model is currently experiencing
high demand”) is per **model**, so a different model genuinely may have capacity. These two errors
look similar in a client and need opposite reactions:

| Error | Meaning | Action | HTTP back to the app |
| --- | --- | --- | --- |
| 503 `UNAVAILABLE` | this model is overloaded | try the next model once | 503 + `Retry-After: 60` |
| 429 `RESOURCE_EXHAUSTED` | the project's quota is spent | stop | 429, message says midnight Pacific |
| 400/403 | key or request is wrong | stop | 502, message names the cause |

```python
def _draft_from(response) -> BillDraft:
    draft = response.parsed
    if isinstance(draft, BillDraft): return draft
    raw = response.text
    ...
    return BillDraft.model_validate_json(raw)
```

The JSON-text fallback covers an SDK/model combination that ignores the schema and returns text.
Note it still validates through the same Pydantic model, so a text reply cannot smuggle in an
unknown field.

The function is synchronous because the SDK is; the route calls it through
`run_in_threadpool`, which is FastAPI's way to keep the event loop responsive without rewriting the
SDK call as async.

### 4.12 `app/services/checks.py` — the cashier

One pure function, `check_bill(draft) -> list[Problem]`. No HTTP, no database, no AI. That is why
its 20+ behaviours can be tested in milliseconds.

```python
def check_bill(draft: BillDraft) -> list[Problem]:
    problems = []
    ...
    return problems
```

What it verifies, and why each one exists:

| Check | Field reported | Reason |
| --- | --- | --- |
| shop present | `shop` | a bill with no seller is unreadable |
| date is ISO `YYYY-MM-DD` | `date` | rejects `12/03/2026` (March or December?) and `2026-02-30` |
| currency allowed (INR) | `currency` | the app only handles one currency today |
| category present | `category` | the app filters by it |
| total > 0 | `total` | a zero bill is not a bill |
| at least one item | `items` | nothing to show |
| item name present | `items[i].name` | per-row highlight in the fix form |
| item qty ≥ 1 (when printed) | `items[i].qty` | `null` is fine; `0` or negative is not |
| item amount present, ≥ 0 | `items[i].amount` | per-row |
| a line is not bigger than the whole bill | `items[i].amount` | an MRP/rate read as money, or paise vs rupees |
| `Σ amounts + round_off ≈ total` | `total` | the printed numbers must reconcile |

The tolerance rule is the interesting part:

```python
expected = item_total + round_off
if abs(expected - draft.total) > tolerance:
    ...
```

```python
tolerance = TOTAL_TOLERANCE if draft.round_off is not None else CASHIER_ROUNDING
```

- If the paper prints a Round Off line, the maths must close to ±0.02 — the printed line has to be
  real.
- If it prints none, up to ₹1.00 is accepted, because a cashier can round 127.49 to 128.00 and the
  paper will not show it. This is exactly the reported real-world bill the project was refined
  against, and it is a nice “tell me about a tricky bug” story (§5.1).

Two structural decisions worth saying out loud:

- **The function collects all problems instead of raising on the first.** A fix form should show
  every bad field in one pass; a `raise` would make the user resubmit once per error.
- **The check runs again in `/confirm`.** The first run judged the model's draft; the second judges
  the *user's edited* draft. A hand-edited wrong total must not slip through just because it was
  once checked.

### 4.13 `app/routers/bills.py`

**`POST /bills/extract`** — validation, then AI, then checks; no writes.

```python
media_type = (file.content_type or "").split(";")[0].strip().lower()
if media_type not in ALLOWED_IMAGE_TYPES:
    raise HTTPException(415, "Upload a JPG or PNG photo.")
photo = await file.read(MAX_UPLOAD_BYTES + 1)
if not photo:
    raise HTTPException(400, "The uploaded file is empty.")
if len(photo) > MAX_UPLOAD_BYTES:
    raise HTTPException(413, …)
```

- `split(";")[0]` because a browser sends `image/png; charset=binary`.
- Reading `MAX + 1` bytes is enough to know it is too big **without** loading a 40 MB file into
  memory. That is the kind of line an interviewer notices.
- Empty file is 400 (bad request), too big is 413 (payload too large) — the statuses say different
  things.

```python
try:
    draft = await run_in_threadpool(extract_bill, photo, media_type)
except ExtractionError as exc:
    raise HTTPException(
        exc.status_code,
        f"Could not read this bill, and nothing was saved. {exc}",
        headers={"Retry-After": "60"} if exc.status_code == 503 else None,
    ) from exc
return ExtractionReport(draft=draft, problems=check_bill(draft))
```

The service chooses the status; the route forwards it and adds “nothing was saved” so the client
knows it does not need to clean up. `Retry-After` only makes sense for a busy model.

**`POST /bills/confirm`** — the only writer.

```python
problems = check_bill(draft)
if problems:
    raise HTTPException(422, detail={
        "message": "Fix the highlighted fields before saving.",
        "problems": [p.model_dump() for p in problems],
    })
```

A structured `detail` (not a string) is what lets the Flutter fix form highlight a row: the client
reads `problems[i].field`.

```python
total_minor=int(draft.total * 100),
items=[json.loads(item.model_dump_json()) for item in draft.items],
```

`model_dump_json()` keeps each amount as the string `"127.49"` — JSON-safe, and it re-validates back
into a `Decimal` when read out. A `model_dump()` would have produced `Decimal` objects that the JSON
column cannot serialise.

```python
session.add(row)
await session.execute(
    update(User).where(User.id == user.id)
    .values(balance_minor=User.balance_minor - row.total_minor)
)
await session.commit()
await session.refresh(row)
```

The decrement is one `UPDATE … SET balance_minor = balance_minor - ?` in the same transaction as the
insert. Two bills confirmed concurrently cannot both read the old value — there is no read in
Python to race with. And because `NULL - 128 = NULL` in SQL, an account with no balance is
untouched without a single `if`.

**`GET /bills`** scopes every query by `user_id` and orders by `date.desc()`. There is no
pagination yet; the honest answer is “this is the point where `limit`/`offset` or keyset pagination
belongs” (§9).

### 4.14 `app/routers/account.py`

```python
async def account_snapshot(user, session) -> AccountOut:
    spent_minor = await session.scalar(
        select(func.coalesce(func.sum(Expense.total_minor), 0))
        .where(Expense.user_id == user.id)
    )
    return AccountOut(
        balance=None if user.balance_minor is None else Decimal(user.balance_minor) / 100,
        spent=Decimal(spent_minor) / 100,
        currency=DEFAULT_CURRENCY,
    )
```

- `coalesce(sum(...), 0)` turns “no rows” (`NULL`) into `0`, so the client never sees a null spent.
- The balance is **stored**, the spend is **derived**. The opening money is something only the user
  knows; a computed balance would silently assume they started at zero.
- `PUT` **replaces** the number rather than adding to it: “I have ₹5,000” is a statement of fact, and
  a second call is a correction. A top-up would be a separate “add” endpoint.
- `ge=0` on the request plus the before-validator means `"-1"` is a 422 while `5000` and `"5000.00"`
  are both accepted.

**Q: what happens if a bill costs more than the balance?** The bill is saved and the balance goes
negative. The bill is real; refusing it would lose data, and an overspent balance is information,
not an error.

### 4.15 `app/services/insights.py`

**Month maths, portable across databases:**

```python
def month_bounds(month: str) -> tuple[date, date]:
    if len(month) != 7 or month[4] != "-" or not (month[:4].isdigit() and month[5:].isdigit()):
        raise ValueError("month must be YYYY-MM, e.g. 2026-10")
    start = date(int(month[:4]), int(month[5:]), 1)
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end
```

A **half-open range** (`>= start AND < end`) instead of `strftime('%Y-%m', date) = month`, because
that function is SQLite/Postgres-specific and it defeats an index. Half-open also handles December
and leap Februaries without special cases.

**The facts — one SELECT, aggregation in Python:**

```python
result = await session.execute(
    select(Expense.category, Expense.shop, Expense.total_minor).where(
        Expense.user_id == user_id, Expense.date >= start, Expense.date < end))
rows = result.all()
...
biggest_category = min(category_totals, key=lambda slug: (-category_totals[slug], slug))
```

For a month, a user has tens of rows, so one indexed read and a few dicts is smaller and more
readable than three `GROUP BY` queries. The tie-break `(-total, slug)` makes “biggest category”
deterministic — without it, two equal categories could flip between requests, and the AI tip could
change for no reason. In a bigger dataset this becomes `GROUP BY` with `ORDER BY SUM(...) DESC
LIMIT 1`.

Average is exact:

```python
average_bill=(_rupees(total_minor) / len(rows)).quantize(_PAISE, rounding=ROUND_HALF_UP)
```

₹690.00 / 2 = ₹345.00; ₹500.00 / 3 = ₹166.67 (round half up), never a float.

**The local tip** is the fallback and the empty-month answer:

```python
def local_tip(facts) -> SmartTip:
    if facts.biggest_category is None:
        return SmartTip(text="No bills this month yet. …", category=None, …)
    fact = facts.biggest_category
    return SmartTip(
        text=f"{fact.label} is your biggest expense this month: {format_money(fact.total)} "
             f"across {fact.bills} bills. Worth a look at the small ones.",
        category=fact.category,
        action_label=f"See {fact.label} bills",
        source="local",
    )
```

It is built from the same numbers as the AI tip, so a failure costs style, not truth.

**The one AI call:**

```python
def _brief(facts, month_label_text) -> str:
    return json.dumps({
        "month": month_label_text, "currency": "INR", "bills": facts.bills,
        "total": format_money(facts.total), "average_bill": format_money(facts.average_bill),
        "biggest_category": {…}, "most_visited_shop": {…},
    }, ensure_ascii=False)
```

Only the computed facts go in — nothing the model could invent. The schema constrains the answer:

```python
class TipDraft(BaseModel):
    category: Category          # enum: cannot name a category the app does not know
    headline: str               # ≤ 2 sentences, under 160 chars (prompt) + not-empty (validator)
    action_label: str           # the button label
```

Same 503/429 policy as extraction, same `candidate_models()` order, different temperature (0.4 —
some variety is fine for prose, not for transcription).

### 4.16 `app/routers/insights.py` — the cache decision

```python
if facts.bills == 0:
    return InsightsOut(…, tip=service.local_tip(facts), …)   # no AI, nothing stored

row = await _saved(session, user.id, month)
if row is not None:
    retry_due = row.source != "ai" and (
        now - row.last_attempt_at >= timedelta(hours=INSIGHTS_RETRY_HOURS))
    if not retry_due:
        return _payload(row, month, label, facts, challenge)
```

Read that as a table:

| Saved row | Age | Result |
| --- | --- | --- |
| `source="ai"` | any | return it — a real tip is final for the month |
| `source="local"` | < 6 h | return it — do not hammer a struggling model |
| `source="local"` | ≥ 6 h | try again; a quota day on the 1st should not own the month |
| none | — | generate once |

Why cache the tip but not the facts: the sentence is the expensive, stable part; the numbers are
cheap and *should* move when the user adds a bill mid-month. Caching a snapshot of the facts would
make yesterday's total appear on today's screen.

```python
try:
    draft = await run_in_threadpool(service.generate_tip, facts, label)
    tip = SmartTip(text=draft.headline, category=draft.category,
                   action_label=draft.action_label, source="ai")
except GeminiError:
    tip = service.local_tip(facts)
```

A quota error, an overloaded model, a missing key, a malformed reply — all become the local tip and
HTTP 200. Only the storage layer could still fail.

```python
if row is None:
    row = MonthlyInsight(user_id=user.id, month=month)
    session.add(row)
row.tip_text = tip.text; …; row.last_attempt_at = now
try:
    await session.commit()
except IntegrityError:
    await session.rollback()
    row = await _saved(session, user.id, month)   # the other request won the race
    if row is None: raise HTTPException(503, …)
    return _payload(row, month, label, facts, challenge)
```

The unique constraint is the concurrency control; `IntegrityError` is the expected outcome of a
race, not a bug. This is the honest “two requests at once” answer: one AI call is wasted, no user
sees an error, and the database stays consistent.

---

## 5. Deep dives (the parts interviewers pull on)

### 5.1 Money: rupees in the API, paise in the database, never a float

The chain is always the same direction:

```
model JSON  "127.49"  →  parse_money → Decimal("127.49") → BillDraft
user confirms       →  int(total * 100) = 12749   → SQLite INTEGER
read back           →  Decimal(total_minor) / 100 → "127.49" in JSON
```

Rules the code enforces:

- **`Decimal`, never `float`.** Floats cannot represent 0.10 exactly, so sums drift; the tests are
  exact comparisons (`127.49 + 0.51 == 128.00`).
- **One conversion point each way** (`/bills/confirm` down, `Expense.total` / `account_snapshot` up).
  Two conversion sites is how a 100× bug happens.
- **The API always speaks rupees with two places**, serialised as a string (`"128.00"`), so a JS
  client cannot turn it into a float by accident. Storage is integer paise, the only exact
  representation SQLite has.
- **Never multiply by quantity.** The printed Amount column is the whole line; qty is information.

**The bug story to tell.** A pharmacy bill printed MRP ₹15.00, qty 10, 15% discount and 5% GST, with
an Amount column of ₹127.49 and a net payable of ₹128.00 (cashier rounding, no Round Off line). The
extractor first returned `price_minor: 12749` while `total_minor: 12800` — a 100× unit mix — and then,
after the unit fix, returned the MRP ₹15.00 as the line amount with qty 10, so the line looked like
₹150 against a ₹128 bill.

Three layers of fix, in order of strength:

1. **The prompt** (v5) now names the printed Amount column as the only line money, says MRP/rate/%
   are never it, and forbids multiplying by qty — with the exact bill as a worked example.
2. **The schema** requires rupees with two places and the enum-constrained category/icon, so whole
   classes of answers are impossible.
3. **The check** catches what the first two miss: a line bigger than the bill is flagged on that row,
   and a ₹1 gap with no printed round-off is accepted as cashier rounding.

The lesson to state: *a prompt is a request, a schema is a constraint, and a check is a guarantee —
you need all three because the model is not a compiler.*

### 5.2 AI: constrain the output, then re-verify it

| Layer | Mechanism | What it prevents |
| --- | --- | --- |
| Generation | `response_schema=BillDraft` / `TipDraft`, `response_mime_type="application/json"` | prose replies, missing fields, invented icon names |
| Parsing | `.parsed` (Pydantic), JSON-text fallback validated by the same model | malformed replies, unknown fields |
| Business rules | `check_bill` | wrong maths, ambiguous dates, foreign currency, MRP as price |
| Human | confirm re-runs the checks on the edited draft | a hand-edited wrong total |

Other deliberate choices:

- **Enums instead of instructions.** “Please use one of these icons” is advice; `Literal[...]` is
  enforced by constrained decoding, so a wrong name cannot be produced.
- **Temperature 0 for extraction, 0.4 for the tip.** Transcription has one right answer; prose does
  not need to be identical every run.
- **Prompt versioning** (`prompt_version` on each bill, `INSIGHTS_PROMPT_VERSION` on each tip) makes
  “extraction got worse after my change” a measurable question.
- **Cost/latency:** one photo = one call; one user-month = one call. A bill costs one image prompt;
  the tip is text-only and cached. The first call is the slow one (1–5 s); the cached screen is a
  single SQL sum.
- **Privacy:** the photo is never persisted, and the model is sent only the month's aggregates, not
  the bill images, for tips.

### 5.3 Caching a monthly AI result

The requirement was “AI monthly, saved, then used by the UI”, and the design that follows:

- **Key = `(user_id, month)`** with a unique constraint — caching is a data-model decision, not a
  dictionary in memory. It survives a restart and works across processes.
- **Store the sentence, recompute the facts.** Facts must stay true as bills arrive; the sentence is
  the expensive part. This split is the whole trick.
- **Lazy generation on first read** rather than a cron job: no scheduler, no “empty screen on the
  1st” window, and a month nobody opens costs nothing. The trade-off is latency on that first open
  (one model call) — acceptable for a screen opened once.
- **A fallback is retryable**, a real tip is not: `source="local"` + `last_attempt_at` older than
  6 h means try again. That avoids a single quota failure freezing the month while still allowing
  exactly one successful AI tip.
- **Empty month is not cached.** A tip written for zero bills would be wrong the moment a bill
  arrives, and it would also block the AI tip forever.
- **Races** are handled by the unique constraint + re-read (§4.16), not by a lock.

**Q: why not generate for all users on the 1st with a background job?** That is the right shape at
scale (queue + worker), but here it adds infrastructure, a scheduler and a failure mode (a job that
fails for one user) to solve a problem that lazy generation already solves. Say which you would pick
and why — that is what is being tested.

### 5.4 Auth and security: the questions behind the code

- **Why bcrypt?** Slow and salted by design; a leaked table is expensive to crack. Never MD5/SHA for
  passwords.
- **What is in the JWT?** `sub` (user id) and `exp`. No roles, no email — anything in a JWT is
  readable by the client, and roles would go stale for 30 minutes.
- **Where is the token stored on the client?** Not this repo's problem, but the honest answer:
  platform secure storage on mobile, `HttpOnly` cookie for web (with CSRF protection) rather than
  `localStorage`.
- **What if an access token is stolen?** It works for at most 30 minutes. The refresh token can be
  revoked and is rotated; a replayed refresh token is already deleted, which is the detection signal.
- **What is stored about the refresh token?** Only `sha256(raw)` + `expires_at`. A database dump does
  not log anyone in.
- **What is *not* here that production needs?** HTTPS termination, rate limiting on login, CORS
  policy for the web client, password reset, email verification, account lockout after repeated
  failures, secret rotation, audit logging. Naming these is stronger than pretending the app is done.
- **PII:** bills are personal. The photo is dropped after the call; the saved row keeps shop, date,
  total, category, items. A real deployment needs a deletion path and, depending on the provider
  tier, a check of whether the AI vendor retains prompts.

### 5.5 Async SQLAlchemy and the database layer

- **Engine vs session vs transaction.** The engine owns the pool (create once). A session is a unit
  of work with an identity map (create per request). A transaction is opened implicitly on first
  use and closed by `commit()`/`rollback()`/context exit.
- **Async everywhere** because the same process spends most of its time waiting on Gemini or the
  network; `async def` routes + `aiosqlite` mean one thread can serve many requests.
- **A blocking SDK call must not block the loop**, hence `run_in_threadpool` around Gemini.
- **`expire_on_commit=False`** avoids implicit lazy loads after commit, which are illegal in async
  code. Where a server default must be read, `await session.refresh(row)` is explicit.
- **Indexes:** `users.email` (unique), `refresh_tokens.token_hash` (unique), `expenses.user_id`,
  `monthly_insights.user_id` and `.month`. Every user-data query filters by `user_id`; the auth
  lookups hit `email` and `token_hash`, both unique indexes.
- **SQLite in dev, Postgres in production.** The SQL is portable: `Integer`, `String`, `Text`, `Date`,
  `DateTime`, `JSON`, `func.now()`, half-open date ranges. `DATABASE_URL` makes the switch a setting;
  the remaining steps are installing `asyncpg`, running the schema creation, and replacing the
  hand-rolled column-adder with Alembic.
- **N+1:** none, because the code never lazy-loads relationships — there are no relationship
  attributes at all. Items are a JSON column precisely to avoid a join for a screen that does not
  need one.

### 5.6 Concurrency and atomicity

Two places where “works on my machine” would otherwise become lost data:

```python
# balance: one statement, no read in Python
update(User).values(balance_minor=User.balance_minor - total_minor)
```

If two confirms arrive together, each `UPDATE` locks the row and computes from the committed value;
neither can overwrite the other. `NULL - x = NULL` means an unset balance stays unset with no branch.

```python
# monthly tip: the unique constraint is the lock
UniqueConstraint("user_id", "month")
...
except IntegrityError:
    await session.rollback(); return the saved row
```

Two first-opens of a month may both call Gemini (one wasted call — the cost of not adding a lock),
but only one row exists and no user sees an error.

**Q: is the confirm route idempotent?** No. A client retry after a lost response saves the bill twice.
The fix is an idempotency key (`Idempotency-Key` header, unique per user) — a good extension answer,
and the reason `/auth/refresh` *is* single-use rotation.

### 5.7 Testing strategy: 79 tests, no network, no shared file

```python
@pytest.fixture()
def client(db_file, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "async_session_maker",
                        async_sessionmaker(engine, expire_on_commit=False))
    from app.app import app
    with TestClient(app) as test_client:
        yield test_client
```

- **Every test gets a brand-new SQLite file in `tmp_path`**, so tests never share rows and can run
  in any order. The app's real `test.db` is untouched.
- The engine is replaced *before* the app is imported in the fixture, and `lifespan` then creates
  the schema in the temp file.
- `TestClient(app)` runs lifespan on enter — same startup path as production, including the
  column-adder.
- `auth_headers` registers and logs in, returning the Bearer header; `expenses()` reads the table
  with plain `sqlite3` to assert what was *actually* saved, not what the API says.
- **Gemini is faked at the seam**: `monkeypatch.setattr(bills_router, "extract_bill", fake)` for
  route tests, and a fake SDK client for `extraction`/`insights` unit tests
  (`SimpleNamespace(parsed=..., text=...)`). No key, no network, no flaky latency.
- Failure modes are tested as first-class behaviour: 415, 413, 503, 429, 422, 401, a busy model,
  a quota error, a fallback retry window, an old database file, two users' data.
- The 14 insights tests include the caching contract: one call for two reads, retry only after the
  window, and no AI call for an empty month.

**Q: what is not tested?** A live Gemini call (no key in CI), Postgres, real concurrent HTTP
requests, and Flutter. Say so before you are asked.

### 5.8 Error design: the status table

| Status | Used when | Where |
| --- | --- | --- |
| 200 | fine | reads, extract |
| 201 | created | register, confirm |
| 204 | done, no body | logout |
| 400 | empty upload | extract |
| 401 | no / invalid / expired token, wrong credentials | deps, auth |
| 409 | email already registered | register |
| 413 | photo over 5 MB | extract |
| 415 | not JPG/PNG | extract |
| 422 | validation, or a draft that fails the cashier check | FastAPI, confirm, insights month |
| 429 | AI quota spent | extract (forwarded) |
| 502 | AI failure that is not capacity/quota | extract (forwarded) |
| 503 | AI model busy (with `Retry-After: 60`) | extract (forwarded) |

Two shapes to remember:

- FastAPI's own validation error: `{"detail": [{"loc": ["body","email"], "msg": "..."}]}`.
- This app's domain errors: `{"detail": {"message": "…", "problems": [{"field", "message"}]}}`
  for a bill, and `{"detail": "…"}` for simple 4xx.

The `field` in a problem (`items[2].qty`) is the contract that lets the client highlight exactly one
input. Designing error payloads *for the UI* is a backend skill worth naming explicitly.

---

## 6. Interview question bank

Answers are short on purpose — practise saying them, not reading them.

### Python & general

1. **`Decimal` vs `float`?** Floats are binary approximations; `0.1 + 0.2 != 0.3`. `Decimal` is exact
   for base-10 money. Here all money is `Decimal` in the API and integer paise in the database.
2. **When does `async def` not help?** When the work is CPU-bound or calls a blocking SDK. Then you
   block the loop; use a thread pool (`run_in_threadpool`) or a real worker.
3. **Why `secrets` and not `random` for tokens?** `random` is a PRNG, predictable from state;
   `secrets` uses the OS CSPRNG.
4. **What does `from __future__`-style typing buy you?** Readability and IDE checking; runtime is
   Pydantic's job here. The project uses `str | None` (3.10+) and 3.14 at runtime.
5. **How do you test a function that calls an external API?** Fake it at your own seam
   (`extract_bill`, `genai.Client`), assert the request you would have sent, and test failures too.

### FastAPI

6. **How does `Depends` work?** FastAPI builds a dependency graph per route, resolves it before the
   handler, and caches each dependency per request. `get_current_user` depends on the bearer scheme
   and the session; the route depends on `get_current_user`.
7. **Why is `get_async_session` a generator?** The `yield` splits setup/teardown; FastAPI closes the
   session after the response, even on exceptions.
8. **`response_model` vs returning a dict?** It validates and filters output, generates OpenAPI, and
   is a security boundary (`UserOut` has no password hash).
9. **`lifespan` vs `on_event`?** `lifespan` is the supported API, gives one async context for
   startup + shutdown, and is testable via `TestClient`.
10. **Why 422 for a wrong total instead of 400?** 422 means syntactically valid but semantically
    unprocessable; FastAPI already uses 422 for validation, so the domain error matches. 400 is for
    a malformed request.
11. **How is a 401 returned before the handler runs?** `get_current_user` raises in a dependency;
    FastAPI aborts the request and renders the exception.
12. **Where does OpenAPI come from?** Pydantic models + route signatures; `/docs` is Swagger UI on
    that JSON. Nothing is written by hand.

### Pydantic

13. **`field_validator(mode="before")` vs `"after"`?** Before runs on raw input (used by
    `parse_money` to accept `"₹1,234.50"`); after runs on validated data (the icon/category check).
14. **`model_validator(mode="after")`?** Cross-field rules: an icon must belong to the category.
15. **Why `Literal` instead of an `Enum`?** Both compile to a JSON Schema enum; `Literal` keeps the
    values in config without a second class, and the labels stay plain strings.
16. **What does `Annotated[Decimal, PlainSerializer(...), WithJsonSchema(...)]` do?** One field type
    with different shapes for two audiences: a plain number to the model, a two-place string to the
    client.
17. **Why are all `BillDraft` fields optional?** “Unreadable” must be representable; the checks
    decide what blocks a save, with a per-field message.
18. **Does Pydantic reject extra fields by default?** No, it ignores them. For `BillDraft` that is
    fine (the schema constrains generation and `.parsed` only carries declared fields); set
    `model_config = {"extra": "forbid"}` if you want strictness.
19. **`from_attributes=True`?** Lets a model validate from an ORM object (`ExpenseOut`, `UserOut`).

### Database / SQLAlchemy

20. **Engine vs session?** Pool vs unit of work; one engine per process, one session per request.
21. **Why `expire_on_commit=False`?** Default expiry triggers lazy loads after commit, which async
    sessions cannot do implicitly.
22. **How do you avoid N+1?** No relationships are loaded lazily; items are JSON; aggregates are a
    single grouped query. If relationships appear, use `selectinload`.
23. **How do you migrate a schema?** Startup adds missing nullable columns and backfills what is
    knowable; bigger changes need Alembic. State the limit rather than hiding it.
24. **Why integer paise?** SQLite has no exact decimal; integers are exact and index well. A `REAL`
    money column would reintroduce float error.
25. **Why a unique constraint for the monthly tip?** Concurrency control in the database, not in
    application code; the loser gets `IntegrityError` and reads the winner's row.
26. **When would you switch to Postgres?** Multiple writers, real concurrent load, JSON queries,
    `NUMERIC` money if you prefer decimals, backups and replication. The code is already portable.

### Auth

27. **JWT vs server sessions?** Stateless and cheap to verify, but not revocable; sessions are
    revocable but need storage. The project uses both: short JWT + revocable refresh row.
28. **Why hash the refresh token?** It is a bearer credential; a DB leak must not be a login.
29. **Why rotate on refresh?** Limits the lifetime of a stolen token and gives a replay signal.
30. **Where is `sub` validated?** It is trusted only because the signature is valid; `int()` failure
    is caught and becomes 401.
31. **How would you add roles?** Read the user from the DB (already done) and check a column; or a
    `require_role` dependency. Do not trust a role claim for 30 minutes without a reason.

### AI integration

32. **How do you stop a model inventing values?** Constrain output with a schema/enums, give it only
    the facts it may use, and re-verify with deterministic code. Not by asking nicely.
33. **503 vs 429?** Per-model capacity vs per-project quota; try another model once vs stop and wait.
34. **Why a fallback model list?** Keys in one project share quota, so a second key does not help; a
    different model may have capacity.
35. **How do you know extraction still works after a prompt change?** `prompt_version` is stored per
    bill, so rows can be traced to the prompt that made them. The 24 checks tests cover the rules
    around a draft, but there is no automated *model-quality* eval yet — the honest answer is a
    fixed 10-bill set scored after every prompt change.
36. **Cost control?** One call per bill, one per user-month, caching, text-only tips, no retries on
    quota, a 5 MB input cap.
37. **What if the model returns a valid but silly tip?** The schema guarantees shape, not taste. A
    human review or a small eval set is the answer; the local fallback sets a floor for usefulness.

### Product / design

38. **Why can’t a user save a bill without a confirm?** Because a model draft is a guess; the confirm
    is the human trust boundary and the only write path.
39. **Why keep a balance at all if you can sum expenses?** The opening money is not derivable; the
    balance records what the user says they have. `spent` is derived alongside it.
40. **What happens when a bill exceeds the balance?** It saves and the balance goes negative — a real
    bill is not an error.
41. **Why return 200 when the AI failed for tips?** The screen's purpose is information, and the
    local tip is still true; a failure card would be worse than a plainer sentence.
42. **What is the one thing you would change first?** Pagination and idempotency on confirm, then
    Alembic before the next non-nullable column.

---

## 7. Scripts: how to talk about this project

### 60 seconds

> It is a FastAPI backend for an expense app. You photograph a bill; Gemini extracts it into a
> Pydantic schema; deterministic checks verify the maths like a cashier; the user confirms and the
> bill is saved, spending down their balance. Auth is JWT access tokens plus revocable, rotated
> refresh tokens. There is also a monthly “Smart Tips” endpoint: facts summed live from that month’s
> bills, one AI sentence cached per user per month, with a local fallback so the screen never fails.
> The code is four layers — routers, schemas, services, models — and every rule is tested: 79 tests,
> no network.

### 3 minutes (architecture first)

1. **Shape:** routers never hold logic; checks/AI live in services; schemas are the contract; models
   are storage. One dependency, `get_current_user`, protects every route.
2. **Bill flow:** upload validation (type, size) → Gemini with `response_schema` and temperature 0 →
   `check_bill` returns field-level problems → the client edits → confirm re-runs the checks and is
   the only writer.
3. **Money:** rupees as two-place `Decimal` in the API, integer paise in SQLite, one conversion each
   way, never multiplied by qty.
4. **Failure policy:** structured output for shape, checks for truth, 503/429/502 for AI so the
   client knows whether to retry, and a local tip when tips cannot be generated.
5. **Data:** four tables, no lazy relationships, unique `(user_id, month)` as the monthly cache, the
   balance decrement as one atomic statement.
6. **Proof:** 79 tests including every failure mode, an old-database upgrade test, and a real bug
   about a ₹127.49 line against a ₹128 bill that changed both prompt and checks.

### The deep-dive answer that lands

Pick **the money bug**. Interviewers remember concrete debugging: the model returned paise once, then
read an MRP as a line amount; both looked plausible. The fix was not “a better prompt” alone — it was
unit-explicit schema fields, an enum-constrained vocabulary, a per-row overflow check, and a
justified ₹1 tolerance for cashier rounding. That demonstrates judgement about LLM reliability,
which is the actual skill being hired for.

---

## 8. Extension exercises (what they may ask next)

| Ask | Where it goes | The key idea |
| --- | --- | --- |
| Paginate the bill list | `GET /bills?limit=&cursor=` | keyset pagination on `(date, id)`, not `OFFSET` at scale |
| Edit / delete an expense | `PATCH`, `DELETE /bills/{id}` | ownership check (`user_id == token`), and reverse the balance delta atomically |
| Idempotent confirm | `Idempotency-Key` header + unique row | a retried request must not save twice |
| Budgets | `budgets` table + checks | a budget is a target, not a hard block |
| Postgres | `asyncpg`, Alembic, `NUMERIC` | portability is already there; migrations are the work |
| Multi-currency | currency per bill + FX rate table | never sum across currencies without a rate and a date |
| Sharing a bill | `expense_members` join table | authorisation becomes a membership check, not an ownership check |
| Rate limiting login | middleware or reverse proxy | protect bcrypt from being used as a CPU amplifier |
| Background tip generation | queue + worker | right at scale; unnecessary for one user's lazy read |
| Image preprocessing | client or server | downscale before upload — cheaper tokens, faster call |

---

## 9. Known limitations (say them before you are asked)

- **No pagination** on `GET /bills`; fine for a demo, wrong for a year of bills.
- **Confirm is not idempotent**; a network retry duplicates a bill.
- **The starter migration only adds nullable columns**; a rename or a non-nullable column needs Alembic.
- **SQLite in dev**; no Postgres run, no concurrency load test.
- **No expense edit/delete**, so the balance can only go down.
- **No rate limiting, CORS policy, password reset, email verification.**
- **AI quality is not evaluated automatically**; the roadmap is a 10-bill eval with a recorded score.
- **The tip freezes for its month**; facts update, the sentence does not.
- **Single currency (INR)**, enforced by `ALLOWED_CURRENCIES`.
- **No live Gemini call in tests**, so provider regressions would only show in a real run.

---

## 10. Cheat sheet

**Files, one line each**

| File | One line |
| --- | --- |
| `main.py` | uvicorn launcher |
| `app/app.py` | app object, lifespan, router inventory |
| `app/config.py` | env settings, categories/icons, prompts, tolerances |
| `app/deps.py` | `get_current_user` (token → `User`) |
| `app/services/db.py` | engine, session factory, startup column-adder |
| `app/models/user.py` | account + nullable balance |
| `app/models/refresh_token.py` | hashed, revocable sessions |
| `app/models/expense.py` | confirmed bill, paise, JSON items |
| `app/models/insight.py` | monthly tip cache |
| `app/schemas/auth.py` | register/login/token/user shapes |
| `app/schemas/bill.py` | draft shape, `parse_money`, `Money`, enums |
| `app/schemas/account.py` | balance request/response |
| `app/schemas/insight.py` | tip/facts/challenge shapes, `TipDraft` |
| `app/services/auth.py` | bcrypt + JWT + refresh-token helpers |
| `app/services/gemini.py` | client, model list, 503/429 classification |
| `app/services/extraction.py` | photo → `BillDraft`, one call per bill |
| `app/services/checks.py` | pure cashier rules → `Problem[]` |
| `app/services/insights.py` | month range, facts, local tip, tip call |
| `app/routers/auth.py` | register/login/refresh/logout/me |
| `app/routers/bills.py` | extract + confirm + list |
| `app/routers/account.py` | set/read balance |
| `app/routers/insights.py` | `GET /insights` with caching |
| `tests/` | 79 tests: routes, rules, AI seams, schema upgrade |

**Env vars**

| Name | Meaning |
| --- | --- |
| `SECRET_KEY` | HS256 signing key for access tokens |
| `DATABASE_URL` | SQLAlchemy URL (`sqlite+aiosqlite:///./test.db` default) |
| `GEMINI_API_KEY` | AI key; unset = 502 on extract, local tip on insights |
| `GEMINI_MODEL` | primary model id |
| `GEMINI_FALLBACK_MODELS` | tried once each on 503 |

**Constants worth quoting**

- access token 30 min, refresh 30 days, bcrypt 72-byte password cap;
- upload cap 5 MB, JPG/PNG only;
- tolerance ₹0.02 with a printed round-off, ₹1.00 cashier rounding without;
- 10 categories, 3 icon names each (exact hugeicons 1.2.0 members);
- prompt v5 for extraction, v1 for tips, 6-hour fallback retry.

**Commands**

```bash
uv sync                       # install
uv run main.py                # dev server + reload
uv run pytest                 # 79 tests
uv run pytest tests/test_checks.py -q   # the pure rules only
```

**Glossary**

- *draft* — an unconfirmed extraction; exists only in an HTTP response.
- *confirm* — the only route that writes a bill.
- *paid/free tier* — Gemini quota by project, not by key.
- *half-open range* — `>= start AND < end` date filter.
- *paise/minor units* — the smallest currency unit stored as an integer.
- *idempotency* — repeating a request has the same effect as doing it once (not yet true for confirm).
- *structured output* — the model's reply constrained by a schema.

---

## 11. Three-day study plan

**Day 1 — the skeleton.** Read §1–§4.6 with the editor open. Draw the four layers, the four tables,
and the login trace from memory. Run `uv run pytest` and read `tests/test_checks.py` — the domain
rules are the best introduction to the codebase.

**Day 2 — the flows.** Read §4.7–§4.16. Trace extract, confirm, and insights on paper, including
status codes. Then answer §6 questions 1–20 without looking.

**Day 3 — the judgement.** Read §5 and §7. Rehearse the 60-second pitch and the money-bug story out
loud. Answer the rest of §6, then take one extension from §8 and sketch its schema and route in five
minutes — that is the whiteboard round.

**Self-check — you are ready when you can, without notes:**

- [ ] name every file in `app/` and its one job
- [ ] explain why money is `Decimal` in the API and paise in SQLite
- [ ] explain the difference between a prompt, a schema and a check, with the ₹127.49 example
- [ ] say why 503 retries on another model and 429 does not
- [ ] explain the balance decrement and why it is one SQL statement
- [ ] explain why the monthly tip is cached but the facts are not
- [ ] describe the refresh-token rotation and what is stored
- [ ] list five things this backend does not do yet, and how you would add them

