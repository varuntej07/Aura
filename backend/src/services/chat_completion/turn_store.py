"""Firestore state for one in-flight chat turn.

One doc per turn at ``users/{uid}/chat_turns/{client_message_id}``. It exists so a
turn can finish server-side after the phone disconnects: the live handler writes it
``generating`` at the start, the foreground stream acks it ``client_complete`` when it
finishes normally, and a delayed Cloud Task claims any turn still ``generating`` and
completes it (regenerate or synthesize), then pushes the reply.

Doc id is the client_message_id (the user message id the client already sends), so the
client can read the finished reply back and hydrate it without duplicates.

Field names live here (one source of truth, CLAUDE.md data-layer rule). Every writer
and reader references these constants.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ..firebase import admin_firestore

COLLECTION = "chat_turns"

# Status lifecycle. generating -> (client_complete | regenerating). regenerating ->
# (complete | failed). A terminal status (complete/client_complete/failed) is never
# reopened, which is what makes the Cloud Task / backstop idempotent.
FIELD_STATUS = "status"
STATUS_GENERATING = "generating"
STATUS_REGENERATING = "regenerating"
STATUS_COMPLETE = "complete"            # finished server-side (background), reply stored
STATUS_CLIENT_COMPLETE = "client_complete"  # the foreground stream delivered it; nothing to do
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_CLIENT_COMPLETE, STATUS_FAILED})

FIELD_SESSION_ID = "session_id"
FIELD_MESSAGE = "message"
FIELD_HISTORY = "history"
FIELD_HAS_ATTACHMENTS = "has_attachments"
FIELD_TIER = "tier"
FIELD_NOTIFICATION_REASON = "notification_reason"
FIELD_CREATED_AT = "created_at"
FIELD_CLAIMED_AT = "claimed_at"
FIELD_ATTEMPTS = "attempts"
FIELD_ANSWER_TEXT = "answer_text"
FIELD_COMPLETED_TOOLS = "completed_tools"  # side-effecting tools that already ran (for synthesize/skip)
FIELD_REMINDER = "reminder"                # reminder payload if the turn created one
FIELD_PUSHED = "pushed"
FIELD_EXPIRES_AT = "expires_at"

# How long a finished/abandoned turn doc lingers before native Firestore TTL reaps it.
# (Set a TTL policy on the chat_turns collection-group field `expires_at` in GCP.)
TURN_TTL = timedelta(days=2)

# A turn is regenerated at most this many times across all Cloud Task retries before we
# give up and mark it failed — a hard backstop against a poison turn looping forever.
MAX_ATTEMPTS = 2

# Defensive caps so a turn doc stays well under Firestore's 1 MB limit.
_MAX_HISTORY_TURNS = 12
_MAX_CONTENT_CHARS = 4000


def _ref(user_id: str, cmid: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(COLLECTION)
        .document(cmid)
    )


def _sanitize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only text content, drop attachments (base64 images/docs would blow the
    1 MB doc limit), and cap length. The regenerated turn loses image context, which is
    why a turn with attachments is marked ineligible for regeneration upstream."""
    out: list[dict[str, Any]] = []
    for item in history[-_MAX_HISTORY_TURNS:]:
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = item.get("content")
        if isinstance(content, list):
            text = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        else:
            text = str(content or "")
        if not text:
            continue
        out.append({"role": role, "content": text[:_MAX_CONTENT_CHARS]})
    return out


async def start_turn(
    user_id: str,
    cmid: str,
    *,
    session_id: str | None,
    message: str,
    history: list[dict[str, Any]],
    has_attachments: bool,
    tier: str,
    notification_reason: str = "",
    now: datetime | None = None,
) -> bool:
    """Write the turn doc as ``generating`` at the start of a chat turn. Fail-open:
    returns False on any error and NEVER raises, so a Firestore glitch can never break
    the live chat stream — it just means this turn won't be recoverable in the background.
    """
    if not cmid:
        return False
    now = now or datetime.now(UTC)

    def _write() -> None:
        _ref(user_id, cmid).set({
            FIELD_STATUS: STATUS_GENERATING,
            FIELD_SESSION_ID: session_id or "",
            FIELD_MESSAGE: message[:_MAX_CONTENT_CHARS],
            FIELD_HISTORY: _sanitize_history(history),
            FIELD_HAS_ATTACHMENTS: bool(has_attachments),
            FIELD_TIER: tier,
            FIELD_NOTIFICATION_REASON: notification_reason,
            FIELD_CREATED_AT: now,
            FIELD_ATTEMPTS: 0,
            FIELD_PUSHED: False,
            FIELD_EXPIRES_AT: now + TURN_TTL,
        })

    try:
        await asyncio.to_thread(_write)
        return True
    except Exception as exc:
        logger.warn("turn_store: start_turn failed (live stream unaffected)", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })
        return False


async def mark_client_complete(user_id: str, cmid: str) -> None:
    """The foreground stream finished and the client has the reply: flip generating ->
    client_complete so the pending Cloud Task becomes a no-op. Only-if-generating, so it
    never clobbers a background completion that already won the race. Fail-open."""
    if not cmid:
        return

    def _apply() -> None:
        ref = _ref(user_id, cmid)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _txn(txn: fs.Transaction) -> None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            if (snap.to_dict() or {}).get(FIELD_STATUS) != STATUS_GENERATING:
                return
            txn.update(ref, {FIELD_STATUS: STATUS_CLIENT_COMPLETE})

        _txn(transaction)

    try:
        await asyncio.to_thread(_apply)
    except Exception as exc:
        logger.warn("turn_store: mark_client_complete failed", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })


async def claim_for_completion(
    user_id: str, cmid: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Atomically claim a still-``generating`` turn for background completion.

    Returns the turn data (so the caller can rebuild the prompt) if claimed, else None
    (missing, already terminal, already being regenerated, or out of attempts). The
    transaction is the race guard: only one of {Cloud Task, retry, backstop sweep} can
    win, so a turn is never regenerated twice concurrently. Fails CLOSED (None) on error.
    """
    now = now or datetime.now(UTC)

    def _apply() -> dict[str, Any] | None:
        ref = _ref(user_id, cmid)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _txn(txn: fs.Transaction) -> dict[str, Any] | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if data.get(FIELD_STATUS) != STATUS_GENERATING:
                return None
            attempts = int(data.get(FIELD_ATTEMPTS, 0))
            if attempts >= MAX_ATTEMPTS:
                txn.update(ref, {FIELD_STATUS: STATUS_FAILED})
                return None
            txn.update(ref, {
                FIELD_STATUS: STATUS_REGENERATING,
                FIELD_CLAIMED_AT: now,
                FIELD_ATTEMPTS: attempts + 1,
            })
            return data

        return _txn(transaction)

    try:
        return await asyncio.to_thread(_apply)
    except Exception as exc:
        logger.warn("turn_store: claim_for_completion failed (fail-closed)", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })
        return None


async def mark_complete(
    user_id: str,
    cmid: str,
    *,
    answer_text: str,
    completed_tools: list[str] | None = None,
    reminder: dict[str, Any] | None = None,
    pushed: bool = False,
    now: datetime | None = None,
) -> None:
    """Store the finished reply and flip status -> complete. Fail-open."""
    now = now or datetime.now(UTC)

    def _write() -> None:
        payload: dict[str, Any] = {
            FIELD_STATUS: STATUS_COMPLETE,
            FIELD_ANSWER_TEXT: answer_text,
            FIELD_PUSHED: pushed,
            FIELD_EXPIRES_AT: now + TURN_TTL,
        }
        if completed_tools is not None:
            payload[FIELD_COMPLETED_TOOLS] = completed_tools
        if reminder is not None:
            payload[FIELD_REMINDER] = reminder
        _ref(user_id, cmid).set(payload, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("turn_store: mark_complete failed", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })


async def mark_failed(user_id: str, cmid: str) -> None:
    """Mark the turn failed (background completion could not produce an answer). Fail-open."""
    def _write() -> None:
        _ref(user_id, cmid).set({FIELD_STATUS: STATUS_FAILED}, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("turn_store: mark_failed failed", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })


async def record_completed_tool(user_id: str, cmid: str, *, tool: str) -> None:
    """Append a side-effecting tool name to the turn doc as it commits during the live
    turn. The completion endpoint reads this to decide synthesize-vs-regenerate. Append
    is idempotent (ArrayUnion). Fail-open: this is bookkeeping, never break the turn."""
    if not cmid or not tool:
        return

    def _write() -> None:
        _ref(user_id, cmid).set(
            {FIELD_COMPLETED_TOOLS: fs.ArrayUnion([tool])}, merge=True
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.debug("turn_store: record_completed_tool no-op", {
            "user_id": user_id, "cmid": cmid, "tool": tool, "error": str(exc),
        })


async def list_stuck_turns(
    *, older_than: datetime, limit: int = 50
) -> list[tuple[str, str, str]]:
    """Collection-group scan for turns still ``generating`` past ``older_than`` — the
    backstop for a Cloud Task that failed to enqueue or fire. Returns
    (user_id, cmid, session_id).

    Fails LOUD then returns [] so a swallowed missing-index 400 can never masquerade as
    "no stuck turns" (the exact silent-zero failure mode CLAUDE.md warns about). Requires
    the chat_turns COLLECTION_GROUP index on (status, created_at).
    """

    def _query() -> list[tuple[str, str, str]]:
        db = admin_firestore()
        query = (
            db.collection_group(COLLECTION)
            .where(filter=FieldFilter(FIELD_STATUS, "==", STATUS_GENERATING))
            .where(filter=FieldFilter(FIELD_CREATED_AT, "<=", older_than))
            .limit(limit)
        )
        out: list[tuple[str, str, str]] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            # users/{uid}/chat_turns/{cmid}: parent is chat_turns, its parent is the user doc.
            user_doc = doc.reference.parent.parent
            uid = user_doc.id if user_doc else ""
            if uid:
                out.append((uid, doc.id, str(data.get(FIELD_SESSION_ID) or "")))
        return out

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        logger.error("turn_store: list_stuck_turns FAILED (check the chat_turns index)", {
            "error": str(exc),
        })
        return []


async def get_turn(user_id: str, cmid: str) -> dict[str, Any] | None:
    """Read a turn doc (for the completion endpoint / tests). None if missing or on error."""
    def _read() -> dict[str, Any] | None:
        snap = _ref(user_id, cmid).get()
        return (snap.to_dict() or {}) if snap.exists else None

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("turn_store: get_turn failed", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })
        return None
