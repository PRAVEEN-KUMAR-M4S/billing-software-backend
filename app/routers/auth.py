"""Auth endpoints: register, login, refresh, logout, and /me."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import RefreshRequest, TokenResponse, UserCreate, UserLogin, UserOut
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_token,
    utcnow,
    verify_password,
)
from app.services.db import get_async_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate, session: AsyncSession = Depends(get_async_session)):
    email = data.email.lower().strip()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(email=email, hashed_password=hash_password(data.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _issue_tokens(user: User, session: AsyncSession) -> TokenResponse:
    """Create a fresh access + refresh pair and store the refresh hash. Shared by login and refresh."""
    raw_token, token_hash, expires_at = create_refresh_token()
    session.add(RefreshToken(token_hash=token_hash, user_id=user.id, expires_at=expires_at))
    await session.commit()
    return TokenResponse(access_token=create_access_token(user.id), refresh_token=raw_token)


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, session: AsyncSession = Depends(get_async_session)):
    user = await session.scalar(select(User).where(User.email == data.email.lower().strip()))
    if user is None or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    return await _issue_tokens(user, session)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(data: RefreshRequest, session: AsyncSession = Depends(get_async_session)):
    """Trade a refresh token for a new pair. Single-use: the old token dies here."""
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(data.refresh_token))
    )
    if row is None or row.expires_at < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")

    user = await session.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")

    # Rotate: delete the used token, then issue the new pair (one atomic commit).
    await session.delete(row)
    return await _issue_tokens(user, session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest, session: AsyncSession = Depends(get_async_session)):
    """Revoke one refresh token. Always 204 — even for a token we don't know."""
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(data.refresh_token))
    )
    if row is not None:
        await session.delete(row)
        await session.commit()


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
