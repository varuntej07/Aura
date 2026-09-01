"""Guide Mode usage rollup, written onto the user document (no subcollection).

Two independent writers land here for the same Guide Mode session, keyed by
``guide_session_id``:

  * the desktop client (via ``POST /devices/guide-usage``) owns the additive
    lifetime counters and the client-observable snapshot fields (duration,
    outcome, frame/step/timeout counts);
  * the voice worker's ``GuideCoordinator`` owns the rich fields it alone can
    see (model, average TTFT, tools used, last user turn, frames processed).

Retention is "rollup only": lifetime counters plus a single latest-session
snapshot on ``users/{uid}``. There is no per-session document, so the rich
fields hold the MOST RECENT session's values, not full history. A transaction
guards the snapshot so a late straggler write from an older session can never
clobber a newer session's snapshot, while both writers for the SAME session
still merge cleanly. Fail-soft everywhere: usage bookkeeping must never break a
request or a voice session.
"""

from __future__ import annotations

import asyncio

from google.cloud import firestore as fs

from ..lib.logger import logger
from . import guide_usage_fields as GF
from .firebase import admin_firestore


async def record_guide_usage(
    uid: str,
    *,
    writer: str,
    guide_session_id: str,
    ended_at_ms: int,
    snapshot_fields: dict,
    increments: dict[str, int] | None = None,
) -> bool:
    """Merge one writer's contribution for a Guide Mode session into the rollup.

    ``snapshot_fields`` are the ``guide_last_*`` fields this writer owns; they are
    applied only when this session is the current latest (same id) or strictly
    newer (``ended_at_ms`` >= the stored latest). ``increments`` are additive
    counters; each writer must own DISTINCT counter keys, and the per-writer
    ``guide_counted_{writer}_session_id`` marker makes them idempotent per
    session, so a client retry after a timed-out-but-committed write (an
    expected event on this fail-soft endpoint) can never double count. Returns
    True on a committed write, False on any failure (logged, never raised).
    """
    increments = increments or {}
    counted_marker = GF.counted_marker(writer)
    ref = admin_firestore().collection("users").document(uid)

    def _txn() -> bool:
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _apply(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            update: dict = {}
            if data.get(counted_marker) != guide_session_id:
                for key, delta in increments.items():
                    update[key] = fs.Increment(delta)
                update[counted_marker] = guide_session_id

            stored_id = data.get(GF.LAST_SESSION_ID)
            stored_ended_ms = data.get(GF.LAST_ENDED_MS)
            is_same_session = stored_id == guide_session_id
            is_newer = not isinstance(stored_ended_ms, (int, float)) or ended_at_ms >= stored_ended_ms
            if is_same_session or is_newer:
                update[GF.LAST_SESSION_ID] = guide_session_id
                update[GF.LAST_ENDED_MS] = ended_at_ms
                update.update(snapshot_fields)
                update[GF.LAST_UPDATED_AT] = fs.SERVER_TIMESTAMP

            if update:
                txn.set(ref, update, merge=True)
            return True

        return _apply(transaction)

    try:
        return await asyncio.to_thread(_txn)
    except Exception as exc:
        logger.warn("guide_usage.store: record_guide_usage failed", {
            "user_id": uid,
            "guide_session_id": guide_session_id,
            "error": str(exc),
        })
        return False
