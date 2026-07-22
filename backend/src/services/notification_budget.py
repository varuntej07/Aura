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

from google.cloud import firestore as fs

from ..lib.logger import logger
from .firebase import admin_auth, admin_firestore
from .timezone_utils import TimezoneResolutionError, localize

# ── Firestore layout: users/{uid}/notification_budget/state ─────────────────
BUDGET_SUBCOLLECTION = "notification_budget"
BUDGET_DOC_ID = "state"

FIELD_PROACTIVE_SENDS_TODAY = "proactive_sends_today"
FIELD_COMMITTED_SENDS_TODAY = "committed_sends_today"
FIELD_SENDS_TODAY_DATE = "sends_today_date"        # "YYYY-MM-DD" in user-local tz
FIELD_LAST_NOTIFICATION_AT = "last_notification_at"  # any send (proactive or committed)

# ── Tuning ──────────────────────────────────────────────────────────────────
# Hard, self-documenting ceiling on ALL proactive sends/day, applied uniformly (see
# try_claim_proactive_slot) after cap resolution regardless of which path produced
# the raw value — adaptive tier, new-account override, or the fail-open default
# below. Beta dogfooding of raw, ungated send volume is over; this restores the
# "CAP + the icebreaker's reserved slot = 4/day" design intent below.
HARD_DAILY_PROACTIVE_CEILING = 3

# Fail-open fallback ONLY: used inside try_claim_proactive_slot if the adaptive-
# engagement Firestore/ledger read fails before a real per-user cap/spacing can be
# resolved. Kept equal to the hard ceiling (not an "effectively unlimited" beta
# placeholder) so an infra hiccup can no longer widen the gate past the intended
# daily ceiling.
GLOBAL_DAILY_PROACTIVE_CAP = HARD_DAILY_PROACTIVE_CEILING
MIN_PROACTIVE_SPACING = timedelta(hours=2)

# Reserved headroom above the cap for a PRIORITY decider (the icebreaker). The
# content engine runs every 15 min and could otherwise consume the whole daily
# cap before the icebreaker's once-a-day window opens, starving the higher-value
# personal opener. A priority claim is allowed up to CAP + this headroom, so the
# icebreaker always has a slot even on a busy content day. Spacing still applies.
# CAP (HARD_DAILY_PROACTIVE_CEILING) + this (1) = a hard 4 proactive
# notifications/day, excluding reminders.
PRIORITY_RESERVED_SLOTS = 1

# ── Adaptive per-user volume ────────────────────────────────────────────────
# The goal is "make every notification earn its tap": a user who taps earns a
# higher daily ceiling + tighter spacing; one who ignores is throttled down so
# Buddy stops nagging. This REPLACES the flat beta ceiling above (which stays as
# the fail-open default), turning volume into a per-user function of engagement
# instead of one global wall — the "adaptive per-user" product decision.
#
# Engagement e = opened / delivered over a trailing window. Tiers (cap = max
# proactive sends/user/local-day; spacing = min gap between two proactive sends):
ADAPTIVE_ENGAGEMENT_WINDOW = timedelta(days=14)
# Below this many delivered notifications we don't have enough signal to judge, so
# a new / quiet user starts on the gentle default tier (never the harsh floor).
ADAPTIVE_MIN_SAMPLE = 5
# (engagement upper-bound exclusive, daily cap, spacing)
# NOTE: the top tier's raw cap (5) is clamped down to HARD_DAILY_PROACTIVE_CEILING
# by try_claim_proactive_slot before use — a highly-engaged user's real ceiling is
# still HARD_DAILY_PROACTIVE_CEILING + PRIORITY_RESERVED_SLOTS, not 5 + 1.
_ADAPTIVE_TIERS: tuple[tuple[float, int, timedelta], ...] = (
    (0.0001, 1, timedelta(hours=6)),    # ignores everything → one gentle ping/day, far apart
    (0.15,   2, timedelta(hours=4)),    # rarely taps
    (0.40,   3, timedelta(hours=2)),    # sometimes taps (balanced middle)
    (1.01,   5, timedelta(minutes=45)), # taps a lot → lean in (clamped to the hard ceiling)
)
# New / insufficient-history default: gentle but present.
_ADAPTIVE_DEFAULT_CAP = 3
_ADAPTIVE_DEFAULT_SPACING = timedelta(minutes=90)


def resolve_adaptive_limits(delivered: int, opened: int) -> tuple[int, timedelta]:
    """Map a user's recent engagement to ``(daily_cap, min_spacing)``. Pure +
    unit-tested. Under ``ADAPTIVE_MIN_SAMPLE`` deliveries → the gentle default
    (we can't yet tell a non-engager from a brand-new user)."""
    if delivered < ADAPTIVE_MIN_SAMPLE:
        return _ADAPTIVE_DEFAULT_CAP, _ADAPTIVE_DEFAULT_SPACING
    engagement = opened / delivered if delivered else 0.0
    for upper, cap, spacing in _ADAPTIVE_TIERS:
        if engagement < upper:
            return cap, spacing
    return _ADAPTIVE_DEFAULT_CAP, _ADAPTIVE_DEFAULT_SPACING


# ── New-account ramp ─────────────────────────────────────────────────────────
# The adaptive tiers above key off DELIVERED HISTORY, not account age: a brand
# new signup and a 2-year-old account that ignores everything look identical
# (both under ADAPTIVE_MIN_SAMPLE) and land on the same "gentle default" (3/day,
# 90min). That default is still a beta_producer's worth of unscripted content on
# day 0. A genuinely new account gets a deliberately lower, flat ceiling for its
# first week — no engagement signal exists yet to personalize on, so don't guess.
NEW_ACCOUNT_WINDOW_DAYS = 7
_NEW_ACCOUNT_CAP = 1
_NEW_ACCOUNT_SPACING = timedelta(hours=4)


def resolve_effective_limits(
    delivered: int, opened: int, account_age_days: int | None
) -> tuple[int, timedelta]:
    """``resolve_adaptive_limits`` plus an account-age override.

    ``account_age_days is None`` means the caller couldn't determine account age
    (lookup failure) — falls through to the pre-existing engagement-only
    resolution unchanged, so a lookup error never tightens or loosens behavior
    beyond what already shipped."""
    if account_age_days is not None and account_age_days < NEW_ACCOUNT_WINDOW_DAYS:
        return _NEW_ACCOUNT_CAP, _NEW_ACCOUNT_SPACING
    return resolve_adaptive_limits(delivered, opened)


# In-process cache: account creation time never changes, so once resolved for a
# user it is cached for the lifetime of this instance (mirrors the pattern in
# fcm_token_registry._active_users_cache). Avoids an Admin SDK auth lookup on
# every proactive claim attempt for the common case (account older than the new-
# account window) once it has been checked once.
_account_created_cache: dict[str, datetime] = {}


def _get_account_created_at(user_id: str) -> datetime | None:
    cached = _account_created_cache.get(user_id)
    if cached is not None:
        return cached
    try:
        user_record = admin_auth().get_user(user_id)
        creation_ms = user_record.user_metadata.creation_timestamp
        if creation_ms is None:
            return None
        created_at = datetime.fromtimestamp(creation_ms / 1000, tz=UTC)
    except Exception as exc:
        logger.warn("notification_budget: account creation lookup failed", {
            "user_id": user_id, "error": str(exc),
        })
        return None
    _account_created_cache[user_id] = created_at
    return created_at


def resolve_account_age_days(user_id: str, now: datetime) -> int | None:
    """Days since signup, or ``None`` on lookup failure (never blocks a send)."""
    created_at = _get_account_created_at(user_id)
    if created_at is None:
        return None
    return (_aware(now) - created_at).days


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
    data: dict,
    *,
    local_date: str,
    now: datetime,
    priority: bool = False,
    cap: int = GLOBAL_DAILY_PROACTIVE_CAP,
    spacing: timedelta = MIN_PROACTIVE_SPACING,
) -> BudgetDecision:
    """Pure decision: given the current budget doc, may a proactive slot be taken?

    No I/O — the transaction below applies it. Unit-tested directly. ``cap`` and
    ``spacing`` are the user's ADAPTIVE limits (resolved from engagement by the
    caller); they default to the flat beta constants so any caller that doesn't pass
    them — and the existing unit tests — behave exactly as before. A ``priority``
    claim (the icebreaker) may use one reserved slot above the cap so it is never
    starved by the every-15-min content engine; spacing still applies to it.
    """
    same_day = data.get(FIELD_SENDS_TODAY_DATE) == local_date
    count = int(data.get(FIELD_PROACTIVE_SENDS_TODAY, 0) or 0) if same_day else 0

    effective_cap = cap + (PRIORITY_RESERVED_SLOTS if priority else 0)
    if count >= effective_cap:
        return BudgetDecision(False, "global_daily_cap")

    last = data.get(FIELD_LAST_NOTIFICATION_AT)
    if isinstance(last, datetime) and (now - _aware(last)) < spacing:
        return BudgetDecision(False, "global_spacing")

    return BudgetDecision(True)


def resolve_user_local_date(user_id: str, now: datetime | None = None) -> str:
    """The user's current local calendar date as 'YYYY-MM-DD'.

    Matches how the signal engine derives its daily-reset key, so proactive and
    committed writes to the shared budget doc always agree on which day it is.
    Falls back to UTC on any timezone failure.
    """
    now = now or datetime.now(UTC)

    def _fetch_tz() -> str | None:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone")  # None when the field is absent
        return None

    try:
        tz_name = _fetch_tz()
        if not tz_name:
            logger.warn("notification_budget: user has no timezone, day boundary uses UTC", {
                "user_id": user_id,
            })
            return now.astimezone(UTC).date().isoformat()
        return localize(now, tz_name).date().isoformat()
    except (TimezoneResolutionError, Exception) as exc:
        logger.warn("notification_budget: timezone resolve failed, using UTC", {
            "user_id": user_id, "error": str(exc),
        })
        return now.astimezone(UTC).date().isoformat()


async def _resolve_effective_claim_limits(
    user_id: str, source: str, now: datetime
) -> tuple[int, timedelta]:
    """Resolve this user's ADAPTIVE limits from recent engagement, then clamp to
    HARD_DAILY_PROACTIVE_CEILING so the hard 4/day ceiling holds regardless of which
    resolution path produced the raw cap (adaptive tier, new-account override, or
    the fail-open default). Fail-open to the flat beta defaults so a read error
    widens the gate, never closes it — the clamp still applies to that fallback."""
    cap, spacing = GLOBAL_DAILY_PROACTIVE_CAP, MIN_PROACTIVE_SPACING
    try:
        from . import notification_ledger

        delivered, opened = await notification_ledger.recent_engagement(
            user_id, since=now - ADAPTIVE_ENGAGEMENT_WINDOW
        )
        account_age_days = await asyncio.to_thread(resolve_account_age_days, user_id, now)
        cap, spacing = resolve_effective_limits(delivered, opened, account_age_days)
    except Exception as exc:
        logger.warn("notification_budget: adaptive resolve failed, using defaults", {
            "user_id": user_id, "source": source, "error": str(exc),
        })
    return min(cap, HARD_DAILY_PROACTIVE_CEILING), spacing


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

    # The ledger read is I/O and must not run inside the transaction below.
    cap, spacing = await _resolve_effective_claim_limits(user_id, source, now)

    def _claim() -> BudgetDecision:
        local_date = user_local_date or resolve_user_local_date(user_id, now)
        ref = _budget_ref(user_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> BudgetDecision:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            decision = evaluate_proactive_claim(
                data, local_date=local_date, now=now, priority=priority,
                cap=cap, spacing=spacing,
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
