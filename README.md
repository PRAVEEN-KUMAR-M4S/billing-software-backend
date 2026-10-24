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
uv run pytest            # 23 tests: checks, routes, the AI module
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
│   └── bills.py     # POST /bills/extract (draft) + POST /bills/confirm (save)
├── schemas/         # LAYER 2 — the shapes: what data comes IN and OUT
│   ├── auth.py
│   └── bill.py      # THE SHAPE: BillDraft, BillItem, Problem
├── services/        # LAYER 3 — the logic: database, crypto, AI, rules
│   ├── db.py
│   ├── auth.py
│   ├── extraction.py  # ★ the only file that talks to Gemini
│   └── checks.py      # pure cashier checks (maths, dates, currency)
└── models/          # LAYER 4 — the storage: tables and columns
    ├── user.py
    ├── refresh_token.py
    └── expense.py   # confirmed bills only (integer paise, ISO date)

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
| POST   | `/bills/extract` | multipart `file` (JPG/PNG ≤5 MB) | `{draft, problems[]}` — writes nothing |
| POST   | `/bills/confirm` | `{draft}` (the user's edited draft) | the saved bill (201), or 422 + `problems[]` |

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
- **The reply is untrusted input.** Maths (`Σ qty × price ≈ total`, ±2 paise),
  ISO dates, allowed currency and non-empty fields are re-checked in
  `services/checks.py`. Problems come back with a `field` so a fix form can
  highlight the exact row.
- **The database is the last stop.** `/extract` writes nothing; `/confirm`
  re-runs every check on the *edited* draft, so a hand-edited wrong total is
  rejected with 422 instead of saved.
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
  -d '{"draft":{"shop":"Lulu","date":"2026-10-22","currency":"INR","total_minor":12500,"items":[{"name":"Rice 1kg","qty":2,"price_minor":4500},{"name":"Milk","qty":1,"price_minor":3500}]}}'
```

## Roadmap

- **Phase 1 (done):** auth
- **Phase 2 (done):** the bill shape → `schemas/bill.py` + `expenses` table
- **Phase 3–4 (done):** photo → Gemini → structured `BillDraft` (one call)
- **Phase 5 (done):** checks before save + confirm endpoint
- **Phase 6:** "My Expenses" list + monthly total, demo bills, ship
- **Then:** 10 real bills through the live link → "N/10" + the miss

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/AUTH.md](docs/AUTH.md).
