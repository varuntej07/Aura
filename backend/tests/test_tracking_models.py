"""Writer->reader round-trip guard for the topic-tracking models.

Per CLAUDE.md's database-field discipline: a Firestore field name that exists on the
writer but not the reader (or vice versa) does NOT error, it silently returns the
default — which looks identical to "no data". These round-trips break CI the moment a
field name drifts on either side of to_dict / from_dict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.tracking import fields as f
from src.services.tracking.models import Checkpoint, TrackedTopic, Tracker

_NOW = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


def test_tracked_topic_round_trip():
    topic = TrackedTopic(
        topic_key="fifa-world-cup-2026",
        title="USA at the FIFA World Cup 2026",
        kind=f.TOPIC_KIND_BOUNDED_EVENT,
        research_query="USA World Cup 2026 fixtures",
        end_condition="USA eliminated or final played",
        starts_at=_NOW,
        ends_at=_NOW,
        expires_at=_NOW,
        timezone="America/New_York",
        country="US",
        language="en",
        live_summary="USA 2-1 AUS, full time",
        live_fetched_at=_NOW,
        live_source_tier=f.TIER_BRAVE,
        next_reconcile_at=_NOW,
        last_reconciled_at=_NOW,
        reconcile_count=3,
        subscriber_count=2,
        pulse_interval_seconds=10800,
        status=f.TOPIC_STATUS_ACTIVE,
        health=f.TOPIC_HEALTH_HEALTHY,
        research_confidence=0.8,
        last_research_tier=f.TIER_GROUNDED,
        last_reconcile_status="ok",
        consecutive_reconcile_failures=0,
        checkpoints_total=9,
        checkpoints_fired=3,
        checkpoints_failed=1,
        created_at=_NOW,
        updated_at=_NOW,
    )
    restored = TrackedTopic.from_dict(topic.to_dict())
    assert restored == topic


def test_tracker_round_trip():
    tracker = Tracker(
        id="abc123",
        user_id="user-1",
        topic_key="ipl-2026",
        status=f.TRACKER_STATUS_ACTIVE,
        created_via="text",
        created_at=_NOW,
        updated_at=_NOW,
        mute_until=_NOW,
        updates_sent=4,
        last_update_at=_NOW,
        last_sent_summary="RCB won by 4 wickets",
    )
    restored = Tracker.from_dict(tracker.to_dict())
    assert restored == tracker


def test_checkpoint_round_trip():
    checkpoint = Checkpoint(
        id="ipl-2026__rcb-vs-csk__2026-06-19__live",
        topic_key="ipl-2026",
        event_label="RCB vs CSK",
        phase=f.CHECKPOINT_PHASE_LIVE,
        fire_at=_NOW,
        status=f.CHECKPOINT_STATUS_PENDING,
        attempts=1,
        claimed_at=_NOW,
        fired_at=_NOW,
        last_summary="RCB 110/3",
        last_fetch_tier=f.TIER_RSS,
        last_fetch_at=_NOW,
        last_error=None,
        created_at=_NOW,
    )
    restored = Checkpoint.from_dict(checkpoint.to_dict())
    assert restored == checkpoint


def test_defaults_survive_empty_dict():
    # A doc written by an older client (missing new fields) must read back as sane
    # defaults, never crash — the forward-compat contract for released app versions.
    topic = TrackedTopic.from_dict({f.TOPIC_KEY: "k"})
    assert topic.topic_key == "k"
    assert topic.status == f.TOPIC_STATUS_ACTIVE
    assert topic.subscriber_count == 0

    tracker = Tracker.from_dict({f.TRACKER_ID: "t", f.TRACKER_USER_ID: "u", f.TRACKER_TOPIC_KEY: "k"})
    assert tracker.status == f.TRACKER_STATUS_ACTIVE
    assert tracker.updates_sent == 0
