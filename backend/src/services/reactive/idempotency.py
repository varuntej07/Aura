"""The ONE shared idempotency primitive for the reactive layer.

Aura already has three parallel idempotency mechanisms (chat ``tool_idempotency``,
reminder ``_find_duplicate_reminder``, the send-layer ledger ``dedup_key``). The
design forbids a fourth: the event consumer, the self-heal envelope's
side-effecting ``act``, and the intent fire-path all route through ``idempotent``
here. The existing three are migrated onto it opportunistically, not rewritten.

``idempotent(key, scope)`` is an atomic create-if-absent: it does a Firestore
``create()`` on ``users/{scope}/processed/{key}``, which the server rejects with
``AlreadyExists`` if the marker already exists. No read-then-write race, no
transaction needed — ``create`` is the create-if-absent. Returns ``True`` for the
first caller (proceed with the side effect) and ``False`` for a duplicate (skip).

Fails OPEN, consistent with ``tool_idempotency``: if the processed store is
unreachable it returns ``True`` and the caller proceeds, because in this app a
rare duplicate (caught downstream by the single-flight lease and the ledger
``dedup_key`` backstop) is far better than silently dropping a user-facing action.
Every fail-open is logged loudly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from google.api_core.exceptions import AlreadyExists  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore
from .fields import (
    FIELD_EXPIRES_AT,
    FIELD_PROCESSED_AT,
    PROCESSED_SUBCOLLECTION,
    PROCESSED_TTL,
    USERS_COLLECTION,
)


async def idempotent(
    key: str, *, scope: str, ttl: timedelta = PROCESSED_TTL
) -> bool:
    """Claim ``key`` once within ``scope`` (the uid). Returns ``True`` if this
    caller is the first to claim it (do the side effect), ``False`` if it was
    already claimed (skip — a duplicate or out-of-order redelivery).
    """
    now = datetime.now(UTC)

    def _claim() -> bool:
        ref = (
            admin_firestore()
            .collection(USERS_COLLECTION)
            .document(scope)
            .collection(PROCESSED_SUBCOLLECTION)
            .document(key)
        )
        # create() is the atomic create-if-absent: ALREADY_EXISTS means a prior
        # caller won, so this delivery is a duplicate.
        ref.create({
            FIELD_PROCESSED_AT: now,
            FIELD_EXPIRES_AT: now + ttl,
        })
        return True

    try:
        return await asyncio.to_thread(_claim)
    except AlreadyExists:
        logger.info("idempotent: duplicate suppressed", {"scope": scope, "key": key})
        return False
    except Exception as exc:
        logger.warn(
            "idempotent: claim failed, proceeding unguarded (fail-open)",
            {"scope": scope, "key": key, "error": str(exc)},
        )
        return True


async def release(key: str, *, scope: str) -> None:
    """Release a claim made by ``idempotent`` so a later legitimate retry (e.g. the
    side effect it guarded failed and is safe to redo) isn't blocked for the rest of
    the TTL. Best-effort: a failed release just means the TTL expires it naturally,
    same tradeoff as ``reactive/lease.py``'s release."""

    def _delete() -> None:
        (
            admin_firestore()
            .collection(USERS_COLLECTION)
            .document(scope)
            .collection(PROCESSED_SUBCOLLECTION)
            .document(key)
            .delete()
        )

    try:
        await asyncio.to_thread(_delete)
    except Exception as exc:
        logger.warn(
            "idempotent: release failed (TTL will reclaim it)",
            {"scope": scope, "key": key, "error": str(exc)},
        )
