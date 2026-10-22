"""Auth building blocks: password hashing + JWT tokens.

Pure functions only — no FastAPI, no database. That keeps this file easy
to test and easy to reason about.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import secrets

import bcrypt
import jwt

from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    REFRESH_TOKEN_EXPIRE_DAYS,
    SECRET_KEY,
)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> int | None:
    """Return the user id, or None if the token is expired/forged/garbage."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def utcnow() -> datetime:
    """UTC now, without timezone info (SQLite drops timezones anyway)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_token(token: str) -> str:
    """One-way hash — the DB never stores a usable refresh token."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token() -> tuple[str, str, datetime]:
    """Return (raw_token, token_hash, expires_at).

    The raw token goes to the client; only the hash goes in the DB.
    """
    raw = secrets.token_urlsafe(32)  # 256 random bits — unguessable
    expires_at = utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    return raw, hash_token(raw), expires_at
