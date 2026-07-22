"""Durable per-user desktop notification outbox.

The backend writes only the versioned, allowlisted contract. Desktop clients
poll with an opaque cursor and acknowledge lifecycle milestones idempotently.
There is intentionally no public send primitive in this module.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.field_path import FieldPath

from ..firebase import admin_firestore
from .proposal import (
    SOURCE_FOLLOWUP,
    SOURCE_ICEBREAKER,
    SOURCE_MEMORY_GRAPH,
    SOURCE_REENGAGE,
    SOURCE_THREAD,
    NotificationProposal,
)

SCHEMA_VERSION = 1
OUTBOX_RETENTION_DAYS = 30
TERMINAL_RETENTION_DAYS = 7
MAX_PAGE_SIZE = 100
MAX_ID_LENGTH = 160

COLLECTION = "desktop_notifications"

FIELD_NOTIFICATION_ID = "notification_id"
FIELD_SCHEMA_VERSION = "schema_version"
FIELD_TYPE = "type"
FIELD_SEVERITY = "severity"
FIELD_TITLE = "title"
FIELD_BODY = "body"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_DEDUP_KEY = "dedup_key"
FIELD_ACTION = "action"
FIELD_RESOURCE_ID = "resource_id"
FIELD_TOAST_POLICY = "toast_policy"
FIELD_SENSITIVE = "sensitive"
FIELD_DELIVERY_STATUS = "delivery_status"
FIELD_RECEIVED_AT = "received_at"
FIELD_SEEN_AT = "seen_at"
FIELD_ACTED_AT = "acted_at"

STATUS_QUEUED = "queued"
STATUS_RECEIVED = "received"
STATUS_SEEN = "seen"
STATUS_ACTED = "acted"
ACK_STATUSES = (STATUS_RECEIVED, STATUS_SEEN, STATUS_ACTED)

NOTIFICATION_TYPES = frozenset({
    "meeting_ready",
    "meeting_needs_attention",
    "meeting_upload_pending",
    "update_ready",
    "auth_required",
    "generic",
})
SEVERITIES = frozenset({"info", "success", "warning", "error"})
TOAST_POLICIES = frozenset({"always", "when_hidden", "inbox_only"})
ACTIONS = frozenset({
    "open_notifications",
    "view_meeting",
    "retry_meeting_upload",
})
SENSITIVE_SOURCES = frozenset({
    SOURCE_FOLLOWUP,
    SOURCE_ICEBREAKER,
    SOURCE_MEMORY_GRAPH,
    SOURCE_REENGAGE,
    SOURCE_THREAD,
})


class InvalidCursorError(ValueError):
    pass


@dataclass(frozen=True)
class OutboxWriteResult:
    accepted: bool
    created: bool


def _collection(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(COLLECTION)
    )


def _bounded_text(value: Any, *, maximum: int) -> str:
    return str(value or "")[:maximum]


def _parse_expiry(value: str, *, now: datetime) -> datetime:
    maximum = now + timedelta(days=OUTBOX_RETENTION_DAYS)
    if not value:
        return maximum
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid desktop notification expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return min(parsed.astimezone(UTC), maximum)


def build_document(
    proposal: NotificationProposal,
    notification_id: str,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Build and strictly validate the backend side of schema version 1."""
    if not notification_id or len(notification_id) > MAX_ID_LENGTH:
        raise ValueError("invalid desktop notification id")
    notification_type = proposal.notification_type
    generic = notification_type not in NOTIFICATION_TYPES
    if generic:
        notification_type = "generic"

    data = proposal.data
    severity = data.get("severity", "info")
    toast_policy = data.get("toast_policy", "when_hidden" if generic else "inbox_only")
    action = data.get("action") or ("open_notifications" if generic else None)
    if severity not in SEVERITIES:
        raise ValueError("invalid desktop notification severity")
    if toast_policy not in TOAST_POLICIES:
        raise ValueError("invalid desktop toast policy")
    if action is not None and action not in ACTIONS:
        raise ValueError("invalid desktop notification action")

    dedup_key = (proposal.dedup_key or notification_id).strip()
    if not dedup_key or len(dedup_key) > 200:
        raise ValueError("invalid desktop notification dedup key")
    resource_id = _bounded_text(data.get("resource_id"), maximum=MAX_ID_LENGTH) or None
    expires_at = _parse_expiry(data.get("expires_at", ""), now=now)

    return {
        FIELD_NOTIFICATION_ID: notification_id,
        FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
        FIELD_TYPE: notification_type,
        FIELD_SEVERITY: severity,
        FIELD_TITLE: _bounded_text(proposal.title, maximum=120),
        FIELD_BODY: _bounded_text(proposal.body, maximum=300),
        FIELD_CREATED_AT: now,
        FIELD_EXPIRES_AT: expires_at,
        FIELD_DEDUP_KEY: dedup_key,
        FIELD_ACTION: action,
        FIELD_RESOURCE_ID: resource_id,
        FIELD_TOAST_POLICY: toast_policy,
        FIELD_SENSITIVE: data.get("sensitive") == "true" or proposal.source in SENSITIVE_SOURCES,
        FIELD_DELIVERY_STATUS: STATUS_QUEUED,
        FIELD_RECEIVED_AT: None,
        FIELD_SEEN_AT: None,
        FIELD_ACTED_AT: None,
    }


async def enqueue(
    proposal: NotificationProposal,
    notification_id: str,
    *,
    now: datetime | None = None,
) -> OutboxWriteResult:
    """Atomically create an outbox row. An existing id is a successful dedup."""
    when = now or datetime.now(UTC)
    doc = build_document(proposal, notification_id, now=when)
    ref = _collection(proposal.user_id).document(notification_id)

    def _create() -> OutboxWriteResult:
        try:
            ref.create(doc)
            return OutboxWriteResult(accepted=True, created=True)
        except AlreadyExists:
            return OutboxWriteResult(accepted=True, created=False)

    return await asyncio.to_thread(_create)


def _encode_cursor(notification_id: str) -> str:
    return base64.urlsafe_b64encode(notification_id.encode()).decode().rstrip("=")


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


def serialize_document(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    timestamp_fields = (
        FIELD_CREATED_AT,
        FIELD_EXPIRES_AT,
        FIELD_RECEIVED_AT,
        FIELD_SEEN_AT,
        FIELD_ACTED_AT,
    )
    for field in timestamp_fields:
        result[field] = _iso(result.get(field))
    return result


async def list_notifications(
    user_id: str,
    *,
    cursor: str = "",
    limit: int = 50,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return one ordered page and an opaque cursor scoped to this user."""
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    collection = _collection(user_id)
    query = collection.order_by(FIELD_CREATED_AT).order_by(
        FieldPath.document_id()
    )
    if cursor:
        cursor_id = _decode_cursor(cursor)
        cursor_snap = await asyncio.to_thread(collection.document(cursor_id).get)
        if not cursor_snap.exists:
            raise InvalidCursorError("cursor no longer exists")
        query = query.start_after(cursor_snap)
    rows = await asyncio.to_thread(lambda: list(query.limit(limit + 1).stream()))
    page = rows[:limit]
    when = now or datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for snap in page:
        data = snap.to_dict() or {}
        data.setdefault(FIELD_NOTIFICATION_ID, snap.id)
        expires_at = data.get(FIELD_EXPIRES_AT)
        if isinstance(expires_at, datetime):
            expires_at = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            if expires_at <= when:
                continue
        items.append(serialize_document(data))
    # Return the last scanned row even at the end of the current result set.
    # Polling clients retain it and ask only for later rows on the next tick.
    next_cursor = _encode_cursor(page[-1].id) if page else None
    return items, next_cursor


async def acknowledge(
    user_id: str,
    notification_id: str,
    *,
    status: str,
    action: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Advance an outbox row monotonically. Repeated acknowledgements are no-ops."""
    if status not in ACK_STATUSES:
        raise ValueError("invalid acknowledgement status")
    if action is not None and action not in ACTIONS:
        raise ValueError("invalid acknowledgement action")
    if status == STATUS_ACTED and action is None:
        raise ValueError("acted acknowledgement requires an action")
    when = now or datetime.now(UTC)
    ref = _collection(user_id).document(notification_id)

    def _update() -> bool:
        snap = ref.get()
        if not snap.exists:
            return False
        current = snap.to_dict() or {}
        expected_action = current.get(FIELD_ACTION)
        if status == STATUS_ACTED and action != expected_action:
            raise ValueError("acknowledgement action does not match notification")
        rank = {STATUS_QUEUED: 0, STATUS_RECEIVED: 1, STATUS_SEEN: 2, STATUS_ACTED: 3}
        if rank.get(str(current.get(FIELD_DELIVERY_STATUS)), 0) >= rank[status]:
            return True

        updates: dict[str, Any] = {FIELD_DELIVERY_STATUS: status}
        if current.get(FIELD_RECEIVED_AT) is None:
            updates[FIELD_RECEIVED_AT] = when
        if status in (STATUS_SEEN, STATUS_ACTED) and current.get(FIELD_SEEN_AT) is None:
            updates[FIELD_SEEN_AT] = when
        if status == STATUS_ACTED:
            updates[FIELD_ACTED_AT] = when
        if status in (STATUS_SEEN, STATUS_ACTED):
            terminal_expiry = when + timedelta(days=TERMINAL_RETENTION_DAYS)
            current_expiry = current.get(FIELD_EXPIRES_AT)
            if isinstance(current_expiry, datetime) and current_expiry.tzinfo is None:
                current_expiry = current_expiry.replace(tzinfo=UTC)
            updates[FIELD_EXPIRES_AT] = (
                min(current_expiry, terminal_expiry)
                if isinstance(current_expiry, datetime)
                else terminal_expiry
            )
        ref.update(updates)
        return True

    return await asyncio.to_thread(_update)
