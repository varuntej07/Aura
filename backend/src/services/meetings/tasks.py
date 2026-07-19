"""Cloud Tasks enqueue for meeting synthesis.

A deliberate small duplication of task_scheduler.py's ``_enqueue`` OIDC
pattern rather than an import: engagement's TaskScheduler is in the middle of
unrelated churn, and this module needs exactly one task shape. Same queue
(juno-engagement), same OIDC signer, same durability rule - the enqueue runs
synchronously before the HTTP response so Cloud Run can never freeze the
instance between responding and enqueueing.

The task name is DETERMINISTIC (meeting-synthesize-{meeting_id}), so a client
retry of POST /meetings/{id}/complete collides with the already-created task
and gets AlreadyExists, which is success. The status compare-and-set in
synthesis.py is the second, durable layer of the same idempotency guarantee
(mirrors schedule_signal_scoring + the generation claim).

POST /meetings/{id}/retry passes a ``dedup_suffix`` (the attempt count) so its
task name differs from the /complete task and any earlier retry. Cloud Tasks
keeps a completed task's name reserved (a tombstone) for up to ~1h, so reusing
the base name would silently swallow a legitimate re-run as AlreadyExists.
"""

from __future__ import annotations

import json
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if _client_singleton is None:
        from google.cloud import tasks_v2  # type: ignore
        _client_singleton = tasks_v2.CloudTasksClient()
    return _client_singleton


def enqueue_synthesis(uid: str, meeting_id: str, *, dedup_suffix: str = "") -> str:
    """Enqueue the one synthesis task for a completed capture. Synchronous
    (gRPC); call via asyncio.to_thread from async handlers. Raises on real
    enqueue failures so /complete answers 5xx and the client retries.

    ``dedup_suffix`` (empty for /complete, the attempt count for /retry) makes a
    deliberate re-run a distinct task name instead of an AlreadyExists no-op."""
    from google.api_core.exceptions import AlreadyExists  # type: ignore
    from google.cloud import tasks_v2  # type: ignore

    client = _client()
    task_id = f"meeting-synthesize-{meeting_id}"
    if dedup_suffix:
        task_id = f"{task_id}-{dedup_suffix}"
    task_path = client.task_path(
        settings.CLOUD_TASKS_PROJECT,
        settings.CLOUD_TASKS_LOCATION,
        settings.CLOUD_TASKS_QUEUE,
        task_id,
    )
    task: dict[str, Any] = {
        "name": task_path,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.BACKEND_INTERNAL_URL}/internal/meetings/synthesize",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"user_id": uid, "meeting_id": meeting_id}).encode(),
            "oidc_token": {
                "service_account_email": settings.SCHEDULER_SA_EMAIL,
                "audience": settings.BACKEND_INTERNAL_URL,
            },
        },
    }

    queue_path = client.queue_path(
        settings.CLOUD_TASKS_PROJECT,
        settings.CLOUD_TASKS_LOCATION,
        settings.CLOUD_TASKS_QUEUE,
    )
    try:
        created = client.create_task(parent=queue_path, task=task)
        task_name = created.name
    except AlreadyExists:
        task_name = task_path
        logger.info("meetings.tasks: duplicate synthesis enqueue suppressed", {
            "user_id": uid, "meeting_id": meeting_id,
        })
        return task_name

    logger.info("meetings.tasks: synthesis enqueued", {
        "user_id": uid, "meeting_id": meeting_id, "task_name": task_name,
    })
    return task_name
