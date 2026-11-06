"""The `monthly_insights` table: one saved Smart Tip per user per month.

Why store it at all: the tip costs one Gemini call, and a monthly screen
opening every day must not mean a call every day. The row is the cache — the
unique (user_id, month) pair is what makes "once a month" true, even if two
requests arrive at the same moment.

The *facts* (biggest category, most visited shop, average bill) are not stored:
they are a cheap SQL sum, and they should stay true when a bill is added later
in the month. Only the sentence on top is frozen.

`source` matters: "ai" is a real Gemini tip and is final for the month. "local"
is the arithmetic fallback used when Gemini was busy or out of quota, and it is
retried after `INSIGHTS_RETRY_HOURS` instead of costing the user the whole month.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.services.db import Base


class MonthlyInsight(Base):
    __tablename__ = "monthly_insights"
    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_monthly_insights_user_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    # "YYYY-MM" — the month the tip talks about.
    month: Mapped[str] = mapped_column(String(7), index=True)
    tip_text: Mapped[str] = mapped_column(Text)
    # Nullable: an "empty month" tip has no category, but those are never stored.
    tip_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(8))  # "ai" | "local"
    prompt_version: Mapped[str] = mapped_column(String(16))
    # When this text was produced — also when a local fallback may be retried.
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
