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
from datetime import datetime, timezone
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
    bootstrap_done: bool = False
    last_updated: datetime | None = None
    # Timestamp of the last UserAura-driven vector refresh (bootstrap or periodic).
    last_bootstrap_at: datetime | None = None
    # Increments each tick where the user got a notification but didn't open it,
    # or where a send was blocked. Resets on any positive engagement event.
    consecutive_no_open_ticks: int = 0

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

    return SignalStoreState(
        user_vector=user_vector,
        time_slot_open_rates=time_slot_open_rates,
        category_affinities={str(k): float(v) for k, v in affinities.items()},
        last_notification_at=raw.get("last_notification_at"),
        sends_today=int(raw.get("sends_today", 0)),
        sends_today_date=str(raw.get("sends_today_date", "")),
        bootstrap_done=bool(raw.get("bootstrap_done", False)),
        last_updated=raw.get("last_updated"),
        last_bootstrap_at=raw.get("last_bootstrap_at"),
        consecutive_no_open_ticks=int(raw.get("consecutive_no_open_ticks", 0)),
    )


def _encode_state(state: SignalStoreState) -> dict[str, Any]:
    return {
        "user_vector": Vector(state.user_vector),
        "time_slot_open_rates": state.time_slot_open_rates,
        "category_affinities": state.category_affinities,
        "last_notification_at": state.last_notification_at,
        "sends_today": state.sends_today,
        "sends_today_date": state.sends_today_date,
        "bootstrap_done": state.bootstrap_done,
        "last_updated": datetime.now(timezone.utc),
        "last_bootstrap_at": state.last_bootstrap_at,
        "consecutive_no_open_ticks": state.consecutive_no_open_ticks,
    }


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
        "timestamp": datetime.now(timezone.utc),
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
) -> None:
    """Record a sent notification awaiting outcome (opened / dismissed / timeout)."""
    doc = {
        "content_id": content_id,
        "score": score,
        "scored_at": scored_at,
        "sent_at": sent_at,
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

    now = datetime.now(timezone.utc)

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


async def list_active_user_ids(inactivity_days: int = 7) -> list[str]:
    """Return user IDs with a registered FCM token seen within inactivity_days.

    Mirrors agents/orchestrator._load_active_user_ids so the scoring loop
    targets the same audience as the legacy agent dispatcher.
    """
    from datetime import timedelta
    from google.cloud.firestore_v1.base_query import FieldFilter

    cutoff = (datetime.now(timezone.utc) - timedelta(days=inactivity_days)).isoformat()

    def _fetch() -> list[str]:
        db = admin_firestore()
        docs = (
            db.collection_group("fcm_tokens")
            .where(filter=FieldFilter("last_seen", ">=", cutoff))
            .stream()
        )
        user_ids: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            parts = doc.reference.path.split("/")
            if len(parts) >= 2:
                uid = parts[1]
                if uid not in seen:
                    seen.add(uid)
                    user_ids.append(uid)
        return user_ids

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.error("feature_store.list_active_user_ids failed", {"error": str(exc)})
        return []
