"""THE bill shape.

One definition, three users: it is the schema Gemini fills, the thing
`check_bill` validates, and the shape `POST /bills/confirm` accepts.
Change a field here and every one of them follows.

Every field is optional on purpose. A blurry bill is a normal outcome, and
"unreadable -> null" must be representable — the checks then tell the user
which field to fix. Money is always in whole minor units (paise).
"""

from datetime import date as date_type

from pydantic import BaseModel, Field

from app.config import DEFAULT_CURRENCY


class BillItem(BaseModel):
    name: str | None = Field(default=None, description="Item text as printed.")
    qty: int | None = Field(default=None, description="Printed quantity; at least 1.")
    price_minor: int | None = Field(
        default=None, description="Price in whole minor units (paise), never a float."
    )


class BillDraft(BaseModel):
    """A draft: it exists only in an HTTP response until a human confirms it."""

    shop: str | None = Field(default=None, description="Business name on the bill.")
    date: str | None = Field(default=None, description="ISO date, YYYY-MM-DD.")
    currency: str = Field(default=DEFAULT_CURRENCY, description="ISO 4217 code.")
    total_minor: int | None = Field(
        default=None, description="Bill total in whole minor units (paise)."
    )
    items: list[BillItem] = Field(default_factory=list)
    notes: str | None = Field(
        default=None, description="Why a field is null, or anything odd on the bill."
    )


class Problem(BaseModel):
    """One thing that must be fixed before the draft can be saved."""

    field: str  # e.g. "total_minor" or "items[2].price_minor"
    message: str


class ExtractionReport(BaseModel):
    """What extraction returns: the draft plus everything wrong with it."""

    draft: BillDraft
    problems: list[Problem]


class BillConfirmRequest(BaseModel):
    """The user's edited draft, on its way to the database."""

    draft: BillDraft


class ExpenseOut(BaseModel):
    """A saved bill, as it leaves the API."""

    id: int
    shop: str
    date: date_type  # serialises to ISO in JSON
    currency: str
    total_minor: int
    items: list[BillItem]
    notes: str | None = None

    model_config = {"from_attributes": True}
