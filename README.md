# Freebuff Desktop — Bill Reader API

Turn a **photo of a bill** into **clean, structured expense data** — with auth first.

**Status:** Phase 1 done ✅ — Authentication (register / login / who-am-I).
**Next:** Phase 2 — upload a bill photo → Claude fills a fixed form → your code checks it.

---

## Run it

```bash
uv sync                  # install dependencies (once)
cp .env.example .env     # then edit SECRET_KEY
uv run main.py           # start the server on http://127.0.0.1:8000
```

- Interactive API docs: http://127.0.0.1:8000/docs
- Local database: `test.db` (SQLite file, auto-created on start, git-ignored)

## Folder structure (and why)

```
main.py              # just starts the server (entry point)
app/
├── app.py           # the app itself: startup hook + "which routers exist"
├── config.py        # settings from .env (SECRET_KEY, token lifetime)
├── deps.py          # shared route protections (get_current_user)
├── routers/         # LAYER 1 — the web: URLs, status codes, HTTP
│   └── auth.py
├── schemas/         # LAYER 2 — the shapes: what data comes IN and OUT
│   └── auth.py
├── services/        # LAYER 3 — the logic: database, crypto
│   ├── db.py
│   └── auth.py
└── models/          # LAYER 4 — the storage: tables and columns
    ├── user.py
    └── refresh_token.py
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

## Roadmap

- **Phase 1 (done):** auth
- **Phase 2:** design the bill shape (schema on paper → `expenses` table)
- **Phase 3:** send a photo to Claude, first as plain description
- **Phase 4:** force the shape with tool use / structured output
- **Phase 5:** validate before save (maths check, highlight bad fields)
- **Phase 6:** "My Expenses" list + monthly total, demo bills, ship

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/AUTH.md](docs/AUTH.md).
