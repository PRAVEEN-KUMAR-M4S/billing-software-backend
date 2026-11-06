"""The money shape: the balance the user owns, in rupees like everything else.

`users.balance_minor` is integer paise and nullable — NULL means "not set yet".
The API speaks rupees with two places (exactly like a bill draft), so the
client never has to know the storage unit too.

The balance is a *stored* number rather than "opening balance minus bills":
the user may have cash in hand that no bill in the app explains, and may
correct it at any time. `spent` is the derived half — the sum of every bill
this user has saved.
"""

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.config import DEFAULT_CURRENCY
from app.schemas.bill import Money, parse_money


class BalanceSetRequest(BaseModel):
    """What `PUT /account/balance` accepts: the money currently available."""

    balance: Money = Field(
        ge=0,
        description=(
            "Money available to spend, in rupees with two decimal places — "
            "5000.00, never paise. The first call is the opening balance; later "
            "calls replace it (a correction, not a top-up)."
        ),
    )

    @field_validator("balance", mode="before")
    @classmethod
    def _balance(cls, value: object) -> Decimal:
        amount = parse_money(value)
        if amount is None:
            raise ValueError("balance must be an amount in rupees, e.g. 5000.00")
        return amount


class AccountOut(BaseModel):
    """The account as the app shows it: what is left, and what has been spent."""

    balance: Money | None = Field(
        default=None,
        description="Money available now, or null when the user has not set one yet.",
    )
    spent: Money = Field(
        description="Sum of this user's saved bills (all time), in rupees.",
    )
    currency: str = Field(default=DEFAULT_CURRENCY, description="ISO 4217 code.")
