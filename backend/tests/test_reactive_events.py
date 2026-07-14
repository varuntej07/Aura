"""Event taxonomy contract + the forward-compatible Event model.

The event type strings are a wire contract: a durable event written by one Cloud
Run revision must still be consumed by the next (deploy.sh shifts 100% of traffic
at once). These tests pin the names (CI-breaking on a rename) and prove from_dict
tolerates an unknown future field and supplies defaults for a missing one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.services.reactive import events as ev
from src.services.reactive import fields as F
from src.services.reactive.event_bus import build_event


# ── Type-name contract (rename -> CI break) ──────────────────────────────────
def test_event_type_constants_are_pinned():
    assert ev.EVENT_APP_OPENED == "app_opened"
    assert ev.EVENT_NOTIFICATION_TAPPED == "notification_tapped"
    assert ev.EVENT_DISMISS_STREAK == "dismiss_streak"
    assert ev.EVENT_MESSAGE_SENT == "message_sent"
    assert ev.EVENT_LIFE_UPDATE == "life_update"
    assert ev.EVENT_REMINDER_CREATED == "reminder_created"
    assert ev.EVENT_TRACKING_UPDATE == "tracking_update"
    assert ev.EVENT_TICK == "tick"
    assert ev.EVENT_INTENT_DUE == "intent_due"
    assert ev.EVENT_USER_DORMANT == "user_dormant"


def test_families_partition_all_event_types_without_overlap():
    families = [
        ev.BEHAVIORAL_EVENTS,
        ev.CONVERSATIONAL_EVENTS,
        ev.DOMAIN_EVENTS,
        ev.TEMPORAL_EVENTS,
        ev.LIFECYCLE_EVENTS,
    ]
    total = sum(len(fam) for fam in families)
    # No event appears in two families (sum of sizes == size of the union).
    assert total == len(ev.ALL_EVENT_TYPES)
    for fam in families:
        assert fam <= ev.ALL_EVENT_TYPES


def test_presence_events_are_registered():
    assert ev.PRESENCE_EVENTS <= ev.ALL_EVENT_TYPES


def test_is_known_event_type():
    assert ev.is_known_event_type(ev.EVENT_APP_OPENED)
    assert not ev.is_known_event_type("definitely_not_an_event")


# ── Round-trip ───────────────────────────────────────────────────────────────
def test_event_roundtrips_through_dict():
    e = build_event("u1", ev.EVENT_MESSAGE_SENT, {"text": "hi"}, source="chat")
    back = ev.Event.from_dict(e.to_dict())
    assert back.uid == "u1"
    assert back.type == ev.EVENT_MESSAGE_SENT
    assert back.payload == {"text": "hi"}
    assert back.source == "chat"
    assert back.event_id == e.event_id
    assert back.dedup_id == e.dedup_id
    assert back.schema_version == ev.SCHEMA_VERSION


def test_dedup_id_defaults_to_event_id_but_is_overridable():
    e = ev.Event(uid="u1", type=ev.EVENT_TICK)
    assert e.dedup_id == e.event_id
    shared = ev.Event(uid="u1", type=ev.EVENT_TICK, dedup_id="shared")
    assert shared.dedup_id == "shared"


# ── Forward / backward compatibility (the deploy-safety requirement) ─────────
def test_from_dict_ignores_unknown_future_fields():
    """A v(N+1) writer added a field this reader doesn't know — must not crash."""
    doc = {
        F.FIELD_EVENT_ID: "e1",
        F.FIELD_UID: "u1",
        F.FIELD_TYPE: ev.EVENT_APP_OPENED,
        F.FIELD_PAYLOAD: {"k": "v"},
        F.FIELD_SOURCE: "client",
        F.FIELD_TS: datetime(2026, 6, 29, tzinfo=UTC),
        F.FIELD_SCHEMA_VERSION: 99,
        F.FIELD_DEDUP_ID: "e1",
        "future_only_field": {"surprise": True},
    }
    e = ev.Event.from_dict(doc)
    assert e.type == ev.EVENT_APP_OPENED
    assert e.schema_version == 99
    assert e.payload == {"k": "v"}


def test_from_dict_defaults_missing_new_fields():
    """An older writer omitted schema_version and dedup_id."""
    doc = {F.FIELD_EVENT_ID: "e9", F.FIELD_UID: "u1", F.FIELD_TYPE: ev.EVENT_TICK}
    e = ev.Event.from_dict(doc)
    assert e.schema_version == 1            # default for a pre-versioning doc
    assert e.dedup_id == "e9"              # falls back to event_id
    assert e.payload == {}


def test_from_dict_coerces_iso_string_timestamp():
    doc = {
        F.FIELD_UID: "u1",
        F.FIELD_TYPE: ev.EVENT_TICK,
        F.FIELD_TS: "2026-06-29T10:00:00+00:00",
    }
    e = ev.Event.from_dict(doc)
    assert e.ts.year == 2026
    assert e.ts.tzinfo is not None


# ── build_event guards (loud, never silent) ──────────────────────────────────
def test_build_event_rejects_unknown_type():
    with pytest.raises(ValueError):
        build_event("u1", "not_a_real_event")


def test_build_event_rejects_missing_uid():
    with pytest.raises(ValueError):
        build_event("", ev.EVENT_TICK)
