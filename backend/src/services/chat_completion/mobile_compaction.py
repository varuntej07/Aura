"""Server-owned context compaction for MOBILE text chat.

Mobile chat is client-owned: the app sends its recent history with every turn
and the server keeps only the last ``CHAT_HISTORY_WINDOW`` messages — anything
older was simply invisible to the model. The phone already backs every message
up to ``users/{uid}/chat_sessions/{cid}/messages`` with ``role``, ``text``,
``status``, and a monotonic ``sequence``, which is exactly the shape the desktop
fold consumes, so this module gives mobile the same rolling summary the desktop
surface has: fold everything older than the verbatim window into the shared
``ContextSummary`` schema and store it as server-owned fields on the session doc.

Server-owned session-doc fields (the client's backup writer merges a fixed key
set and never touches these; a privacy delete removes the session doc itself, so
the summary dies with the conversation):

    context_summary               summary JSON (shared/context_summary.py)
    summarized_through_sequence   watermark, default -1
    compaction_claimed_at         transactional lease

Never on the hot path: the chat handler fires this after the reply and does not
await it. Every failure path leaves the watermark unchanged; degradation is
honest — if the client's backup lags, the fold covers what exists, which is
strictly more than the nothing those users had before.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ...config.settings import settings
from ...lib.logger import logger
from ...shared.context_summary import estimate_tokens, is_effectively_empty
from ..firebase import admin_firestore
from .text_compaction import (
    COMPACTION_CLAIM_LEASE,
    COMPACTION_TRIGGER_MESSAGES,
    COMPACTION_TRIGGER_TOKENS,
    MAX_FOLD_MESSAGES,
    fold_summary,
    serialize_messages,
)

MOBILE_COMPACTOR_VERSION = "2026-08-30.1"

FIELD_CONTEXT_SUMMARY = "context_summary"
FIELD_SUMMARIZED_THROUGH_SEQUENCE = "summarized_through_sequence"
FIELD_COMPACTION_CLAIMED_AT = "compaction_claimed_at"


def _session_ref(user_id: str, conversation_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection("chat_sessions")
        .document(conversation_id)
    )


def _read_fold_range(
    user_id: str, conversation_id: str
) -> tuple[str, int, list[dict[str, object]], int] | None:
    """Read summary state and the foldable message range in one pass.

    Returns ``(prior_summary, watermark, messages_to_fold, through_sequence)``
    or None when there is nothing to do. Blocking; run in a thread.
    """
    session_ref = _session_ref(user_id, conversation_id)
    snapshot = session_ref.get()
    if not snapshot.exists:
        return None
    state = snapshot.to_dict() or {}
    prior_summary = str(state.get(FIELD_CONTEXT_SUMMARY) or "")
    watermark = int(state.get(FIELD_SUMMARIZED_THROUGH_SEQUENCE, -1) or -1)

    claimed_at = state.get(FIELD_COMPACTION_CLAIMED_AT)
    if claimed_at is not None:
        try:
            if datetime.now(UTC) - claimed_at < COMPACTION_CLAIM_LEASE:
                return None
        except TypeError:
            pass  # unparseable claim: treat as stale rather than deadlock

    messages_col = session_ref.collection("messages")
    latest = list(
        messages_col.order_by("sequence", direction="DESCENDING").limit(1).stream()
    )
    if not latest:
        return None
    max_sequence = int((latest[0].to_dict() or {}).get("sequence", 0) or 0)

    # Everything the client still resends verbatim stays out of the fold, so the
    # summary and the raw window never overlap.
    desired_through = max_sequence - settings.CHAT_HISTORY_WINDOW
    if desired_through <= watermark:
        return None

    from google.cloud.firestore_v1 import FieldFilter

    docs = list(
        messages_col.where(filter=FieldFilter("sequence", ">", watermark))
        .where(filter=FieldFilter("sequence", "<=", desired_through))
        .order_by("sequence")
        .limit(MAX_FOLD_MESSAGES)
        .stream()
    )
    messages = [doc.to_dict() or {} for doc in docs]
    if not messages:
        return None
    through_sequence = int(messages[-1].get("sequence", desired_through) or 0)
    return prior_summary, watermark, messages, through_sequence


def _claim(user_id: str, conversation_id: str) -> bool:
    """Take the compaction lease transactionally. Blocking; run in a thread."""
    db = admin_firestore()
    session_ref = _session_ref(user_id, conversation_id)
    transaction = db.transaction()

    from google.cloud import firestore as fs

    @fs.transactional
    def _txn(txn) -> bool:
        snapshot = session_ref.get(transaction=txn)
        if not snapshot.exists:
            return False
        claimed_at = (snapshot.to_dict() or {}).get(FIELD_COMPACTION_CLAIMED_AT)
        if claimed_at is not None:
            try:
                if datetime.now(UTC) - claimed_at < COMPACTION_CLAIM_LEASE:
                    return False
            except TypeError:
                pass
        txn.update(session_ref, {FIELD_COMPACTION_CLAIMED_AT: datetime.now(UTC)})
        return True

    return bool(_txn(transaction))


def _store(user_id: str, conversation_id: str, summary: str, through_sequence: int) -> None:
    _session_ref(user_id, conversation_id).update(
        {
            FIELD_CONTEXT_SUMMARY: summary,
            FIELD_SUMMARIZED_THROUGH_SEQUENCE: through_sequence,
            FIELD_COMPACTION_CLAIMED_AT: None,
        }
    )


def _release(user_id: str, conversation_id: str) -> None:
    try:
        _session_ref(user_id, conversation_id).update(
            {FIELD_COMPACTION_CLAIMED_AT: None}
        )
    except Exception:
        pass  # the lease expires on its own; never let cleanup mask the real error


async def load_context_summary(user_id: str, conversation_id: str) -> str:
    """Read the stored summary for the prompt path. Fail-open to ""."""
    try:
        snapshot = await asyncio.to_thread(
            lambda: _session_ref(user_id, conversation_id).get()
        )
        if not snapshot.exists:
            return ""
        return str((snapshot.to_dict() or {}).get(FIELD_CONTEXT_SUMMARY) or "")
    except Exception as exc:
        logger.warn("mobile_compaction: summary read failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return ""


async def maybe_compact(user_id: str, conversation_id: str) -> bool:
    """Run one bounded fold cycle. Fire-and-forget; returns whether a summary stored."""
    try:
        fold_range = await asyncio.to_thread(_read_fold_range, user_id, conversation_id)
        if fold_range is None:
            return False
        prior_summary, watermark, messages, through_sequence = fold_range

        serialized = serialize_messages(messages)
        if not serialized:
            # Nothing foldable (all empty). Advance so the range is not re-read
            # forever; keep the prior summary as-is.
            claimed = await asyncio.to_thread(_claim, user_id, conversation_id)
            if claimed:
                await asyncio.to_thread(
                    _store, user_id, conversation_id, prior_summary, through_sequence
                )
            return False

        # Batching guard: wait for a meaningful chunk unless the backlog is big.
        if (
            len(messages) < COMPACTION_TRIGGER_MESSAGES
            and estimate_tokens(len(serialized)) < COMPACTION_TRIGGER_TOKENS
        ):
            return False

        claimed = await asyncio.to_thread(_claim, user_id, conversation_id)
        if not claimed:
            return False
        try:
            summary = await fold_summary(prior_summary, serialized)
            if is_effectively_empty(summary):
                # Advancing the watermark over real turns on a bad model reply
                # would delete them from context for good. Keep them raw; retry
                # next turn.
                logger.warn("mobile_compaction: empty summary rejected", {
                    "user_id": user_id,
                    "folded_messages": len(messages),
                    "mobile_compactor_version": MOBILE_COMPACTOR_VERSION,
                })
                await asyncio.to_thread(_release, user_id, conversation_id)
                return False
            await asyncio.to_thread(
                _store, user_id, conversation_id, summary, through_sequence
            )
            logger.info("mobile_compaction: summary stored", {
                "user_id": user_id,
                "folded_messages": len(messages),
                "from_sequence": watermark,
                "through_sequence": through_sequence,
                "mobile_compactor_version": MOBILE_COMPACTOR_VERSION,
            })
            return True
        except Exception as exc:
            logger.warn("mobile_compaction: fold failed", {
                "user_id": user_id, "error_type": type(exc).__name__,
            })
            await asyncio.to_thread(_release, user_id, conversation_id)
            return False
    except Exception as exc:
        logger.warn("mobile_compaction: cycle failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return False
