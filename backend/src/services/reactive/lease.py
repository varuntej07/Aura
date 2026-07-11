"""Per-user single-flight lease for the orchestrator.

The inline dispatch path and the sweep path can both invoke orchestrate for the
same user on two Cloud Run instances at once. Firestore sustains only ~1 write/s
to one doc, so naive concurrency on the per-user state causes transaction-retry
storms. Each orchestrate invocation claims a short TTL lease first; a second
concurrent invocation finds it held and drops (its events stay in the inbox for
the holder to drain).

The lease is an OPTIMIZATION, not the correctness guarantee — the orchestrator's
idempotent per-event consume is what actually prevents double dispatch. So
acquiring fails OPEN: if the lease store is unreachable we proceed anyway, and the
idempotent consume backstops any resulting duplicate.

The lease has a TTL so a crashed holder cannot block a user forever; the TTL is set
above the worst-case orchestrate duration (a few bounded LLM calls) so the lease is
never stolen mid-pass.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore

LOCKS_SUBCOLLECTION = "locks"
ORCHESTRATE_LEASE_DOC = "orchestrate_lease"

# Above the worst-case orchestrate pass (a few bounded, self-healing LLM calls).
LEASE_TTL = timedelta(seconds=120)

FIELD_TOKEN = "token"
FIELD_ACQUIRED_AT = "acquired_at"
FIELD_EXPIRES_AT = "expires_at"


def _lease_ref(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(LOCKS_SUBCOLLECTION)
        .document(ORCHESTRATE_LEASE_DOC)
    )


def _as_aware(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


async def acquire(user_id: str, *, now: datetime | None = None) -> str | None:
    """Claim the lease. Returns a token if acquired (caller must release it), or
    ``None`` if another invocation holds an unexpired lease. Fails OPEN (returns a
    token) on a store error — the idempotent consume is the real guarantee."""
    when = now or datetime.now(UTC)
    token = uuid.uuid4().hex

    def _txn() -> str | None:
        db = admin_firestore()
        ref = _lease_ref(user_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> str | None:
            snap = ref.get(transaction=txn)
            if snap.exists:
                expires = _as_aware((snap.to_dict() or {}).get(FIELD_EXPIRES_AT))
                if expires is not None and expires > when:
                    return None  # held and not expired
            txn.set(ref, {
                FIELD_TOKEN: token,
                FIELD_ACQUIRED_AT: when,
                FIELD_EXPIRES_AT: when + LEASE_TTL,
            })
            return token

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("lease.acquire failed, proceeding without lease (fail-open)", {
            "user_id": user_id, "error": str(exc),
        })
        return token


async def release(user_id: str, token: str) -> None:
    """Release the lease iff this caller still holds it (token match). Best-effort;
    a missed release just lets the TTL expire it."""

    def _txn() -> None:
        db = admin_firestore()
        ref = _lease_ref(user_id)
        transaction = db.transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            if snap.exists and (snap.to_dict() or {}).get(FIELD_TOKEN) == token:
                txn.delete(ref)

        _apply(transaction)

    try:
        await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("lease.release failed (TTL will reclaim it)", {
            "user_id": user_id, "error": str(exc),
        })
