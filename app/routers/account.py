"""The balance endpoints: one number each user owns, spent down by their bills.

Two rules keep this honest:

- The balance is what the *user* stored, so it is only ever changed by an
  explicit `PUT` or by the atomic decrement in `routers/bills.py`. Nothing
  recomputes it from expenses, because the opening money is not something the
  app can know.
- The decrement happens in the same SQL statement and the same transaction as
  the insert, so two bills confirmed against one balance cannot each subtract
  from the same old value.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_CURRENCY
from app.deps import get_current_user
from app.models.expense import Expense
from app.models.user import User
from app.schemas.account import AccountOut, BalanceSetRequest
from app.services.db import get_async_session

router = APIRouter(prefix="/account", tags=["account"])


async def account_snapshot(user: User, session: AsyncSession) -> AccountOut:
    """Rupees out of paise, plus the all-time total of this user's bills."""
    spent_minor = await session.scalar(
        select(func.coalesce(func.sum(Expense.total_minor), 0)).where(
            Expense.user_id == user.id
        )
    )
    return AccountOut(
        balance=None if user.balance_minor is None else Decimal(user.balance_minor) / 100,
        spent=Decimal(spent_minor) / 100,
        currency=DEFAULT_CURRENCY,
    )


@router.put("/balance", response_model=AccountOut)
async def set_balance(
    data: BalanceSetRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Set the money available: the opening balance first, a correction later.

    This replaces the number instead of adding to it. "I have 5,000" is a
    statement of fact; the amount spent is tracked separately, by the bills.
    """
    user.balance_minor = int(data.balance * 100)
    await session.commit()
    return await account_snapshot(user, session)


@router.get("", response_model=AccountOut)
async def get_account(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """The current balance and everything spent through the app."""
    return await account_snapshot(user, session)
