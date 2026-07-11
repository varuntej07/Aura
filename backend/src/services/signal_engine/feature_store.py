"""
Per-user signal store CRUD.

The state document at users/{uid}/signal_store/state is the only durable
per-user state the engine reads on every scoring tick. All writes are
performed via asyncio.to_thread because firebase-admin is sync.

This module is pure I/O. No scoring logic, no business decisions, no
per-request state held on the function or module level. Each call reads
or writes Firestore and returns plain dicts or dataclasses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1.vector import Vector

from ...lib.logger import logger
from ..firebase import admin_firestore

USER_VECTOR_DIMENSION = 768
TIME_SLOTS_PER_DAY = 48

# Reset value used when a user has no usable signal yet. A flat 0.5 across
# all 48 slots gives every hour an equal baseline open rate before the
# first outcome lands.
INITIAL_TIME_SLOT_OPEN_RATE = 0.5


@dataclass
class SignalStoreState:
    """In-memory view of users/{uid}/signal_store/state."""

    user_vector: list[float] = field(default_factory=lambda: [0.0] * USER_VECTOR_DIMENSION)
    time_slot_open_rates: list[float] = field(
        default_factory=lambda: [INITIAL_TIME_SLOT_OPEN_RATE] * TIME_SLOTS_PER_DAY
    )
    category_affinities: dict[str, float] = field(default_factory=dict)
    last_notification_at: datetime | None = None
    sends_today: int = 0
    sends_today_date: str = ""
    # Breaking-lane sends today (subset of sends_today). Capped per day by
    # scoring.MAX_BREAKING_SENDS_PER_DAY and reset alongside sends_today when the
    # user-local calendar day flips. Legacy docs default 0.
    breaking_sends_today: int = 0
    bootstrap_done: bool = False
    last_updated: datetime | None = None
    # Timestamp of the last UserAura-driven vector refresh (bootstrap or periodic).
    last_bootstrap_at: datetime | None = None
    # Increments each tick where the user got a notification but didn't open it,
    # or where a send was blocked. Resets on any positive engagement event.
    consecutive_no_open_ticks: int = 0
    # Denormalized ring buffer of recent real sends — {"content_id", "category",
    # "sent_at"} dicts, oldest first, capped at RECENT_SENDS_MAX (scoring_loop.py).
    # Read once alongside the rest of this doc instead of two separate per-tick
    # range queries against the outcomes subcollection (already-sent suppression +
    # the diversity tie-breaker). Appended to by on_news_delivered on a real send.
    recent_sends: list[dict[str, Any]] = field(default_factory=list)
    # False for every pre-existing doc (the field didn't exist before this was
    # added) — scoring_loop's _ensure_recent_sends_backfilled does a ONE-TIME
    # backfill from the outcomes subcollection the first tick after deploy, then
    # sets this True so it never re-queries again, even for a genuinely-empty
    # history. Without this flag, "empty" and "not yet backfilled" look identical.
    recent_sends_backfilled: bool = False

    def is_empty(self) -> bool:
        """True when no real signal has been recorded yet."""
        return not self.bootstrap_done and self.last_updated is None


def _state_doc_ref(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection("signal_store")
        .document("state")
    )


def _decode_state(raw: dict[str, Any]) -> SignalStoreState:
    vec_field = raw.get("user_vector")
    if isinstance(vec_field, Vector):
        user_vector = list(vec_field.to_map_value()["value"])  # type: ignore[attr-defined]
    elif isinstance(vec_field, list):
        user_vector = [float(x) for x in vec_field]
    else:
        user_vector = [0.0] * USER_VECTOR_DIMENSION

    rates_raw = raw.get("time_slot_open_rates")
    if isinstance(rates_raw, list) and len(rates_raw) == TIME_SLOTS_PER_DAY:
        time_slot_open_rates = [float(r) for r in rates_raw]
    else:
        time_slot_open_rates = [INITIAL_TIME_SLOT_OPEN_RATE] * TIME_SLOTS_PER_DAY

    affinities = raw.get("category_affinities") or {}
    if not isinstance(affinities, dict):
        affinities = {}

    recent_sends_raw = raw.get("recent_sends")
    recent_sends: list[dict[str, Any]] = []
    if isinstance(recent_sends_raw, list):
        for entry in recent_sends_raw:
            if not isinstance(entry, dict):
                continue
            content_id = str(entry.get("content_id", "") or "")
            if not content_id:
                continue
            recent_sends.append({
                "content_id": content_id,
                "category": str(entry.get("category", "") or ""),
                "sent_at": entry.get("sent_at"),
            })

    return SignalStoreState(
        user_vector=user_vector,
        time_slot_open_rates=time_slot_open_rates,
        category_affinities={str(k): float(v) for k, v in affinities.items()},
        last_notification_at=raw.get("last_notification_at"),
        sends_today=int(raw.get("sends_today", 0)),
        sends_today_date=str(raw.get("sends_today_date", "")),
        breaking_sends_today=int(raw.get("breaking_sends_today", 0)),
        bootstrap_done=bool(raw.get("bootstrap_done", False)),
        last_updated=raw.get("last_updated"),
        last_bootstrap_at=raw.get("last_bootstrap_at"),
        consecutive_no_open_ticks=int(raw.get("consecutive_no_open_ticks", 0)),
        recent_sends=recent_sends,
        recent_sends_backfilled=bool(raw.get("recent_sends_backfilled", False)),
    )


async def read_time_slot_open_rates(user_id: str) -> list[float]:
    """Just the 48 time-slot open rates, via a field mask so the 768-float user_vector
    is NOT loaded. Used by the drain's smart-timing hold. Returns the flat default
    (every hour equal) when absent or on any error, so a missing profile / read failure
    never makes a slot look 'bad' and hold a send."""
    flat = [INITIAL_TIME_SLOT_OPEN_RATE] * TIME_SLOTS_PER_DAY

    def _read() -> list[float]:
        snap = _state_doc_ref(user_id).get(field_paths=["time_slot_open_rates"])
        if not snap.exists:
            return flat
        rates = (snap.to_dict() or {}).get("time_slot_open_rates")
        if isinstance(rates, list) and len(rates) == TIME_SLOTS_PER_DAY:
            return [float(r) for r in rates]
        return flat

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("feature_store.read_time_slot_open_rates failed", {
            "user_id": user_id, "error": str(exc),
        })
        return flat


def _encode_state(state: SignalStoreState) -> dict[str, Any]:
    return {
        "user_vector": Vector(state.user_vector),
        "time_slot_open_rates": state.time_slot_open_rates,
        "category_affinities": state.category_affinities,
        "last_notification_at": state.last_notification_at,
        "sends_today": state.sends_today,
        "sends_today_date": state.sends_today_date,
        "breaking_sends_today": state.breaking_sends_today,
        "bootstrap_done": state.bootstrap_done,
        "last_updated": datetime.now(UTC),
        "last_bootstrap_at": state.last_bootstrap_at,
        "consecutive_no_open_ticks": state.consecutive_no_open_ticks,
        "recent_sends": state.recent_sends,
        "recent_sends_backfilled": state.recent_sends_backfilled,
    }


def record_recent_send(
    state: SignalStoreState, *, content_id: str, category: str, sent_at: datetime, cap: int,
) -> None:
    """Append a real send to the ring buffer in place, oldest-first, trimmed to
    ``cap``. Pure (no I/O) — the caller's existing state write persists it."""
    if not content_id:
        return
    state.recent_sends.append({"content_id": content_id, "category": category, "sent_at": sent_at})
    if len(state.recent_sends) > cap:
        state.recent_sends = state.recent_sends[-cap:]


async def read_state(user_id: str) -> SignalStoreState:
    """Read state. Returns a default-initialised state when no doc exists."""
    def _fetch() -> SignalStoreState:
        snap = _state_doc_ref(user_id).get()
        if not snap.exists:
            return SignalStoreState()
        return _decode_state(snap.to_dict() or {})

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("feature_store.read_state failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return SignalStoreState()


async def write_state(user_id: str, state: SignalStoreState) -> None:
    """Overwrite the state document. Callers must read-modify-write.
    Raises on failure so callers can decide whether to abort the current tick
    (preventing sends_today from going un-persisted, which would allow spam).
    """
    def _put() -> None:
        _state_doc_ref(user_id).set(_encode_state(state))

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:
        logger.warn("feature_store.write_state failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        raise


async def append_event(
    user_id: str,
    *,
    event_type: str,
    content_id: str | None,
    category: str | None,
    duration_ms: int | None = None,
) -> None:
    """Append a raw event row. Used for audit + future model retraining."""
    doc = {
        "event_type": event_type,
        "content_id": content_id,
        "category": category,
        "duration_ms": duration_ms,
        "timestamp": datetime.now(UTC),
    }

    def _put() -> None:
        (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("signal_store")
            .document("state")
            .collection("events")
            .add(doc)
        )

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:
        logger.warn("feature_store.append_event failed", {
            "user_id": user_id,
            "event_type": event_type,
            "error": str(exc),
        })


async def write_outcome_pending(
    user_id: str,
    notification_id: str,
    *,
    content_id: str,
    score: float,
    scored_at: datetime,
    sent_at: datetime,
    relevance_reason: str = "",
    category: str = "",
) -> None:
    """Record a sent notification awaiting outcome (opened / dismissed / timeout).

    relevance_reason is the framer's defensible justification for why this
    notification fired (the named interest it matched). Persisted so every send is
    auditable after the fact, not just a bare score.

    category is stored inline so a later reader never needs a content_pool join to
    learn what category this send was (the pre-denormalization design queried
    outcomes for recency, then did a SEPARATE per-row content_pool.get_candidate
    call just to recover the category — this makes that join unnecessary for any
    outcome doc written from here on).
    """
    doc = {
        "content_id": content_id,
        "category": category,
        "score": score,
        "scored_at": scored_at,
        "sent_at": sent_at,
        "relevance_reason": relevance_reason,
        "outcome": "pending",
        "outcome_at": None,
    }

    def _put() -> None:
        (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("signal_store")
            .document("state")
            .collection("outcomes")
            .document(notification_id)
            .set(doc)
        )

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:
        logger.warn("feature_store.write_outcome_pending failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "error": str(exc),
        })


async def resolve_outcome(
    user_id: str,
    notification_id: str,
    *,
    outcome: str,
) -> dict[str, Any] | None:
    """Mark an outcome as opened / dismissed / timeout. Returns the previous doc
    so callers can reward / penalise the user vector accordingly."""
    if outcome not in {"opened", "dismissed", "timeout"}:
        raise ValueError(f"resolve_outcome: unknown outcome '{outcome}'")

    now = datetime.now(UTC)

    def _update() -> dict[str, Any] | None:
        ref = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("signal_store")
            .document("state")
            .collection("outcomes")
            .document(notification_id)
        )
        snap = ref.get()
        if not snap.exists:
            return None
        current = snap.to_dict() or {}
        if current.get("outcome") != "pending":
            return current
        ref.update({"outcome": outcome, "outcome_at": now})
        return current

    try:
        return await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("feature_store.resolve_outcome failed", {
            "user_id": user_id,
            "notification_id": notification_id,
            "outcome": outcome,
            "error": str(exc),
        })
        return None


async def list_active_user_ids(inactivity_days: int = 7, *, force_refresh: bool = False) -> list[str]:
    """Async wrapper over ``fcm_token_registry.list_active_user_ids`` — the single
    source of truth for the ``fcm_tokens`` schema and the active-user query. The
    scoring loop and the agent orchestrator both go through that one query so they
    target the same audience and can never disagree on the field name.

    ``force_refresh`` bypasses that function's in-process TTL cache — pass it only
    for callers where freshness matters more than avoiding a Firestore round trip
    (e.g. the once-a-day plan fan-out).

    Dark-test gate: when ``settings.PROACTIVE_NOTIFICATION_UID_ALLOWLIST`` is set,
    the audience is intersected with that allowlist so a candidate revision can
    send a proactive-notification change to only the tester's phone. Unset (the
    live/production default) means no restriction — every active user is returned,
    exactly as before. The agent orchestrator is intentionally NOT gated: it calls
    ``fcm_token_registry.list_active_user_ids`` directly and only fetches data, it
    never sends."""
    from ..fcm_token_registry import list_active_user_ids as _list_active_user_ids

    try:
        user_ids = await asyncio.to_thread(_list_active_user_ids, inactivity_days, force_refresh=force_refresh)
    except Exception as exc:
        logger.error("feature_store.list_active_user_ids failed", {"error": str(exc)})
        return []

    return apply_proactive_allowlist(user_ids)


def apply_proactive_allowlist(user_ids: list[str]) -> list[str]:
    """Intersect ``user_ids`` with ``settings.PROACTIVE_NOTIFICATION_UID_ALLOWLIST``
    when the dark-test gate is set, so a candidate revision can send a
    proactive-notification change to only the tester's phone. Unset (the
    live/production default) means no restriction — the input list comes back
    unchanged. Shared by every PROACTIVE-lane audience discovery path (this
    function's own list_active_user_ids, and the proactive-drain's
    queue-based discovery in scheduler.py) so a dark-test candidate can never
    send outside the allowlist regardless of which path found the user."""
    from ...config.settings import settings

    allowlist = settings.proactive_notification_uid_allowlist
    if not allowlist:
        return user_ids

    allowed = set(allowlist)
    restricted = [uid for uid in user_ids if uid in allowed]
    logger.warn(
        "feature_store.apply_proactive_allowlist: PROACTIVE_NOTIFICATION_UID_ALLOWLIST is set, "
        "proactive notifications restricted to allowlisted uids. This MUST be unset on the "
        "live/production revision or most users receive nothing.",
        {
            "allowlist_size": len(allowed),
            "before": len(user_ids),
            "after": len(restricted),
        },
    )
    return restricted
