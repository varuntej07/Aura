"""Tracking engine fire path: the fixture/moment/fact-transition pipeline.

Monkeypatches the store I/O seams (tracking_store functions) and the fetch/extract/
compose seams (module-level functions in tracking_engine) so every branch of the
moment dispatcher is exercised without Firestore, an LLM call, or the orchestrator.

The properties under test are the incident fixes themselves:
  - a result push happens ONLY on a fact transition (reworded same-state abstains)
  - a raced transition sends exactly once (CAS loser abstains)
  - stale/wrong-fixture content re-arms instead of sending
  - pre/kickoff abstain when their usefulness window passed
  - the per-user daily cap holds the fan-out (wake_override results bypass)
  - legacy poll-grid docs expire on sight (safe cutover)
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import cast

from src.services.notifications.proposal import Disposition
from src.services.tracking import fields as f
from src.services.tracking import tracking_engine, tracking_store as store
from src.services.tracking.fact_gate import FactState, extract_transition
from src.services.tracking.models import Checkpoint, Fixture, TrackedTopic, Tracker
from src.services.tracking.moments import MAX_RESULT_CHECKS, RESULT_RECHECK_DELAY
from src.services.tracking.topic_fetcher import FetchResult

NOW = datetime(2026, 7, 10, 20, 30, tzinfo=UTC)  # inside the default 8-23 notify window
KICKOFF = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)
_UNUSED_MODELS = cast(tracking_engine.ModelProvider, None)

_BASE_TOPIC = TrackedTopic(
    topic_key="fifa-world-cup-2026", title="FIFA World Cup 2026",
    status=f.TOPIC_STATUS_ACTIVE, expires_at=None, timezone="UTC",
)
_BASE_FIXTURE = Fixture(
    id="20260710-1800", topic_key="fifa-world-cup-2026",
    label="Quarter-final: Spain vs Belgium",
    start_at=KICKOFF, expected_end_at=KICKOFF + timedelta(hours=2),
    status=f.FIXTURE_STATUS_LIVE,
)
_BASE_RESULT_CP = Checkpoint(
    id="fifa-world-cup-2026__20260710-1800__result",
    topic_key="fifa-world-cup-2026", event_label="Quarter-final: Spain vs Belgium",
    phase=f.CHECKPOINT_PHASE_RESULT, fire_at=NOW, fixture_id="20260710-1800",
)


def _topic(**overrides) -> TrackedTopic:
    return dataclasses.replace(_BASE_TOPIC, **overrides) if overrides else _BASE_TOPIC


def _fixture(**overrides) -> Fixture:
    return dataclasses.replace(_BASE_FIXTURE, **overrides) if overrides else _BASE_FIXTURE


def _checkpoint(**overrides) -> Checkpoint:
    return dataclasses.replace(_BASE_RESULT_CP, **overrides) if overrides else _BASE_RESULT_CP


class _Harness:
    """Collects every side effect the fire path produces."""

    def __init__(self):
        self.mark_calls: list[tuple[str, str, dict]] = []
        self.audits: list[dict] = []
        self.committed: list[FactState] = []
        self.pushes: list[dict] = []
        self.slot_claims: list[dict] = []


def _install(
    monkeypatch, harness: _Harness, *,
    topic: TrackedTopic, fixture: Fixture | None,
    commit_result: str | None = "unset",
    slot_available: bool = True,
    push_delivered: bool = True,
    subscribers: list[Tracker] | None = None,
):
    subs = subscribers if subscribers is not None else [
        Tracker(id="t1", user_id="u1", topic_key=topic.topic_key, status=f.TRACKER_STATUS_ACTIVE),
    ]

    async def _claim_checkpoint(cid):
        return True

    async def _get_tracked_topic(topic_key):
        return topic

    async def _get_fixture(topic_key, fixture_id):
        return fixture

    async def _list_active_subscribers(topic_key):
        return subs

    async def _mark_checkpoint(cid, status, **extra):
        harness.mark_calls.append((cid, status, extra))

    async def _record_fire_audit(topic_key, fixture_id, **kwargs):
        harness.audits.append({"topic_key": topic_key, "fixture_id": fixture_id, **kwargs})

    async def _commit_fact_transition(topic_key, fixture_id, seen, *, now):
        harness.committed.append(seen)
        if commit_result == "unset":
            # Default behavior: recompute like the real CAS would against `fixture`.
            prior = FactState(
                status=fixture.status, score=fixture.fact_score,
                winner=fixture.fact_winner, note=fixture.fact_note,
            )
            return extract_transition(prior, seen)
        return commit_result

    async def _try_claim_tracker_daily_slot(tracker_id, *, today, cap, force=False):
        harness.slot_claims.append({"tracker_id": tracker_id, "cap": cap, "force": force})
        return force or slot_available

    async def _record_tracker_outcome(tracker_id, *, summary, at):
        pass

    async def _update_topic_live_cache(topic_key, *, summary, fetched_at, tier):
        pass

    async def _update_tracked_topic(topic_key, updates):
        pass

    async def _rearm_pulse(cid, *, fire_at, tier, at, summary=None):
        harness.mark_calls.append((cid, "pulse_rearmed", {"fire_at": fire_at}))

    async def _send_tracker_push(**kwargs):
        harness.pushes.append(kwargs)

        class _Decision:
            disposition = Disposition.SEND
            delivered = push_delivered

        return _Decision()

    monkeypatch.setattr(store, "claim_checkpoint", _claim_checkpoint)
    monkeypatch.setattr(store, "get_tracked_topic", _get_tracked_topic)
    monkeypatch.setattr(store, "get_fixture", _get_fixture)
    monkeypatch.setattr(store, "list_active_subscribers", _list_active_subscribers)
    monkeypatch.setattr(store, "mark_checkpoint", _mark_checkpoint)
    monkeypatch.setattr(store, "record_fire_audit", _record_fire_audit)
    monkeypatch.setattr(store, "commit_fact_transition", _commit_fact_transition)
    monkeypatch.setattr(store, "try_claim_tracker_daily_slot", _try_claim_tracker_daily_slot)
    monkeypatch.setattr(store, "record_tracker_outcome", _record_tracker_outcome)
    monkeypatch.setattr(store, "update_topic_live_cache", _update_topic_live_cache)
    monkeypatch.setattr(store, "update_tracked_topic", _update_tracked_topic)
    monkeypatch.setattr(store, "rearm_pulse", _rearm_pulse)
    monkeypatch.setattr(tracking_engine, "_send_tracker_push", _send_tracker_push)


def _patch_fetch(monkeypatch, *, ok=True, text="Spain beat Belgium 1-0, Merino scored late."):
    async def _fetch_topic(query, *, country=None, language=None, not_before=None, use_cache=True):
        if not ok:
            return FetchResult(text="", tier=f.TIER_NONE)
        return FetchResult(text=text, tier=f.TIER_RSS, latest_published=NOW - timedelta(minutes=10))

    monkeypatch.setattr(tracking_engine, "fetch_topic", _fetch_topic)


def _patch_extract(monkeypatch, *, refers=True, status=f.FIXTURE_STATUS_FINISHED,
                   score="1-0", winner="Spain", note=""):
    async def _extract(models, fixture, context, now):
        return tracking_engine._ExtractedFacts(
            refers_to_this_fixture=refers,
            facts=FactState(status=status, score=score, winner=winner, note=note),
        )

    monkeypatch.setattr(tracking_engine, "_extract_facts", _extract)


def _patch_compose(monkeypatch):
    async def _compose(*args, **kwargs):
        return tracking_engine._PushCopy(
            title="Spain advance!", body="Spain beat Belgium 1-0.",
            opening_chat_message="What a match!", summary="Spain beat Belgium 1-0",
        )

    monkeypatch.setattr(tracking_engine, "_compose_result_push", _compose)
    monkeypatch.setattr(tracking_engine, "_compose_fixture_moment_push", _compose)


def _statuses(harness: _Harness) -> list[str]:
    return [status for _, status, _ in harness.mark_calls]


# ── result moment ────────────────────────────────────────────────────────────
async def test_result_sends_on_fact_transition(monkeypatch):
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=_fixture())
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch)
    _patch_compose(monkeypatch)

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(), _UNUSED_MODELS, NOW, summary)

    assert summary.fired == 1 and summary.sent == 1
    assert len(harness.pushes) == 1
    # The dedup key derives from the DESTINATION state, never from wording.
    assert harness.pushes[0]["dedup_key"] == "tracker_fifa-world-cup-2026_20260710-1800_finished"
    assert f.CHECKPOINT_STATUS_FIRED in _statuses(harness)
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_SENT


async def test_result_abstains_when_facts_did_not_move(monkeypatch):
    # The core incident fix: a fetch that re-words the already-known state (fixture
    # already finished, article recaps it) must NOT send, ever.
    harness = _Harness()
    fixture = _fixture(status=f.FIXTURE_STATUS_FINISHED, fact_winner="Spain", fact_score="1-0")
    _install(monkeypatch, harness, topic=_topic(), fixture=fixture)
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch)  # extraction reports finished/Spain again, reworded

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(), _UNUSED_MODELS, NOW, summary)

    assert harness.pushes == []
    assert summary.sent == 0
    assert f.CHECKPOINT_STATUS_SKIPPED in _statuses(harness)


async def test_result_rearms_when_not_determinable(monkeypatch):
    # Match still live at the expected end: re-check later, spend no uncertainty
    # budget (a verified-live wait is correct, not uncertainty).
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=_fixture())
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch, status=f.FIXTURE_STATUS_LIVE, score="0-0", winner="")

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(), _UNUSED_MODELS, NOW, summary)

    assert harness.pushes == []
    assert summary.rearmed_result == 1
    cid, status, extra = harness.mark_calls[-1]
    assert status == f.CHECKPOINT_STATUS_PENDING
    assert extra[f.CHECKPOINT_FIRE_AT] == NOW + RESULT_RECHECK_DELAY
    assert f.CHECKPOINT_RESULT_CHECKS not in extra  # confirmed-live wait is free
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_REARMED


async def test_result_wrong_fixture_content_rearms_and_spends_budget(monkeypatch):
    # Coverage of a DIFFERENT match (yesterday's round of 16) must never compose as
    # this fixture's outcome — the "advancing to the quarter-final they were playing
    # in" incident push.
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=_fixture())
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch, refers=False)

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(), _UNUSED_MODELS, NOW, summary)

    assert harness.pushes == []
    assert summary.rearmed_result == 1
    _, status, extra = harness.mark_calls[-1]
    assert status == f.CHECKPOINT_STATUS_PENDING
    assert f.CHECKPOINT_RESULT_CHECKS in extra
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_ABSTAIN_WRONG_FIXTURE


async def test_result_fails_after_recheck_budget_spent(monkeypatch):
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=_fixture())
    _patch_fetch(monkeypatch, ok=False)

    summary = tracking_engine.CheckpointTickSummary()
    cp = _checkpoint(result_checks=MAX_RESULT_CHECKS)
    await tracking_engine._fire_checkpoint(cp, _UNUSED_MODELS, NOW, summary)

    assert summary.failed == 1
    assert f.CHECKPOINT_STATUS_FAILED in _statuses(harness)
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_FAILED_FETCH


async def test_result_race_loser_abstains(monkeypatch):
    # Two moments race on one outcome: the CAS loser (commit returns None) must not
    # send, even with a fully composed push in hand.
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=_fixture(), commit_result=None)
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch)
    _patch_compose(monkeypatch)

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(), _UNUSED_MODELS, NOW, summary)

    assert harness.pushes == []
    assert f.CHECKPOINT_STATUS_SKIPPED in _statuses(harness)
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_ABSTAIN_RACE_LOST


# ── pre / kickoff moments ────────────────────────────────────────────────────
async def test_pre_abstains_after_kickoff(monkeypatch):
    # "Kicks off soon!" delivered 40 minutes after kickoff was a real incident push.
    harness = _Harness()
    fixture = _fixture(status=f.FIXTURE_STATUS_SCHEDULED)
    _install(monkeypatch, harness, topic=_topic(), fixture=fixture)
    _patch_compose(monkeypatch)

    late_now = KICKOFF + timedelta(minutes=40)
    cp = _checkpoint(
        id="fifa-world-cup-2026__20260710-1800__pre", phase=f.CHECKPOINT_PHASE_PRE,
    )
    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(cp, _UNUSED_MODELS, late_now, summary)

    assert harness.pushes == []
    assert f.CHECKPOINT_STATUS_SKIPPED in _statuses(harness)
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_ABSTAIN_TOO_LATE


async def test_pre_sends_before_kickoff(monkeypatch):
    harness = _Harness()
    fixture = _fixture(status=f.FIXTURE_STATUS_SCHEDULED)
    _install(monkeypatch, harness, topic=_topic(), fixture=fixture)
    _patch_compose(monkeypatch)

    cp = _checkpoint(
        id="fifa-world-cup-2026__20260710-1800__pre", phase=f.CHECKPOINT_PHASE_PRE,
    )
    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(cp, _UNUSED_MODELS, KICKOFF - timedelta(minutes=30), summary)

    assert len(harness.pushes) == 1
    assert harness.pushes[0]["dedup_key"] == "tracker_fifa-world-cup-2026_20260710-1800_pre"
    assert summary.fired == 1


async def test_kickoff_sends_only_when_it_wins_the_live_transition(monkeypatch):
    # A fixture already known finished (raced result) must never get "it started!".
    harness = _Harness()
    fixture = _fixture(status=f.FIXTURE_STATUS_FINISHED)
    _install(monkeypatch, harness, topic=_topic(), fixture=fixture)  # CAS recomputes -> None
    _patch_compose(monkeypatch)

    cp = _checkpoint(
        id="fifa-world-cup-2026__20260710-1800__kickoff", phase=f.CHECKPOINT_PHASE_KICKOFF,
    )
    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(cp, _UNUSED_MODELS, KICKOFF + timedelta(minutes=2), summary)

    assert harness.pushes == []
    assert harness.audits[-1]["decision"] == f.AUDIT_DECISION_ABSTAIN_RACE_LOST


# ── daily cap ────────────────────────────────────────────────────────────────
async def test_daily_cap_holds_fan_out(monkeypatch):
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=_fixture(), slot_available=False)
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch)
    _patch_compose(monkeypatch)

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(), _UNUSED_MODELS, NOW, summary)

    assert harness.pushes == []           # capped before the orchestrator
    assert summary.skipped_cap == 1
    assert summary.sent == 0
    assert harness.slot_claims[0]["cap"] == tracking_engine.TRACKER_DAILY_SEND_CAP


async def test_wake_override_result_bypasses_cap(monkeypatch):
    harness = _Harness()
    fixture = _fixture(wake_override=True)
    _install(monkeypatch, harness, topic=_topic(), fixture=fixture, slot_available=False)
    _patch_fetch(monkeypatch)
    _patch_extract(monkeypatch)
    _patch_compose(monkeypatch)

    summary = tracking_engine.CheckpointTickSummary()
    await tracking_engine._fire_checkpoint(_checkpoint(wake_override=True), _UNUSED_MODELS, NOW, summary)

    assert len(harness.pushes) == 1       # a final's result must land
    assert harness.slot_claims[0]["force"] is True


# ── legacy drain (the safe-cutover guarantee) ────────────────────────────────
async def test_legacy_poll_grid_docs_expire_on_sight(monkeypatch):
    harness = _Harness()
    _install(monkeypatch, harness, topic=_topic(), fixture=None)

    summary = tracking_engine.CheckpointTickSummary()
    for phase in (f.CHECKPOINT_PHASE_LIVE, f.CHECKPOINT_PHASE_POST, f.CHECKPOINT_PHASE_MILESTONE):
        await tracking_engine._fire_checkpoint(
            _checkpoint(id=f"legacy__{phase}", phase=phase, fixture_id=""),
            _UNUSED_MODELS, NOW, summary,
        )
    # An old-era "pre" with no fixture binding is legacy too.
    await tracking_engine._fire_checkpoint(
        _checkpoint(id="legacy__pre", phase=f.CHECKPOINT_PHASE_PRE, fixture_id=""),
        _UNUSED_MODELS, NOW, summary,
    )

    assert summary.expired_legacy == 4
    assert all(status == f.CHECKPOINT_STATUS_EXPIRED for _, status, _ in harness.mark_calls)


# ── parse helpers ────────────────────────────────────────────────────────────
def test_parse_push_copy_rejects_missing_title_or_body():
    assert tracking_engine._parse_push_copy('{"title":"t"}') is None
    assert tracking_engine._parse_push_copy("not json") is None
    parsed = tracking_engine._parse_push_copy(
        '{"title":"T","body":"B","opening_chat_message":"O","summary":"S"}'
    )
    assert parsed is not None and parsed.summary == "S"


def test_parse_json_object_strips_fences():
    parsed = tracking_engine._parse_json_object('```json\n{"a": 1}\n```')
    assert parsed == {"a": 1}
