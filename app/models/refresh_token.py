"""The `refresh_tokens` table: one row per logged-in device/session.

We store only a HASH of the token — if the database leaks, the attacker
gets unusable hashes, not tokens that log people in.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.services.db import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
