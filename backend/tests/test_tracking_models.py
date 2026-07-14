"""Writer->reader round-trip guard for the topic-tracking models.

Per CLAUDE.md's database-field discipline: a Firestore field name that exists on the
writer but not the reader (or vice versa) does NOT error, it silently returns the
default — which looks identical to "no data". These round-trips break CI the moment a
field name drifts on either side of to_dict / from_dict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.tracking import fields as f
from src.services.tracking.models import Checkpoint, Fixture, TrackedTopic, Tracker

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
        sent_today=2,
        sent_today_date="2026-06-15",
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
        fixture_id="20260619-1400",
        result_checks=2,
    )
    restored = Checkpoint.from_dict(checkpoint.to_dict())
    assert restored == checkpoint


def test_fixture_round_trip():
    fixture = Fixture(
        id="20260710-1800",
        topic_key="fifa-world-cup-2026",
        label="Quarter-final: Spain vs Belgium",
        start_at=_NOW,
        expected_end_at=_NOW,
        kind=f.EVENT_KIND_SPAN,
        lead_minutes=45,
        wake_override=True,
        status=f.FIXTURE_STATUS_FINISHED,
        fact_score="1-0",
        fact_winner="Spain",
        fact_note="Merino scored the decisive goal",
        facts_updated_at=_NOW,
        last_transition="live->finished",
        created_at=_NOW,
        updated_at=_NOW,
    )
    restored = Fixture.from_dict(fixture.to_dict())
    assert restored == fixture


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
    assert tracker.sent_today == 0
    assert tracker.sent_today_date == ""

    # Pre-migration checkpoint docs carry no fixture binding — they must read back as
    # the legacy discriminator values (empty fixture_id), never crash.
    checkpoint = Checkpoint.from_dict({
        f.CHECKPOINT_ID: "c", f.CHECKPOINT_TOPIC_KEY: "k",
        f.CHECKPOINT_EVENT_LABEL: "e", f.CHECKPOINT_PHASE: f.CHECKPOINT_PHASE_LIVE,
        f.CHECKPOINT_FIRE_AT: _NOW,
    })
    assert checkpoint.fixture_id == ""
    assert checkpoint.result_checks == 0

    fixture = Fixture.from_dict({f.FIXTURE_ID: "x", f.FIXTURE_TOPIC_KEY: "k", f.FIXTURE_START_AT: _NOW})
    assert fixture.status == f.FIXTURE_STATUS_SCHEDULED
    assert fixture.fact_winner == ""
    assert fixture.last_transition == ""
