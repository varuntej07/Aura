"""Canonical Desktop chat history in Firestore.

Layout::

    users/{uid}/desktop_chat_sessions/{conversation_id}
    users/{uid}/desktop_chat_sessions/{conversation_id}/desktop_chat_messages/{message_id}

The subcollection is named `desktop_chat_messages`, not `messages`, on purpose. Firestore
index exemptions and collection-group queries key off the collection-group IDENTIFIER, so a
subcollection called `messages` would join the group that mobile's client-written
`chat_sessions/*/messages` and `threads/*/messages` already occupy, and any per-field
exemption declared for it would silently apply to those too.

This is PERMANENT history, deliberately separate from ``chat_turns`` (2-day TTL
recovery/idempotency state) and from mobile's client-written ``chat_sessions``. Nothing
here ever carries ``expires_at`` and no TTL policy may be registered against it.

The backend is the only writer. Desktop reads it back through /desktop/chat/* and never
touches Firestore directly.

Message ids are deterministic, which is what makes every write idempotent and what lets
the client reconcile a hydrated transcript against its own bubbles without duplicating
them: the user message is stored under the client_message_id it already sent, and the
assistant message under ``{client_message_id}__assistant`` (same convention as
voice_transcript_reconciliation's ``{conversation_id}__v{index}``).

Field names live here (one source of truth, CLAUDE.md data-layer rule).

The session document also carries server-owned CONTEXT state (``context_summary``,
``summarized_through_seq``, ``compaction_claimed_at``): a bounded typed summary of the
turns that have aged out of the raw history window, so a long thread keeps its earlier
decisions without the client re-uploading the whole transcript every turn. It is derived
from the user's own messages, never from a system prompt, and it is absent from
``serialize_session`` so it never reaches a client. See services/chat_completion/
text_compaction.py.

NEVER stored: tokens, screenshot bytes or base64 or signed URLs, tool-progress frames,
system prompts, hidden reasoning, raw provider responses, analytics payloads. Screen
context survives only as the ``has_attachments`` boolean.
"""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore as fs
from google.cloud.firestore_v1.field_path import FieldPath

from ..lib.logger import logger
from .firebase import admin_firestore

SCHEMA_VERSION = 1
SESSIONS_COLLECTION = "desktop_chat_sessions"
MESSAGES_SUBCOLLECTION = "desktop_chat_messages"
SURFACE = "desktop"

MAX_SESSION_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 100
MAX_TEXT_CHARS = 16_000
MAX_PREVIEW_CHARS = 160
MAX_ID_LENGTH = 128

# Both ids arrive from the client and become Firestore document ids, so they are
# validated against a charset that cannot build a different path: no "/", no "." or
# "..", and no reserved "__.*__" form. A UUIDv4 satisfies it.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RESERVED_ID_PATTERN = re.compile(r"^__.*__$")

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

STATUS_SENT = "sent"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_FAILED})

LANE_COLD = "cold"

# put_* outcomes. "duplicate" is a success for the caller's purposes (the canonical
# document is durable); only "error" means nothing landed.
RESULT_CREATED = "created"
RESULT_DUPLICATE = "duplicate"
RESULT_ERROR = "error"

# Session document fields.
FIELD_SCHEMA_VERSION = "schema_version"
FIELD_SURFACE = "surface"
FIELD_CREATED_AT = "created_at"
FIELD_UPDATED_AT = "updated_at"
FIELD_LAST_ACTIVITY_AT = "last_activity_at"
FIELD_MESSAGE_COUNT = "message_count"
FIELD_NEXT_SEQ = "next_seq"
FIELD_PENDING_TURN_COUNT = "pending_turn_count"
FIELD_LAST_MESSAGE_PREVIEW = "last_message_preview"

# Server-owned conversation context. These are deliberately absent from
# serialize_session: that projection is an allowlist and these never reach a
# client. The summary is prompt material, not transcript, and exposing the
# watermark would let a client reason about what the model can still see.
FIELD_CONTEXT_SUMMARY = "context_summary"
FIELD_SUMMARIZED_THROUGH_SEQ = "summarized_through_seq"
FIELD_COMPACTION_CLAIMED_AT = "compaction_claimed_at"

# Message document fields.
FIELD_MESSAGE_ID = "message_id"
FIELD_CLIENT_MESSAGE_ID = "client_message_id"
FIELD_TURN_ID = "turn_id"
FIELD_CONVERSATION_ID = "conversation_id"
FIELD_ROLE = "role"
FIELD_TEXT = "text"
FIELD_STATUS = "status"
FIELD_SEQ = "seq"
FIELD_COMPLETED_AT = "completed_at"
FIELD_SOURCE_LANE = "source_lane"
FIELD_HAS_ATTACHMENTS = "has_attachments"
FIELD_REMINDER = "reminder"


class InvalidCursorError(ValueError):
    """The caller supplied a cursor this user's collection cannot resolve."""


def is_safe_document_id(value: Any) -> bool:
    """Whether a client-supplied id can be used as a Firestore document id at all.

    The minimum guard, applied to EVERY surface: a "/" would build a different document
    path than intended, and "." / ".." / "__x__" are reserved. Deliberately permissive
    about everything else so an existing mobile id format cannot be rejected; the strict
    Desktop contract is is_valid_id below.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_ID_LENGTH:
        return False
    if "/" in value or value in (".", ".."):
        return False
    return not _RESERVED_ID_PATTERN.match(value)


def is_valid_id(value: Any) -> bool:
    """Whether an id meets the strict Desktop canonical-history charset."""
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        return False
    return not _RESERVED_ID_PATTERN.match(value)


def assistant_message_id(cmid: str) -> str:
    """Deterministic id for the one assistant answer that belongs to this turn."""
    return f"{cmid}__assistant"


def _session_ref(user_id: str, conversation_id: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(SESSIONS_COLLECTION)
        .document(conversation_id)
    )


def _messages_collection(user_id: str, conversation_id: str) -> fs.CollectionReference:
    return _session_ref(user_id, conversation_id).collection(MESSAGES_SUBCOLLECTION)


def _preview(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:MAX_PREVIEW_CHARS]


def _encode_cursor(document_id: str) -> str:
    return base64.urlsafe_b64encode(document_id.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> str:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = base64.urlsafe_b64decode(cursor + padding).decode()
    except Exception as exc:
        raise InvalidCursorError("invalid cursor") from exc
    if not value or len(value) > MAX_ID_LENGTH:
        raise InvalidCursorError("invalid cursor")
    return value


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def serialize_session(conversation_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted projection. Only declared fields ever reach a client."""
    return {
        FIELD_CONVERSATION_ID: conversation_id,
        FIELD_SCHEMA_VERSION: int(data.get(FIELD_SCHEMA_VERSION, SCHEMA_VERSION)),
        FIELD_SURFACE: str(data.get(FIELD_SURFACE) or SURFACE),
        FIELD_CREATED_AT: _iso(data.get(FIELD_CREATED_AT)),
        FIELD_UPDATED_AT: _iso(data.get(FIELD_UPDATED_AT)),
        FIELD_LAST_ACTIVITY_AT: _iso(data.get(FIELD_LAST_ACTIVITY_AT)),
        FIELD_MESSAGE_COUNT: int(data.get(FIELD_MESSAGE_COUNT, 0)),
        FIELD_PENDING_TURN_COUNT: int(data.get(FIELD_PENDING_TURN_COUNT, 0)),
        FIELD_LAST_MESSAGE_PREVIEW: str(data.get(FIELD_LAST_MESSAGE_PREVIEW) or ""),
    }


def serialize_message(message_id: str, data: dict[str, Any]) -> dict[str, Any]:
    reminder = data.get(FIELD_REMINDER)
    return {
        FIELD_MESSAGE_ID: message_id,
        FIELD_CLIENT_MESSAGE_ID: str(data.get(FIELD_CLIENT_MESSAGE_ID) or ""),
        FIELD_TURN_ID: str(data.get(FIELD_TURN_ID) or ""),
        FIELD_CONVERSATION_ID: str(data.get(FIELD_CONVERSATION_ID) or ""),
        FIELD_ROLE: str(data.get(FIELD_ROLE) or ""),
        FIELD_TEXT: str(data.get(FIELD_TEXT) or ""),
        FIELD_STATUS: str(data.get(FIELD_STATUS) or ""),
        FIELD_SEQ: int(data.get(FIELD_SEQ, 0)),
        FIELD_CREATED_AT: _iso(data.get(FIELD_CREATED_AT)),
        FIELD_COMPLETED_AT: _iso(data.get(FIELD_COMPLETED_AT)),
        FIELD_SOURCE_LANE: str(data.get(FIELD_SOURCE_LANE) or LANE_COLD),
        FIELD_HAS_ATTACHMENTS: bool(data.get(FIELD_HAS_ATTACHMENTS)),
        FIELD_REMINDER: reminder if isinstance(reminder, dict) else None,
    }


async def put_user_message(
    user_id: str,
    conversation_id: str,
    cmid: str,
    *,
    text: str,
    has_attachments: bool,
    now: datetime | None = None,
) -> str:
    """Idempotently persist the user's message and open or extend its session.

    One transaction reads the session, allocates the ordering seq, creates the message
    under the client_message_id, and bumps the session counters, so a duplicate POST can
    never double-count. Returns RESULT_CREATED, RESULT_DUPLICATE (the message is already
    durable) or RESULT_ERROR.
    """
    if not is_valid_id(conversation_id) or not is_valid_id(cmid):
        return RESULT_ERROR
    when = now or datetime.now(UTC)
    body = text[:MAX_TEXT_CHARS]

    def _apply() -> str:
        session_ref = _session_ref(user_id, conversation_id)
        message_ref = _messages_collection(user_id, conversation_id).document(cmid)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _txn(txn: fs.Transaction) -> str:
            # Every read first: Firestore forbids a read after a write in a transaction.
            message_snap = message_ref.get(transaction=txn)
            session_snap = session_ref.get(transaction=txn)
            if message_snap.exists:
                return RESULT_DUPLICATE
            session = session_snap.to_dict() or {} if session_snap.exists else {}
            seq = int(session.get(FIELD_NEXT_SEQ, 0))
            txn.create(message_ref, {
                FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
                FIELD_MESSAGE_ID: cmid,
                FIELD_CLIENT_MESSAGE_ID: cmid,
                FIELD_TURN_ID: cmid,
                FIELD_CONVERSATION_ID: conversation_id,
                FIELD_ROLE: ROLE_USER,
                FIELD_TEXT: body,
                FIELD_STATUS: STATUS_SENT,
                FIELD_SEQ: seq,
                FIELD_CREATED_AT: when,
                FIELD_SOURCE_LANE: LANE_COLD,
                FIELD_HAS_ATTACHMENTS: bool(has_attachments),
            })
            session_payload: dict[str, Any] = {
                FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
                FIELD_SURFACE: SURFACE,
                FIELD_UPDATED_AT: when,
                FIELD_LAST_ACTIVITY_AT: when,
                FIELD_MESSAGE_COUNT: int(session.get(FIELD_MESSAGE_COUNT, 0)) + 1,
                FIELD_NEXT_SEQ: seq + 1,
                FIELD_PENDING_TURN_COUNT: int(session.get(FIELD_PENDING_TURN_COUNT, 0)) + 1,
                FIELD_LAST_MESSAGE_PREVIEW: _preview(body),
            }
            if not session_snap.exists:
                session_payload[FIELD_CREATED_AT] = when
            txn.set(session_ref, session_payload, merge=True)
            return RESULT_CREATED

        return _txn(transaction)

    try:
        return await asyncio.to_thread(_apply)
    except AlreadyExists:
        return RESULT_DUPLICATE
    except Exception as exc:
        logger.warn("desktop_chat_store: put_user_message failed", {
            "user_id": user_id, "cmid": cmid, "error_type": type(exc).__name__,
        })
        return RESULT_ERROR


async def put_assistant_message(
    user_id: str,
    conversation_id: str,
    cmid: str,
    *,
    text: str,
    status: str,
    reminder: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Persist the one terminal answer for this turn, create-if-absent.

    The foreground stream and the delayed Cloud Task both call this. The deterministic id
    plus create-if-absent is the race guard: exactly one terminal result survives, and a
    repeated call is a harmless RESULT_DUPLICATE. Only the winner moves the session
    counters, so pending_turn_count cannot drift.
    """
    if not is_valid_id(conversation_id) or not is_valid_id(cmid):
        return RESULT_ERROR
    if status not in TERMINAL_STATUSES:
        return RESULT_ERROR
    when = now or datetime.now(UTC)
    body = text[:MAX_TEXT_CHARS]
    message_id = assistant_message_id(cmid)

    def _apply() -> str:
        session_ref = _session_ref(user_id, conversation_id)
        message_ref = _messages_collection(user_id, conversation_id).document(message_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _txn(txn: fs.Transaction) -> str:
            message_snap = message_ref.get(transaction=txn)
            session_snap = session_ref.get(transaction=txn)
            if message_snap.exists:
                return RESULT_DUPLICATE
            session = session_snap.to_dict() or {} if session_snap.exists else {}
            seq = int(session.get(FIELD_NEXT_SEQ, 0))
            payload: dict[str, Any] = {
                FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
                FIELD_MESSAGE_ID: message_id,
                FIELD_CLIENT_MESSAGE_ID: cmid,
                FIELD_TURN_ID: cmid,
                FIELD_CONVERSATION_ID: conversation_id,
                FIELD_ROLE: ROLE_ASSISTANT,
                FIELD_TEXT: body,
                FIELD_STATUS: status,
                FIELD_SEQ: seq,
                FIELD_CREATED_AT: when,
                FIELD_COMPLETED_AT: when,
                FIELD_SOURCE_LANE: LANE_COLD,
                FIELD_HAS_ATTACHMENTS: False,
            }
            if isinstance(reminder, dict) and reminder:
                payload[FIELD_REMINDER] = reminder
            txn.create(message_ref, payload)
            session_payload: dict[str, Any] = {
                FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
                FIELD_SURFACE: SURFACE,
                FIELD_UPDATED_AT: when,
                FIELD_LAST_ACTIVITY_AT: when,
                FIELD_MESSAGE_COUNT: int(session.get(FIELD_MESSAGE_COUNT, 0)) + 1,
                FIELD_NEXT_SEQ: seq + 1,
                # Floored: a repaired or replayed turn must never drive this negative.
                FIELD_PENDING_TURN_COUNT: max(
                    0, int(session.get(FIELD_PENDING_TURN_COUNT, 0)) - 1
                ),
            }
            if body:
                session_payload[FIELD_LAST_MESSAGE_PREVIEW] = _preview(body)
            if not session_snap.exists:
                session_payload[FIELD_CREATED_AT] = when
            txn.set(session_ref, session_payload, merge=True)
            return RESULT_CREATED

        return _txn(transaction)

    try:
        return await asyncio.to_thread(_apply)
    except AlreadyExists:
        return RESULT_DUPLICATE
    except Exception as exc:
        logger.warn("desktop_chat_store: put_assistant_message failed", {
            "user_id": user_id, "cmid": cmid, "error_type": type(exc).__name__,
        })
        return RESULT_ERROR


async def get_assistant_message(
    user_id: str, conversation_id: str, cmid: str
) -> dict[str, Any] | None:
    """Read back the stored answer for a turn, or None. Used by the duplicate-POST
    replay path so a retried request returns the answer it already produced instead of
    paying for a second generation."""
    if not is_valid_id(conversation_id) or not is_valid_id(cmid):
        return None

    def _read() -> dict[str, Any] | None:
        ref = _messages_collection(user_id, conversation_id).document(
            assistant_message_id(cmid)
        )
        snap = ref.get()
        return (snap.to_dict() or {}) if snap.exists else None

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("desktop_chat_store: get_assistant_message failed", {
            "user_id": user_id, "cmid": cmid, "error_type": type(exc).__name__,
        })
        return None


async def get_session(user_id: str, conversation_id: str) -> dict[str, Any] | None:
    if not is_valid_id(conversation_id):
        raise ValueError("invalid conversation id")

    def _read() -> dict[str, Any] | None:
        snap = _session_ref(user_id, conversation_id).get()
        if not snap.exists:
            return None
        return serialize_session(snap.id, snap.to_dict() or {})

    return await asyncio.to_thread(_read)


async def get_context_state(
    user_id: str, conversation_id: str
) -> dict[str, Any] | None:
    """The server-owned context fields for one conversation, unprojected.

    Deliberately NOT serialize_session: that is the client-facing allowlist, and
    the summary and its watermark are prompt material that never leaves the
    server. Returns None when the conversation has no document yet (the first
    turn of a new thread), which the caller treats as "no summary".
    """
    if not is_valid_id(conversation_id):
        return None

    def _read() -> dict[str, Any] | None:
        snap = _session_ref(user_id, conversation_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        return {
            FIELD_CONTEXT_SUMMARY: str(data.get(FIELD_CONTEXT_SUMMARY) or ""),
            FIELD_SUMMARIZED_THROUGH_SEQ: int(data.get(FIELD_SUMMARIZED_THROUGH_SEQ, -1)),
            FIELD_MESSAGE_COUNT: int(data.get(FIELD_MESSAGE_COUNT, 0)),
            FIELD_NEXT_SEQ: int(data.get(FIELD_NEXT_SEQ, 0)),
        }

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        # Fail open: a turn runs on its raw tail rather than not running at all.
        logger.warn("desktop_chat_store: get_context_state failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return None


async def claim_compaction(
    user_id: str,
    conversation_id: str,
    *,
    lease: timedelta,
    now: datetime | None = None,
) -> bool:
    """Atomically take the right to compact this conversation.

    Two turns finishing close together would otherwise both decide the thread is
    over the trigger and both pay for a summary, with the loser's write clobbering
    the winner's. The lease is what stops a crashed compaction from wedging the
    conversation forever: an expired claim can be taken again.

    Fails CLOSED (False). A missed compaction only means the next turn tries
    again; a double compaction costs money and can lose summary content.
    """
    if not is_valid_id(conversation_id):
        return False
    now = now or datetime.now(UTC)

    def _apply() -> bool:
        ref = _session_ref(user_id, conversation_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _txn(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            claimed_at = (snap.to_dict() or {}).get(FIELD_COMPACTION_CLAIMED_AT)
            if isinstance(claimed_at, datetime):
                held = claimed_at if claimed_at.tzinfo else claimed_at.replace(tzinfo=UTC)
                if held > now - lease:
                    return False
            txn.update(ref, {FIELD_COMPACTION_CLAIMED_AT: now})
            return True

        return _txn(transaction)

    try:
        return await asyncio.to_thread(_apply)
    except Exception as exc:
        logger.warn("desktop_chat_store: claim_compaction failed (fail-closed)", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return False


async def store_context_summary(
    user_id: str,
    conversation_id: str,
    *,
    summary: str,
    through_seq: int,
) -> bool:
    """Persist a finished summary and advance the watermark, releasing the claim.

    Watermark and summary move together in one write: a summary stored without its
    watermark would be re-folded on the next turn, and a watermark stored without
    its summary would silently drop the compacted turns from context entirely.
    """
    if not is_valid_id(conversation_id):
        return False

    def _write() -> bool:
        _session_ref(user_id, conversation_id).update({
            FIELD_CONTEXT_SUMMARY: summary,
            FIELD_SUMMARIZED_THROUGH_SEQ: int(through_seq),
            FIELD_COMPACTION_CLAIMED_AT: None,
            FIELD_UPDATED_AT: datetime.now(UTC),
        })
        return True

    try:
        return await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("desktop_chat_store: store_context_summary failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return False


async def release_compaction_claim(user_id: str, conversation_id: str) -> None:
    """Drop the claim without advancing anything, after a failed summary.

    Without this a failed compaction holds its lease for the full duration before
    anyone may retry, so a transient model error would stall compaction on an
    actively growing thread.
    """
    if not is_valid_id(conversation_id):
        return

    def _write() -> None:
        _session_ref(user_id, conversation_id).update({
            FIELD_COMPACTION_CLAIMED_AT: None,
        })

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("desktop_chat_store: release_compaction_claim failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })


async def list_messages_in_seq_range(
    user_id: str,
    conversation_id: str,
    *,
    after_seq: int,
    through_seq: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Messages with after_seq < seq <= through_seq, oldest first.

    The compaction read. Ordered and bounded by ``seq`` alone, which Firestore
    covers automatically, so like list_messages it needs no composite index.
    """
    if not is_valid_id(conversation_id):
        return []
    if limit < 1 or after_seq >= through_seq:
        return []

    def _read() -> list[dict[str, Any]]:
        query = (
            _messages_collection(user_id, conversation_id)
            .where(filter=fs.FieldFilter(FIELD_SEQ, ">", after_seq))
            .where(filter=fs.FieldFilter(FIELD_SEQ, "<=", through_seq))
            .order_by(FIELD_SEQ, direction=fs.Query.ASCENDING)
            .limit(limit)
        )
        return [
            serialize_message(snap.id, snap.to_dict() or {})
            for snap in query.stream()
        ]

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("desktop_chat_store: list_messages_in_seq_range failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return []


async def list_sessions(
    user_id: str, *, cursor: str = "", limit: int = 20
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of this user's Desktop conversations, most recently active first.

    Ordered by a single field plus the document id, which Firestore always covers
    automatically. No composite index exists or is needed, so this read can never fail
    with the missing-index 400 that turn_store.list_stuck_turns warns about.
    """
    if limit < 1 or limit > MAX_SESSION_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_SESSION_PAGE_SIZE}")
    collection = (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(SESSIONS_COLLECTION)
    )
    query = collection.order_by(
        FIELD_LAST_ACTIVITY_AT, direction=fs.Query.DESCENDING
    ).order_by(FieldPath.document_id(), direction=fs.Query.DESCENDING)

    def _read() -> tuple[list[dict[str, Any]], str | None]:
        scoped = query
        if cursor:
            cursor_id = _decode_cursor(cursor)
            cursor_snap = collection.document(cursor_id).get()
            if not cursor_snap.exists:
                raise InvalidCursorError("cursor no longer exists")
            scoped = scoped.start_after(cursor_snap)
        rows = list(scoped.limit(limit + 1).stream())
        page = rows[:limit]
        items = [serialize_session(snap.id, snap.to_dict() or {}) for snap in page]
        next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
        return items, next_cursor

    return await asyncio.to_thread(_read)


async def list_messages(
    user_id: str, conversation_id: str, *, before: str = "", limit: int = 100
) -> tuple[list[dict[str, Any]], str | None]:
    """The NEWEST page of a conversation, returned in send order, plus a cursor for
    walking further back.

    The query runs newest-first and the page is reversed before returning, so the default
    response is the tail of the conversation rather than its opening. That is what a client
    restoring a session actually needs: an ascending-first page would show a long
    conversation's first hundred messages and silently omit everything the user just said.

    ``before`` is the ``older_cursor`` from a previous call. The returned cursor is None
    once the start of history is reached, so a caller can stop without a second probe.

    Ordering is one field plus the document id, which Firestore covers automatically in
    either direction. No composite index exists or is needed, so this read can never fail
    with the missing-index 400 that turn_store.list_stuck_turns warns about.
    """
    if not is_valid_id(conversation_id):
        raise ValueError("invalid conversation id")
    if limit < 1 or limit > MAX_MESSAGE_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_MESSAGE_PAGE_SIZE}")
    collection = _messages_collection(user_id, conversation_id)
    query = collection.order_by(
        FIELD_SEQ, direction=fs.Query.DESCENDING
    ).order_by(FieldPath.document_id(), direction=fs.Query.DESCENDING)

    def _read() -> tuple[list[dict[str, Any]], str | None]:
        scoped = query
        if before:
            cursor_id = _decode_cursor(before)
            cursor_snap = collection.document(cursor_id).get()
            if not cursor_snap.exists:
                raise InvalidCursorError("cursor no longer exists")
            scoped = scoped.start_after(cursor_snap)
        rows = list(scoped.limit(limit + 1).stream())
        page = rows[:limit]
        # page is newest-first here, so its last element is the OLDEST row returned and
        # is what the next page must start after.
        older_cursor = _encode_cursor(page[-1].id) if len(rows) > limit and page else None
        items = [serialize_message(snap.id, snap.to_dict() or {}) for snap in reversed(page)]
        return items, older_cursor

    return await asyncio.to_thread(_read)
