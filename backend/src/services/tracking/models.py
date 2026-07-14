"""Domain models for topic tracking.

Three persisted entities (each top-level + flat so the hot due-scan is a tight
range query, not a collection_group fan-out):

  TrackedTopic — SHARED. One public event/topic, researched once, fanned to all
                 subscribers. Carries the schedule's health metadata.
  Tracker      — PER-USER. One user's subscription to a topic_key.
  Checkpoint   — one scheduled (pre|live|post) fire in the flat due-queue.

Plus two value objects the research agent emits and the fixture matcher consumes
(never persisted on their own): TopicResearch and ResearchedFixture. The persisted
Fixture (stable identity + fact state) lives in a subcollection under its topic.

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


# ── Value objects (agent output → fixture matcher input; not persisted alone) ──
@dataclass
class ResearchedFixture:
    """One real-world fixture as the research/reconcile LLM reported it (a match, a
    hearing, a launch window). ``fixture_matcher`` resolves each to a stored Fixture:
    by the echoed ``fixture_id`` first (the reconcile prompt hands the model the
    existing fixtures and asks it to echo the id of any it recognizes), then by
    time-window + label-token matching, and only mints a NEW id when neither matches.
    Unlike the old ScheduledEvent there is no poll cadence — a fixture gets a fixed
    set of moments (pre/kickoff/result), never a poll grid."""

    label: str
    start_at: datetime
    end_at: datetime | None = None
    event_kind: str = f.EVENT_KIND_SPAN
    # Minutes before start_at the "pre" heads-up fires. 0 -> the moments default (30).
    lead_minutes: int = 0
    # A can't-miss fixture (a final, a verdict): its result may push outside the
    # notify window and bypasses the per-user daily cap (still counted).
    wake_override: bool = False
    # The stored fixture id the reconcile LLM recognized this as, or "" when the model
    # said it is new / the pass had no existing fixtures to match against.
    echoed_fixture_id: str = ""


@dataclass
class TopicResearch:
    """Structured output of one research pass (topic_agent). The store turns this
    into a TrackedTopic; ``fixtures`` becomes/updates the stable Fixture docs whose
    moments are the due-queue entries."""

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
    fixtures: list[ResearchedFixture] = field(default_factory=list)
    # Heartbeat cadence the agent chooses for "nothing scheduled right now" (minutes):
    # a live tournament between match-days a few hours, a slow legal story daily. 0 -> the
    # engine's adaptive default. Seeds the topic's pulse_interval at provision.
    idle_poll_minutes: int = 0
    # Local notify window (hours 0-23). -1 means "agent gave nothing", caller uses defaults.
    notify_start_hour: int = -1
    notify_end_hour: int = -1
    # The event is real but its date is not announced yet (fixtures stays empty).
    awaiting_date: bool = False


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
    notify_start_hour: int = f.DEFAULT_NOTIFY_START_HOUR
    notify_end_hour: int = f.DEFAULT_NOTIFY_END_HOUR
    awaiting_date: bool = False
    recent_development_keys: list[str] = field(default_factory=list)
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
            f.TOPIC_NOTIFY_START_HOUR: self.notify_start_hour,
            f.TOPIC_NOTIFY_END_HOUR: self.notify_end_hour,
            f.TOPIC_AWAITING_DATE: self.awaiting_date,
            f.TOPIC_RECENT_DEVELOPMENT_KEYS: self.recent_development_keys,
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
            # Old docs predating the notify window default to the standard waking hours,
            # so a topic created before this field existed is never silenced or 24/7.
            notify_start_hour=int(data.get(f.TOPIC_NOTIFY_START_HOUR, f.DEFAULT_NOTIFY_START_HOUR)),
            notify_end_hour=int(data.get(f.TOPIC_NOTIFY_END_HOUR, f.DEFAULT_NOTIFY_END_HOUR)),
            awaiting_date=bool(data.get(f.TOPIC_AWAITING_DATE, False)),
            recent_development_keys=[
                str(k) for k in (data.get(f.TOPIC_RECENT_DEVELOPMENT_KEYS) or []) if str(k)
            ],
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


# ── tracked_topics/{topic_key}/fixtures/{fixture_id} ────────────────────────
@dataclass
class Fixture:
    """One real-world fixture with a STABLE identity and structured fact state.

    The id is minted once from the fixture's start slot (see
    ``fixture_matcher.mint_fixture_id``) and never re-derived from the label, so a
    reconcile that rewords the label updates this doc in place — a parallel series
    for the same match is structurally impossible. The fact fields are the send
    gate: a moment pushes only when they TRANSITION (``fact_gate``), never because
    a fresh composition worded the same state differently."""

    id: str
    topic_key: str
    label: str
    start_at: datetime
    expected_end_at: datetime | None = None
    kind: str = f.EVENT_KIND_SPAN
    lead_minutes: int = 0
    wake_override: bool = False
    status: str = f.FIXTURE_STATUS_SCHEDULED
    fact_score: str = ""
    fact_winner: str = ""
    fact_note: str = ""
    facts_updated_at: datetime | None = None
    last_transition: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            f.FIXTURE_ID: self.id,
            f.FIXTURE_TOPIC_KEY: self.topic_key,
            f.FIXTURE_LABEL: self.label,
            f.FIXTURE_START_AT: self.start_at,
            f.FIXTURE_EXPECTED_END_AT: self.expected_end_at,
            f.FIXTURE_KIND: self.kind,
            f.FIXTURE_LEAD_MINUTES: self.lead_minutes,
            f.FIXTURE_WAKE_OVERRIDE: self.wake_override,
            f.FIXTURE_STATUS: self.status,
            f.FIXTURE_FACT_SCORE: self.fact_score,
            f.FIXTURE_FACT_WINNER: self.fact_winner,
            f.FIXTURE_FACT_NOTE: self.fact_note,
            f.FIXTURE_FACTS_UPDATED_AT: self.facts_updated_at,
            f.FIXTURE_LAST_TRANSITION: self.last_transition,
            f.FIXTURE_CREATED_AT: self.created_at,
            f.FIXTURE_UPDATED_AT: self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fixture:
        return cls(
            id=str(data.get(f.FIXTURE_ID, "")),
            topic_key=str(data.get(f.FIXTURE_TOPIC_KEY, "")),
            label=str(data.get(f.FIXTURE_LABEL, "")),
            start_at=_coerce_datetime(data.get(f.FIXTURE_START_AT)) or datetime.now(UTC),
            expected_end_at=_coerce_datetime(data.get(f.FIXTURE_EXPECTED_END_AT)),
            kind=str(data.get(f.FIXTURE_KIND, f.EVENT_KIND_SPAN)),
            lead_minutes=int(data.get(f.FIXTURE_LEAD_MINUTES, 0) or 0),
            wake_override=bool(data.get(f.FIXTURE_WAKE_OVERRIDE, False)),
            status=str(data.get(f.FIXTURE_STATUS, f.FIXTURE_STATUS_SCHEDULED)),
            fact_score=str(data.get(f.FIXTURE_FACT_SCORE, "") or ""),
            fact_winner=str(data.get(f.FIXTURE_FACT_WINNER, "") or ""),
            fact_note=str(data.get(f.FIXTURE_FACT_NOTE, "") or ""),
            facts_updated_at=_coerce_datetime(data.get(f.FIXTURE_FACTS_UPDATED_AT)),
            last_transition=str(data.get(f.FIXTURE_LAST_TRANSITION, "") or ""),
            created_at=_coerce_datetime(data.get(f.FIXTURE_CREATED_AT)),
            updated_at=_coerce_datetime(data.get(f.FIXTURE_UPDATED_AT)),
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
    sent_today: int = 0
    sent_today_date: str = ""

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
            f.TRACKER_SENT_TODAY: self.sent_today,
            f.TRACKER_SENT_TODAY_DATE: self.sent_today_date,
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
            sent_today=int(data.get(f.TRACKER_SENT_TODAY, 0) or 0),
            sent_today_date=str(data.get(f.TRACKER_SENT_TODAY_DATE, "") or ""),
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
    wake_override: bool = False
    fixture_id: str = ""
    result_checks: int = 0

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
            f.CHECKPOINT_WAKE_OVERRIDE: self.wake_override,
            f.CHECKPOINT_FIXTURE_ID: self.fixture_id,
            f.CHECKPOINT_RESULT_CHECKS: self.result_checks,
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
            wake_override=bool(data.get(f.CHECKPOINT_WAKE_OVERRIDE, False)),
            fixture_id=str(data.get(f.CHECKPOINT_FIXTURE_ID, "") or ""),
            result_checks=int(data.get(f.CHECKPOINT_RESULT_CHECKS, 0) or 0),
        )
