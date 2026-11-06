# Freebuff Desktop — Bill Reader API

Turn a **photo of a bill** into **clean, structured expense data** — with auth first.

**Status:** Phase 1 done ✅ — authentication. Phases 2–5 done ✅ — a bill photo becomes a
validated draft via Gemini, and only a human-confirmed draft reaches the database.
**Next:** Phase 6 — "My Expenses" list + monthly total, demo bills, ship, 10-bill score.

---

## Run it

```bash
uv sync                  # install dependencies (once)
cp .env.example .env     # then set SECRET_KEY and GEMINI_API_KEY
uv run main.py           # start the server on http://127.0.0.1:8000
uv run pytest            # 79 tests: checks, routes, balance, smart tips, the AI modules, schema upgrades
```

- Interactive API docs: http://127.0.0.1:8000/docs
- Local database: `test.db` (SQLite file, auto-created on start, git-ignored)

## Folder structure (and why)

```
main.py              # just starts the server (entry point)
app/
├── app.py           # the app itself: startup hook + "which routers exist"
├── config.py        # settings from .env (secrets, Gemini, prompt, tolerance)
├── deps.py          # shared route protections (get_current_user)
├── routers/         # LAYER 1 — the web: URLs, status codes, HTTP
│   ├── auth.py
│   ├── account.py   # PUT /account/balance (set) + GET /account (left, spent)
│   ├── bills.py     # POST /bills/extract (draft) + POST /bills/confirm (save)
│   └── insights.py  # GET /insights — Smart Tips (tip + facts + challenge)
├── schemas/         # LAYER 2 — the shapes: what data comes IN and OUT
│   ├── auth.py
│   ├── account.py   # BalanceSetRequest, AccountOut
│   ├── bill.py      # THE SHAPE: BillDraft, BillItem, Problem
│   └── insight.py   # InsightsOut, SmartTip, QuickFacts, TipDraft
├── services/        # LAYER 3 — the logic: database, crypto, AI, rules
│   ├── db.py
│   ├── auth.py
│   ├── gemini.py      # ★ shared client, model fallback list, error codes
│   ├── extraction.py  # reads one bill photo (one Gemini call)
│   ├── insights.py    # month maths, facts, local tip, the monthly tip call
│   └── checks.py      # pure cashier checks (maths, dates, currency)
└── models/          # LAYER 4 — the storage: tables and columns
    ├── user.py      # + balance_minor (paise, NULL until the user sets one)
    ├── refresh_token.py
    ├── expense.py   # confirmed bills only (paise in SQLite, ISO date)
    └── insight.py   # monthly_insights: one saved tip per user per month

tests/               # pytest: checks, routes, extraction wiring
```

One rule to remember: **routers call services, services never call routers.**
Each folder answers one question:

| Folder     | Question it answers                          | Example                          |
| ---------- | -------------------------------------------- | -------------------------------- |
| `routers/` | *Which URL? Which HTTP code?*                | `POST /auth/login` → 401         |
| `schemas/` | *What fields, what types?*                   | password must be 6–72 characters |
| `services/`| *How is it done?*                            | bcrypt hash, JWT sign            |
| `models/`  | *What is stored?*                            | `users` table                    |
| `deps.py`  | *Who is calling (are they logged in)?*       | Bearer token → `User`            |

## The API so far

| Method | URL              | Body                          | Returns                              |
| ------ | ---------------- | ----------------------------- | ------------------------------------ |
| POST   | `/auth/register` | `{email, password}`           | `{id, email}` (201)                  |
| POST   | `/auth/login`    | `{email, password}`           | `{access_token, refresh_token}`      |
| POST   | `/auth/refresh`  | `{refresh_token}`             | `{access_token, refresh_token}` (new pair) |
| POST   | `/auth/logout`   | `{refresh_token}`             | nothing (204) — revokes that session |
| GET    | `/auth/me`       | — (Bearer token header)       | `{id, email}`                        |
| PUT    | `/account/balance` | `{balance}` (rupees, e.g. `"5000.00"`) | `{balance, spent, currency}` — replaces the balance |
| GET    | `/account`       | — (Bearer token header)       | `{balance, spent, currency}`; `balance` is null until set |
| POST   | `/bills/extract` | multipart `file` (JPG/PNG ≤5 MB) | `{draft, problems[]}` — writes nothing |
| POST   | `/bills/confirm` | `{draft}` (the user's edited draft) | the saved bill (201), or 422 + `problems[]` |
| GET    | `/insights?month=YYYY-MM` | — (Bearer token; month defaults to now) | `{month, month_label, tip, facts, challenge}` — one AI call per user per month |

Try it:

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"ana@example.com","password":"secret123"}'

curl -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ana@example.com","password":"secret123"}'

curl http://127.0.0.1:8000/auth/me \
  -H "Authorization: Bearer <paste token here>"
```

## How login works (30 seconds)

1. **Register** — your password is hashed with bcrypt; the plain password is never stored.
2. **Login** — bcrypt checks the password; if right, the server returns **two tokens**:
   a short-lived **access token** (30 min JWT) and a long-lived **refresh token**
   (30 days, stored hashed in the `refresh_tokens` table).
3. **Every protected route** — you send `Authorization: Bearer <access token>`; `get_current_user`
   checks the signature, loads your `User`, and 401s if anything is off.
4. **When the access token expires** — `POST /auth/refresh` with the refresh token
   trades it for a **new pair** (the old refresh token is deleted — single-use).
5. **Logout** — `POST /auth/logout` deletes that refresh token, so no new access
   tokens can be minted for that session.

Why two tokens? The access token is stateless and can't be revoked, so it's only
trusted for 30 minutes. The refresh token lives in the DB, so the server can
revoke it (logout) and replace it on every use (rotation) — that's what makes
the setup safe for a month-long login.

## The balance (one number per user, spent by every bill)

```bash
# the money available right now — the opening balance the first time,
# a correction on any later call (it replaces, it does not add)
curl -X PUT http://127.0.0.1:8000/account/balance \
  -H "Authorization: Bearer <access token>" -H 'Content-Type: application/json' \
  -d '{"balance":"5000.00"}'
# {"balance":"5000.00","spent":"0.00","currency":"INR"}

curl http://127.0.0.1:8000/account -H "Authorization: Bearer <access token>"
# after a 128.00 bill: {"balance":"4872.00","spent":"128.00","currency":"INR"}
```

- **Stored, not derived.** The opening money is something only the user knows, so
  `users.balance_minor` holds what is left; `spent` is the derived half (the sum,
  in SQL, of that user's saved bills). A balance for one user never moves because
  of another user's bill.
- **NULL means "not set yet".** A bill confirmed before any balance exists is
  saved normally and leaves the balance unset — the app simply has no number to
  spend yet. Setting it afterwards is the opening balance, not 5000 − 125.
- **One atomic decrement.** `/bills/confirm` inserts the row and runs
  `UPDATE users SET balance_minor = balance_minor - ?` in the same transaction,
  so two bills confirmed at once can't both subtract from the same old value.
  A rejected draft spends nothing.
- **Overspending is allowed.** Going below zero is reported (a negative
  `balance`), not refused: the bill is real, and the number is information.
- **Rupees in, paise stored, rupees out** — `"5000.00"` in the API, `500000` in
  SQLite, exactly like a bill total.

## Smart Tips (the monthly screen, one AI call)

```bash
curl "http://127.0.0.1:8000/insights?month=2026-10" \
  -H "Authorization: Bearer <access token>"
```

```json
{
  "month": "2026-10",
  "month_label": "October 2026",
  "currency": "INR",
  "generated_at": "2026-10-30T13:00:18",
  "tip": {
    "text": "Food & drink is where you have spent the most. Keep an eye on the small purchases there.",
    "category": "dining",
    "action_label": "See Food & drink bills",
    "source": "ai"
  },
  "facts": {
    "bills": 2,
    "total": "690.00",
    "average_bill": "345.00",
    "biggest_category": {"category": "dining", "label": "Food & drink", "icon": "strokeRoundedRestaurant01", "total": "690.00", "bills": 2},
    "most_visited_shop": {"shop": "Asha Tiffins", "bills": 2}
  },
  "challenge": {"title": "Record every bill for 7 days", "note": "A quick note after each purchase makes spending easier to follow.", "week_start": "2026-10-26"}
}
```

- **One call per user per month, ever.** The tip is written on the first open of
  that month and saved in `monthly_insights` (unique on `user_id` + `month`).
  Opening the screen 50 more times costs nothing. A past month opened later is
  generated at that point, once.
- **The facts are never cached.** They are one indexed SQL sum over that month,
  recomputed on every request, so a bill added today shows in today's totals.
  Only the sentence is frozen for the month.
- **`source` tells you which tip you got.** `ai` is Gemini's, constrained by
  `TipDraft` to a category from the enum, a two-sentence headline and a button
  label. `local` is arithmetic: "Food & drink is your biggest expense this
  month: 690.00 across 2 bills…".
- **The screen never fails.** Busy model or spent quota → `local` tip and HTTP
  200. No key at all → `local` tip. A fallback is retried after
  `INSIGHTS_RETRY_HOURS` (6), so one bad quota day on the 1st does not own the
  month; a real `ai` tip is never replaced.
- **Empty month = no AI call, nothing cached.** The response says "No bills this
  month yet" with zeroed facts, because a tip written for zero bills would be
  wrong the moment the first bill arrives.
- **Postgres-ready.** All the queries are portable SQL (half-open date range,
  no `strftime`), and `DATABASE_URL` is a setting — switch to
  `postgresql+asyncpg://…` without code changes.

## How a bill becomes a row (and never silently)

```
photo ──► POST /bills/extract ──► services/extraction.py ──► Gemini
              (auth, JPG/PNG,             one call,                 │
               size cap)             response_schema=BillDraft      │
              ◄──────────────────────────────────────────────────────┘
              returns {draft, problems[]}   ← services/checks.py runs on it
                        │
        user fixes any highlighted field
                        ▼
            POST /bills/confirm ──► checks run AGAIN ──► expenses row
```

- **Structured output, not "reply in JSON":** `response_mime_type="application/json"`
  plus `response_schema=BillDraft` constrains Gemini to exactly our fields, and
  `.parsed` hands back a Pydantic object. Gemini has no Claude-style forced tool
  call; this is its equivalent.
- **The reply is untrusted input.** Maths (`Σ line amounts + printed round-off ≈
  total`), ISO dates, allowed currency and non-empty fields are re-checked in
  `services/checks.py`. Problems come back with a `field` so a fix form can
  highlight the exact row.
- **Money is rupees, not paise.** `total`, `round_off` and `items[].amount`
  are `Decimal` rupees with two places and leave the API as strings —
  `"128.00"`, never `12800` and never a float. SQLite still gets exact integer
  paise (`total_minor`); only the confirm route converts, so there is one
  conversion in the whole project.
- **The printed Amount column is the truth.** A pharmacy line prints an MRP, a
  rate, discount percentages and GST, and finally an amount. `items[].amount`
  is that last number — the money for the whole line, discount applied and tax
  included — so `qty` never multiplies money and an MRP is never a price. A
  line bigger than the whole bill is flagged on that row, and a printed
  `Round Off` has its own field (`round_off`) instead of being folded into the
  tolerance.
- **Rounding.** A stated round-off has to close the maths to the paise (±0.02).
  With none printed, up to ₹1.00 of cashier rounding (127.49 paid as 128.00)
  is accepted rather than blocked.
- **Every bill is classified and gets a drawable icon.** `category` is one of
  ten fixed slugs (`groceries`, `dining`, `transport`, `health`, `utilities`,
  `shopping`, `rent`, `entertainment`, `education`, `other`); `icon` is an exact
  hugeicons 1.2.0 member name such as `strokeRoundedHome01`. Both are enums in
  `response_schema`, so the model cannot invent a category or an icon name
  Flutter does not have. If its icon pick is missing or belongs to another
  category, that category's default (e.g. `strokeRoundedShoppingBasket01` for
  groceries) is used instead — never an invented name.
- **The database is the last stop.** `/extract` writes nothing; `/confirm`
  re-runs every check on the *edited* draft, so a hand-edited wrong total is
  rejected with 422 instead of saved.
- **A busy model falls over to another model.** `503 UNAVAILABLE — high demand`
  is the model's capacity, not your key: every key in the same project hits it,
  so the next id in `GEMINI_FALLBACK_MODELS` is tried once instead. Spent quota
  (429) is *not* retried — the route answers `503` + `Retry-After: 60` for a
  busy model and `429` for quota, so you can tell "wait a minute" from "wait
  until midnight Pacific".
- **The photo is not stored.** It is read into memory, sent once, and dropped.
  Bills are personal data; whether your Gemini key's tier allows Google to use
  requests for training is a decision for you to check in AI Studio.
- **Every saved row records `prompt_version`**, so a change in extraction quality
  can be traced to the prompt that produced it.

```bash
# draft a bill
curl -X POST http://127.0.0.1:8000/bills/extract \
  -H "Authorization: Bearer <access token>" \
  -F "file=@bill.jpg"

# save it (after fixing anything in problems[])
curl -X POST http://127.0.0.1:8000/bills/confirm \
  -H "Authorization: Bearer <access token>" -H 'Content-Type: application/json' \
  -d '{"draft":{"shop":"Lulu","date":"2026-10-22","currency":"INR","category":"groceries","icon":"strokeRoundedShoppingBasket01","total":"125.00","round_off":null,"items":[{"name":"Rice 1kg","qty":2,"amount":"80.00"},{"name":"Milk","qty":1,"amount":"45.00"}]}}'
```

## Roadmap

- **Phase 1 (done):** auth
- **Phase 2 (done):** the bill shape → `schemas/bill.py` + `expenses` table
- **Phase 3–4 (done):** photo → Gemini → structured `BillDraft` (one call)
- **Phase 5 (done):** checks before save + confirm endpoint
- **Balance (done):** per-user balance (`PUT /account/balance`, `GET /account`), spent down by each confirmed bill
- **Smart Tips (done):** `GET /insights` — monthly AI tip cached per user/month, live facts, weekly challenge
- **Phase 6:** "My Expenses" list + monthly total, demo bills, ship
- **Then:** 10 real bills through the live link → "N/10" + the miss

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/AUTH.md](docs/AUTH.md).

**Learning the code?** [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md) is a code-by-code walkthrough of the
whole backend — every file, the money/AI/caching decisions, and an interview question bank with a three-day
study plan.
