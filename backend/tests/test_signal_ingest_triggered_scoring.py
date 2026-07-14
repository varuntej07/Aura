"""Ingest-triggered signal scoring: the handler wiring and the deploy contract.

Scoring used to run on its own 15-30 min Cloud Scheduler job — 16 near-identical
KNN passes per user against a pool that only changes on the 4-hour ingest
cadence. Now the ingest handler enqueues ONE durable, generation-named Cloud
Task per completed ingest. These tests pin:

  * a successful ingest enqueues exactly one scoring task (even with 0 writes);
  * a failed ingest enqueues nothing;
  * an ingest retry collides with the deterministic task name instead of
    duplicating the scoring work (AlreadyExists is treated as success);
  * the tick handler no-ops a completed generation, defers a live lease with a
    409 (so Cloud Tasks retries later), marks completion with the run counters,
    and leaves a failed pass retryable;
  * the KNN retrieval limit stays 50 (isolating cadence savings from any
    recommendation-quality change);
  * deploy.sh carries NO recurring signal-engine scoring job anymore — the
    same file-text contract style as test_scheduler_oidc_audience.py, so a
    reintroduced cron breaks CI, not the Firestore bill.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import AlreadyExists

from src.config.settings import settings
from src.handlers import signal_content_ingest, signal_tick
from src.services.engagement.task_scheduler import TaskScheduler
from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_ingest import IngestSummary
from src.services.signal_engine.content_pool import MAX_NEAREST_CANDIDATES
from src.services.signal_engine.generation_store import ClaimOutcome

_DEPLOY_SH = Path(__file__).resolve().parents[1] / "deploy.sh"

GENERATION_ID_PATTERN = re.compile(r"^\d{8}T(00|04|08|12|16|20)00Z$")


# ── Ingest handler → exactly one scoring task ────────────────────────────────

def _scheduler_mock() -> MagicMock:
    scheduler = MagicMock()
    scheduler.schedule_signal_scoring = MagicMock(return_value="projects/p/tasks/t")
    return scheduler


async def test_successful_ingest_enqueues_exactly_one_scoring_task(monkeypatch):
    summary = IngestSummary(google_news_fetched=4, newsdata_fetched=6, total_written=10)
    monkeypatch.setattr(
        signal_content_ingest, "run_ingest", AsyncMock(return_value=summary)
    )
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_content_ingest, "record_ingest_completed", record)
    scheduler = _scheduler_mock()
    monkeypatch.setattr(signal_content_ingest, "get_task_scheduler", lambda: scheduler)

    result = await signal_content_ingest.handle_signal_content_ingest()

    assert scheduler.schedule_signal_scoring.call_count == 1
    generation_id = scheduler.schedule_signal_scoring.call_args.args[0]
    assert GENERATION_ID_PATTERN.match(generation_id)
    record.assert_awaited_once_with(generation_id, new_candidates_written=10)
    assert result["generation_id"] == generation_id
    assert result["total_written"] == 10


async def test_ingest_with_zero_new_candidates_still_schedules_one_generation(monkeypatch):
    # User vectors moved since the last pass even when the pool did not, so a
    # valid-but-empty ingest still buys its generation exactly one scoring pass.
    summary = IngestSummary(total_written=0)
    monkeypatch.setattr(
        signal_content_ingest, "run_ingest", AsyncMock(return_value=summary)
    )
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_content_ingest, "record_ingest_completed", record)
    scheduler = _scheduler_mock()
    monkeypatch.setattr(signal_content_ingest, "get_task_scheduler", lambda: scheduler)

    await signal_content_ingest.handle_signal_content_ingest()

    assert scheduler.schedule_signal_scoring.call_count == 1
    record.assert_awaited_once()
    assert record.call_args.kwargs["new_candidates_written"] == 0


async def test_failed_ingest_enqueues_no_scoring_task(monkeypatch):
    monkeypatch.setattr(
        signal_content_ingest,
        "run_ingest",
        AsyncMock(side_effect=RuntimeError("embedder quota exhausted")),
    )
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_content_ingest, "record_ingest_completed", record)
    scheduler = _scheduler_mock()
    monkeypatch.setattr(signal_content_ingest, "get_task_scheduler", lambda: scheduler)

    with pytest.raises(RuntimeError):
        await signal_content_ingest.handle_signal_content_ingest()

    scheduler.schedule_signal_scoring.assert_not_called()
    record.assert_not_awaited()


# ── Deterministic task name → ingest retry cannot duplicate scoring ─────────

class _FakeCloudTasksClient:
    """Real-shaped Cloud Tasks client that enforces name uniqueness the way the
    server does: creating a task whose name already exists raises AlreadyExists."""

    def __init__(self):
        self.created: list[dict] = []
        self._names: set[str] = set()

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project: str, location: str, queue: str, task: str) -> str:
        return f"{self.queue_path(project, location, queue)}/tasks/{task}"

    def create_task(self, parent: str, task: dict):
        name = task["name"]
        if name in self._names:
            raise AlreadyExists(f"task {name} already exists")
        self._names.add(name)
        self.created.append(task)
        result = MagicMock()
        result.name = name
        return result


def test_ingest_retry_collides_with_the_deterministic_task_name():
    scheduler = TaskScheduler()
    fake_client = _FakeCloudTasksClient()
    scheduler._client = fake_client

    first = scheduler.schedule_signal_scoring("20260709T1200Z")
    retry = scheduler.schedule_signal_scoring("20260709T1200Z")

    assert len(fake_client.created) == 1          # one task, not two
    assert first == retry                          # retry resolves to the same task
    assert first.endswith("/tasks/signal-scoring-20260709T1200Z")

    task = fake_client.created[0]
    http = task["http_request"]
    assert http["url"] == f"{settings.BACKEND_INTERNAL_URL}/internal/signal-engine/tick"
    # Same OIDC identity + audience contract every internal Cloud Task uses.
    assert http["oidc_token"]["service_account_email"] == settings.SCHEDULER_SA_EMAIL
    assert http["oidc_token"]["audience"] == settings.BACKEND_INTERNAL_URL
    assert b"20260709T1200Z" in http["body"]


def test_distinct_generations_get_distinct_tasks():
    scheduler = TaskScheduler()
    fake_client = _FakeCloudTasksClient()
    scheduler._client = fake_client

    scheduler.schedule_signal_scoring("20260709T1200Z")
    scheduler.schedule_signal_scoring("20260709T1600Z")

    assert len(fake_client.created) == 2


# ── Tick handler: claim → score → complete/fail ─────────────────────────────

def _tick_summary(**overrides) -> scoring_loop.TickSummary:
    summary = scoring_loop.TickSummary()
    summary.users_considered = 5
    summary.users_scored = 3
    summary.users_skipped_no_state = 1
    summary.users_skipped_no_candidates = 1
    summary.knn_queries = 4
    summary.notifications_sent = 2
    for key, value in overrides.items():
        setattr(summary, key, value)
    return summary


async def test_completed_generation_noops_without_scoring(monkeypatch):
    claim = AsyncMock(return_value=ClaimOutcome.ALREADY_COMPLETE)
    monkeypatch.setattr(signal_tick, "claim_for_scoring", claim)
    run_tick = AsyncMock()
    monkeypatch.setattr(signal_tick, "run_tick", run_tick)

    result = await signal_tick.handle_signal_tick({"generation_id": "20260709T1200Z"})

    assert result["status"] == "skipped_already_complete"
    assert result["generation_id"] == "20260709T1200Z"
    run_tick.assert_not_awaited()


async def test_live_lease_defers_with_409_so_the_task_retries(monkeypatch):
    monkeypatch.setattr(
        signal_tick, "claim_for_scoring", AsyncMock(return_value=ClaimOutcome.LEASE_HELD)
    )
    run_tick = AsyncMock()
    monkeypatch.setattr(signal_tick, "run_tick", run_tick)

    with pytest.raises(HTTPException) as exc_info:
        await signal_tick.handle_signal_tick({"generation_id": "20260709T1200Z"})

    assert exc_info.value.status_code == 409
    run_tick.assert_not_awaited()


async def test_claimed_generation_scores_and_marks_complete_with_counters(monkeypatch):
    monkeypatch.setattr(
        signal_tick, "claim_for_scoring", AsyncMock(return_value=ClaimOutcome.CLAIMED)
    )
    monkeypatch.setattr(signal_tick, "run_tick", AsyncMock(return_value=_tick_summary()))
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_tick, "mark_scoring_complete", complete)
    failed = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_tick, "mark_scoring_failed", failed)

    result = await signal_tick.handle_signal_tick({"generation_id": "20260709T1200Z"})

    complete.assert_awaited_once()
    stats = complete.call_args.args[1]
    assert stats.users_considered == 5
    assert stats.users_scored == 3
    assert stats.users_skipped == 2
    assert stats.knn_query_count == 4
    failed.assert_not_awaited()
    assert result["status"] == "complete"
    assert result["knn_query_count"] == 4
    assert result["notifications_sent"] == 2


async def test_scoring_failure_marks_failed_and_reraises_for_retry(monkeypatch):
    monkeypatch.setattr(
        signal_tick, "claim_for_scoring", AsyncMock(return_value=ClaimOutcome.CLAIMED)
    )
    monkeypatch.setattr(
        signal_tick, "run_tick", AsyncMock(side_effect=RuntimeError("firestore down"))
    )
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_tick, "mark_scoring_complete", complete)
    failed = AsyncMock(return_value=None)
    monkeypatch.setattr(signal_tick, "mark_scoring_failed", failed)

    with pytest.raises(RuntimeError):
        await signal_tick.handle_signal_tick({"generation_id": "20260709T1200Z"})

    failed.assert_awaited_once()
    assert failed.call_args.args[0] == "20260709T1200Z"
    complete.assert_not_awaited()


async def test_manual_recovery_without_body_derives_the_current_generation(monkeypatch):
    claim = AsyncMock(return_value=ClaimOutcome.ALREADY_COMPLETE)
    monkeypatch.setattr(signal_tick, "claim_for_scoring", claim)

    result = await signal_tick.handle_signal_tick(None)

    derived = claim.call_args.args[0]
    assert GENERATION_ID_PATTERN.match(derived)
    assert result["generation_id"] == derived


# ── Recommendation retrieval unchanged: KNN limit stays 50 ──────────────────

async def test_personal_lane_knn_limit_stays_50(monkeypatch):
    monkeypatch.setattr(
        scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"})
    )
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "_should_refresh_user_vector", lambda state: False)
    find_nearest = AsyncMock(return_value=[])
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", find_nearest)
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))

    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.recent_sends_backfilled = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION

    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid", MagicMock(), summary, [])

    find_nearest.assert_awaited_once()
    assert find_nearest.call_args.kwargs["limit"] == 50
    assert MAX_NEAREST_CANDIDATES == 50
    # The pass's KNN cost is now first-class telemetry, not an inferred guess.
    assert summary.knn_queries == 1
    assert summary.users_scored == 0  # empty KNN result → skipped, not scored


# ── Deploy contract: no recurring signal-engine scoring job ─────────────────

def test_deploy_config_has_no_recurring_signal_scoring_job():
    text = _DEPLOY_SH.read_text(encoding="utf-8")

    assert 'ensure_scheduler_job "juno-signal-engine-tick"' not in text, (
        "The recurring signal-engine scoring cron is back in deploy.sh. Scoring "
        "is ingest-triggered (one durable Cloud Task per 4h generation); a "
        "recurring job would re-run identical KNN passes against an unchanged "
        "pool and could race ingestion."
    )
    assert 'remove_scheduler_job_if_exists "juno-signal-engine-tick"' in text, (
        "deploy.sh must actively DELETE the retired juno-signal-engine-tick job, "
        "not just stop reconciling it — otherwise the live cron keeps firing."
    )
    # No scheduler job may target the scoring endpoint on ANY recurring cadence
    # (15-min, 30-min, or otherwise). Only the ingest endpoint keeps a cron.
    for line in text.splitlines():
        if "ensure_scheduler_job" in line and "/internal/signal-engine/tick" in line:
            raise AssertionError(
                f"deploy.sh registers a recurring job for the scoring endpoint: {line!r}"
            )
    assert 'ensure_scheduler_job "juno-content-ingest" "0 */4 * * *"' in text, (
        "Content ingest must stay on the four-hour cron — it is what triggers "
        "the six daily scoring generations."
    )
