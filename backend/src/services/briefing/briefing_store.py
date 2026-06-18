"""Firestore access layer for the Daily Briefing engine.

All Firebase Admin SDK calls are blocking, so every public function is an async
wrapper dispatching the sync work via ``asyncio.to_thread`` (matching
``icebreaker.icebreaker_store`` / ``signal_engine.feature_store``). Reads degrade
to a safe default on failure so a Firestore blip can never crash a scheduler tick.

The heart of this module is ``claim_today``: a single Firestore transaction that
atomically claims today's single briefing slot by creating
``users/{uid}/daily_briefing/{local_date}`` with status ``generating``. Claiming
inside the transaction — BEFORE the LLM call — is what makes "at most one briefing
per user per local date" hold even when two scheduler ticks run concurrently: the
loser reads the doc already present and stands down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as f

# A `generating` claim older than this is treated as stale (the claiming tick most
# likely crashed before writing a terminal status) and may be re-claimed, so a hard
# crash never burns the day permanently. Comfortably longer than the ~10s LLM call.
STALE_CLAIM_MINUTES = 30


@dataclass
class BriefingTargeting:
    """The slice of ``users/{uid}`` the engine needs, fetched in one read."""

    consent_granted: bool = False
    timezone: str = "UTC"
    locale: str = ""
    language: str = "English"
    gender: str | None = None
    display_name: str | None = None


@dataclass
class ClaimResult:
    """Outcome of ``claim_today``."""

    claimed: bool
    # "claimed" | "in_progress" | "already_generated" | "error"
    reason: str


@dataclass
class StoredBriefing:
    """A briefing document read back for the endpoint."""

    local_date: str
    status: str
    narrative: str
    chat_seed_message: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)


def _coerce_utc(value: Any) -> datetime | None:
    """Best-effort coerce a Firestore timestamp to a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def evaluate_claim(
    data: dict,
    *,
    local_date: str,
    now: datetime,
) -> tuple[dict, ClaimResult]:
    """Pure claim decision: given the current ``daily_briefing/{local_date}`` doc,
    what to write and whether today's slot is claimed. No I/O — the transaction
    below applies the update.

    This is the idempotency core, unit-tested directly: feed back the doc with the
    returned update applied and a SECOND call returns ``in_progress`` — exactly what
    a second overlapping tick sees, so it stands down. Mirrors
    ``icebreaker_store.evaluate_claim``.
    """
    status = data.get(f.FIELD_STATUS) if data else None

    # No doc yet, or a prior attempt failed: claim it.
    if not status or status == f.STATUS_FAILED:
        update = {
            f.FIELD_STATUS: f.STATUS_GENERATING,
            f.FIELD_LOCAL_DATE: local_date,
            f.FIELD_CREATED_AT: now,
        }
        return update, ClaimResult(True, "claimed")

    # A stale `generating` claim (claimer crashed mid-flight) may be re-claimed so a
    # hard crash never permanently burns the day.
    if status == f.STATUS_GENERATING:
        created = _coerce_utc(data.get(f.FIELD_CREATED_AT))
        if created is not None and now - created > timedelta(minutes=STALE_CLAIM_MINUTES):
            update = {
                f.FIELD_STATUS: f.STATUS_GENERATING,
                f.FIELD_LOCAL_DATE: local_date,
                f.FIELD_CREATED_AT: now,
            }
            return update, ClaimResult(True, "claimed")
        return {}, ClaimResult(False, "in_progress")

    # ready / skipped (or any unknown terminal state): already handled today.
    return {}, ClaimResult(False, "already_generated")


def _briefing_ref(user_id: str, local_date: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(f.DAILY_BRIEFING_SUBCOLLECTION)
        .document(local_date)
    )


async def read_user_targeting(user_id: str) -> BriefingTargeting:
    """Read consent + timezone + locale + language + gender + display_name in one get.

    Returns a consent-denied default on any error, so a read failure fails CLOSED
    for the briefing (no generation) — the same way the icebreaker fails closed,
    because generating a behavioural digest without a confirmed consent read would
    be the wrong way to fail.
    """

    def _fetch() -> BriefingTargeting:
        snap = admin_firestore().collection("users").document(user_id).get()
        if not snap.exists:
            return BriefingTargeting()
        data = snap.to_dict() or {}
        raw_name = str(data.get("display_name", "") or "").strip()
        return BriefingTargeting(
            consent_granted=data.get("aura_consent_granted", False) is True,
            timezone=str(data.get("timezone", "UTC") or "UTC"),
            locale=str(data.get("locale", "") or ""),
            language=str(data.get("language", "English") or "English"),
            gender=data.get("gender"),
            display_name=raw_name if raw_name and raw_name != "User" else None,
        )

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("briefing.store: read_user_targeting failed (failing closed)", {
            "user_id": user_id,
            "error": str(exc),
        })
        return BriefingTargeting()


async def claim_today(user_id: str, *, local_date: str) -> ClaimResult:
    """Atomically claim today's single briefing slot.

    Creating the ``daily_briefing/{local_date}`` doc with status ``generating``
    inside the transaction is the lock that makes "one briefing per user per local
    date" idempotent under overlapping ticks. Fails CLOSED (``claimed=False``) on
    any error — a store failure must never double-generate.
    """

    now = datetime.now(UTC)

    def _txn() -> ClaimResult:
        ref = _briefing_ref(user_id, local_date)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> ClaimResult:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            update, result = evaluate_claim(data, local_date=local_date, now=now)
            if update:
                txn.set(ref, update, merge=True)
            return result

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("briefing.store: claim_today failed (failing closed)", {
            "user_id": user_id,
            "local_date": local_date,
            "error": str(exc),
        })
        return ClaimResult(False, "error")


async def write_briefing(
    user_id: str,
    *,
    local_date: str,
    narrative: str,
    chat_seed_message: str,
    sources: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> None:
    """Flip the claimed doc to ``ready`` and store the generated content (merge).

    Best-effort: a failure here only means the user misses today's briefing, never
    a double-send (the claim already happened). Logged loudly so a write outage is
    visible rather than looking like "nothing was relevant".
    """

    def _write() -> None:
        _briefing_ref(user_id, local_date).set({
            f.FIELD_STATUS: f.STATUS_READY,
            f.FIELD_LOCAL_DATE: local_date,
            f.FIELD_NARRATIVE: narrative,
            f.FIELD_CHAT_SEED_MESSAGE: chat_seed_message,
            f.FIELD_SOURCES: sources,
            f.FIELD_ITEMS: items,
            f.FIELD_GENERATED_AT: datetime.now(UTC),
        }, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("briefing.store: write_briefing failed", {
            "user_id": user_id,
            "local_date": local_date,
            "error": str(exc),
        })


async def mark_terminal(user_id: str, *, local_date: str, status: str) -> None:
    """Set a terminal status (``skipped`` / ``failed``) on the claimed doc.

    ``skipped`` = nothing worth sending today (no push). ``failed`` = the LLM/store
    errored; a later tick may re-claim and retry per ``evaluate_claim``.
    """

    def _write() -> None:
        _briefing_ref(user_id, local_date).set({
            f.FIELD_STATUS: status,
            f.FIELD_LOCAL_DATE: local_date,
            f.FIELD_GENERATED_AT: datetime.now(UTC),
        }, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("briefing.store: mark_terminal failed", {
            "user_id": user_id,
            "local_date": local_date,
            "status": status,
            "error": str(exc),
        })


async def get_briefing(user_id: str, *, local_date: str) -> StoredBriefing | None:
    """Read ``daily_briefing/{local_date}``. Returns None when absent or on error so
    the endpoint can answer ``{briefing: null}`` (the client shows an empty state)."""

    def _fetch() -> StoredBriefing | None:
        snap = _briefing_ref(user_id, local_date).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        raw_sources = data.get(f.FIELD_SOURCES)
        sources = [s for s in raw_sources if isinstance(s, dict)] if isinstance(raw_sources, list) else []
        raw_items = data.get(f.FIELD_ITEMS)
        items = [i for i in raw_items if isinstance(i, dict)] if isinstance(raw_items, list) else []
        return StoredBriefing(
            local_date=str(data.get(f.FIELD_LOCAL_DATE, local_date)),
            status=str(data.get(f.FIELD_STATUS, "")),
            narrative=str(data.get(f.FIELD_NARRATIVE, "") or ""),
            chat_seed_message=str(data.get(f.FIELD_CHAT_SEED_MESSAGE, "") or ""),
            sources=sources,
            items=items,
        )

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("briefing.store: get_briefing failed", {
            "user_id": user_id,
            "local_date": local_date,
            "error": str(exc),
        })
        return None


def lookback_dates(local_date: str, days: int) -> list[str]:
    """``local_date`` then the prior ``days-1`` dates, newest first. Pure (testable)."""
    try:
        start = date.fromisoformat(local_date)
    except ValueError:
        return [local_date]
    return [(start - timedelta(days=i)).isoformat() for i in range(max(1, days))]


async def get_latest_ready_briefing(
    user_id: str, *, local_date: str, lookback_days: int,
) -> StoredBriefing | None:
    """Today's ready briefing, or the most recent ready one within the lookback window.

    Point-reads ``daily_briefing/{date}`` by id walking back from today, so the screen
    can show yesterday's briefing the moment it opens instead of an empty state, with no
    collection query and no index. Stops at the first ready doc found.
    """
    for d in lookback_dates(local_date, lookback_days):
        stored = await get_briefing(user_id, local_date=d)
        if stored is not None and stored.status == f.STATUS_READY:
            return stored
    return None
