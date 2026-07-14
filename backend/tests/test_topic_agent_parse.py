"""Research-parse coverage for topic_agent: tolerant JSON parsing (fences, stray
text, truncation repair), past/undated fixture filtering, kind/locale/cadence-window
normalization. Relocated from the retired test_tracking_schedule.py when the
poll-grid schedule builder was deleted (2026-07-11 cleanup).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services.tracking import fields as f
from src.services.tracking.topic_agent import (
    _loads_tolerant,
    _parse_research,
    _repair_truncated_json,
)


def test_parse_research_filters_past_fixtures_and_slugifies():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        "here you go ```json\n"
        '{"topic_key":"World Cup 2026!!","title":"World Cup","kind":"bounded_event",'
        '"research_query":"world cup","end_condition":"final played","starts_at":null,'
        '"ends_at":"2026-07-19T00:00:00+00:00","timezone":"UTC","confidence":0.8,'
        '"fixtures":[{"label":"A vs B","start_at":"2026-06-19T17:00:00+00:00"},'
        '{"label":"already played","start_at":"2026-06-01T00:00:00+00:00"}]}\n``` done'
    )
    research = _parse_research(text, now=now, request="world cup")
    assert research is not None
    assert research.topic_key == "world-cup-2026"     # slugified, punctuation stripped
    assert research.kind == f.TOPIC_KIND_BOUNDED_EVENT
    assert len(research.fixtures) == 1                # the long-past fixture is filtered out
    assert research.fixtures[0].label == "A vs B"


def test_parse_research_returns_none_on_garbage():
    assert _parse_research("not json at all", now=datetime(2026, 6, 15, tzinfo=UTC), request="x") is None


def test_parse_research_keeps_fixture_beyond_old_horizon():
    # Regression (Fed decision silence): the only dated fixture is ~5 weeks out — past the
    # old 21-day cutoff that silently discarded it. It must survive (kept nearest-first).
    now = datetime(2026, 6, 15, tzinfo=UTC)
    far = (now + timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    text = (
        '{"topic_key":"fomc","title":"FOMC","kind":"recurring_season","research_query":"fomc",'
        '"end_condition":"meetings stop","timezone":"America/New_York","confidence":0.9,'
        f'"fixtures":[{{"label":"FOMC rate decision","kind":"point","start_at":"{far}"}}]}}'
    )
    research = _parse_research(text, now=now, request="fed decision")
    assert research is not None
    assert len(research.fixtures) == 1
    assert research.fixtures[0].label == "FOMC rate decision"


def test_repair_truncated_json_closes_a_cut_off_fixtures_array():
    # The model overran its token budget mid-fixtures-list. Repair recovers the fields +
    # the complete fixtures that DID arrive instead of discarding the whole pass.
    truncated = (
        '{"topic_key":"wc","title":"WC","kind":"bounded_event","research_query":"wc",'
        '"timezone":"UTC","confidence":0.8,"fixtures":['
        '{"label":"A vs B","start_at":"2026-06-19T17:00:00+00:00"},'
        '{"label":"C vs D","start_at":"2026-06-19T20:00:00+00:00"},'
        '{"label":"E vs F","start_at":"2026-06-1'  # cut off mid-object
    )
    repaired = _repair_truncated_json(truncated)
    assert repaired is not None
    data = _loads_tolerant(truncated)
    assert isinstance(data, dict)
    assert data["topic_key"] == "wc"
    # The two COMPLETE fixtures (those that kept their start_at) are recovered intact;
    # the partial third lacks a date, so the coercion layer drops it.
    complete = [fx["label"] for fx in data["fixtures"] if fx.get("start_at")]
    assert complete == ["A vs B", "C vs D"]


def test_parse_research_recovers_from_truncated_output():
    # End-to-end: a truncated grounded payload still yields a usable TopicResearch with
    # the fixtures that arrived, rather than None (which would fail the whole setup).
    now = datetime(2026, 6, 15, tzinfo=UTC)
    truncated = (
        '{"topic_key":"wc","title":"World Cup","kind":"bounded_event","research_query":"world cup",'
        '"end_condition":"final","timezone":"UTC","confidence":0.8,"fixtures":['
        '{"label":"A vs B","start_at":"2026-06-19T17:00:00+00:00"},'
        '{"label":"C vs D","start_at":"2026-06-20T20:00'  # cut off
    )
    research = _parse_research(truncated, now=now, request="world cup")
    assert research is not None
    assert research.topic_key == "wc"
    assert [fx.label for fx in research.fixtures] == ["A vs B"]  # only the complete fixture survives


def test_parse_research_reads_point_fixture_kind():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"gta-6","title":"GTA 6","kind":"open_interest",'
        '"research_query":"gta 6 release date","end_condition":"released",'
        '"timezone":"UTC","confidence":0.6,'
        '"fixtures":[{"label":"GTA 6 release","kind":"point",'
        '"start_at":"2026-06-19T17:00:00+00:00"}]}'
    )
    research = _parse_research(text, now=now, request="gta 6")
    assert research is not None
    assert len(research.fixtures) == 1
    assert research.fixtures[0].event_kind == f.EVENT_KIND_POINT


def test_parse_research_tolerates_legacy_events_key():
    # A model that still answers with the pre-redesign "events" key parses identically.
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"wc","title":"WC","kind":"bounded_event","research_query":"wc",'
        '"timezone":"UTC","confidence":0.5,'
        '"events":[{"label":"A vs B","start_at":"2026-06-19T19:00:00+00:00"}]}'
    )
    research = _parse_research(text, now=now, request="wc")
    assert research is not None
    assert [fx.label for fx in research.fixtures] == ["A vs B"]


def test_parse_research_reads_and_normalizes_locale():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"pushpa-2","title":"Pushpa 2","kind":"open_interest",'
        '"research_query":"pushpa 2 release","end_condition":"released",'
        '"timezone":"Asia/Kolkata","country":"in","language":"TE","confidence":0.6,"fixtures":[]}'
    )
    research = _parse_research(text, now=now, request="pushpa 2")
    assert research is not None
    assert research.country == "IN"   # upper-cased ISO country
    assert research.language == "te"  # lower-cased ISO language


def test_parse_research_reads_cadence_window_and_fixture_fields():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"wc","title":"WC","kind":"bounded_event","research_query":"wc",'
        '"end_condition":"final","timezone":"UTC","confidence":0.7,'
        '"idle_poll_minutes":240,"notify_start_hour":9,"notify_end_hour":22,"awaiting_date":false,'
        '"fixtures":[{"label":"A vs B","kind":"span","start_at":"2026-06-19T19:00:00+00:00",'
        '"end_at":"2026-06-19T21:00:00+00:00","lead_minutes":60,'
        '"wake_override":true,"fixture_id":"20260619-1900"}]}'
    )
    research = _parse_research(text, now=now, request="wc")
    assert research is not None
    assert research.idle_poll_minutes == 240
    assert research.notify_start_hour == 9 and research.notify_end_hour == 22
    assert research.awaiting_date is False
    fx = research.fixtures[0]
    assert fx.lead_minutes == 60 and fx.wake_override is True
    assert fx.echoed_fixture_id == "20260619-1900"


def test_parse_research_clamps_cadence_and_rejects_bad_hours():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"wc","title":"WC","kind":"bounded_event","research_query":"wc",'
        '"timezone":"UTC","confidence":0.5,'
        '"idle_poll_minutes":10,"notify_start_hour":99,"notify_end_hour":-3,'
        '"fixtures":[{"label":"A vs B","kind":"span","start_at":"2026-06-19T19:00:00+00:00"}]}'
    )
    research = _parse_research(text, now=now, request="wc")
    assert research is not None
    assert research.idle_poll_minutes == 60      # floored from 10 -> 1h minimum
    assert research.notify_start_hour == -1      # 99 is out of 0..23 -> unset (caller defaults)
    assert research.notify_end_hour == -1        # -3 likewise
    assert len(research.fixtures) == 1


def test_parse_research_fields_default_when_absent():
    now = datetime(2026, 6, 15, tzinfo=UTC)
    text = (
        '{"topic_key":"x","title":"X","kind":"open_interest","research_query":"x",'
        '"timezone":"UTC","confidence":0.5,'
        '"fixtures":[{"label":"A vs B","start_at":"2026-06-19T19:00:00+00:00"}]}'
    )
    research = _parse_research(text, now=now, request="x")
    assert research is not None
    assert research.idle_poll_minutes == 0       # absent -> engine default
    assert research.notify_start_hour == -1 and research.notify_end_hour == -1
    assert research.awaiting_date is False
    assert research.fixtures[0].lead_minutes == 0
    assert research.fixtures[0].wake_override is False
    assert research.fixtures[0].echoed_fixture_id == ""
