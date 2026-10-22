# Authentication — what was built (Phase 1)

Three endpoints, one protected helper, zero fluff.

## The endpoints

| Route | Does | Succeeds | Fails with |
| ----- | ---- | -------- | ---------- |
| `POST /auth/register` | creates an account | `201 {id, email}` | `409` if email exists, `422` if input invalid |
| `POST /auth/login` | checks password, issues token pair | `200 {access_token, refresh_token}` | `401` if email or password wrong |
| `POST /auth/refresh` | trades refresh token for a new pair | `200 {access_token, refresh_token}` | `401` if token unknown/expired/already used |
| `POST /auth/logout` | revokes one refresh token | `204` (always, even for unknown tokens) | — |
| `GET /auth/me` | says who the token belongs to | `200 {id, email}` | `401` if token missing/invalid |

## The two tokens (and why there are two)

| | Access token | Refresh token |
| -------------- | ------------------------------- | -------------------------------- |
| What it is | JWT signed with `SECRET_KEY` | 43-char random string (`secrets.token_urlsafe(32)`) |
| Lifetime | 30 minutes | 30 days |
| Stored where | Nowhere — stateless | `refresh_tokens` table, **hashed** (sha256) |
| Can be revoked? | No (it expires on its own) | Yes — delete the row |
| Sent as | `Authorization: Bearer …` header | JSON body of `/auth/refresh` |

The deal: the access token is small and fast but the server can't un-issue it —
so it's only trusted for 30 minutes. The refresh token can be revoked and is
**single-use** — every refresh deletes the old row and issues a new pair
("rotation"). So a stolen access token dies in ≤30 min, and a stolen refresh
token dies the next time the real client refreshes.

## The refresh flow, step by step

```
POST /auth/refresh {"refresh_token": "<raw>"}
  1. sha256 the raw token
  2. look up that hash in refresh_tokens        → missing? 401
  3. expires_at < now?                          → 401
  4. load the user behind it                    → gone? 401 (same message — no hints)
  5. delete the old row
  6. issue a new access + refresh pair (one commit — atomic)
```

`POST /auth/logout` does steps 1–2, deletes the row, and returns 204 even if
the token was already unknown — logout never leaks information.

Each login creates its own row, so logging out one device doesn't log out the
others.

## The moving parts

```
schemas/auth.py      UserCreate / UserLogin / RefreshRequest  →  validate the JSON body
services/auth.py     hash_password, verify_password, JWT create/decode,
                     refresh-token create + hash  (pure functions)
deps.py              get_current_user  →  token → User   (used by every protected route)
routers/auth.py      the five routes above
models/user.py       users table: id, email, hashed_password, created_at
models/refresh_token.py  refresh_tokens table: token_hash, user_id, expires_at, created_at
```

## Why passwords look like this in the DB

```
plain:   secret123
stored:  $2b$12$Kix6hbh1N9Yc3f0u2Vq0XeY5oS2rS8f6o6bJmVd0Yw3xqAqBqCqWq
```

bcrypt does three things plain hashing doesn't:

1. **Salt** — two identical passwords produce different hashes (no rainbow tables).
2. **Slow rounds** — `gensalt()` defaults to cost 12; each login takes ~100–300 ms
   *on purpose*, so stolen hashes are expensive to crack.
3. **72-byte limit** — hence `max_length=72` in the schema.

If the DB leaks, attackers get hashes, not passwords. (This is why you should
still pick a real password — but that's their problem, not yours.)

## Why JWT

```
header.payload.signature
eyJhbGci...    eyJzdWIi...   DHKf8...
```

- `sub` = your user id, `exp` = expiry (30 minutes from `config.py`).
- The signature is made with `SECRET_KEY` — **if any part is tampered with, the
  signature fails and `decode_access_token` returns None → 401.**
- The server keeps **no session state**: the token *is* the session. Restart the
  server and everyone stays logged in.

Never commit `SECRET_KEY`. It lives in `.env` (git-ignored). Anyone with the key
can mint tokens for any user — treat it like a password.

## The three security checks you should be able to explain out loud

1. **Wrong password at login** → `verify_password` returns False → 401
   (same message as unknown email, so attackers can't enumerate accounts).
2. **Forged/expired token** → `jwt.decode` raises → 401, route body never runs.
3. **Password never leaves storage** → register hashes before `session.add()`,
   and `response_model=UserOut` only allows `id` and `email` back out.

## Manual test script

```bash
BASE=http://127.0.0.1:8000

curl -i -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"secret123"}'        # 201

curl -i -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"secret123"}'        # 409

TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"secret123"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -i $BASE/auth/me -H "Authorization: Bearer $TOKEN"           # 200
curl -i $BASE/auth/me                                             # 401

# --- refresh + logout ---
RT=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com","password":"secret123"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["refresh_token"])')

curl -i -X POST $BASE/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$RT\"}"                                 # 200 new pair
curl -i -X POST $BASE/auth/refresh -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$RT\"}"                                 # 401 — already used
curl -i -X POST $BASE/auth/logout -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$RT\"}"                                 # 204
```

## What we deliberately skipped (and when to add it)

- **Email verification / password reset** — needs an email service.
- **Rate limiting** — add before going public.
- **Roles / permissions** — add when there's more than one kind of user.
- **Reuse detection (token families)** — if a used refresh token is replayed, we
  just 401. Production systems often revoke ALL of that user's sessions at that
  point (a replay means the token was probably stolen). Add when you ship to
  real users.
- **Expired-row cleanup** — expired refresh rows just sit there until used;
  a nightly `DELETE … WHERE expires_at < now` is plenty when needed.
