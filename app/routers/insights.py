"""Smart Tips: one request fills the whole monthly screen.

`GET /insights?month=YYYY-MM` returns the tip, the facts and the weekly
challenge together. The tip is the only expensive part, so it is generated on
the first open of that month and saved in `monthly_insights` — the rest is a
cheap sum, recomputed every time so it stays true as bills are added.

The route never answers 5xx because of Gemini: a failed generation becomes the
local tip, sourced from the same numbers. Only a genuine storage failure could
500, and a duplicate insert is handled by re-reading the row.
"""

from datetime import date as date_type, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import INSIGHTS_PROMPT_VERSION, INSIGHTS_RETRY_HOURS
from app.deps import get_current_user
from app.models.insight import MonthlyInsight
from app.models.user import User
from app.schemas.insight import Challenge, InsightsOut, QuickFacts, SmartTip
from app.services import insights as service
from app.services.auth import utcnow
from app.services.db import get_async_session
from app.services.gemini import GeminiError

router = APIRouter(prefix="/insights", tags=["insights"])


def _payload(
    row: MonthlyInsight, month: str, label: str, facts: QuickFacts, challenge: Challenge
) -> InsightsOut:
    """A saved tip, with today's facts around it."""
    return InsightsOut(
        month=month,
        month_label=label,
        generated_at=row.last_attempt_at,
        tip=SmartTip(
            text=row.tip_text,
            category=row.tip_category,
            action_label=row.action_label,
            source=row.source,
        ),
        facts=facts,
        challenge=challenge,
    )


async def _saved(
    session: AsyncSession, user_id: int, month: str
) -> MonthlyInsight | None:
    return await session.scalar(
        select(MonthlyInsight).where(
            MonthlyInsight.user_id == user_id, MonthlyInsight.month == month
        )
    )


@router.get("", response_model=InsightsOut)
async def get_insights(
    month: str | None = Query(
        default=None, description="YYYY-MM. Defaults to the current month."
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """This month's tip (generated once), the facts, and a small challenge."""
    month = month or service.current_month()
    try:
        start, end = service.month_bounds(month)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "month must be YYYY-MM, e.g. 2026-10."
        )

    facts = await service.facts_for(session, user.id, start, end)
    label = service.month_label(month)
    challenge = service.challenge_for(date_type.today())

    # Nothing saved yet: no AI call, and nothing cached — the month can still
    # fill up, and a tip written for zero bills would be wrong by tomorrow.
    if facts.bills == 0:
        return InsightsOut(
            month=month,
            month_label=label,
            generated_at=utcnow(),
            tip=service.local_tip(facts),
            facts=facts,
            challenge=challenge,
        )

    now = utcnow()
    row = await _saved(session, user.id, month)
    if row is not None:
        # A real tip is final for the month. A fallback gets another try once
        # the retry window has passed — a quota day on the 1st should not
        # decide the month — but not on every single open.
        retry_due = row.source != "ai" and (
            now - row.last_attempt_at >= timedelta(hours=INSIGHTS_RETRY_HOURS)
        )
        if not retry_due:
            return _payload(row, month, label, facts, challenge)

    try:
        draft = await run_in_threadpool(service.generate_tip, facts, label)
        tip = SmartTip(
            text=draft.headline,
            category=draft.category,
            action_label=draft.action_label,
            source="ai",
        )
    except GeminiError:
        # The numbers on the screen are ours; only the sentence is plainer.
        tip = service.local_tip(facts)

    if row is None:
        row = MonthlyInsight(user_id=user.id, month=month)
        session.add(row)
    row.tip_text = tip.text
    row.tip_category = tip.category
    row.action_label = tip.action_label
    row.source = tip.source
    row.prompt_version = INSIGHTS_PROMPT_VERSION
    row.last_attempt_at = now

    try:
        await session.commit()
    except IntegrityError:
        # Two first-opens of the month raced. The other request already saved
        # the tip; show that one instead of failing this request.
        await session.rollback()
        row = await _saved(session, user.id, month)
        if row is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not save this month's tip. Try again in a moment.",
            )
        return _payload(row, month, label, facts, challenge)

    return InsightsOut(
        month=month,
        month_label=label,
        generated_at=now,
        tip=tip,
        facts=facts,
        challenge=challenge,
    )
