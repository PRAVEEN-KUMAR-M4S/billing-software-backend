"""The shapes of data that enter and leave the auth endpoints.

- Request schemas: what the client must send us (we validate it).
- Response schemas: what we allow back out (never the raw database row
  with the password hash in it).
"""

from pydantic import BaseModel, Field


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


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str
