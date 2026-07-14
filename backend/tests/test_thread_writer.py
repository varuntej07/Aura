"""The worthiness judge must gate thread creation without ever raising into the
fire-and-forget reminder-creation path, and must fail open (create the thread)
on any judge error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.services.threads import thread_writer
from src.services.threads.thread_writer import _ReminderWorthinessJudgment


def _models_returning(worth_asking_about: bool, reason: str = "") -> MagicMock:
    models = MagicMock()
    models.cheap = AsyncMock(
        return_value=_ReminderWorthinessJudgment(worth_asking_about=worth_asking_about, reason=reason)
    )
    return models


async def test_mundane_reminder_is_not_threaded(monkeypatch):
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(False))
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="call mom", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    create_thread.assert_not_called()


async def test_interesting_reminder_is_threaded(monkeypatch):
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: _models_returning(True))
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="big presentation monday", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    create_thread.assert_awaited_once()


async def test_judge_failure_fails_open_to_threaded(monkeypatch):
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("gemini down"))
    monkeypatch.setattr(thread_writer, "get_model_provider", lambda: models)
    create_thread = AsyncMock()
    monkeypatch.setattr(thread_writer.thread_store, "create_thread", create_thread)

    await thread_writer.record_reminder_thread(
        "u1", reminder_id="r1", message="call mom", trigger_at_iso="2026-06-10T18:00:00+00:00",
    )
    create_thread.assert_awaited_once()


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
