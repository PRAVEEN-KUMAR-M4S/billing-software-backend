"""THE bill shape.

One definition, three users: it is the schema Gemini fills, the thing
`check_bill` validates, and the shape `POST /bills/confirm` accepts.
Change a field here and every one of them follows.

Every field is optional on purpose. A blurry bill is a normal outcome, and
"unreadable -> null" must be representable — the checks then tell the user
which field to fix.

Money is in RUPEES, as a `Decimal` with two places, and serialises to JSON as
the string "128.00" — never a float (0.1 + 0.2 problems) and never paise. The
database still stores integer paise; the conversion lives in the confirm route
only, so the API has one money unit and the storage has one exact one.
"""

from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.config import DEFAULT_CURRENCY, EXPENSE_CATEGORIES

_TWO_PLACES = Decimal("0.01")
_SYMBOLS = ("₹", "$", "£", "€", ",", "\u00a0")


def parse_money(raw: object) -> Decimal | None:
    """Anything a model may say for an amount -> rupees with two places, or None.

    Accepts 128, 128.0, "128", "128.00", "₹1,234.50", "Rs. 128". Returns None
    for junk ("N/A", "-", "") so one unreadable cell costs one field instead of
    the whole bill — the checks then report that field as unreadable.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        amount = raw
    elif isinstance(raw, int):
        amount = Decimal(raw)
    elif isinstance(raw, float):
        amount = Decimal(str(raw))  # str first: 128.0 must not pick up binary noise
    elif isinstance(raw, str):
        text = raw.strip()
        for symbol in _SYMBOLS:
            text = text.replace(symbol, "")
        text = text.upper().replace("RS.", "").replace("RS", "").replace("INR", "").strip()
        if not text:
            return None
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None

    if not amount.is_finite():
        return None
    try:
        amount = amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation:  # absurd exponent, e.g. 1E+40
        return None
    return Decimal("0.00") if amount == 0 else amount


def format_money(amount: Decimal | None) -> str | None:
    """128 -> '128.00', for JSON and for messages a human reads."""
    return None if amount is None else f"{amount:.2f}"


# One category from the fixed list, and one HugeIcons name from that category.
# Both are enums, so Gemini cannot answer with a category the app does not know
# or an icon name Flutter cannot draw.
Category = Literal[tuple(EXPENSE_CATEGORIES)]
Icon = Literal[tuple(icon for icons in EXPENSE_CATEGORIES.values() for icon in icons)]


# One money field: rupees in, two-place Decimal inside, "128.00" out.
#
# Pydantic's own schema for Decimal is `number | string`, which Gemini would
# receive as anyOf(NUMBER, STRING) — an ambiguous instruction that invites
# "128.00" as a string. `WithJsonSchema` pins the schema Gemini sees to a plain
# number; `| None` then becomes NUMBER + nullable. The serializer formats the
# JSON we return; the before-validator accepts whatever the model said.
Money = Annotated[
    Decimal,
    PlainSerializer(format_money, return_type=str, when_used="json"),
    WithJsonSchema({"type": "number"}, mode="validation"),
]


class BillItem(BaseModel):
    name: str | None = Field(
        default=None,
        description=(
            "Product text as printed, including pack size or strength "
            "(\"10 TAB\", \"500 MG\")."
        ),
    )
    qty: int | None = Field(
        default=None,
        description=(
            "Quantity printed on the line (the Qty column, e.g. 10 tablets). "
            "For information only: money never multiplies by it. Null when the "
            "bill prints no quantity. A pack size in the name (\"10's\", \"500 MG\") "
            "is part of the name, never a quantity."
        ),
    )
    amount: Money | None = Field(
        default=None,
        description=(
            "The money charged for this WHOLE line, in rupees with two decimal "
            "places: the Amount / Net column, after any discount and including "
            "tax. Never a unit rate, never an MRP, and never multiplied by qty. "
            "Rs 127.49 is 127.49."
        ),
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: object) -> Decimal | None:
        return parse_money(value)


class BillDraft(BaseModel):
    """A draft: it exists only in an HTTP response until a human confirms it."""

    shop: str | None = Field(default=None, description="Business name on the bill.")
    date: str | None = Field(default=None, description="ISO date, YYYY-MM-DD.")
    currency: str = Field(default=DEFAULT_CURRENCY, description="ISO 4217 code.")
    category: Category | None = Field(
        default=None,
        description=(
            "One category for the whole bill (e.g. groceries, health, transport), "
            "from the schema's enum. Required."
        ),
    )
    icon: Icon | None = Field(
        default=None,
        description=(
            "One HugeIcons 1.2.0 stroke-rounded member name from the schema's enum, "
            "e.g. strokeRoundedHome01, chosen from the category's own icons."
        ),
    )
    total: Money | None = Field(
        default=None,
        description="The printed bill total / net payable, in rupees with two decimal places.",
    )
    round_off: Money | None = Field(
        default=None,
        description=(
            "The printed Round Off / rounding / adjustment line, signed, in "
            "rupees (e.g. 0.51 for +0.51, -0.50 for -0.50). Null when the bill "
            "prints none — never invent one."
        ),
    )
    items: list[BillItem] = Field(default_factory=list)

    @field_validator("total", "round_off", mode="before")
    @classmethod
    def _money_fields(cls, value: object) -> Decimal | None:
        return parse_money(value)

    @model_validator(mode="after")
    def _icon_belongs_to_category(self) -> "BillDraft":
        """Always leave with a drawable icon.

        The model's pick is kept when it is one of that category's icons;
        otherwise the category's default is used. Nothing is invented, so the
        Flutter side always has a real `HugeIcons.<name>` to render.
        """
        if self.category is not None and self.icon not in EXPENSE_CATEGORIES[self.category]:
            self.icon = EXPENSE_CATEGORIES[self.category][0]
        return self
    notes: str | None = Field(
        default=None, description="Why a field is null, or anything odd on the bill."
    )


class Problem(BaseModel):
    """One thing that must be fixed before the draft can be saved."""

    field: str  # e.g. "total" or "items[2].qty"
    message: str


class ExtractionReport(BaseModel):
    """What extraction returns: the draft plus everything wrong with it."""

    draft: BillDraft
    problems: list[Problem]


class BillConfirmRequest(BaseModel):
    """The user's edited draft, on its way to the database."""

    draft: BillDraft


class ExpenseOut(BaseModel):
    """A saved bill, as it leaves the API. `total` is rupees, e.g. "128.00"."""

    id: int
    shop: str
    date: date_type  # serialises to ISO in JSON
    currency: str
    total: Money
    category: Category
    icon: Icon
    items: list[BillItem]
    notes: str | None = None

    model_config = {"from_attributes": True}
