"""The `expenses` table: one row per bill the user confirmed.

Nothing writes here on extraction — a row only exists after the user has seen
the draft, fixed what was flagged, and pressed confirm.

Storage keeps money as an integer in the smallest unit (paise), never a float:
12500 is Rs 125.00. SQLite has no real decimal type, so integer paise is the
only representation that is exact. The API speaks rupees (`total`, a Decimal),
so the `total` property below is the one place the two meet; the confirm route
converts the other direction.
Items are a JSON column to start — one table, no joins.
"""

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

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
    # Nullable in storage only: the API requires both (checks refuse a draft
    # without a category), and old rows from before this field existed are NULL.
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    items: Mapped[list[dict]] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which prompt text produced the draft this row came from.
    prompt_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def total(self) -> Decimal:
        """The stored paise as rupees, e.g. 12800 -> Decimal("128.00")."""
        return (Decimal(self.total_minor) / 100).quantize(Decimal("0.01"))
