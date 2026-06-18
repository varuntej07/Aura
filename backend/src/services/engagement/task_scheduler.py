"""
TaskScheduler — durable job scheduling via Google Cloud Tasks.

All Cloud Tasks calls are synchronous (gRPC under the hood) and must be
wrapped in asyncio.to_thread when called from async handlers.

Two kinds of tasks:
  orchestrate -> immediate, POST /internal/engage/orchestrate
                 called from the chat/calendar trigger paths right before the HTTP response.
                 Owns the full context-load -> decision -> copy-gen pipeline.

  notify -> delayed by decision.delay_minutes, POST /internal/engage/notify
                 owns send-first, check-and-reengage, check-and-expire, expire.

Task names are returned and stored in engagement_log.cloud_task_name so
pending re-engagement tasks can be cancelled when the user responds.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger


class TaskScheduler:
    def __init__(self) -> None:
        self._client: Any = None   # google.cloud.tasks_v2.CloudTasksClient, lazy

    def schedule_orchestration(
        self,
        user_id: str,
        trigger_event: str,
        trigger_payload: dict[str, Any],
    ) -> str:
        """Enqueue an immediate orchestration task. Called synchronously before
        the HTTP response is sent — guarantees durability on Cloud Run.

        Returns the Cloud Task name (for logging/debugging).
        """
        payload = {
            "action": "orchestrate",
            "user_id": user_id,
            "trigger_event": trigger_event,
            "trigger_payload": trigger_payload,
        }
        task_name = self._enqueue(
            payload=payload,
            delay_seconds=0,
            url_path="/internal/engage/orchestrate",
        )
        logger.info("TaskScheduler: orchestration enqueued", {
            "user_id": user_id,
            "trigger_event": trigger_event,
            "task_name": task_name,
        })
        return task_name

    def schedule_notification(
        self,
        engagement_id: str,
        user_id: str,
        action: str,
        delay_seconds: int,
    ) -> str:
        """Enqueue a delayed notification task.

        action: "send_first" | "check_and_reengage" | "check_and_expire" | "expire"

        Returns the Cloud Task name (stored in engagement_log for cancellation).
        """
        payload = {
            "action": action,
            "engagement_id": engagement_id,
            "user_id": user_id,
        }
        task_name = self._enqueue(
            payload=payload,
            delay_seconds=delay_seconds,
            url_path="/internal/engage/notify",
        )
        logger.info("TaskScheduler: notification task enqueued", {
            "engagement_id": engagement_id,
            "action": action,
            "delay_seconds": delay_seconds,
            "task_name": task_name,
        })
        return task_name

    def cancel_task(self, task_name: str) -> None:
        """Cancel a pending Cloud Task. Safe to call if already fired (no-op)."""
        try:
            self._get_client().delete_task(name=task_name)
            logger.info("TaskScheduler: task cancelled", {"task_name": task_name})
        except Exception as exc:
            # NOT_FOUND is fine — task already fired or was never created
            logger.debug("TaskScheduler: cancel no-op", {
                "task_name": task_name,
                "error": str(exc),
            })

    # Internal 
    def _enqueue(
        self,
        payload: dict[str, Any],
        delay_seconds: int,
        url_path: str,
    ) -> str:
        from google.cloud import tasks_v2  # type: ignore
        from google.protobuf import timestamp_pb2  # type: ignore

        client = self._get_client()
        queue_path = client.queue_path(
            settings.CLOUD_TASKS_PROJECT,
            settings.CLOUD_TASKS_LOCATION,
            settings.CLOUD_TASKS_QUEUE,
        )

        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{settings.BACKEND_INTERNAL_URL}{url_path}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
                # OIDC token so /internal/engage/* can verify via _verify_scheduler_token
                "oidc_token": {
                    "service_account_email": settings.SCHEDULER_SA_EMAIL,
                    "audience": settings.BACKEND_INTERNAL_URL,
                },
            }
        }

        if delay_seconds > 0:
            eta = timestamp_pb2.Timestamp()
            eta.FromSeconds(int(time.time()) + delay_seconds)
            task["schedule_time"] = eta

        created = client.create_task(parent=queue_path, task=task)
        return created.name

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import tasks_v2  # type: ignore
            self._client = tasks_v2.CloudTasksClient()
        return self._client

# Module-level singleton

_scheduler: TaskScheduler | None = None


def get_task_scheduler() -> TaskScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler
