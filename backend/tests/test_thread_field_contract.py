"""Writer -> reader round-trip for the open-loop thread document.

Per the data-layer discipline in CLAUDE.md: the field names a writer produces
and the field names a reader consumes must be pinned by one shared source
(``threads/fields.py``) and guarded by a round-trip test, so a rename on either
side breaks CI instead of silently returning zero rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.threads import fields as f
from src.services.threads.models import Thread, ThreadSource, ThreadStatus


def _sample_thread() -> Thread:
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    return Thread(
        thread_id="rem_abc",
        trigger_text="implement live fetch instead of stale cache",
        source=ThreadSource.REMINDER,
        source_ref="rem_abc",
        category="technology_computing",
        known_summary="The user set a reminder about: stale cache",
        unknown=["what the project is", "why it matters"],
        status=ThreadStatus.OPEN,
        created_at=now,
        last_touched_at=now,
        expected_resolution_at=now,
        follow_ups_sent=1,
        last_follow_up_at=now,
    )


def test_thread_roundtrips_through_firestore_shape():
    original = _sample_thread()
    rebuilt = Thread.from_dict(original.to_dict())

    assert rebuilt == original


def test_to_dict_keys_are_exactly_the_field_constants():
    # If a field is added to the model but not to fields.py (or vice versa) this
    # set comparison fails — the contract can never drift silently.
    expected_keys = {
        f.FIELD_THREAD_ID,
        f.FIELD_TRIGGER_TEXT,
        f.FIELD_SOURCE,
        f.FIELD_SOURCE_REF,
        f.FIELD_CATEGORY,
        f.FIELD_KNOWN_SUMMARY,
        f.FIELD_UNKNOWN,
        f.FIELD_STATUS,
        f.FIELD_CREATED_AT,
        f.FIELD_LAST_TOUCHED_AT,
        f.FIELD_EXPECTED_RESOLUTION_AT,
        f.FIELD_FOLLOW_UPS_SENT,
        f.FIELD_LAST_FOLLOW_UP_AT,
    }
    assert set(_sample_thread().to_dict().keys()) == expected_keys


def test_from_dict_tolerates_iso_string_timestamps():
    # Old or hand-written docs may carry ISO strings instead of Firestore
    # Timestamps; the reader must coerce them, not crash.
    thread = Thread.from_dict({
        f.FIELD_THREAD_ID: "t1",
        f.FIELD_TRIGGER_TEXT: "x",
        f.FIELD_SOURCE: "reminder",
        f.FIELD_CREATED_AT: "2026-06-10T12:00:00+00:00",
    })
    assert thread.created_at == datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


async def test_writer_produces_a_readable_document(monkeypatch):
    # The reminder writer must emit a document the reader can rebuild — the real
    # writer -> reader contract, end to end.
    from src.config.settings import settings
    from src.services.threads import thread_store, thread_writer

    monkeypatch.setattr(settings, "THREAD_ENGINE_ENABLED", True)

    captured: dict[str, Thread] = {}

    async def _capture(user_id: str, thread: Thread) -> None:
        captured["thread"] = thread

    monkeypatch.setattr(thread_store, "create_thread", _capture)

    await thread_writer.record_reminder_thread(
        "u_1",
        reminder_id="rem_77",
        message="send a cold DM to a recruiter",
        trigger_at_iso="2026-06-10T15:00:00+00:00",
    )

    thread = captured["thread"]
    rebuilt = Thread.from_dict(thread.to_dict())
    assert rebuilt.thread_id == "rem_77"
    assert rebuilt.source == ThreadSource.REMINDER
    assert rebuilt.trigger_text == "send a cold DM to a recruiter"
    assert rebuilt.status == ThreadStatus.OPEN
    assert rebuilt.expected_resolution_at == datetime(2026, 6, 10, 15, 0, tzinfo=UTC)


async def test_writer_is_noop_when_engine_disabled(monkeypatch):
    from src.config.settings import settings
    from src.services.threads import thread_store, thread_writer

    monkeypatch.setattr(settings, "THREAD_ENGINE_ENABLED", False)

    called = False

    async def _should_not_run(user_id: str, thread: Thread) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(thread_store, "create_thread", _should_not_run)

    await thread_writer.record_reminder_thread(
        "u_1", reminder_id="rem_1", message="anything", trigger_at_iso="",
    )
    assert called is False
