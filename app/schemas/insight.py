"""The Smart Tips shapes: one month of tips, facts and a small challenge.

The screen has three blocks and the API returns all three in one response, so
the app opens it once:

- `tip`     — the AI-written nudge (or the local fallback), which is the only
              part that costs a Gemini call and so the only part cached per month.
- `facts`   — arithmetic from the user's own bills for that month: how many,
              the total, the biggest category, the most visited shop, the
              average bill. Always true as of now.
- `challenge` — a small, deterministic weekly nudge. No AI, no storage.

Money is rupees with two places, exactly like every other amount in the API.
"""

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.config import DEFAULT_CURRENCY
from app.schemas.bill import Category, Icon, Money


class TipDraft(BaseModel):
    """What Gemini fills for the card. One sentence set, constrained by the schema."""

    category: Category = Field(
        description=(
            "The category the tip is about — usually the biggest one. One slug "
            "from the schema's enum."
        ),
    )
    headline: str = Field(
        description=(
            "The tip itself: at most two sentences, under 160 characters, second "
            "person, specific to the facts given. No emojis, no numbers you were "
            "not given."
        ),
    )
    action_label: str = Field(
        description=(
            "A short button label naming the category, under 32 characters, e.g. "
            "\"See Food & drink bills\"."
        ),
    )

    @field_validator("headline", "action_label", mode="before")
    @classmethod
    def _one_line(cls, value: object) -> str:
        """Collapse whitespace; an empty string is a failed generation, not a tip."""
        text = " ".join(str(value or "").split())
        if not text:
            raise ValueError("must not be empty")
        return text


class CategoryFact(BaseModel):
    """The category that took the most money this month."""

    category: Category
    label: str = Field(description="Human name, e.g. \"Food & drink\".")
    icon: Icon = Field(description="The category's default hugeicons name.")
    total: Money
    bills: int


class ShopFact(BaseModel):
    """The shop that appears on the most bills this month."""

    shop: str
    bills: int


class QuickFacts(BaseModel):
    """Everything on the card that is arithmetic rather than language."""

    bills: int = Field(description="Bills saved in this month.")
    total: Money = Field(description="Money spent this month.")
    average_bill: Money = Field(description="total / bills, to the paise; 0.00 with no bills.")
    biggest_category: CategoryFact | None = None
    most_visited_shop: ShopFact | None = None


class SmartTip(BaseModel):
    """The card at the top: a sentence, and the category it points at."""

    text: str
    category: Category | None = Field(
        default=None, description="Category to filter the bill list by; null for an empty month."
    )
    action_label: str | None = Field(
        default=None, description="Button label, e.g. \"See Food & drink bills\"."
    )
    source: Literal["ai", "local"] = Field(
        description="\"ai\" for a Gemini-written tip, \"local\" for the arithmetic fallback.",
    )


class Challenge(BaseModel):
    """The small weekly nudge — deterministic, so it costs nothing and never fails."""

    title: str
    note: str
    week_start: date_type = Field(description="Monday of the week this challenge belongs to.")


class InsightsOut(BaseModel):
    """One request fills the whole Smart Tips screen."""

    month: str = Field(description="YYYY-MM")
    month_label: str = Field(description="Human label, e.g. \"October 2026\".")
    currency: str = DEFAULT_CURRENCY
    generated_at: datetime = Field(
        description="When this tip was written. A cached month keeps its original time.",
    )
    tip: SmartTip
    facts: QuickFacts
    challenge: Challenge
