"""fixture_matcher: stable identity under reconcile rewording.

The acid test reproduces the 2026-07-10 incident: the SAME real match appeared in
prod under four differently-worded labels across daily reconciles, and each minted a
parallel checkpoint series (4+ series, 1,190 docs, 19 pushes). Here, all four
wordings MUST resolve to the one stored fixture, in any order, with zero creates.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from src.services.tracking import fields as f
from src.services.tracking.fixture_matcher import (
    DEFAULT_SPAN_DURATION,
    POINT_RESULT_LAG,
    FixturePlan,
    expected_end_of,
    mint_fixture_id,
    reconcile_fixtures,
)
from src.services.tracking.models import Fixture, ResearchedFixture

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
_KICKOFF = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
_TOPIC = "fifa-world-cup-2026"


def _stored(**overrides) -> Fixture:
    base = Fixture(
        id="20260710-1800",
        topic_key=_TOPIC,
        label="Quarterfinal 3",
        start_at=_KICKOFF,
        expected_end_at=_KICKOFF + DEFAULT_SPAN_DURATION,
        kind=f.EVENT_KIND_SPAN,
        created_at=_NOW,
        updated_at=_NOW,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _fresh(label: str, *, start_at: datetime | None = None, **overrides) -> ResearchedFixture:
    return ResearchedFixture(label=label, start_at=start_at or _KICKOFF, **overrides)


# ── minting ──────────────────────────────────────────────────────────────────
def test_mint_is_slot_based_never_label_based():
    assert mint_fixture_id(_KICKOFF, set()) == "20260710-1800"


def test_mint_suffixes_on_slot_collision_deterministically():
    taken = {"20260710-1800"}
    first = mint_fixture_id(_KICKOFF, taken)
    assert first == "20260710-1800-a"
    taken.add(first)
    assert mint_fixture_id(_KICKOFF, taken) == "20260710-1800-b"


# ── the incident acid test ───────────────────────────────────────────────────
_INCIDENT_LABELS = [
    "Quarterfinal 3",
    "Quarter-final - Match 98",
    "Quarter-final: Portugal/Spain Winner vs USA/Belgium Winner",
    "Quarter-final: Spain vs Belgium",
]


def test_all_four_incident_labels_resolve_to_one_fixture():
    # Each reconcile pass rewords the label; every wording must UPDATE the same
    # stored fixture, never create a sibling. The stored label evolves pass to pass
    # (placeholder labels widen the match window), exactly like prod would.
    stored = [_stored(label=_INCIDENT_LABELS[0])]
    for reworded in _INCIDENT_LABELS[1:]:
        plan = reconcile_fixtures(stored, [_fresh(reworded)], topic_key=_TOPIC, now=_NOW)
        assert plan.creates == [], f"'{reworded}' forked a new fixture"
        assert len(plan.updates) == 1
        assert plan.updates[0].id == "20260710-1800"
        assert plan.updates[0].label == reworded
        stored = plan.updates


def test_echoed_fixture_id_wins_even_when_time_shifted():
    # The reconcile LLM is shown stored fixtures and echoes the id it recognizes.
    # Trust the echo even when the fresh start moved beyond the time window (a
    # postponed match): identity survives the reschedule.
    moved = _KICKOFF + timedelta(hours=26)
    plan = reconcile_fixtures(
        [_stored()],
        [_fresh("Spain vs Belgium (postponed)", start_at=moved, echoed_fixture_id="20260710-1800")],
        topic_key=_TOPIC,
        now=_NOW,
    )
    assert plan.creates == []
    assert plan.updates[0].id == "20260710-1800"
    assert plan.updates[0].start_at == moved


def test_placeholder_matches_through_widened_window_without_shared_tokens():
    # A bracket-slot placeholder's estimated kickoff was ~4h off in prod, and
    # "Portugal/Spain Winner vs USA/Belgium Winner" shares zero tokens with
    # "Spain vs Belgium" by construction.
    stored = _stored(label="Winner Match 87 vs Winner Match 88", start_at=_KICKOFF - timedelta(hours=4))
    plan = reconcile_fixtures([stored], [_fresh("Spain vs Belgium")], topic_key=_TOPIC, now=_NOW)
    assert plan.creates == []
    assert plan.updates[0].id == stored.id
    assert plan.updates[0].label == "Spain vs Belgium"


def test_unrelated_fixture_same_day_mints_its_own_id():
    # Two real fixtures hours apart with disjoint teams are NOT the same fixture.
    stored = _stored(id="20260710-1300", label="France vs Morocco", start_at=_KICKOFF - timedelta(hours=5))
    plan = reconcile_fixtures([stored], [_fresh("Spain vs Belgium")], topic_key=_TOPIC, now=_NOW)
    assert plan.updates == []
    assert len(plan.creates) == 1
    assert plan.creates[0].id == "20260710-1800"


def test_fact_state_survives_a_label_update():
    stored = _stored(
        status=f.FIXTURE_STATUS_FINISHED, fact_winner="Spain", fact_score="1-0",
        last_transition="live->finished",
    )
    plan = reconcile_fixtures([stored], [_fresh("Quarter-final: Spain vs Belgium")], topic_key=_TOPIC, now=_NOW)
    updated = plan.updates[0]
    assert updated.status == f.FIXTURE_STATUS_FINISHED
    assert updated.fact_winner == "Spain"
    assert updated.last_transition == "live->finished"


def test_wake_override_only_escalates():
    stored = _stored(wake_override=True)
    plan = reconcile_fixtures([stored], [_fresh("Spain vs Belgium", wake_override=False)], topic_key=_TOPIC, now=_NOW)
    assert plan.updates[0].wake_override is True


# ── cancellation conservatism ────────────────────────────────────────────────
def test_far_future_unmatched_fixture_is_cancelled_by_a_substantial_pass():
    far = _stored(id="20260716-2000", label="Semifinal 2", start_at=_NOW + timedelta(days=7))
    fresh = [_fresh("Spain vs Belgium"), _fresh("France vs Morocco", start_at=_KICKOFF - timedelta(hours=5))]
    plan = reconcile_fixtures([far], fresh, topic_key=_TOPIC, now=_NOW)
    assert plan.cancel_ids == ["20260716-2000"]


def test_imminent_or_started_fixtures_are_never_cancelled_by_absence():
    imminent = _stored(start_at=_NOW + timedelta(hours=6))
    live = _stored(id="20260709-1000", start_at=_NOW + timedelta(days=7), status=f.FIXTURE_STATUS_LIVE)
    fresh = [
        _fresh("Norway vs England", start_at=_NOW + timedelta(days=2)),
        _fresh("Argentina vs Switzerland", start_at=_NOW + timedelta(days=2, hours=4)),
    ]
    plan = reconcile_fixtures([imminent, live], fresh, topic_key=_TOPIC, now=_NOW)
    assert plan.cancel_ids == []


def test_thin_research_pass_never_cancels_anything():
    # An empty or one-line pass is "no information", not "the schedule vanished" —
    # the same never-nuke rule the old plan_reconcile learned from the World Cup
    # schedule collapse.
    far = _stored(start_at=_NOW + timedelta(days=7))
    assert reconcile_fixtures([far], [], topic_key=_TOPIC, now=_NOW) == FixturePlan()
    plan_one = reconcile_fixtures([far], [_fresh("Some Final", start_at=_NOW + timedelta(days=9))], topic_key=_TOPIC, now=_NOW)
    assert plan_one.cancel_ids == []


# ── expected end derivation ──────────────────────────────────────────────────
def test_expected_end_prefers_research_end_then_kind_default():
    explicit = _fresh("x", start_at=_KICKOFF)
    explicit.end_at = _KICKOFF + timedelta(hours=3)
    assert expected_end_of(explicit) == _KICKOFF + timedelta(hours=3)

    span = _fresh("x", start_at=_KICKOFF)
    assert expected_end_of(span) == _KICKOFF + DEFAULT_SPAN_DURATION

    point = _fresh("x", start_at=_KICKOFF, event_kind=f.EVENT_KIND_POINT)
    assert expected_end_of(point) == _KICKOFF + POINT_RESULT_LAG
