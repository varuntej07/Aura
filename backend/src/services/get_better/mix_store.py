"""Firestore access for per-user weekly Get Better issues.

Every Firebase Admin SDK call is blocking, so each public function is an async
wrapper dispatching via ``asyncio.to_thread`` — matching ``briefing.briefing_store``.

The heart of this module is :func:`claim_week`: one transaction that atomically
claims a user's weekly slot before any generation is enqueued. That claim is what
makes "one issue per user per week" hold when the same user opens the screen
fifty times, and it doubles as the rate limiter — there is no separate throttle.

Reads degrade to a safe default and NEVER raise: a Firestore blip on the serving
path must fall back to the curated catalog, exactly as ``catalog.py:140-148``
does, rather than break a page that has perfectly good content to show.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from .activity_store import ACTIVITY_SUBCOLLECTION
from .models import OPEN_EVENTS, RESONANCE_EVENTS, RESONANCE_RETRACTIONS
from .mix_models import (
    MIX_STATUS_FAILED,
    MIX_STATUS_GENERATING,
    MIX_STATUS_READY,
    MIX_STATUS_SKIPPED,
    GetBetterMix,
)

MIX_SUBCOLLECTION = "get_better_mix"
SAVED_SUBCOLLECTION = "get_better_saved"
# ACTIVITY_SUBCOLLECTION is imported from activity_store (its writer) rather than
# re-declared here, so the reader and writer of that collection cannot drift.

FIELD_STATUS = "status"
FIELD_WEEK_START = "week_start"
FIELD_CREATED_AT = "created_at"
FIELD_UPDATED_AT = "updated_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_SKIP_REASON = "skip_reason"
FIELD_NUDGE_SENT = "nudge_sent"

# A `generating` claim older than this may be re-claimed, so a crash mid-flight
# never permanently burns a user's week. Deliberately longer than the briefing's
# 30 minutes: that is one short call, this waits on a batch job that legitimately
# runs for hours. Too short and a second worker re-claims a live generation and
# the issue is paid for twice.
STALE_CLAIM_MINUTES = 60

# How long a ready issue keeps serving past its own week. One grace week means a
# single failed generation does not drop the user back to curated, while a
# month-old issue never lingers.
SERVE_GRACE_DAYS = 7

# Operational records, kept long enough to investigate a bad week.
MIX_TTL_DAYS = 60


@dataclass
class ClaimResult:
    claimed: bool
    # "claimed" | "in_progress" | "already_generated" | "error"
    reason: str


def _coerce_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _mix_ref(user_id: str, week_start: str) -> Any:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(MIX_SUBCOLLECTION)
        .document(week_start)
    )


def evaluate_claim(
    data: dict, *, week_start: str, now: datetime
) -> tuple[dict, ClaimResult]:
    """Pure claim decision — no I/O, so it is inspectable directly.

    Mirrors ``briefing_store.evaluate_claim`` and ``icebreaker_store.evaluate_claim``:
    feed the returned update back in and a SECOND call returns ``in_progress``,
    which is exactly what a concurrent request sees, so it stands down.
    """
    status = data.get(FIELD_STATUS) if data else None

    # No doc yet, or a prior attempt failed: claim it. `skipped` is deliberately
    # NOT re-claimable within the same week — a user whose profile was too thin
    # on Monday should not re-run generation on every visit until Sunday.
    if not status or status == MIX_STATUS_FAILED:
        return (
            {
                FIELD_STATUS: MIX_STATUS_GENERATING,
                FIELD_WEEK_START: week_start,
                FIELD_CREATED_AT: now,
                FIELD_UPDATED_AT: now,
                FIELD_EXPIRES_AT: now + timedelta(days=MIX_TTL_DAYS),
            },
            ClaimResult(True, "claimed"),
        )

    if status == MIX_STATUS_GENERATING:
        created = _coerce_utc(data.get(FIELD_CREATED_AT))
        if created is not None and now - created > timedelta(minutes=STALE_CLAIM_MINUTES):
            return (
                {
                    FIELD_STATUS: MIX_STATUS_GENERATING,
                    FIELD_WEEK_START: week_start,
                    FIELD_CREATED_AT: now,
                    FIELD_UPDATED_AT: now,
                },
                ClaimResult(True, "claimed"),
            )
        return {}, ClaimResult(False, "in_progress")

    # ready / skipped: this week is handled.
    return {}, ClaimResult(False, "already_generated")


async def claim_week(user_id: str, *, week_start: str) -> ClaimResult:
    """Atomically claim this user's weekly generation slot.

    Fails CLOSED on any error — a store failure must never double-generate, which
    on this path means paying twice for a magazine.
    """
    now = datetime.now(UTC)

    def _txn() -> ClaimResult:
        ref = _mix_ref(user_id, week_start)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> ClaimResult:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            update, result = evaluate_claim(data, week_start=week_start, now=now)
            if update:
                txn.set(ref, update, merge=True)
            return result

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("get_better.mix_store: claim_week failed (failing closed)", {
            "user_id": user_id,
            "week_start": week_start,
            "error": str(exc),
        })
        return ClaimResult(False, "error")


def is_servable(data: dict, *, today: date) -> bool:
    """Pure: is this stored issue still worth serving?

    Ready, and within its own week plus one grace week.
    """
    if not data or data.get(FIELD_STATUS) != MIX_STATUS_READY:
        return False
    raw_week = str(data.get(FIELD_WEEK_START) or "")
    try:
        week_start = date.fromisoformat(raw_week)
    except ValueError:
        return False
    return (today - week_start).days <= (SERVE_GRACE_DAYS + 7)


async def read_servable_mix(user_id: str, *, today: date | None = None) -> GetBetterMix | None:
    """The issue to serve this user, or None to fall back to the curated catalog.

    NEVER raises. Any failure — read error, schema drift, a doc written by an
    older shape — returns None, and the caller serves curated. That is the same
    contract ``catalog.py:140-148`` keeps, and it is what makes every failure row
    in this feature's matrix land somewhere the user can still read something.
    """
    today = today or datetime.now(UTC).date()

    def _read() -> GetBetterMix | None:
        # Read several recent issues, not just the newest, and serve the newest
        # SERVABLE one.
        #
        # Reading only the latest doc looks right and is wrong in the most common
        # case there is: the moment this week's claim writes a `generating` doc it
        # becomes the newest, so a `limit(1)` read sees a non-servable doc and
        # falls back to curated — shadowing last week's perfectly readable issue
        # for as long as generation takes (up to 24h on batch). That would happen
        # to every engaged user every week and the grace window would never once
        # apply.
        query = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection(MIX_SUBCOLLECTION)
            .order_by("__name__", direction=fs.Query.DESCENDING)
            .limit(3)
        )
        for doc in query.stream():
            data = doc.to_dict() or {}
            if is_servable(data, today=today):
                return GetBetterMix.model_validate(data)
        return None

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("get_better.mix_store: read_servable_mix failed (serving curated)", {
            "user_id": user_id,
            "error": str(exc),
        })
        return None


async def write_mix(user_id: str, mix: GetBetterMix) -> bool:
    """Persist a finished issue and mark it ready.

    Written BEFORE any notification is proposed, so the issue is viewable in-app
    even when the push is held, dropped or never sent — the same ordering the
    briefing uses deliberately (`briefing_engine.py:190-199`).
    """
    def _write() -> bool:
        now = datetime.now(UTC)
        payload = mix.to_firestore()
        payload[FIELD_STATUS] = MIX_STATUS_READY
        payload[FIELD_UPDATED_AT] = now
        payload[FIELD_EXPIRES_AT] = now + timedelta(days=MIX_TTL_DAYS)
        payload.setdefault(FIELD_NUDGE_SENT, False)
        _mix_ref(user_id, mix.week_start).set(payload, merge=True)
        return True

    try:
        ok = await asyncio.to_thread(_write)
        logger.info("get_better.mix_store: issue written", {
            "user_id": user_id,
            "week_start": mix.week_start,
            "stories": len(mix.stories),
            "generated": len(mix.generated_stories()),
        })
        return ok
    except Exception as exc:
        logger.error("get_better.mix_store: write_mix failed", {
            "user_id": user_id,
            "week_start": mix.week_start,
            "error": str(exc),
        })
        return False


async def mark_terminal(
    user_id: str, *, week_start: str, status: str, reason: str = ""
) -> None:
    """Record a non-ready outcome.

    ``failed`` is re-claimable by a later visit; ``skipped`` is not, because a
    thin profile will still be thin an hour later and re-running generation on
    every visit would spend money to reach the same conclusion.
    """
    if status not in (MIX_STATUS_FAILED, MIX_STATUS_SKIPPED):
        raise ValueError(f"mark_terminal expects failed/skipped, got {status!r}")

    def _write() -> None:
        _mix_ref(user_id, week_start).set(
            {
                FIELD_STATUS: status,
                FIELD_WEEK_START: week_start,
                FIELD_SKIP_REASON: reason[:200] or None,
                FIELD_UPDATED_AT: datetime.now(UTC),
            },
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
        logger.info("get_better.mix_store: issue marked terminal", {
            "user_id": user_id,
            "week_start": week_start,
            "status": status,
            "reason": reason,
        })
    except Exception as exc:
        logger.error("get_better.mix_store: mark_terminal failed", {
            "user_id": user_id,
            "week_start": week_start,
            "status": status,
            "error": str(exc),
        })


@dataclass
class AvoidList:
    """What NOT to write again this week."""

    recent_titles: list[str]
    resonated_ids: list[str]
    ignored_ids: list[str]

    @property
    def is_empty(self) -> bool:
        return not (self.recent_titles or self.resonated_ids or self.ignored_ids)


async def read_avoid_list(user_id: str, *, weeks: int = 3) -> AvoidList:
    """Titles from recent issues plus engagement signal from the activity log.

    This is the FIRST read of ``get_better_activity`` anywhere in the codebase —
    it has been write-only since it shipped. The counts are logged deliberately:
    an avoid-list that is silently always empty makes week 4 identical to week 1
    with no visible symptom (CLAUDE.md:84-85).
    """

    def _read() -> AvoidList:
        user_ref = admin_firestore().collection("users").document(user_id)

        # Doc ids are ISO dates, so lexical order is chronological — no index.
        mix_docs = list(
            user_ref.collection(MIX_SUBCOLLECTION)
            .order_by("__name__", direction=fs.Query.DESCENDING)
            .limit(max(1, weeks))
            .stream()
        )
        titles: list[str] = []
        served_ids: set[str] = set()
        for doc in mix_docs:
            for story in (doc.to_dict() or {}).get("stories") or []:
                if not isinstance(story, dict):
                    continue
                title = str(story.get("title") or "").strip()
                if title:
                    titles.append(title)
                if story.get("personalized") and story.get("id"):
                    served_ids.add(str(story["id"]))

        # Classified through the sets that live beside the event Literal, never a
        # hand-copied tuple — see `models.RESONANCE_EVENTS`.
        resonated: set[str] = set()
        retracted: set[str] = set()
        opened: set[str] = set()
        batches = list(
            user_ref.collection(ACTIVITY_SUBCOLLECTION)
            .order_by(FIELD_CREATED_AT, direction=fs.Query.DESCENDING)
            .limit(30)
            .stream()
        )
        for batch in batches:
            for event in (batch.to_dict() or {}).get("events") or []:
                if not isinstance(event, dict):
                    continue
                story_id = str(event.get("story_id") or "")
                if not story_id:
                    continue
                event_type = str(event.get("event_type") or "")
                if event_type in RESONANCE_EVENTS:
                    resonated.add(story_id)
                elif event_type in RESONANCE_RETRACTIONS:
                    # The log is append-only, so an unsave does not remove the
                    # save — it has to cancel it, or a retracted signal keeps
                    # steering next week's issue.
                    retracted.add(story_id)
                if event_type in OPEN_EVENTS:
                    opened.add(story_id)
        resonated -= retracted

        return AvoidList(
            recent_titles=titles[:60],
            resonated_ids=sorted(resonated)[:30],
            ignored_ids=sorted(served_ids - opened)[:30],
        )

    try:
        result = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.error("get_better.mix_store: read_avoid_list failed (week 4 may repeat week 1)", {
            "user_id": user_id,
            "error": str(exc),
        })
        return AvoidList([], [], [])

    log = logger.warn if result.is_empty else logger.info
    log("get_better.mix_store: avoid list read", {
        "user_id": user_id,
        "recent_titles": len(result.recent_titles),
        "resonated": len(result.resonated_ids),
        "ignored": len(result.ignored_ids),
        "empty": result.is_empty,
    })
    return result
