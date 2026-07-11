"""
Outbound-draft store - the writer shared by the voice worker
(``agent/voice/draft_outbound.py``) and the REST refine handler
(``handlers/draft_outbound.py``), and the reader behind ``handlers/drafts.py``.

One document per draft_id holding the LATEST version only: creates write the
full doc, refines overwrite ``text``/``length`` in place. Every write path is
fail-soft (log and swallow) because persistence is secondary to the live draft
flow - a lost write costs a dashboard row, never the spoken reply or the card.

Update paths are update-only-if-exists, deliberately on BOTH the worker and
REST legs: a dashboard delete must be final (a merge-set would silently
resurrect a doc the user just deleted mid-session), and a client-supplied
draft_id over REST must never be able to mint a doc.

Log lines carry ids and ``text_chars`` only, never draft text, matching the
worker's own ``_publish_draft_event`` discipline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F


def _drafts_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.ITEM_PARENT_COLLECTION).document(uid)
        .collection(F.ITEM_SUBCOLLECTION)
    )


def _expiry(now: datetime) -> datetime:
    return now + timedelta(days=F.RETENTION_DAYS)


async def create_draft(
    uid: str,
    draft_id: str,
    *,
    channel: str,
    length: str,
    text: str,
    context_summary: str,
    recipient_hint: str,
    session_id: str,
    now: datetime | None = None,
) -> None:
    """Write one draft doc at the worker-minted draft_id, revision 1.

    Never raises - called right after the ``draft.created`` publish, and the
    card must never lose a draft to a Firestore hiccup."""
    now = now or datetime.now(UTC)
    doc = {
        F.CHANNEL: channel,
        F.LENGTH: length,
        F.TEXT: text,
        F.CONTEXT_SUMMARY: context_summary,
        F.RECIPIENT_HINT: recipient_hint or "",
        F.REVISION: 1,
        F.SESSION_ID: session_id,
        F.CREATED_AT: now.isoformat(),
        F.UPDATED_AT: now.isoformat(),
        F.EXPIRES_AT: _expiry(now),
    }
    try:
        await asyncio.to_thread(_drafts_ref(uid).document(draft_id).set, doc)
        logger.info("drafts.store: created", {
            "user_id": uid, "draft_id": draft_id, "channel": channel,
            "length": length, "text_chars": len(text),
        })
    except Exception as exc:
        logger.warn("drafts.store: create failed", {
            "user_id": uid, "draft_id": draft_id, "error": str(exc),
        })


async def update_draft_text(
    uid: str,
    draft_id: str,
    *,
    text: str,
    length: str,
    now: datetime | None = None,
) -> None:
    """Overwrite the stored draft with its refined text, update-only-if-exists.

    A refine also pushes ``expires_at`` out, so a draft the user is actively
    working never expires under them. Never raises: a missing doc (deleted
    from the dashboard, expired, or its create write failed) is logged and
    skipped - deletion stays final, and REST callers can never mint a doc."""
    now = now or datetime.now(UTC)
    update = {
        F.TEXT: text,
        F.LENGTH: length,
        F.REVISION: fs.Increment(1),
        F.UPDATED_AT: now.isoformat(),
        F.EXPIRES_AT: _expiry(now),
    }
    try:
        await asyncio.to_thread(_drafts_ref(uid).document(draft_id).update, update)
        logger.info("drafts.store: updated", {
            "user_id": uid, "draft_id": draft_id, "length": length,
            "text_chars": len(text),
        })
    except NotFound:
        logger.info("drafts.store: update skipped, doc missing", {
            "user_id": uid, "draft_id": draft_id,
        })
    except Exception as exc:
        logger.warn("drafts.store: update failed", {
            "user_id": uid, "draft_id": draft_id, "error": str(exc),
        })


async def list_drafts(uid: str, *, limit: int = F.LIST_LIMIT) -> list[dict[str, Any]]:
    """Recent drafts, newest first, capped. Fails closed (empty list) rather
    than raising, matching screen_saves.store's read path. Rows whose
    ``expires_at`` has passed are dropped here because the Firestore TTL
    sweeper can lag up to ~72h behind the deadline."""
    if not uid:
        return []

    def _read() -> list[dict[str, Any]]:
        query = (
            _drafts_ref(uid)
            .order_by(F.CREATED_AT, direction="DESCENDING")
            .limit(limit)
        )
        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            expires_at = data.get(F.EXPIRES_AT)
            if expires_at is not None and expires_at < now:
                continue
            rows.append({
                "draft_id": snap.id,
                F.CHANNEL: data.get(F.CHANNEL, ""),
                F.LENGTH: data.get(F.LENGTH, ""),
                F.TEXT: data.get(F.TEXT, ""),
                F.CONTEXT_SUMMARY: data.get(F.CONTEXT_SUMMARY, ""),
                F.RECIPIENT_HINT: data.get(F.RECIPIENT_HINT, ""),
                F.REVISION: data.get(F.REVISION, 1),
                F.CREATED_AT: data.get(F.CREATED_AT, ""),
                F.UPDATED_AT: data.get(F.UPDATED_AT, ""),
            })
        return rows

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("drafts.store: list failed", {"user_id": uid, "error": str(exc)})
        return []


async def delete_draft(uid: str, draft_id: str) -> bool:
    """Hard delete one draft. Never raises - returns False on failure."""
    try:
        await asyncio.to_thread(_drafts_ref(uid).document(draft_id).delete)
        return True
    except Exception as exc:
        logger.warn("drafts.store: delete failed", {
            "user_id": uid, "draft_id": draft_id, "error": str(exc),
        })
        return False
