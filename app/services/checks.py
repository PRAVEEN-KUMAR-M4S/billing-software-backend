"""The cashier check: pure functions that re-check a draft before we trust it.

No HTTP, no database, no Gemini — so every rule here is testable on its own.
`check_bill` returns a list of Problems; an empty list means "safe to save".

Money here is rupees as `Decimal`; every comparison is exact (no floats), so
127.49 + 0.51 is exactly 128.00.
"""

from datetime import date as date_type
from decimal import Decimal

from app.config import ALLOWED_CURRENCIES, CASHIER_ROUNDING, TOTAL_TOLERANCE
from app.schemas.bill import BillDraft, Problem, format_money


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
    if draft.total is None:
        problems.append(Problem(field="total", message="Total is missing or unreadable."))
    elif draft.total <= 0:
        problems.append(Problem(field="total", message="Total must be greater than zero."))

    # 6. Item sanity, per item, so the fix form can highlight the exact row.
    if not draft.items:
        problems.append(Problem(field="items", message="No line items were found."))

    item_total = Decimal("0.00")
    items_complete = bool(draft.items)
    line_overflow = False  # a line bigger than the bill already explains the mismatch
    for index, item in enumerate(draft.items):
        prefix = f"items[{index}]"
        if not item.name or not item.name.strip():
            problems.append(Problem(field=f"{prefix}.name", message="Item name is unreadable."))
        # qty is informational — a bill that prints none is not an error — but a
        # zero or negative quantity always is.
        if item.qty is not None and item.qty < 1:
            problems.append(Problem(field=f"{prefix}.qty", message="Quantity must be at least 1."))
        if item.amount is None:
            problems.append(
                Problem(field=f"{prefix}.amount", message="Line amount is unreadable.")
            )
            items_complete = False
        elif item.amount < 0:
            problems.append(
                Problem(field=f"{prefix}.amount", message="Line amount cannot be negative.")
            )
            items_complete = False
        else:
            item_total += item.amount
            # One line can never be bigger than the whole bill. When it is, an
            # MRP or a unit rate was read as the line's money — or the amounts
            # are in the old unit (paise) while the total is in rupees.
            # Flagging the row points the fix form at the field to edit.
            if (
                draft.total is not None
                and draft.total > 0
                and item.amount > draft.total + TOTAL_TOLERANCE
            ):
                line_overflow = True
                problems.append(
                    Problem(
                        field=f"{prefix}.amount",
                        message=(
                            f"'{item.name}' is {format_money(item.amount)}, more than "
                            f"the whole bill ({format_money(draft.total)}). This must "
                            f"be the amount charged for the line in rupees — not an "
                            f"MRP, a unit rate, or paise."
                        ),
                    )
                )

    # 2. The maths. Only meaningful once every line amount is known, and only
    #    once we have a total to compare against. A printed round-off line is
    #    part of the arithmetic, but it has to be stated on the draft, not
    #    silently absorbed into the tolerance.
    if (
        draft.total is not None
        and draft.total > 0
        and items_complete
        and item_total > 0
        and not line_overflow
    ):
        round_off = draft.round_off if draft.round_off is not None else Decimal("0.00")
        expected = item_total + round_off
        # A stated round-off means the paper adds up to the paise; with none
        # printed the cashier may still have rounded to the nearest rupee.
        tolerance = TOTAL_TOLERANCE if draft.round_off is not None else CASHIER_ROUNDING
        if abs(expected - draft.total) > tolerance:
            if draft.round_off is not None:
                sign = "+" if round_off >= 0 else "-"
                message = (
                    f"Items add up to {format_money(item_total)} {sign} round-off "
                    f"{format_money(abs(round_off))} = {format_money(expected)}, but the "
                    f"total says {format_money(draft.total)}."
                )
            else:
                gap = abs(item_total - draft.total)
                message = (
                    f"Items add up to {format_money(item_total)} but the total says "
                    f"{format_money(draft.total)} (off by {format_money(gap)})."
                )
            problems.append(Problem(field="total", message=message))

    return problems
