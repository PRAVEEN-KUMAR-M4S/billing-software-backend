# Authentication — what was built (Phase 1)

Three endpoints, one protected helper, zero fluff.

## The endpoints

| Route | Does | Succeeds | Fails with |
| ----- | ---- | -------- | ---------- |
| `POST /auth/register` | creates an account | `201 {id, email}` | `409` if email exists, `422` if input invalid |
| `POST /auth/login` | checks password, issues token | `200 {access_token}` | `401` if email or password wrong |
| `GET /auth/me` | says who the token belongs to | `200 {id, email}` | `401` if token missing/invalid |

## The moving parts

```
schemas/auth.py      UserCreate / UserLogin  →  validate the JSON body
services/auth.py     hash_password, verify_password, create/decode token  (pure functions)
deps.py              get_current_user  →  token → User   (used by every protected route)
routers/auth.py      the three routes above
models/user.py       users table: id, email, hashed_password, created_at
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

- `sub` = your user id, `exp` = expiry (1 day from `config.py`).
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
  -d '{"email":"test@example.com","password":"secret123"}' | python -m json.tool)

curl -i $BASE/auth/me -H "Authorization: Bearer $TOKEN"           # 200
curl -i $BASE/auth/me                                             # 401
```

## What we deliberately skipped (and when to add it)

- **Refresh tokens** — add when access tokens expire during normal use.
- **Email verification / password reset** — needs an email service.
- **Rate limiting** — add before going public.
- **Roles / permissions** — add when there's more than one kind of user.

Everything above is Phase 2+ material. Ship the boring core first.
