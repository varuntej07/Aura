"""Coverage for the schedule builder and the research parser.

Pins: a dated event materializes pre/live/post checkpoints; a phase already in the
past is not enqueued; a fresh research pass expires checkpoints for events that fell
off the schedule; and the agent's JSON parse is tolerant (fences/stray text) while
filtering out past/undated events.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.tracking import fields as f
from src.services.tracking import schedule_builder as sb
from src.services.tracking.models import Checkpoint, ScheduledEvent, TopicResearch
from src.services.tracking.topic_agent import _parse_research


def _research_one_event(start: datetime) -> TopicResearch:
    return TopicResearch(
        topic_key="k", title="t", kind=f.TOPIC_KIND_BOUNDED_EVENT, research_query="q",
        events=[ScheduledEvent(label="A vs B", start_at=start)],
    )


def test_build_checkpoints_three_phases():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    cps = sb.build_checkpoints(_research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC)), topic_key="k", now=now)
    assert sorted(c.phase for c in cps) == [
        f.CHECKPOINT_PHASE_LIVE, f.CHECKPOINT_PHASE_POST, f.CHECKPOINT_PHASE_PRE,
    ]
    # pre fires 2h before kickoff, post 2.5h after.
    by_phase = {c.phase: c.fire_at for c in cps}
    assert by_phase[f.CHECKPOINT_PHASE_PRE] == datetime(2026, 6, 19, 15, 0, tzinfo=UTC)
    assert by_phase[f.CHECKPOINT_PHASE_POST] == datetime(2026, 6, 19, 19, 30, tzinfo=UTC)


def test_build_checkpoints_skips_past_phase():
    # now is 30 min before kickoff -> the pre checkpoint (15:00) is already in the past.
    now = datetime(2026, 6, 19, 16, 30, tzinfo=UTC)
    cps = sb.build_checkpoints(_research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC)), topic_key="k", now=now)
    phases = {c.phase for c in cps}
    assert f.CHECKPOINT_PHASE_PRE not in phases
    assert f.CHECKPOINT_PHASE_LIVE in phases


def test_plan_reconcile_expires_dropped_event():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    research = _research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC))
    stale = Checkpoint(
        id="k__cancelled__2026-06-10__live", topic_key="k", event_label="cancelled",
        phase=f.CHECKPOINT_PHASE_LIVE, fire_at=datetime(2026, 6, 20, tzinfo=UTC),
        status=f.CHECKPOINT_STATUS_PENDING,
    )
    upserts, expire_ids = sb.plan_reconcile(research, [stale], topic_key="k", now=now)
    assert "k__cancelled__2026-06-10__live" in expire_ids
    assert len(upserts) == 3  # pre/live/post for the surviving event


def test_build_checkpoints_keys_to_topic_doc_not_research_slug():
    # Regression (2026-06-16 World Cup outage): provision-time research timed out, so the
    # topic doc + the user's subscription were created under a request-derived slug; the
    # later reconcile's research returned its OWN clean slug. Checkpoints must be keyed to
    # the TOPIC DOC, never research.topic_key — else every checkpoint fires, finds no doc
    # at research.topic_key, and silently self-expires (the user gets zero notifications).
    now = datetime(2026, 6, 15, tzinfo=UTC)
    research = _research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC))
    assert research.topic_key == "k"  # the research's own slug...
    doc_key = "fifa-world-cup-2026-keep-me-posted-until-t"  # ...differs from the doc key
    cps = sb.build_checkpoints(research, topic_key=doc_key, now=now)
    assert cps  # events materialized
    assert all(c.topic_key == doc_key for c in cps)
    assert all(c.id.startswith(f"{doc_key}__") for c in cps)
    assert all("__k__" not in c.id and not c.id.startswith("k__") for c in cps)


def test_plan_reconcile_diffs_against_doc_keyed_existing():
    # With the doc key threaded through, a reconcile whose research slug differs from the
    # doc still upserts under the doc key and diffs correctly against doc-keyed existing
    # checkpoints (a dropped event under the doc key is expired, not orphaned under a
    # second key).
    now = datetime(2026, 6, 15, tzinfo=UTC)
    research = _research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC))
    doc_key = "fifa-world-cup-2026-keep-me-posted-until-t"
    stale = Checkpoint(
        id=f"{doc_key}__cancelled__2026-06-10__live", topic_key=doc_key, event_label="cancelled",
        phase=f.CHECKPOINT_PHASE_LIVE, fire_at=datetime(2026, 6, 20, tzinfo=UTC),
        status=f.CHECKPOINT_STATUS_PENDING,
    )
    upserts, expire_ids = sb.plan_reconcile(research, [stale], topic_key=doc_key, now=now)
    assert f"{doc_key}__cancelled__2026-06-10__live" in expire_ids
    assert upserts and all(c.topic_key == doc_key for c in upserts)


def test_plan_reconcile_keeps_fired_checkpoints():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    research = _research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC))
    fired = Checkpoint(
        id="k__old__2026-06-10__post", topic_key="k", event_label="old",
        phase=f.CHECKPOINT_PHASE_POST, fire_at=datetime(2026, 6, 10, tzinfo=UTC),
        status=f.CHECKPOINT_STATUS_FIRED,
    )
    _, expire_ids = sb.plan_reconcile(research, [fired], topic_key="k", now=now)
    # An already-fired checkpoint is never re-expired.
    assert fired.id not in expire_ids


def test_parse_research_filters_past_events_and_slugifies():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        "here you go ```json\n"
        '{"topic_key":"World Cup 2026!!","title":"World Cup","kind":"bounded_event",'
        '"research_query":"world cup","end_condition":"final played","starts_at":null,'
        '"ends_at":"2026-07-19T00:00:00+00:00","timezone":"UTC","confidence":0.8,'
        '"events":[{"label":"A vs B","start_at":"2026-06-19T17:00:00+00:00"},'
        '{"label":"already played","start_at":"2026-06-01T00:00:00+00:00"}]}\n``` done'
    )
    research = _parse_research(text, now=now, request="world cup")
    assert research is not None
    assert research.topic_key == "world-cup-2026"     # slugified, punctuation stripped
    assert research.kind == f.TOPIC_KIND_BOUNDED_EVENT
    assert len(research.events) == 1                  # the past event is filtered out
    assert research.events[0].label == "A vs B"


def test_parse_research_returns_none_on_garbage():
    assert _parse_research("not json at all", now=datetime(2026, 6, 15, tzinfo=UTC), request="x") is None


# ── pulse (recurring heartbeat for open-ended topics) ─────────────────────────
def test_next_pulse_interval_tightens_when_new_loosens_when_not():
    base = sb.PULSE_INTERVAL_INITIAL_S
    # Found something new -> poll sooner (halve); nothing new -> back off (x1.5).
    assert sb.next_pulse_interval(base, found_new=True) == int(base * 0.5)
    assert sb.next_pulse_interval(base, found_new=False) == int(base * 1.5)


def test_next_pulse_interval_clamps_to_min_and_max():
    assert sb.next_pulse_interval(sb.PULSE_INTERVAL_MIN_S, found_new=True) == sb.PULSE_INTERVAL_MIN_S
    assert sb.next_pulse_interval(sb.PULSE_INTERVAL_MAX_S, found_new=False) == sb.PULSE_INTERVAL_MAX_S


def test_next_pulse_interval_zero_starts_from_initial():
    # A topic written before the field existed (0) gets a sane first cadence, not 0.
    assert sb.next_pulse_interval(0, found_new=True) == int(sb.PULSE_INTERVAL_INITIAL_S * 0.5)


def test_build_pulse_checkpoint_shape():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    fire_at = datetime(2026, 6, 15, 6, tzinfo=UTC)
    cp = sb.build_pulse_checkpoint("gta-6", fire_at=fire_at, now=now)
    assert cp.id == "gta-6__pulse"
    assert cp.phase == f.CHECKPOINT_PHASE_PULSE
    assert cp.fire_at == fire_at
    assert cp.status == f.CHECKPOINT_STATUS_PENDING


def test_plan_reconcile_never_expires_pulse():
    # The recurring pulse is not event-derived, so it is never in the fresh set; it must
    # survive reconcile or an ongoing topic's heartbeat would die on the first re-research.
    now = datetime(2026, 6, 15, tzinfo=UTC)
    research = _research_one_event(datetime(2026, 6, 19, 17, tzinfo=UTC))
    pulse = Checkpoint(
        id="k__pulse", topic_key="k", event_label="",
        phase=f.CHECKPOINT_PHASE_PULSE, fire_at=datetime(2026, 6, 15, 6, tzinfo=UTC),
        status=f.CHECKPOINT_STATUS_PENDING,
    )
    _, expire_ids = sb.plan_reconcile(research, [pulse], topic_key="k", now=now)
    assert "k__pulse" not in expire_ids


# ── point vs span event shape ─────────────────────────────────────────────────
def test_point_event_builds_pre_and_milestone_only():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    research = TopicResearch(
        topic_key="k", title="t", kind=f.TOPIC_KIND_BOUNDED_EVENT, research_query="q",
        events=[ScheduledEvent(
            label="GTA 6 release", start_at=datetime(2026, 6, 19, 17, tzinfo=UTC),
            event_kind=f.EVENT_KIND_POINT,
        )],
    )
    phases = {c.phase for c in sb.build_checkpoints(research, topic_key="k", now=now)}
    assert phases == {f.CHECKPOINT_PHASE_PRE, f.CHECKPOINT_PHASE_MILESTONE}
    assert f.CHECKPOINT_PHASE_LIVE not in phases
    assert f.CHECKPOINT_PHASE_POST not in phases


def test_parse_research_reads_point_event_kind():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"gta-6","title":"GTA 6","kind":"open_interest",'
        '"research_query":"gta 6 release date","end_condition":"released",'
        '"timezone":"UTC","confidence":0.6,'
        '"events":[{"label":"GTA 6 release","kind":"point",'
        '"start_at":"2026-06-19T17:00:00+00:00"}]}'
    )
    research = _parse_research(text, now=now, request="gta 6")
    assert research is not None
    assert len(research.events) == 1
    assert research.events[0].event_kind == f.EVENT_KIND_POINT
    assert research.events[0].phases == [f.CHECKPOINT_PHASE_PRE, f.CHECKPOINT_PHASE_MILESTONE]


def test_parse_research_reads_and_normalizes_locale():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"pushpa-2","title":"Pushpa 2","kind":"open_interest",'
        '"research_query":"pushpa 2 release","end_condition":"released",'
        '"timezone":"Asia/Kolkata","country":"in","language":"TE","confidence":0.6,"events":[]}'
    )
    research = _parse_research(text, now=now, request="pushpa 2")
    assert research is not None
    assert research.country == "IN"   # upper-cased ISO country
    assert research.language == "te"  # lower-cased ISO language


# ── fetch query construction (2026-06-19 World Cup "fired but composer abstained") ─
def test_clean_topic_descriptor_strips_trailing_request_clause():
    # The exact stored query that made every WC checkpoint fetch a generic jumble.
    raw = "FIFA World Cup 2026 - keep me posted on all results, scores, and key updates until the tournament ends"
    assert sb.clean_topic_descriptor(raw) == "FIFA World Cup 2026"


def test_clean_topic_descriptor_strips_leading_request_verb():
    assert sb.clean_topic_descriptor("let me know when GRRM releases Winds of Winter") == \
        "GRRM releases Winds of Winter"
    assert sb.clean_topic_descriptor("keep me updated on the 2026 US midterms") == "2026 US midterms"
    assert sb.clean_topic_descriptor("notify me about the next Fed interest rate decision") == \
        "next Fed interest rate decision"


def test_clean_topic_descriptor_leaves_a_clean_subject_untouched():
    # A query that is already just the subject is returned unchanged (idempotent).
    assert sb.clean_topic_descriptor("Tesla stock and product launches") == "Tesla stock and product launches"
    assert sb.clean_topic_descriptor("major earthquake near Tokyo") == "major earthquake near Tokyo"


def test_clean_topic_descriptor_never_empties():
    # If the heuristics would consume everything, the original survives (never a blank query).
    assert sb.clean_topic_descriptor("keep me posted") == "keep me posted"
    assert sb.clean_topic_descriptor("   ") == ""


def test_build_fetch_query_event_checkpoint_anchors_specific_beat():
    # The fix: an event checkpoint searches its OWN match, anchored by the clean topic,
    # instead of the verbose topic sentence every checkpoint used to share.
    q = sb.build_fetch_query(
        event_label="United States vs. Australia",
        research_query="FIFA World Cup 2026 - keep me posted on all results until the tournament ends",
        title="FIFA World Cup 2026",
    )
    assert q == "United States vs. Australia FIFA World Cup 2026"


def test_build_fetch_query_pulse_uses_clean_topic_query():
    # A pulse / no-label checkpoint has no specific beat -> the cleaned topic query.
    q = sb.build_fetch_query(
        event_label="",
        research_query="let me know when GRRM releases Winds of Winter",
        title="Winds of Winter",
    )
    assert q == "GRRM releases Winds of Winter"


def test_build_fetch_query_falls_back_to_title_when_no_query():
    q = sb.build_fetch_query(event_label="", research_query="", title="Tesla news")
    assert q == "Tesla news"
