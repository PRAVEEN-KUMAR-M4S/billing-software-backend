"""The shapes of data that enter and leave the auth endpoints.

- Request schemas: what the client must send us (we validate it).
- Response schemas: what we allow back out (never the raw database row
  with the password hash in it).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.config import DEFAULT_CURRENCY
from app.schemas.bill import Money


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=72)  # bcrypt's limit is 72 bytes


class UserLogin(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str

    model_config = {"from_attributes": True}


class MeOut(BaseModel):
    """The signed-in user plus the balance stored on their own row.

    The balance is read straight from `users.balance_minor`: no expenses are
    summed or subtracted here. `/account` still returns the derived `spent`
    total for screens that show it.
    """

    id: int
    email: str
    created_at: datetime | None = None
    balance: Money | None = Field(
        default=None,
        description="Money available now, from the user's row; null until set.",
    )
    currency: str = Field(default=DEFAULT_CURRENCY, description="ISO 4217 code.")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str
