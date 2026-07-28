"""Transactional Firestore store for durable Guide tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from google.cloud import firestore as fs  # type: ignore

from ..agent.voice.guide_models import GuideTask
from ..lib.logger import logger
from .firebase import admin_firestore

GUIDE_TASKS_SUBCOLLECTION = "guide_tasks"
LEASE_TTL = timedelta(seconds=30)
RECENT_INSTRUCTION_LIMIT = 32
VERIFIED_EVIDENCE_LIMIT = 64


class GuideTaskConflictError(RuntimeError):
    pass


class GuideTaskLeaseError(RuntimeError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _bounded(task: GuideTask) -> GuideTask:
    task.recent_instruction_ids = task.recent_instruction_ids[-RECENT_INSTRUCTION_LIMIT:]
    task.verified_evidence = task.verified_evidence[-VERIFIED_EVIDENCE_LIMIT:]
    return task


class GuideTaskStore:
    """One document per task, with lease and revision guarded writes."""

    def _ref(self, db, user_id: str, task_id: str):
        return (
            db.collection("users")
            .document(user_id)
            .collection(GUIDE_TASKS_SUBCOLLECTION)
            .document(task_id)
        )

    async def load(self, user_id: str, task_id: str) -> GuideTask | None:
        started = datetime.now(UTC)

        def _read() -> GuideTask | None:
            db = admin_firestore()
            snap = self._ref(db, user_id, task_id).get()
            if not snap.exists:
                return None
            return GuideTask.model_validate(snap.to_dict() or {})

        task = await asyncio.to_thread(_read)
        logger.info(
            "GuideTelemetry: task load",
            {
                "user_id": user_id,
                "task_id": task_id,
                "found": task is not None,
                "latency_ms": round((datetime.now(UTC) - started).total_seconds() * 1000),
            },
        )
        return task

    async def create(self, task: GuideTask) -> GuideTask:
        task = _bounded(task)

        def _txn() -> GuideTask:
            db = admin_firestore()
            ref = self._ref(db, task.user_id, task.task_id)
            transaction = db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction) -> GuideTask:
                snap = ref.get(transaction=txn)
                if snap.exists:
                    return GuideTask.model_validate(snap.to_dict() or {})
                txn.create(ref, task.model_dump(mode="python"))
                return task

            return _apply(transaction)

        return await asyncio.to_thread(_txn)

    async def acquire_lease(
        self,
        user_id: str,
        task_id: str,
        lease_owner: str,
        *,
        resumed: bool,
    ) -> GuideTask:
        now = datetime.now(UTC)

        def _txn() -> GuideTask:
            db = admin_firestore()
            ref = self._ref(db, user_id, task_id)
            transaction = db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction) -> GuideTask:
                snap = ref.get(transaction=txn)
                if not snap.exists:
                    raise GuideTaskConflictError("Guide task does not exist")
                task = GuideTask.model_validate(snap.to_dict() or {})
                expires = _aware(task.lease_expires_at)
                if (
                    task.lease_owner
                    and task.lease_owner != lease_owner
                    and expires is not None
                    and expires > now
                ):
                    raise GuideTaskLeaseError("Guide task lease is held")
                task.lease_owner = lease_owner
                task.lease_expires_at = now + LEASE_TTL
                task.updated_at = now
                task.last_resumed_at = now
                if resumed:
                    task.resume_count += 1
                task.revision += 1
                txn.set(ref, _bounded(task).model_dump(mode="python"))
                return task

            return _apply(transaction)

        return await asyncio.to_thread(_txn)

    async def renew_lease(
        self,
        user_id: str,
        task_id: str,
        lease_owner: str,
    ) -> GuideTask:
        now = datetime.now(UTC)

        def _txn() -> GuideTask:
            db = admin_firestore()
            ref = self._ref(db, user_id, task_id)
            transaction = db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction) -> GuideTask:
                snap = ref.get(transaction=txn)
                if not snap.exists:
                    raise GuideTaskConflictError("Guide task does not exist")
                task = GuideTask.model_validate(snap.to_dict() or {})
                if task.lease_owner != lease_owner:
                    raise GuideTaskLeaseError("Guide task lease changed")
                task.lease_expires_at = now + LEASE_TTL
                task.updated_at = now
                txn.update(
                    ref,
                    {
                        "lease_expires_at": task.lease_expires_at,
                        "updated_at": task.updated_at,
                    },
                )
                return task

            return _apply(transaction)

        return await asyncio.to_thread(_txn)

    async def mutate(
        self,
        user_id: str,
        task_id: str,
        lease_owner: str,
        expected_revision: int,
        reducer: Callable[[GuideTask], GuideTask],
    ) -> GuideTask:
        started = datetime.now(UTC)
        now = started

        def _txn() -> GuideTask:
            db = admin_firestore()
            ref = self._ref(db, user_id, task_id)
            transaction = db.transaction()

            @fs.transactional
            def _apply(txn: fs.Transaction) -> GuideTask:
                snap = ref.get(transaction=txn)
                if not snap.exists:
                    raise GuideTaskConflictError("Guide task does not exist")
                task = GuideTask.model_validate(snap.to_dict() or {})
                if task.revision != expected_revision:
                    raise GuideTaskConflictError(
                        f"Guide task revision is {task.revision}, expected {expected_revision}"
                    )
                expires = _aware(task.lease_expires_at)
                if task.lease_owner != lease_owner or expires is None or expires <= now:
                    raise GuideTaskLeaseError("Guide task lease is absent or expired")
                updated = _bounded(reducer(task.model_copy(deep=True)))
                updated.revision = expected_revision + 1
                updated.updated_at = now
                updated.lease_expires_at = now + LEASE_TTL
                txn.set(ref, updated.model_dump(mode="python"))
                return updated

            return _apply(transaction)

        result = await asyncio.to_thread(_txn)
        logger.info(
            "GuideTelemetry: task CAS",
            {
                "user_id": user_id,
                "task_id": task_id,
                "task_revision": result.revision,
                "latency_ms": round((datetime.now(UTC) - started).total_seconds() * 1000),
            },
        )
        return result
