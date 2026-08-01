"""Transient hourly tick delivery without Firestore outbox amplification."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import AlreadyExists

from src.handlers import orchestrate as orchestrate_handler
from src.handlers import scheduler as scheduler_handler
from src.services.engagement.task_scheduler import TaskScheduler
from src.services.reactive import inbox, lease, orchestrator, reconcile
from src.services.reactive.events import EVENT_APP_OPENED, EVENT_TICK, Event
from src.services.reactive.fields import FIELD_TS, FIELD_TYPE, FIELD_UID

NOW = datetime(2026, 7, 28, 3, 17, tzinfo=UTC)


@pytest.mark.asyncio
async def test_hourly_tick_fanout_enqueues_cloud_tasks_without_outbox_writes(
    monkeypatch,
) -> None:
    task_scheduler = MagicMock()
    monkeypatch.setattr(
        "src.services.signal_engine.feature_store.list_active_user_ids",
        AsyncMock(return_value=["u1", "u2"]),
    )
    monkeypatch.setattr(
        "src.services.engagement.task_scheduler.get_task_scheduler",
        lambda: task_scheduler,
    )

    enqueued = await scheduler_handler._enqueue_tick_tasks(tick_at=NOW)

    assert enqueued == 2
    assert task_scheduler.schedule_reactive_tick.call_count == 2
    task_scheduler.schedule_reactive_tick.assert_any_call("u1", tick_at=NOW)
    task_scheduler.schedule_reactive_tick.assert_any_call("u2", tick_at=NOW)


@pytest.mark.asyncio
async def test_hourly_tick_fanout_fails_for_scheduler_retry_on_partial_enqueue(
    monkeypatch,
) -> None:
    task_scheduler = MagicMock()
    task_scheduler.schedule_reactive_tick.side_effect = [
        "tasks/u1",
        RuntimeError("queue unavailable"),
    ]
    monkeypatch.setattr(
        "src.services.signal_engine.feature_store.list_active_user_ids",
        AsyncMock(return_value=["u1", "u2"]),
    )
    monkeypatch.setattr(
        "src.services.engagement.task_scheduler.get_task_scheduler",
        lambda: task_scheduler,
    )

    with pytest.raises(RuntimeError, match="1 of 2 users"):
        await scheduler_handler._enqueue_tick_tasks(tick_at=NOW)


def test_tick_task_is_deterministic_and_carries_event_in_payload(monkeypatch) -> None:
    scheduler = TaskScheduler()
    enqueue = MagicMock(return_value="tasks/reactive-tick")
    monkeypatch.setattr(scheduler, "_enqueue", enqueue)

    first = scheduler.schedule_reactive_tick("user-1", tick_at=NOW)
    second = scheduler.schedule_reactive_tick(
        "user-1",
        tick_at=NOW.replace(minute=59),
    )

    assert first == second == "tasks/reactive-tick"
    first_call = enqueue.call_args_list[0].kwargs
    second_call = enqueue.call_args_list[1].kwargs
    assert first_call["task_id"] == second_call["task_id"]
    assert first_call["url_path"] == "/internal/orchestrate"
    event = first_call["payload"]["transient_event"]
    assert event[FIELD_UID] == "user-1"
    assert event[FIELD_TYPE] == EVENT_TICK
    assert event[FIELD_TS] == "2026-07-28T03:00:00+00:00"


def test_duplicate_tick_task_name_is_treated_as_success(monkeypatch) -> None:
    scheduler = TaskScheduler()
    monkeypatch.setattr(
        scheduler,
        "_enqueue",
        MagicMock(side_effect=AlreadyExists("duplicate")),
    )
    client = MagicMock()
    client.task_path.return_value = "tasks/existing-reactive-tick"
    monkeypatch.setattr(scheduler, "_get_client", lambda: client)

    result = scheduler.schedule_reactive_tick("user-1", tick_at=NOW)

    assert result == "tasks/existing-reactive-tick"
    client.task_path.assert_called_once()


@pytest.mark.asyncio
async def test_transient_event_skips_firestore_inbox_read(monkeypatch) -> None:
    monkeypatch.setattr(lease, "acquire", AsyncMock(return_value="lease-token"))
    monkeypatch.setattr(lease, "release", AsyncMock())
    drain = AsyncMock()
    monkeypatch.setattr(inbox, "drain", drain)
    monkeypatch.setattr(inbox, "mark_consumed", AsyncMock())
    monkeypatch.setattr(reconcile, "reconcile", AsyncMock(return_value=set()))

    result = await orchestrator.run_orchestrate(
        "u1",
        transient_events=[
            Event(uid="u1", type=EVENT_APP_OPENED, ts=NOW),
        ],
    )

    assert result["events"] == 1
    assert result["tasks"] == 0
    drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_tick_returns_retryable_error_when_lease_is_busy(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        orchestrate_handler,
        "run_orchestrate",
        AsyncMock(return_value={"skipped": "lease_held"}),
    )
    event = Event(uid="u1", type=EVENT_TICK, ts=NOW).to_dict()
    event[FIELD_TS] = NOW.isoformat()

    with pytest.raises(HTTPException) as exc_info:
        await orchestrate_handler.handle_orchestrate({
            "user_id": "u1",
            "transient_event": event,
        })

    assert exc_info.value.status_code == 503
