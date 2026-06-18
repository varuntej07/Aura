"""Domain models for topic tracking.

Three persisted entities (each top-level + flat so the hot due-scan is a tight
range query, not a collection_group fan-out):

  TrackedTopic — SHARED. One public event/topic, researched once, fanned to all
                 subscribers. Carries the schedule's health metadata.
  Tracker      — PER-USER. One user's subscription to a topic_key.
  Checkpoint   — one scheduled (pre|live|post) fire in the flat due-queue.

Plus two value objects the research agent emits and the schedule builder consumes
(never persisted on their own): TopicResearch and ScheduledEvent.

Every serialised key goes through fields.py — no string literal is typed twice, so
a rename lives in one place and a writer->reader round-trip test can guard it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import fields as f


def _coerce_datetime(value: Any) -> datetime | None:
    """Firestore returns tz-aware datetimes; tolerate ISO strings too. A naive value
    is treated as UTC so a stored-without-tz timestamp can't shift the due-scan."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


# ── Value objects (agent output → schedule builder input; not persisted alone) ──
@dataclass
class ScheduledEvent:
    """One dated beat of a topic (a match, a hearing, a launch keynote). The schedule
    builder turns each into Checkpoint docs. The phases follow the event shape: a
    ``span`` (has duration, e.g. a match) gets pre/live/post; a ``point`` (instantaneous,
    e.g. a verdict, a launch, a release) gets a heads-up pre + a single milestone at the
    moment, so an instant event never reads like a fake "live, 0-0"."""

    label: str
    start_at: datetime
    end_at: datetime | None = None
    event_kind: str = f.EVENT_KIND_SPAN
    phases: list[str] | None = None

    def __post_init__(self) -> None:
        if self.phases is None:
            if self.event_kind == f.EVENT_KIND_POINT:
                self.phases = [f.CHECKPOINT_PHASE_PRE, f.CHECKPOINT_PHASE_MILESTONE]
            else:
                self.phases = [
                    f.CHECKPOINT_PHASE_PRE, f.CHECKPOINT_PHASE_LIVE, f.CHECKPOINT_PHASE_POST,
                ]


@dataclass
class TopicResearch:
    """Structured output of one research pass (topic_agent). The store turns this
    into a TrackedTopic; the schedule builder turns ``events`` into Checkpoints."""

    topic_key: str
    title: str
    kind: str
    research_query: str
    end_condition: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    expires_at: datetime | None = None
    timezone: str = "UTC"
    country: str = ""
    language: str = ""
    confidence: float = 0.0
    events: list[ScheduledEvent] = field(default_factory=list)


# ── tracked_topics/{topic_key} ───────────────────────────────────────────────
@dataclass
class TrackedTopic:
    topic_key: str
    title: str = ""
    kind: str = f.TOPIC_KIND_OPEN_INTEREST
    research_query: str = ""
    end_condition: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    expires_at: datetime | None = None
    timezone: str = "UTC"
    country: str = ""
    language: str = ""
    live_summary: str = ""
    live_fetched_at: datetime | None = None
    live_source_tier: str = ""
    next_reconcile_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    reconcile_count: int = 0
    subscriber_count: int = 0
    pulse_interval_seconds: int = 0
    status: str = f.TOPIC_STATUS_ACTIVE
    health: str = f.TOPIC_HEALTH_HEALTHY
    research_confidence: float = 0.0
    last_research_tier: str = ""
    last_reconcile_status: str = ""
    last_reconcile_error: str | None = None
    consecutive_reconcile_failures: int = 0
    checkpoints_total: int = 0
    checkpoints_fired: int = 0
    checkpoints_failed: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            f.TOPIC_KEY: self.topic_key,
            f.TOPIC_TITLE: self.title,
            f.TOPIC_KIND: self.kind,
            f.TOPIC_RESEARCH_QUERY: self.research_query,
            f.TOPIC_END_CONDITION: self.end_condition,
            f.TOPIC_STARTS_AT: self.starts_at,
            f.TOPIC_ENDS_AT: self.ends_at,
            f.TOPIC_EXPIRES_AT: self.expires_at,
            f.TOPIC_TIMEZONE: self.timezone,
            f.TOPIC_COUNTRY: self.country,
            f.TOPIC_LANGUAGE: self.language,
            f.TOPIC_LIVE_SUMMARY: self.live_summary,
            f.TOPIC_LIVE_FETCHED_AT: self.live_fetched_at,
            f.TOPIC_LIVE_SOURCE_TIER: self.live_source_tier,
            f.TOPIC_NEXT_RECONCILE_AT: self.next_reconcile_at,
            f.TOPIC_LAST_RECONCILED_AT: self.last_reconciled_at,
            f.TOPIC_RECONCILE_COUNT: self.reconcile_count,
            f.TOPIC_SUBSCRIBER_COUNT: self.subscriber_count,
            f.TOPIC_PULSE_INTERVAL_SECONDS: self.pulse_interval_seconds,
            f.TOPIC_STATUS: self.status,
            f.TOPIC_HEALTH: self.health,
            f.TOPIC_RESEARCH_CONFIDENCE: self.research_confidence,
            f.TOPIC_LAST_RESEARCH_TIER: self.last_research_tier,
            f.TOPIC_LAST_RECONCILE_STATUS: self.last_reconcile_status,
            f.TOPIC_LAST_RECONCILE_ERROR: self.last_reconcile_error,
            f.TOPIC_CONSECUTIVE_RECONCILE_FAILURES: self.consecutive_reconcile_failures,
            f.TOPIC_CHECKPOINTS_TOTAL: self.checkpoints_total,
            f.TOPIC_CHECKPOINTS_FIRED: self.checkpoints_fired,
            f.TOPIC_CHECKPOINTS_FAILED: self.checkpoints_failed,
            f.TOPIC_CREATED_AT: self.created_at,
            f.TOPIC_UPDATED_AT: self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackedTopic:
        return cls(
            topic_key=str(data.get(f.TOPIC_KEY, "")),
            title=str(data.get(f.TOPIC_TITLE, "")),
            kind=str(data.get(f.TOPIC_KIND, f.TOPIC_KIND_OPEN_INTEREST)),
            research_query=str(data.get(f.TOPIC_RESEARCH_QUERY, "")),
            end_condition=str(data.get(f.TOPIC_END_CONDITION, "")),
            starts_at=_coerce_datetime(data.get(f.TOPIC_STARTS_AT)),
            ends_at=_coerce_datetime(data.get(f.TOPIC_ENDS_AT)),
            expires_at=_coerce_datetime(data.get(f.TOPIC_EXPIRES_AT)),
            timezone=str(data.get(f.TOPIC_TIMEZONE, "UTC") or "UTC"),
            country=str(data.get(f.TOPIC_COUNTRY, "")),
            language=str(data.get(f.TOPIC_LANGUAGE, "")),
            live_summary=str(data.get(f.TOPIC_LIVE_SUMMARY, "")),
            live_fetched_at=_coerce_datetime(data.get(f.TOPIC_LIVE_FETCHED_AT)),
            live_source_tier=str(data.get(f.TOPIC_LIVE_SOURCE_TIER, "")),
            next_reconcile_at=_coerce_datetime(data.get(f.TOPIC_NEXT_RECONCILE_AT)),
            last_reconciled_at=_coerce_datetime(data.get(f.TOPIC_LAST_RECONCILED_AT)),
            reconcile_count=int(data.get(f.TOPIC_RECONCILE_COUNT, 0) or 0),
            subscriber_count=int(data.get(f.TOPIC_SUBSCRIBER_COUNT, 0) or 0),
            pulse_interval_seconds=int(data.get(f.TOPIC_PULSE_INTERVAL_SECONDS, 0) or 0),
            status=str(data.get(f.TOPIC_STATUS, f.TOPIC_STATUS_ACTIVE)),
            health=str(data.get(f.TOPIC_HEALTH, f.TOPIC_HEALTH_HEALTHY)),
            research_confidence=float(data.get(f.TOPIC_RESEARCH_CONFIDENCE, 0.0) or 0.0),
            last_research_tier=str(data.get(f.TOPIC_LAST_RESEARCH_TIER, "")),
            last_reconcile_status=str(data.get(f.TOPIC_LAST_RECONCILE_STATUS, "")),
            last_reconcile_error=data.get(f.TOPIC_LAST_RECONCILE_ERROR),
            consecutive_reconcile_failures=int(data.get(f.TOPIC_CONSECUTIVE_RECONCILE_FAILURES, 0) or 0),
            checkpoints_total=int(data.get(f.TOPIC_CHECKPOINTS_TOTAL, 0) or 0),
            checkpoints_fired=int(data.get(f.TOPIC_CHECKPOINTS_FIRED, 0) or 0),
            checkpoints_failed=int(data.get(f.TOPIC_CHECKPOINTS_FAILED, 0) or 0),
            created_at=_coerce_datetime(data.get(f.TOPIC_CREATED_AT)),
            updated_at=_coerce_datetime(data.get(f.TOPIC_UPDATED_AT)),
        )


# ── trackers/{tracker_id} ────────────────────────────────────────────────────
@dataclass
class Tracker:
    id: str
    user_id: str
    topic_key: str
    status: str = f.TRACKER_STATUS_ACTIVE
    created_via: str = "text"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    mute_until: datetime | None = None
    updates_sent: int = 0
    last_update_at: datetime | None = None
    last_sent_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            f.TRACKER_ID: self.id,
            f.TRACKER_USER_ID: self.user_id,
            f.TRACKER_TOPIC_KEY: self.topic_key,
            f.TRACKER_STATUS: self.status,
            f.TRACKER_CREATED_VIA: self.created_via,
            f.TRACKER_CREATED_AT: self.created_at,
            f.TRACKER_UPDATED_AT: self.updated_at,
            f.TRACKER_MUTE_UNTIL: self.mute_until,
            f.TRACKER_UPDATES_SENT: self.updates_sent,
            f.TRACKER_LAST_UPDATE_AT: self.last_update_at,
            f.TRACKER_LAST_SENT_SUMMARY: self.last_sent_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tracker:
        return cls(
            id=str(data.get(f.TRACKER_ID, "")),
            user_id=str(data.get(f.TRACKER_USER_ID, "")),
            topic_key=str(data.get(f.TRACKER_TOPIC_KEY, "")),
            status=str(data.get(f.TRACKER_STATUS, f.TRACKER_STATUS_ACTIVE)),
            created_via=str(data.get(f.TRACKER_CREATED_VIA, "text")),
            created_at=_coerce_datetime(data.get(f.TRACKER_CREATED_AT)),
            updated_at=_coerce_datetime(data.get(f.TRACKER_UPDATED_AT)),
            mute_until=_coerce_datetime(data.get(f.TRACKER_MUTE_UNTIL)),
            updates_sent=int(data.get(f.TRACKER_UPDATES_SENT, 0) or 0),
            last_update_at=_coerce_datetime(data.get(f.TRACKER_LAST_UPDATE_AT)),
            last_sent_summary=str(data.get(f.TRACKER_LAST_SENT_SUMMARY, "")),
        )


# ── checkpoints/{checkpoint_id} ──────────────────────────────────────────────
@dataclass
class Checkpoint:
    id: str
    topic_key: str
    event_label: str
    phase: str
    fire_at: datetime
    status: str = f.CHECKPOINT_STATUS_PENDING
    attempts: int = 0
    claimed_at: datetime | None = None
    fired_at: datetime | None = None
    last_summary: str = ""
    last_fetch_tier: str = ""
    last_fetch_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            f.CHECKPOINT_ID: self.id,
            f.CHECKPOINT_TOPIC_KEY: self.topic_key,
            f.CHECKPOINT_EVENT_LABEL: self.event_label,
            f.CHECKPOINT_PHASE: self.phase,
            f.CHECKPOINT_FIRE_AT: self.fire_at,
            f.CHECKPOINT_STATUS: self.status,
            f.CHECKPOINT_ATTEMPTS: self.attempts,
            f.CHECKPOINT_CLAIMED_AT: self.claimed_at,
            f.CHECKPOINT_FIRED_AT: self.fired_at,
            f.CHECKPOINT_LAST_SUMMARY: self.last_summary,
            f.CHECKPOINT_LAST_FETCH_TIER: self.last_fetch_tier,
            f.CHECKPOINT_LAST_FETCH_AT: self.last_fetch_at,
            f.CHECKPOINT_LAST_ERROR: self.last_error,
            f.CHECKPOINT_CREATED_AT: self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            id=str(data.get(f.CHECKPOINT_ID, "")),
            topic_key=str(data.get(f.CHECKPOINT_TOPIC_KEY, "")),
            event_label=str(data.get(f.CHECKPOINT_EVENT_LABEL, "")),
            phase=str(data.get(f.CHECKPOINT_PHASE, f.CHECKPOINT_PHASE_LIVE)),
            fire_at=_coerce_datetime(data.get(f.CHECKPOINT_FIRE_AT)) or datetime.now(UTC),
            status=str(data.get(f.CHECKPOINT_STATUS, f.CHECKPOINT_STATUS_PENDING)),
            attempts=int(data.get(f.CHECKPOINT_ATTEMPTS, 0) or 0),
            claimed_at=_coerce_datetime(data.get(f.CHECKPOINT_CLAIMED_AT)),
            fired_at=_coerce_datetime(data.get(f.CHECKPOINT_FIRED_AT)),
            last_summary=str(data.get(f.CHECKPOINT_LAST_SUMMARY, "")),
            last_fetch_tier=str(data.get(f.CHECKPOINT_LAST_FETCH_TIER, "")),
            last_fetch_at=_coerce_datetime(data.get(f.CHECKPOINT_LAST_FETCH_AT)),
            last_error=data.get(f.CHECKPOINT_LAST_ERROR),
            created_at=_coerce_datetime(data.get(f.CHECKPOINT_CREATED_AT)),
        )
