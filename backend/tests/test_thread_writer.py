"""The worthiness judge gates thread creation without ever raising into the
fire-and-forget reminder-creation path, and fails CLOSED (skip the thread) on any
judge error — a curiosity thread is the lowest-value proactive push, so silence
beats spam. The subject-dedup layer keeps ONE curiosity loop per subject instead
of forking a new thread (and a fresh follow-up budget) per reminder_id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from src.services.threads import thread_writer
from src.services.threads.models import Thread, ThreadSource, ThreadStatus
from src.services.threads.thread_writer import _ReminderWorthinessJudgment


def _models_returning(worth_asking_about: bool, reason: str = "") -> MagicMock:
    models = MagicMock()
    models.cheap = AsyncMock(
        return_value=_ReminderWorthinessJudgment(worth_asking_about=worth_asking_about, reason=reason)
    )
    return models


def _no_existing_threads(monkeypatch) -> None:
    monkeypatch.setattr(
        thread_writer.thread_store,
        "list_threads_for_subject_dedup",
        AsyncMock(return_value=[]),
    )


def _open_thread(thread_id: str, trigger_text: str, status: ThreadStatus = ThreadStatus.OPEN) -> Thread:
    return Thread(
        thread_id=thread_id,
        trigger_text=trigger_text,
        source=ThreadSource.REMINDER,
        status=status,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        last_touched_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


async def test_mundane_reminder_is_not_threaded(monkeypatch):
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(False))
    _no_existing_threads(monkeypatch)
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="call mom", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    create_thread.assert_not_called()


async def test_interesting_reminder_is_threaded(monkeypatch):
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(True))
    _no_existing_threads(monkeypatch)
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="big presentation monday", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    create_thread.assert_awaited_once()


async def test_judge_failure_fails_closed_to_no_thread(monkeypatch):
    # A judge outage must NOT spam a proactive curiosity push; silence wins.
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("gemini down"))
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: models)
    _no_existing_threads(monkeypatch)
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="big presentation monday", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    create_thread.assert_not_called()


async def test_empty_message_never_invokes_judge(monkeypatch):
    models = _models_returning(True)
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: models)
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="   ", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    models.cheap.assert_not_called()
    create_thread.assert_not_called()


async def test_exact_same_subject_reuses_open_thread_no_new_doc(monkeypatch):
    # A re-set of the exact same reminder must reuse the open loop (bump recency),
    # never fork a new thread that re-arms its own follow-up budget.
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(True))
    monkeypatch.setattr(
        thread_writer.thread_store,
        "list_threads_for_subject_dedup",
        AsyncMock(return_value=[_open_thread("rem_old", "big presentation monday")]),
    )
    create_thread = AsyncMock()
    touch_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)
    monkeypatch.setattr(thread_writer.thread_store, "touch_thread", touch_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="rem_new", message="Big Presentation Monday",
        trigger_at_iso="2026-06-17T18:00:00+00:00",
    )
    create_thread.assert_not_called()
    touch_thread.assert_awaited_once()
    assert touch_thread.await_args.args[1] == "rem_old"


async def test_same_subject_dormant_thread_is_not_resurrected(monkeypatch):
    # A DORMANT thread means the subject was already followed up to exhaustion.
    # A fresh reminder on it must NOT open a new thread (the repeat-push bug).
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(True))
    monkeypatch.setattr(
        thread_writer.thread_store,
        "list_threads_for_subject_dedup",
        AsyncMock(return_value=[
            _open_thread("rem_old", "the annapurna project", status=ThreadStatus.DORMANT),
        ]),
    )
    create_thread = AsyncMock()
    touch_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)
    monkeypatch.setattr(thread_writer.thread_store, "touch_thread", touch_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="rem_new", message="the annapurna project",
        trigger_at_iso="2026-06-17T18:00:00+00:00",
    )
    create_thread.assert_not_called()
    touch_thread.assert_not_called()


async def test_distinct_subject_still_creates_new_thread(monkeypatch):
    # A genuinely different loop must still open its own thread even when another
    # open thread exists (exact-text miss + semantic below threshold).
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(True))
    monkeypatch.setattr(
        thread_writer.thread_store,
        "list_threads_for_subject_dedup",
        AsyncMock(return_value=[_open_thread("rem_old", "the annapurna project")]),
    )
    # Embedder returns orthogonal vectors -> cosine 0, well below threshold.
    async def _embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0], [0.0, 1.0]]

    import src.services.signal_engine.embedder as embedder
    monkeypatch.setattr(embedder, "embed_texts", _embed)

    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="rem_new", message="date night with sarah",
        trigger_at_iso="2026-06-17T18:00:00+00:00",
    )
    create_thread.assert_awaited_once()
