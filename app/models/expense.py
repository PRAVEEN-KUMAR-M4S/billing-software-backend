"""The `expenses` table: one row per bill the user confirmed.

Nothing writes here on extraction — a row only exists after the user has seen
the draft, fixed what was flagged, and pressed confirm.

Money is an integer in the smallest unit (paise), never a float: 1250 is
Rs 12.50. The column name says `minor` so future-you cannot forget.
Items are a JSON column to start — one table, no joins.
"""

from datetime import date as date_type
from datetime import datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.services.db import Base


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    shop: Mapped[str] = mapped_column(String(255))
    date: Mapped[date_type] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    total_minor: Mapped[int] = mapped_column(Integer)
    items: Mapped[list[dict]] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which prompt text produced the draft this row came from.
    prompt_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
