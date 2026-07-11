"""Per-user/day LLM-call ceiling — a runaway guard, not a rationing knob.

The reactive loop can, in a pathological cycle (a misfiring trigger, a repair storm),
call the LLM far more than a real user warrants. This caps LLM-bearing orchestrate
work per user per UTC day. It is deliberately GENEROUS (a real tester never reaches
it) and FAILS OPEN: a read error never blocks a genuine notification. It is a
circuit-breaker against cost blow-ups, consistent with the project's "generous caps,
no feature flags" stance.

Counter doc: ``users/{uid}/cost/{YYYY-MM-DD}.llm_calls`` (atomic Increment).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore
from .fields import COST_SUBCOLLECTION, FIELD_LLM_CALLS, USERS_COLLECTION

# Generous: ~one LLM-bearing orchestrate pass every few minutes, all day, before the
# breaker trips. A real user's reactive load is a tiny fraction of this.
DAILY_LLM_CALL_CAP = 100


def _cost_ref(uid: str, day: str):
    return (
        admin_firestore()
        .collection(USERS_COLLECTION)
        .document(uid)
        .collection(COST_SUBCOLLECTION)
        .document(day)
    )


def _day(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


async def within_daily_budget(uid: str, *, now: datetime | None = None) -> bool:
    """True if the user is under today's LLM-call ceiling. Fails OPEN (True) on a
    read error — never let the cost guard silence a real notification."""
    when = now or datetime.now(UTC)

    def _read() -> int:
        snap = _cost_ref(uid, _day(when)).get()
        if not snap.exists:
            return 0
        return int((snap.to_dict() or {}).get(FIELD_LLM_CALLS, 0) or 0)

    try:
        used = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("cost_cap.within_daily_budget read failed (fail-open)", {
            "user_id": uid, "error": str(exc),
        })
        return True

    if used >= DAILY_LLM_CALL_CAP:
        logger.warn("cost_cap: daily LLM ceiling reached, standing down LLM work", {
            "user_id": uid, "used": used, "cap": DAILY_LLM_CALL_CAP,
        })
        return False
    return True


async def record_llm_call(uid: str, *, n: int = 1, now: datetime | None = None) -> None:
    """Increment today's LLM-call counter. Best-effort; a missed increment only
    loosens the cap, never tightens it (so it can never wrongly block a send)."""
    if n <= 0:
        return
    when = now or datetime.now(UTC)

    def _bump() -> None:
        _cost_ref(uid, _day(when)).set(
            {FIELD_LLM_CALLS: fs.Increment(n), "updated_at": when},
            merge=True,
        )

    try:
        await asyncio.to_thread(_bump)
    except Exception as exc:
        logger.warn("cost_cap.record_llm_call failed", {"user_id": uid, "error": str(exc)})
