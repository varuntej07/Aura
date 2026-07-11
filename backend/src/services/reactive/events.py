"""The typed event taxonomy + the ``Event`` model.

Every user action, tool call, and clock tick becomes one ``Event``. Type names
live HERE as ``EVENT_*`` constants grouped by family (the families keep the
policy table and the tests legible); a producer names its event type from these
constants and nowhere else, and ``test_reactive_events.py`` round-trips them so a
rename breaks CI.

Forward-compatibility is a deploy-safety requirement: a durable event written by
revision N must still be consumable by N+1 (``deploy.sh`` shifts 100% of traffic
at once). ``Event.from_dict`` therefore tolerates unknown future fields and
supplies defaults for fields a newer schema adds, and every event carries a
``schema_version``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .fields import (
    FIELD_DEDUP_ID,
    FIELD_EVENT_ID,
    FIELD_PAYLOAD,
    FIELD_SCHEMA_VERSION,
    FIELD_SOURCE,
    FIELD_TS,
    FIELD_TYPE,
    FIELD_UID,
)

# Bump when the event wire-shape changes in a way readers must branch on. Readers
# tolerate a higher version's extra fields (forward-compat); this is the version
# the CURRENT writer stamps.
SCHEMA_VERSION = 1


# ── Event taxonomy (5 families) ──────────────────────────────────────────────
# Behavioral — what the user did with the surface.
EVENT_APP_OPENED = "app_opened"
EVENT_NOTIFICATION_TAPPED = "notification_tapped"
EVENT_NOTIFICATION_DISMISSED = "notification_dismissed"
EVENT_DISMISS_STREAK = "dismiss_streak"  # derived: N dismissals with no taps
EVENT_CONTENT_VIEWED = "content_viewed"  # high-frequency; sampled before the bus

# Conversational — what the user said.
EVENT_MESSAGE_SENT = "message_sent"
EVENT_WARM_REPLY = "warm_reply"
EVENT_THREAD_IGNORED = "thread_ignored"
EVENT_LIFE_UPDATE = "life_update"     # "mom's home and fine" — can resolve an intent
EVENT_CORRECTION = "correction"

# Domain — a first-class thing changed in the user's world.
EVENT_REMINDER_CREATED = "reminder_created"
EVENT_REMINDER_FIRED = "reminder_fired"
EVENT_CALENDAR_EVENT_UPCOMING = "calendar_event_upcoming"
EVENT_TRACKING_UPDATE = "tracking_update"

# Temporal — the clock.
EVENT_TICK = "tick"               # cron, demoted to one event source
EVENT_INTENT_DUE = "intent_due"   # a scheduled reactive action came due

# Lifecycle — engagement state transitions.
EVENT_USER_IDLE = "user_idle"
EVENT_USER_DORMANT = "user_dormant"
EVENT_USER_REACTIVATED = "user_reactivated"


BEHAVIORAL_EVENTS = frozenset({
    EVENT_APP_OPENED,
    EVENT_NOTIFICATION_TAPPED,
    EVENT_NOTIFICATION_DISMISSED,
    EVENT_DISMISS_STREAK,
    EVENT_CONTENT_VIEWED,
})
CONVERSATIONAL_EVENTS = frozenset({
    EVENT_MESSAGE_SENT,
    EVENT_WARM_REPLY,
    EVENT_THREAD_IGNORED,
    EVENT_LIFE_UPDATE,
    EVENT_CORRECTION,
})
DOMAIN_EVENTS = frozenset({
    EVENT_REMINDER_CREATED,
    EVENT_REMINDER_FIRED,
    EVENT_CALENDAR_EVENT_UPCOMING,
    EVENT_TRACKING_UPDATE,
})
TEMPORAL_EVENTS = frozenset({
    EVENT_TICK,
    EVENT_INTENT_DUE,
})
LIFECYCLE_EVENTS = frozenset({
    EVENT_USER_IDLE,
    EVENT_USER_DORMANT,
    EVENT_USER_REACTIVATED,
})

# Presence/behavioral events want the low-latency inline dispatch (§4.1); domain,
# temporal and lifecycle events ride the 60s outbox sweep. This split is data, so
# a producer never hard-codes a dispatch decision.
PRESENCE_EVENTS = frozenset({
    EVENT_APP_OPENED,
    EVENT_NOTIFICATION_TAPPED,
    EVENT_WARM_REPLY,
})

ALL_EVENT_TYPES = (
    BEHAVIORAL_EVENTS
    | CONVERSATIONAL_EVENTS
    | DOMAIN_EVENTS
    | TEMPORAL_EVENTS
    | LIFECYCLE_EVENTS
)


def is_known_event_type(event_type: str) -> bool:
    """True when ``event_type`` is a registered type. The bus stays a thin,
    debuggable transport, so an unknown type is loudly rejected at emit time
    rather than silently carried."""
    return event_type in ALL_EVENT_TYPES


def _coerce_ts(value: Any) -> datetime:
    """Firestore returns native datetimes; tolerate ISO strings and missing/naive
    values too so a forward/backward doc shape never crashes a reader."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


@dataclass
class Event:
    """One typed, durable event. ``dedup_id`` is the idempotent-consumption key;
    it defaults to ``event_id`` (every event unique) but two producers can share a
    ``dedup_id`` to mean "the same logical event"."""

    uid: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = SCHEMA_VERSION
    dedup_id: str = ""

    def __post_init__(self) -> None:
        if not self.dedup_id:
            self.dedup_id = self.event_id

    def to_dict(self) -> dict[str, Any]:
        return {
            FIELD_EVENT_ID: self.event_id,
            FIELD_UID: self.uid,
            FIELD_TYPE: self.type,
            FIELD_PAYLOAD: self.payload or {},
            FIELD_SOURCE: self.source,
            FIELD_TS: self.ts,
            FIELD_SCHEMA_VERSION: self.schema_version,
            FIELD_DEDUP_ID: self.dedup_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        """Rebuild an Event from a stored doc. Ignores unknown future fields and
        supplies defaults for fields an older writer omitted (forward-compat)."""
        event_id = str(data.get(FIELD_EVENT_ID) or uuid.uuid4().hex)
        return cls(
            uid=str(data.get(FIELD_UID, "")),
            type=str(data.get(FIELD_TYPE, "")),
            payload=dict(data.get(FIELD_PAYLOAD) or {}),
            source=str(data.get(FIELD_SOURCE, "")),
            event_id=event_id,
            ts=_coerce_ts(data.get(FIELD_TS)),
            schema_version=int(data.get(FIELD_SCHEMA_VERSION, 1)),
            dedup_id=str(data.get(FIELD_DEDUP_ID) or event_id),
        )
