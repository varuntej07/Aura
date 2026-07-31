"""Durable, server-owned approval state for high-impact chat actions.

Model context is never authority. An action is prepared in one request and can
only be executed from a later authenticated request carrying the exact,
server-recognized confirmation phrase. The stored canonical arguments are the
arguments that execute, so a model cannot change them after approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore

COLLECTION = "pending_actions"
STATUS_PENDING = "pending"
STATUS_EXECUTING = "executing"
STATUS_DONE = "done"
STATUS_UNKNOWN = "unknown"

FIELD_TOOL = "tool"
FIELD_ARGS = "args"
FIELD_ARGS_HASH = "args_hash"
FIELD_STATUS = "status"
FIELD_REQUESTED_CMID = "requested_client_message_id"
FIELD_EXECUTION_CMID = "execution_client_message_id"
FIELD_CREATED_AT = "created_at"
FIELD_UPDATED_AT = "updated_at"
FIELD_EXPIRES_AT = "expires_at"
FIELD_RESULT = "result"

APPROVAL_TTL = timedelta(minutes=15)
EXECUTION_LEASE = timedelta(minutes=2)
EMAIL_CONFIRMATION_PHRASE = "send email"
CALENDAR_CONFIRMATION_PHRASE = "create event"
CONFIRMATION_PHRASES = {
    "send_email": EMAIL_CONFIRMATION_PHRASE,
    "create_calendar_event": CALENDAR_CONFIRMATION_PHRASE,
}


@dataclass(frozen=True)
class ApprovedAction:
    approval_id: str
    tool: str
    args: dict[str, Any]


def canonical_args_hash(args: dict[str, Any]) -> str:
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _collection(user_id: str) -> fs.CollectionReference:
    return admin_firestore().collection("users").document(user_id).collection(COLLECTION)


async def prepare(
    user_id: str,
    *,
    tool: str,
    args: dict[str, Any],
    requested_client_message_id: str,
) -> dict[str, Any]:
    """Persist one pending action or return its existing durable state."""
    now = datetime.now(UTC)
    identity = f"{user_id}\n{tool}\n{requested_client_message_id}\n{canonical_args_hash(args)}"
    approval_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    payload = {
        FIELD_TOOL: tool,
        FIELD_ARGS: args,
        FIELD_ARGS_HASH: canonical_args_hash(args),
        FIELD_STATUS: STATUS_PENDING,
        FIELD_REQUESTED_CMID: requested_client_message_id,
        FIELD_CREATED_AT: now,
        FIELD_UPDATED_AT: now,
        FIELD_EXPIRES_AT: now + APPROVAL_TTL,
    }
    try:
        await asyncio.to_thread(_collection(user_id).document(approval_id).create, payload)
        return {
            "approval_id": approval_id,
            "status": STATUS_PENDING,
        }
    except AlreadyExists:
        # The same model turn can repeat a tool call. Reuse the original pending
        # approval instead of creating multiple confirmation targets.
        snap = await asyncio.to_thread(_collection(user_id).document(approval_id).get)
        existing = snap.to_dict() or {}
        return {
            "approval_id": approval_id,
            "status": existing.get(FIELD_STATUS, STATUS_UNKNOWN),
            "result": existing.get(FIELD_RESULT),
        }


async def pending_action_for_confirmation(
    user_id: str,
    message: str,
) -> ApprovedAction | None:
    """Resolve the latest matching action only for its exact confirmation phrase."""
    normalized_message = " ".join(message.casefold().split())
    requested_tool = next(
        (tool for tool, phrase in CONFIRMATION_PHRASES.items() if normalized_message == phrase),
        None,
    )
    if requested_tool is None:
        return None
    now = datetime.now(UTC)

    def _read() -> ApprovedAction | None:
        candidates: list[tuple[datetime, ApprovedAction]] = []
        query = _collection(user_id).where(FIELD_STATUS, "==", STATUS_PENDING).limit(20)
        for snap in query.stream():
            row = snap.to_dict() or {}
            if row.get(FIELD_TOOL) != requested_tool:
                continue
            expires_at = row.get(FIELD_EXPIRES_AT)
            if not isinstance(expires_at, datetime) or expires_at <= now:
                continue
            args = row.get(FIELD_ARGS)
            created_at = row.get(FIELD_CREATED_AT)
            if not isinstance(args, dict) or not isinstance(created_at, datetime):
                continue
            candidates.append(
                (
                    created_at,
                    ApprovedAction(
                        approval_id=snap.id,
                        tool=requested_tool,
                        args=dict(args),
                    ),
                )
            )
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn(
            "approval_store: pending email lookup failed",
            {"user_id": user_id, "error_type": type(exc).__name__},
        )
        return None


async def claim(
    user_id: str,
    action: ApprovedAction,
    *,
    execution_client_message_id: str,
) -> bool:
    """Atomically bind a pending approval to one execution request."""
    now = datetime.now(UTC)

    def _apply() -> bool:
        ref = _collection(user_id).document(action.approval_id)
        transaction = admin_firestore().transaction()

        @fs.transactional
        def _txn(txn: fs.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            row = snap.to_dict() or {}
            if row.get(FIELD_STATUS) != STATUS_PENDING:
                return False
            expires_at = row.get(FIELD_EXPIRES_AT)
            stored_args = row.get(FIELD_ARGS)
            if (
                not isinstance(expires_at, datetime)
                or expires_at <= now
                or not isinstance(stored_args, dict)
                or canonical_args_hash(stored_args) != canonical_args_hash(action.args)
            ):
                return False
            txn.update(
                ref,
                {
                    FIELD_STATUS: STATUS_EXECUTING,
                    FIELD_EXECUTION_CMID: execution_client_message_id,
                    FIELD_UPDATED_AT: now,
                    FIELD_EXPIRES_AT: now + EXECUTION_LEASE,
                },
            )
            return True

        return _txn(transaction)

    try:
        return await asyncio.to_thread(_apply)
    except Exception as exc:
        logger.warn(
            "approval_store: claim failed",
            {
                "user_id": user_id,
                "approval_id": action.approval_id,
                "error_type": type(exc).__name__,
            },
        )
        return False


async def finish(
    user_id: str,
    approval_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Record the terminal provider outcome. UNKNOWN is deliberately not retried."""
    if status not in {STATUS_DONE, STATUS_UNKNOWN}:
        raise ValueError(f"Unsupported approval terminal status: {status}")
    payload: dict[str, Any] = {
        FIELD_STATUS: status,
        FIELD_UPDATED_AT: datetime.now(UTC),
    }
    if result is not None:
        payload[FIELD_RESULT] = result
    try:
        await asyncio.to_thread(
            _collection(user_id).document(approval_id).set,
            payload,
            merge=True,
        )
    except Exception as exc:
        logger.error(
            "approval_store: terminal state persistence failed",
            {
                "user_id": user_id,
                "approval_id": approval_id,
                "status": status,
                "error_type": type(exc).__name__,
            },
        )
