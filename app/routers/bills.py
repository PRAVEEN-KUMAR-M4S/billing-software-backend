"""Bill endpoints: photo in -> draft out; edited draft in -> saved row out.

The router orchestrates. Gemini lives in services/extraction.py, the rules
live in services/checks.py, and the database is touched only at the very end —
in `/confirm`, after a human has approved a draft that passes every check.
"""

import json
from datetime import date as date_type

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import ALLOWED_IMAGE_TYPES, MAX_UPLOAD_BYTES, PROMPT_VERSION
from app.deps import get_current_user
from app.models.expense import Expense
from app.models.user import User
from app.schemas.bill import (
    BillConfirmRequest,
    ExpenseOut,
    ExtractionReport,
)
from app.services.checks import check_bill
from app.services.db import get_async_session
from app.services.extraction import ExtractionError, extract_bill

router = APIRouter(prefix="/bills", tags=["bills"])


@router.post("/extract", response_model=ExtractionReport)
async def extract(
    file: UploadFile = File(...),
    _user: User = Depends(get_current_user),
):
    """Read one bill photo and return a draft. Writes nothing, stores nothing."""
    media_type = (file.content_type or "").split(";")[0].strip().lower()
    if media_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a JPG or PNG photo."
        )

    # Read one byte past the cap: enough to know it is too big, without
    # loading a 40 MB file into memory to find out.
    photo = await file.read(MAX_UPLOAD_BYTES + 1)
    if not photo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")
    if len(photo) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Photo is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    # The SDK call is blocking; a thread keeps the event loop free.
    try:
        draft = await run_in_threadpool(extract_bill, photo, media_type)
    except ExtractionError as exc:
        # 503 = the model is busy (retry shortly), 429 = quota is spent (wait for
        # the reset), 502 = anything else. The message says which, so a dev knows
        # whether retrying is worth it.
        raise HTTPException(
            exc.status_code,
            f"Could not read this bill, and nothing was saved. {exc}",
            headers={"Retry-After": "60"} if exc.status_code == 503 else None,
        ) from exc

    return ExtractionReport(draft=draft, problems=check_bill(draft))


@router.post("/confirm", response_model=ExpenseOut, status_code=status.HTTP_201_CREATED)
async def confirm(
    data: BillConfirmRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Save an edited draft — but only if it passes every check again.

    This is the only door into the database. Re-checking here (and not just
    trusting the draft we returned) is what stops a hand-edited wrong total
    from being saved.
    """
    draft = data.draft
    problems = check_bill(draft)
    if problems:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Fix the highlighted fields before saving.",
                "problems": [problem.model_dump() for problem in problems],
            },
        )

    row = Expense(
        user_id=user.id,
        shop=draft.shop.strip(),
        date=date_type.fromisoformat(draft.date),
        currency=draft.currency.upper(),
        category=draft.category,
        icon=draft.icon,
        # The API speaks rupees; SQLite gets exact integer paise.
        total_minor=int(draft.total * 100),
        # model_dump_json keeps each amount as the string "127.49", which is
        # JSON-safe and re-validates back into a Decimal when read out.
        items=[json.loads(item.model_dump_json()) for item in draft.items],
        notes=draft.notes,
        prompt_version=PROMPT_VERSION,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("", response_model=list[ExpenseOut])
async def get_all_expenses(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Return all expenses belonging to the authenticated user."""

    result = await session.execute(
        select(Expense)
        .where(Expense.user_id == user.id)
        .order_by(Expense.date.desc())
    )

    expenses = result.scalars().all()
    return expenses
