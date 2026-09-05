"""The real dispatcher. Everything Cloud-Tasks-shaped lives behind this one class.

Installed into ``tasks.set_dispatcher`` at startup, which is the only place the engine
learns that Cloud Tasks exists. Stage bodies, the store and the sweeper all remain
ignorant of it, so swapping the execution substrate touches this file and nothing else.

Three properties are load bearing and each has a comment where it is implemented:

* **A separate queue.** ``juno-research`` runs at 10 dispatches/second with maxAttempts
  **2**, matching ``store.STAGE_ATTEMPT_CAP``, and is provisioned/pinned by
  backend/deploy.sh. The shared ``juno-engagement`` queue runs
  at 500/s with maxAttempts **100**; inheriting that would let Cloud Tasks redeliver a
  stage 100 times, each attempt spending Brave queries, Firecrawl credits and model
  tokens on work the engine has already declared terminal.
* **An explicit dispatch deadline.** No other caller in this repository sets one, so
  every existing task silently rides the 10-minute HTTP default. Research sets it to 600
  seconds explicitly: comfortably above the 150-second quick stage bound and far under
  Cloud Run's 3600.
* **Deterministic names.** A duplicate enqueue collides with ``AlreadyExists``, which is
  treated as success rather than an error. The name carries the attempt and dispatch
  counters because Cloud Tasks reserves a completed task's name for a tombstone window of
  roughly an hour, so a legitimate retry MUST mint a new name or the enqueue is silently
  swallowed and the stage never runs again.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger


def _client() -> Any:
    from google.cloud import tasks_v2  # type: ignore

    return tasks_v2.CloudTasksClient()


class CloudTasksDispatcher:
    """Delivers one research stage to ``POST /internal/research/step``."""

    available = True

    async def dispatch(
        self,
        uid: str,
        run_id: str,
        stage_id: str,
        *,
        task_name: str,
        schedule_time: datetime | None = None,
    ) -> str:
        """Enqueue one stage. Returns the delivered task name, or "" if not delivered.

        Returning "" rather than raising on a soft failure matters: ``tasks.dispatch_job``
        leaves the outbox row DUE when delivery did not happen, and a row left due is
        recoverable by the sweeper. A row marked dispatched that never ran is not.
        """
        return await asyncio.to_thread(
            self._dispatch_sync, uid, run_id, stage_id, task_name, schedule_time
        )

    def _dispatch_sync(
        self,
        uid: str,
        run_id: str,
        stage_id: str,
        task_name: str,
        schedule_time: datetime | None = None,
    ) -> str:
        from google.api_core.exceptions import AlreadyExists  # type: ignore
        from google.cloud import tasks_v2  # type: ignore
        from google.protobuf import duration_pb2, timestamp_pb2  # type: ignore

        client = _client()
        project = settings.CLOUD_TASKS_PROJECT
        location = settings.CLOUD_TASKS_LOCATION
        queue = settings.CLOUD_TASKS_RESEARCH_QUEUE

        task_path = client.task_path(project, location, queue, task_name)
        task: dict[str, Any] = {
            "name": task_path,
            # Set EXPLICITLY. Left unset this inherits the queue's 10-minute HTTP
            # default, which is the behaviour every other caller in this repo has.
            "dispatch_deadline": duration_pb2.Duration(
                seconds=int(settings.RESEARCH_TASK_DISPATCH_DEADLINE_S)
            ),
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{settings.BACKEND_INTERNAL_URL}/internal/research/step",
                "headers": {"Content-Type": "application/json"},
                # The body carries identity only. Everything else the stage needs is
                # read from Firestore under the lease, so a task body can never carry a
                # stale plan, a stale budget, or page content.
                "body": json.dumps(
                    {"user_id": uid, "run_id": run_id, "stage_id": stage_id}
                ).encode(),
                "oidc_token": {
                    "service_account_email": settings.SCHEDULER_SA_EMAIL,
                    "audience": settings.BACKEND_INTERNAL_URL,
                },
            },
        }
        if schedule_time is not None:
            # A retry is committed with a jittered due time and handed to the dispatcher
            # immediately. Without this the only two options were to fire now, throwing
            # away the backoff, or to leave it for the five-minute recovery sweep, which
            # is longer than a quick run's entire 240-second wall clock. Cloud Tasks holds
            # the task until this instant, so the backoff is honoured by the queue rather
            # than by anyone sleeping.
            stamp = timestamp_pb2.Timestamp()
            stamp.FromDatetime(schedule_time)
            task["schedule_time"] = stamp

        queue_path = client.queue_path(project, location, queue)
        try:
            created = client.create_task(parent=queue_path, task=task)
            return created.name
        except AlreadyExists:
            # The deterministic-name guarantee doing its job. A crash between enqueue and
            # the commit that records it leaves the redelivery colliding here, which is
            # success: the task exists exactly once.
            logger.info(
                "research.cloud_tasks: duplicate enqueue suppressed",
                {"run_id": run_id, "stage_id": stage_id, "task_name": task_name},
            )
            return task_path
        except Exception as exc:
            # Soft failure. The outbox row stays due and the sweeper retries it, which is
            # strictly safer than claiming a delivery that did not happen.
            logger.error(
                "research.cloud_tasks: enqueue failed",
                {
                    "run_id": run_id,
                    "stage_id": stage_id,
                    "error": str(exc),
                    "error_code": "research_enqueue_failed",
                },
            )
            return ""


_local_tasks: dict[str, asyncio.Task[None]] = {}


class LocalResearchDispatcher:
    """Run real research stages in the local API process, without Cloud Tasks."""

    available = True

    async def dispatch(
        self,
        uid: str,
        run_id: str,
        stage_id: str,
        *,
        task_name: str,
        schedule_time: datetime | None = None,
    ) -> str:
        local_name = f"local:{task_name}"
        existing = _local_tasks.get(local_name)
        if existing and not existing.done():
            return local_name

        async def _run() -> None:
            try:
                if schedule_time is not None:
                    delay = max(0.0, (schedule_time - datetime.now(UTC)).total_seconds())
                    if delay:
                        await asyncio.sleep(delay)
                from .engine import StepRef, get_research_engine

                await get_research_engine().advance(
                    uid, StepRef(uid=uid, run_id=run_id, stage_id=stage_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "research.local_dispatch: stage failed",
                    {"run_id": run_id, "stage_id": stage_id, "error": str(exc)},
                )
            finally:
                _local_tasks.pop(local_name, None)

        _local_tasks[local_name] = asyncio.create_task(_run(), name=local_name)
        return local_name


async def delete_task(task_name: str) -> bool:
    """Best-effort deletion of a queued task. Never raises.

    Cancellation is a WRITE, not an interrupt, so the claim-time check is what actually
    makes it safe: a task that fires anyway finds the run blocked and abandons itself.
    Deleting the not-yet-fired tasks is what makes it CHEAP, which on a wide fan-out is
    the difference between a cancelled run costing nothing and costing a full wave of
    Firecrawl credits. Both are needed, and this is the half that was never wired up:
    ``store.request_cancel`` has always returned the task names and nothing consumed them.

    A NOT_FOUND is success, not an error. The task already fired, or was never created.
    """
    if not task_name:
        return False
    if task_name.startswith("local:"):
        task = _local_tasks.pop(task_name, None)
        if task is None:
            return False
        task.cancel()
        return True

    def _run() -> bool:
        from google.api_core.exceptions import NotFound  # type: ignore

        try:
            _client().delete_task(name=task_name)
            return True
        except NotFound:
            return False

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        # The race-safe fallback still holds, so a failure here costs money, never
        # correctness. Logged rather than raised for exactly that reason.
        logger.warn(
            "research.cloud_tasks: task deletion failed, claim-time retirement still applies",
            {"task_name": task_name, "error": str(exc)},
        )
        return False


def install() -> None:
    """Install the real dispatcher. Called once at startup, never at import time.

    Import-time installation would make merely importing the package start delivering
    work, which is exactly what phase two's NullDispatcher default existed to prevent.
    """
    from . import tasks

    dispatcher = CloudTasksDispatcher() if settings.is_production else LocalResearchDispatcher()
    tasks.set_dispatcher(dispatcher)
    logger.info(
        "research.cloud_tasks: dispatcher installed",
        {
            "queue": settings.CLOUD_TASKS_RESEARCH_QUEUE if settings.is_production else "local",
            "dispatch_deadline_s": settings.RESEARCH_TASK_DISPATCH_DEADLINE_S,
        },
    )
