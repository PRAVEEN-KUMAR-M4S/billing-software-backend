"""Monthly insights: the facts are arithmetic, the sentence is AI, the screen never fails.

Everything here is portable SQL — one grouped read of the month's expenses, no
SQLite-only date functions — so the same code runs on Postgres by changing
`DATABASE_URL`.

Order of operations on the screen's first open of a month:

1. Sum the month (always, cheap).
2. No bills? Return a friendly local tip and stop — no AI call, nothing cached.
3. Bills? Ask Gemini for one tip, constrained to `TipDraft`.
4. Gemini busy or out of quota? Use `local_tip`, still written from the real
   numbers, and save it as a retryable fallback.

Nothing in this module can turn into a 5xx for the app: the caller gets a tip
either way.
"""

import json
from datetime import date as date_type
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from google.genai import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    DEFAULT_CURRENCY,
    EXPENSE_CATEGORIES,
    EXPENSE_CATEGORY_LABELS,
    INSIGHTS_PROMPT,
)
from app.models.expense import Expense
from app.schemas.bill import format_money
from app.schemas.insight import CategoryFact, Challenge, QuickFacts, ShopFact, SmartTip, TipDraft
from app.services.gemini import GeminiError, candidate_models, get_client, upstream_code

_PAISE = Decimal("0.01")


def _rupees(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(_PAISE)


def month_bounds(month: str) -> tuple[date_type, date_type]:
    """'2026-10' -> (2026-10-01, 2026-11-01). Raises ValueError for junk.

    Bounds rather than `strftime('%Y-%m', date)`: that function differs between
    SQLite and Postgres, and a half-open range uses an index on both.
    """
    if (
        len(month) != 7
        or month[4] != "-"
        or not (month[:4].isdigit() and month[5:].isdigit())
    ):
        raise ValueError("month must be YYYY-MM, e.g. 2026-10")
    start = date_type(int(month[:4]), int(month[5:]), 1)  # ValueError for month 00/13
    end = date_type(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def month_label(month: str) -> str:
    """'2026-10' -> 'October 2026'."""
    start, _ = month_bounds(month)
    return f"{start:%B %Y}"


def current_month() -> str:
    return date_type.today().strftime("%Y-%m")


def challenge_for(day: date_type) -> Challenge:
    """A small weekly nudge, rotated by week, needing no AI and no storage."""
    week_start = day - timedelta(days=day.weekday())
    challenges = (
        (
            "Record every bill for 7 days",
            "A quick note after each purchase makes spending easier to follow.",
        ),
        (
            "Look at one bill twice",
            "Compare the printed total with what you saved — catch a misread before it adds up.",
        ),
        (
            "Set one daily limit",
            "Pick a number you can keep for a day and see how close you get.",
        ),
        (
            "Name your biggest category",
            "Before you spend today, guess which category is costing you most this month.",
        ),
    )
    title, note = challenges[week_start.isocalendar().week % len(challenges)]
    return Challenge(title=title, note=note, week_start=week_start)


async def facts_for(
    session: AsyncSession, user_id: int, start: date_type, end: date_type
) -> QuickFacts:
    """Everything on the card that is arithmetic, from this user's bills only."""
    result = await session.execute(
        select(Expense.category, Expense.shop, Expense.total_minor).where(
            Expense.user_id == user_id,
            Expense.date >= start,
            Expense.date < end,
        )
    )
    rows = result.all()
    if not rows:
        return QuickFacts(
            bills=0,
            total=Decimal("0.00"),
            average_bill=Decimal("0.00"),
        )

    total_minor = sum(row.total_minor for row in rows)
    category_totals: dict[str, int] = {}
    category_bills: dict[str, int] = {}
    shop_bills: dict[str, int] = {}
    for row in rows:
        # Old rows predate the category field; they were migrated to "other".
        category = row.category if row.category in EXPENSE_CATEGORIES else "other"
        category_totals[category] = category_totals.get(category, 0) + row.total_minor
        category_bills[category] = category_bills.get(category, 0) + 1
        shop_bills[row.shop] = shop_bills.get(row.shop, 0) + 1

    # Deterministic: the bigger total wins, and a tie goes to the earlier slug.
    top_category = min(category_totals, key=lambda slug: (-category_totals[slug], slug))
    top_shop = min(shop_bills, key=lambda shop: (-shop_bills[shop], shop))

    return QuickFacts(
        bills=len(rows),
        total=_rupees(total_minor),
        average_bill=(_rupees(total_minor) / len(rows)).quantize(
            _PAISE, rounding=ROUND_HALF_UP
        ),
        biggest_category=CategoryFact(
            category=top_category,
            label=EXPENSE_CATEGORY_LABELS[top_category],
            icon=EXPENSE_CATEGORIES[top_category][0],
            total=_rupees(category_totals[top_category]),
            bills=category_bills[top_category],
        ),
        most_visited_shop=ShopFact(shop=top_shop, bills=shop_bills[top_shop]),
    )


def local_tip(facts: QuickFacts) -> SmartTip:
    """The tip that needs no AI: still true, just plainer.

    Used for an empty month and whenever Gemini is busy, out of quota, or has no
    key. The screen has a tip either way, which is the point.
    """
    if facts.biggest_category is None:
        return SmartTip(
            text=(
                "No bills this month yet. Add your first one and this space starts "
                "finding patterns for you."
            ),
            category=None,
            action_label=None,
            source="local",
        )

    fact = facts.biggest_category
    bills = "bill" if fact.bills == 1 else "bills"
    return SmartTip(
        text=(
            f"{fact.label} is your biggest expense this month: {format_money(fact.total)} "
            f"across {fact.bills} {bills}. Worth a look at the small ones."
        ),
        category=fact.category,
        action_label=f"See {fact.label} bills",
        source="local",
    )


def _brief(facts: QuickFacts, month_label_text: str) -> str:
    """The facts as JSON — the only input the model gets, and nothing it can invent."""
    biggest = facts.biggest_category
    shop = facts.most_visited_shop
    return json.dumps(
        {
            "month": month_label_text,
            "currency": DEFAULT_CURRENCY,
            "bills": facts.bills,
            "total": format_money(facts.total),
            "average_bill": format_money(facts.average_bill),
            "biggest_category": None
            if biggest is None
            else {
                "slug": biggest.category,
                "label": biggest.label,
                "total": format_money(biggest.total),
                "bills": biggest.bills,
            },
            "most_visited_shop": None
            if shop is None
            else {"shop": shop.shop, "bills": shop.bills},
        },
        ensure_ascii=False,
    )


def _tip_from(response) -> TipDraft:
    draft = response.parsed
    if isinstance(draft, TipDraft):
        return draft

    raw = response.text
    if not raw:
        raise GeminiError("Gemini returned no tip.")
    try:
        return TipDraft.model_validate_json(raw)
    except ValueError as exc:
        raise GeminiError("Gemini returned something that is not a tip.") from exc


def generate_tip(facts: QuickFacts, month_label_text: str) -> TipDraft:
    """One Gemini call for one month's tip. Blocking — call it from a thread."""
    client = get_client()
    config = types.GenerateContentConfig(
        system_instruction=INSIGHTS_PROMPT,
        response_mime_type="application/json",
        response_schema=TipDraft,
        temperature=0.4,
    )
    brief = _brief(facts, month_label_text)

    overloaded: list[str] = []
    for model in candidate_models():
        try:
            response = client.models.generate_content(
                model=model,
                contents=["Write this month's tip from these facts.", brief],
                config=config,
            )
        except Exception as exc:  # network, auth, quota, overload, ...
            code = upstream_code(exc)
            if code == 503:
                overloaded.append(model)
                continue
            if code == 429:
                raise GeminiError(
                    f"{model} is out of quota (429 RESOURCE_EXHAUSTED).", status_code=429
                ) from exc
            raise GeminiError(f"Gemini request failed: {exc}") from exc
        return _tip_from(response)

    raise GeminiError(
        "Gemini is overloaded right now (503 UNAVAILABLE — high demand) on "
        f"{', '.join(overloaded)}.",
        status_code=503,
    )
