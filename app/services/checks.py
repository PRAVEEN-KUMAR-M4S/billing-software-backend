"""The cashier check: pure functions that re-check a draft before we trust it.

No HTTP, no database, no Gemini — so every rule here is testable on its own.
`check_bill` returns a list of Problems; an empty list means "safe to save".
"""

from datetime import date as date_type

from app.config import ALLOWED_CURRENCIES, TOTAL_TOLERANCE_MINOR
from app.schemas.bill import BillDraft, Problem


def _money(minor: int) -> str:
    """1250 -> '12.50', for messages a human reads."""
    return f"{minor / 100:.2f}"


def check_bill(draft: BillDraft) -> list[Problem]:
    problems: list[Problem] = []

    # 1 + 6. Required, sane values. A missing value is a fix-me field, not a crash.
    if not draft.shop or not draft.shop.strip():
        problems.append(Problem(field="shop", message="Shop name is missing or unreadable."))

    # 3. ISO date only — this is what kills the 12/03 ambiguity.
    if not draft.date:
        problems.append(Problem(field="date", message="Bill date is missing or unreadable."))
    else:
        try:
            parsed_date = date_type.fromisoformat(draft.date)
        except ValueError:
            parsed_date = None
        if parsed_date is None:
            problems.append(
                Problem(field="date", message="Date must be ISO YYYY-MM-DD (e.g. 2026-10-22).")
            )

    # 4. Currency must be one we actually handle.
    if not draft.currency or draft.currency.upper() not in ALLOWED_CURRENCIES:
        problems.append(
            Problem(
                field="currency",
                message=f"Currency must be one of: {', '.join(sorted(ALLOWED_CURRENCIES))}.",
            )
        )

    # 6. Total sanity.
    if draft.total_minor is None:
        problems.append(Problem(field="total_minor", message="Total is missing or unreadable."))
    elif draft.total_minor <= 0:
        problems.append(Problem(field="total_minor", message="Total must be greater than zero."))

    # 6. Item sanity, per item, so the fix form can highlight the exact row.
    if not draft.items:
        problems.append(Problem(field="items", message="No line items were found."))

    item_total = 0
    items_complete = bool(draft.items)
    for index, item in enumerate(draft.items):
        prefix = f"items[{index}]"
        if not item.name or not item.name.strip():
            problems.append(Problem(field=f"{prefix}.name", message="Item name is unreadable."))
        if item.qty is None:
            problems.append(Problem(field=f"{prefix}.qty", message="Quantity is unreadable."))
            items_complete = False
        elif item.qty < 1:
            problems.append(Problem(field=f"{prefix}.qty", message="Quantity must be at least 1."))
            items_complete = False
        if item.price_minor is None:
            problems.append(Problem(field=f"{prefix}.price_minor", message="Price is unreadable."))
            items_complete = False
        elif item.price_minor < 0:
            problems.append(Problem(field=f"{prefix}.price_minor", message="Price cannot be negative."))
            items_complete = False
        else:
            item_total += (item.qty or 0) * item.price_minor

    # 2. The maths. Only meaningful once every qty/price is known, and only
    #    once we have a total to compare against.
    if (
        draft.total_minor is not None
        and draft.total_minor > 0
        and items_complete
        and item_total > 0
    ):
        difference = abs(item_total - draft.total_minor)
        if difference > TOTAL_TOLERANCE_MINOR:
            problems.append(
                Problem(
                    field="total_minor",
                    message=(
                        f"Items add up to {_money(item_total)} but the total says "
                        f"{_money(draft.total_minor)}."
                    ),
                )
            )

    return problems
