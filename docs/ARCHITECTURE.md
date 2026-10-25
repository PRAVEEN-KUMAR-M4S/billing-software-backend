# Architecture — what each piece is and how a request flows

This project is deliberately small, but it has the **same shape a big project would have**.
Learn the shape now and it scales all the way to the bill-reading feature.

---

## 1. The four layers

```
        HTTP request (JSON)
                │
                ▼
┌───────────────────────────────┐
│  routers/     "the web desk"  │  Which URL? What status code? Never holds logic.
└──────────────┬────────────────┘
               │  uses
        ┌──────┴───────┐
        ▼              ▼
┌──────────────┐  ┌──────────────┐
│  schemas/    │  │  deps.py     │  schemas = shape of data in/out
│  "the form"  │  │  "bouncer"   │  deps    = who is calling (logged in?)
└──────────────┘  └──────┬───────┘
                         │  uses
                         ▼
┌───────────────────────────────┐
│  services/    "the workshop"  │  How things are done: SQL, hashing, JWT,
└──────────────┬────────────────┘  calling Gemini, the bill checks. No HTTP.
               │  reads/writes
               ▼
┌───────────────────────────────┐
│  models/      "the warehouse" │  Tables and columns (what is stored).
└───────────────────────────────┘
```

**Dependency rule:** arrows only point *down*. A model never imports a router;
a service never knows what HTTP is. That's what lets you swap the web framework
or the database without rewriting your logic.

## 2. One request, end to end

`POST /auth/login` with `{"email","password"}`:

```
1. FastAPI sees POST /auth/login  → hands it to routers/auth.py
2. Pydantic (schemas/auth.py)     → validates the body; rejects bad input with 422
3. routers/auth.py:
     a. queries users table for the email          (services/db.py session)
     b. bcrypt compares password vs stored hash    (services/auth.py)
     c. if OK: jwt.encode({"sub": user_id, exp})   (services/auth.py)
     d. returns 200 {"access_token": "..."}
```

`GET /auth/me` (protected):

```
1. deps.get_current_user runs BEFORE your route body:
     a. reads "Authorization: Bearer <token>"
     b. jwt.decode → signature + expiry check → user id (or 401)
     c. loads User row (or 401 if deleted)
2. route body receives the actual User object and just returns it
```

This is the pattern every future protected route reuses — including
`POST /bills` (upload photo) and `GET /expenses` (list).

## 3. Files and the one question each answers

| File                | Question                                  | Notes |
| ------------------- | ----------------------------------------- | ----- |
| `main.py`           | How do I start the server?                | uvicorn + reload |
| `app/app.py`        | What is the app? (startup + routers)      | lifespan creates tables |
| `app/config.py`     | What settings are we using?               | from `.env`, never hardcode secrets |
| `app/routers/auth.py` | Which URLs serve auth?                  | register / login / me |
| `app/schemas/auth.py`| What are the exact fields and types?     | validation happens here |
| `app/services/auth.py`| How do we hash and sign?                | pure functions, no HTTP, no DB |
| `app/services/db.py`  | How do we talk to the database?         | async engine + session factory |
| `app/models/user.py`  | What does a user row look like?         | SQLAlchemy table definition |
| `app/deps.py`        | Is this caller authenticated?             | token → User |
| `app/routers/bills.py` | Which URLs read and save bills?        | extract (draft) / confirm (row) |
| `app/schemas/bill.py`  | What is a bill, exactly?               | one shape: Gemini, checks, confirm |
| `app/services/extraction.py` | How do we read a photo?          | the only Gemini call |
| `app/services/checks.py` | Is this draft trustworthy?           | pure functions, no I/O |
| `app/models/expense.py` | What is a saved bill?                 | paise in SQLite, rupees out, ISO date |

## 4. State: who owns what

| State                | Owner                | Why |
| -------------------- | -------------------- | --- |
| `SECRET_KEY`, expiry | `config.py` (from `.env`) | one place, never committed |
| database connection  | `services/db.py`     | single engine, sessions are per-request |
| the logged-in user   | the JWT itself       | servers stay stateless — no session table |

## 5. How this maps onto the bill project

The plan's vocabulary already exists in this structure:

| Plan term           | Where it will live                    |
| ------------------- | ------------------------------------- |
| **JSON schema**     | a `schemas/bill.py` Pydantic model    |
| **Validation**      | Pydantic (entry) + maths check (logic)|
| **Structured output**| `services/extraction.py` — Gemini `response_schema=BillDraft` (no "reply in JSON") |
| **Vision**          | the photo bytes sent in that same service |
| **Fix-it form**     | router returns 422 with `problems[{field, message}]` |

Phases 2–5 add files only in slots that already exist: `schemas/bill.py` (the
fixed shape), `models/expense.py` (the table), `services/extraction.py` (the AI
call), `services/checks.py` (the rules), `routers/bills.py` (extract + confirm).
Nothing in auth had to change — that is the test of the structure.

`/bills/extract` writes nothing. `/bills/confirm` re-runs every check on the
user's edited draft before inserting a row, so there is no path from a photo to
the database that skips a human and a check.

## 6. Design choices worth knowing (and why)

- **Async everything** — SQLite via `aiosqlite`, async SQLAlchemy, `async def` routes.
  Same style you need when calling Gemini's API: the SDK call is blocking,
  so `services/extraction.py` runs it in a thread and the route stays async.
- **JWT instead of server-side sessions** — no session storage; the token carries
  the identity. Cost: tokens can't be revoked early (fine for this project).
- **bcrypt, not SHA-256** — passwords need a slow, salted hash; fast hashes are
  brute-forceable.
- **`response_model` on every route** — FastAPI strips anything not in the schema,
  so `hashed_password` can never leak out by accident.
