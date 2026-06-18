"""Unified per-user notification budget.

One daily allowance + spacing that every PROACTIVE decider draws from, so the
signal engine, curiosity threads, and engagement nudges can no longer each spam
a user from their own independent cap. Committed sends the user asked for
(reminders, calendar meeting reminders) are never blocked, but they are recorded
so a proactive push is spaced away from them.

SAFETY: the claim is ADDITIVE — each decider keeps its own per-source cap as a
sub-limit; this only adds one coordinated ceiling on top. It also FAILS OPEN: any
Firestore error allows the send, because a budget read failure must never become
a notification outage.

Field names live here (single source of truth) and are round-tripped in
``backend/tests/test_notification_budget.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.cloud import firestore as fs

from ..lib.logger import logger
from .firebase import admin_firestore

# ── Firestore layout: users/{uid}/notification_budget/state ─────────────────
BUDGET_SUBCOLLECTION = "notification_budget"
BUDGET_DOC_ID = "state"

FIELD_PROACTIVE_SENDS_TODAY = "proactive_sends_today"
FIELD_COMMITTED_SENDS_TODAY = "committed_sends_today"
FIELD_SENDS_TODAY_DATE = "sends_today_date"        # "YYYY-MM-DD" in user-local tz
FIELD_LAST_NOTIFICATION_AT = "last_notification_at"  # any send (proactive or committed)

# ── Tuning ──────────────────────────────────────────────────────────────────
# Proactive notifications are intentionally UNCAPPED during beta: Varun is dogfooding
# on his own phone and wants to see the raw, ungated send volume before deciding what
# (if any) ceiling to impose. These are plain tuning constants, not a feature flag —
# to re-impose a real ceiling later, set GLOBAL_DAILY_PROACTIVE_CAP back to a small
# number (e.g. 3) and MIN_PROACTIVE_SPACING back to a real window (e.g. 2h). The
# claim/spacing machinery below is left in place so that is a one-line change.
GLOBAL_DAILY_PROACTIVE_CAP = 100   # effectively unlimited
MIN_PROACTIVE_SPACING = timedelta(0)  # no spacing between proactive sends

# Reserved headroom above the cap for a PRIORITY decider (the icebreaker). The
# content engine runs every 15 min and could otherwise consume the whole daily
# cap before the icebreaker's once-a-day window opens, starving the higher-value
# personal opener. A priority claim is allowed up to CAP + this headroom, so the
# icebreaker always has a slot even on a busy content day. Spacing still applies.
# CAP (3) + this (1) = a hard 4 proactive notifications/day, excluding reminders.
PRIORITY_RESERVED_SLOTS = 1


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str | None = None


def _budget_ref(user_id: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(BUDGET_SUBCOLLECTION)
        .document(BUDGET_DOC_ID)
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def evaluate_proactive_claim(
    data: dict, *, local_date: str, now: datetime, priority: bool = False
) -> BudgetDecision:
    """Pure decision: given the current budget doc, may a proactive slot be taken?

    No I/O — the transaction below applies it. Unit-tested directly. A ``priority``
    claim (the icebreaker) may use one reserved slot above the shared cap so it is
    never starved by the every-15-min content engine; spacing still applies to it.
    """
    same_day = data.get(FIELD_SENDS_TODAY_DATE) == local_date
    count = int(data.get(FIELD_PROACTIVE_SENDS_TODAY, 0) or 0) if same_day else 0

    effective_cap = GLOBAL_DAILY_PROACTIVE_CAP + (PRIORITY_RESERVED_SLOTS if priority else 0)
    if count >= effective_cap:
        return BudgetDecision(False, "global_daily_cap")

    last = data.get(FIELD_LAST_NOTIFICATION_AT)
    if isinstance(last, datetime) and (now - _aware(last)) < MIN_PROACTIVE_SPACING:
        return BudgetDecision(False, "global_spacing")

    return BudgetDecision(True)


def resolve_user_local_date(user_id: str, now: datetime | None = None) -> str:
    """The user's current local calendar date as 'YYYY-MM-DD'.

    Matches how the signal engine derives its daily-reset key, so proactive and
    committed writes to the shared budget doc always agree on which day it is.
    Falls back to UTC on any timezone failure.
    """
    now = now or datetime.now(UTC)

    def _fetch_tz() -> str:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"

    try:
        tz_name = _fetch_tz()
        return now.astimezone(ZoneInfo(tz_name)).date().isoformat()
    except (ZoneInfoNotFoundError, Exception):
        return now.astimezone(UTC).date().isoformat()


async def try_claim_proactive_slot(
    user_id: str,
    *,
    source: str,
    user_local_date: str | None = None,
    now: datetime | None = None,
    priority: bool = False,
) -> BudgetDecision:
    """Atomically claim one slot of the shared daily proactive budget.

    Returns ``allowed=False`` with a reason when the daily cap is reached or the
    last send was within the spacing window. Fails OPEN on any error.
    ``priority=True`` (the icebreaker) may use one reserved slot above the cap so
    it is never starved.
    """
    now = _aware(now or datetime.now(UTC))

    def _claim() -> BudgetDecision:
        local_date = user_local_date or resolve_user_local_date(user_id, now)
        ref = _budget_ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> BudgetDecision:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            decision = evaluate_proactive_claim(
                data, local_date=local_date, now=now, priority=priority
            )
            if not decision.allowed:
                return decision

            same_day = data.get(FIELD_SENDS_TODAY_DATE) == local_date
            count = int(data.get(FIELD_PROACTIVE_SENDS_TODAY, 0) or 0) if same_day else 0
            txn.set(ref, {
                FIELD_PROACTIVE_SENDS_TODAY: count + 1,
                FIELD_SENDS_TODAY_DATE: local_date,
                FIELD_LAST_NOTIFICATION_AT: now,
            }, merge=True)
            return decision

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_claim)
    except Exception as exc:
        # Fail OPEN: a budget read failure must never silence all notifications.
        logger.warn("notification_budget: claim failed, allowing send (fail-open)", {
            "user_id": user_id,
            "source": source,
            "error": str(exc),
        })
        return BudgetDecision(True)


async def record_committed_send(
    user_id: str,
    *,
    source: str,
    user_local_date: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record a committed send (user reminder / calendar reminder).

    Never blocks — the user asked for these. Bumps a committed counter and sets
    ``last_notification_at`` so a later proactive push is spaced away from it.
    Swallows all errors.
    """
    now = _aware(now or datetime.now(UTC))

    def _record() -> None:
        local_date = user_local_date or resolve_user_local_date(user_id, now)
        ref = _budget_ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            same_day = data.get(FIELD_SENDS_TODAY_DATE) == local_date
            committed = int(data.get(FIELD_COMMITTED_SENDS_TODAY, 0) or 0) if same_day else 0
            update: dict = {
                FIELD_COMMITTED_SENDS_TODAY: committed + 1,
                FIELD_SENDS_TODAY_DATE: local_date,
                FIELD_LAST_NOTIFICATION_AT: now,
            }
            # Preserve the proactive count on a same-day doc; reset it on a new day.
            if not same_day:
                update[FIELD_PROACTIVE_SENDS_TODAY] = 0
            txn.set(ref, update, merge=True)

        _apply(transaction)

    try:
        await asyncio.to_thread(_record)
    except Exception as exc:
        logger.warn("notification_budget: committed-send record failed", {
            "user_id": user_id,
            "source": source,
            "error": str(exc),
        })
