"""Per-turn idempotency for side-effecting tools.

A backgrounded chat turn is regenerated server-side (see completion.py), which re-runs
the LLM and can re-call a side-effecting tool: create the reminder twice, send the email
twice, track the topic twice. Each side-effecting call claims a key derived from
(client_message_id, tool, args) before it commits. The first call wins and runs; a repeat
(the regenerated turn, or a manual client retry that reuses the message id) reads back the
stored result instead of running the side effect again.

This also closes a pre-existing bug: the client's "retry" reuses the message id, so before
this a manual retry could re-fire tools.

Top-level ``tool_idempotency`` collection so Firestore default-deny rules keep it
backend-only (it is never read by a client).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import turn_store

ToolResult = dict[str, Any]

# The tools whose effects are externally visible and must not run twice. Mirrors the
# user-requested, state-changing tools in shared/tools.py. Read-only tools (web_surf,
# list_*, query_memory, get_*) are absent: re-running them on a regen is harmless.
SIDE_EFFECTING_TOOLS = frozenset(
    {
        "set_reminder",
        "cancel_reminder",
        "create_calendar_event",
        "update_calendar_event",
        "send_email",
        "store_memory",
        "delete_memory",
        "track_topic",
        "cancel_tracker",
        "report_feedback",
        "start_research",
    }
)

COLLECTION = "tool_idempotency"
FIELD_RESULT = "result"
FIELD_STATUS = "status"
FIELD_TOOL = "tool"
FIELD_CMID = "client_message_id"
FIELD_USER_ID = "user_id"
FIELD_CREATED_AT = "created_at"
FIELD_UPDATED_AT = "updated_at"
FIELD_LEASE_UNTIL = "lease_until"
FIELD_OWNER = "owner"
FIELD_ATTEMPTS = "attempts"
FIELD_EXPIRES_AT = "expires_at"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_UNKNOWN = "unknown"

# Claim docs are disposable once the turn can no longer be regenerated. Native Firestore
# TTL on `expires_at` reaps them (set the policy alongside the chat_turns one).
IDEMPOTENCY_TTL = timedelta(days=2)
CLAIM_LEASE = timedelta(minutes=2)


def _key(
    cmid: str,
    tool: str,
    input_data: dict[str, Any],
    *,
    user_id: str = "",
) -> str:
    """Build a stable receipt key while preserving the legacy helper contract."""
    identity: Any = (
        {"user_id": user_id, "input": input_data}
        if user_id
        else input_data
    )
    blob = json.dumps(identity, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{cmid}:{tool}:{digest}"


def _succeeded(result: ToolResult) -> bool:
    if result.get("ok") is False:
        return False
    return not bool(result.get("error"))


async def run_idempotent(
    user_id: str,
    cmid: str,
    tool_name: str,
    input_data: dict[str, Any],
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
) -> ToolResult:
    """Run a side effect under a tenant-scoped lease and durable receipt.

    Existing callers historically fail open when the receipt store is unavailable.
    Provider-level reconciliation and normal application deduplication still apply.
    """
    key = _key(cmid, tool_name, input_data, user_id=user_id)
    now = datetime.now(UTC)
    owner = uuid4().hex

    try:
        ref = admin_firestore().collection(COLLECTION).document(key)

        def _claim() -> tuple[str, Any]:
            transaction = admin_firestore().transaction()

            @fs.transactional
            def _txn(txn: fs.Transaction) -> tuple[str, Any]:
                snap = ref.get(transaction=txn)
                if snap.exists:
                    row = snap.to_dict() or {}
                    if row.get(FIELD_USER_ID) != user_id:
                        return "conflict", None
                    status = row.get(FIELD_STATUS)
                    result = row.get(FIELD_RESULT)
                    if status == STATUS_DONE and isinstance(result, dict):
                        return STATUS_DONE, result
                    if status == STATUS_UNKNOWN:
                        return STATUS_UNKNOWN, result
                    lease_until = row.get(FIELD_LEASE_UNTIL)
                    if (
                        status == STATUS_RUNNING
                        and isinstance(lease_until, datetime)
                        and lease_until > now
                    ):
                        return STATUS_RUNNING, None
                    attempts = int(row.get(FIELD_ATTEMPTS, 1))
                    txn.update(
                        ref,
                        {
                            FIELD_STATUS: STATUS_RUNNING,
                            FIELD_OWNER: owner,
                            FIELD_LEASE_UNTIL: now + CLAIM_LEASE,
                            FIELD_UPDATED_AT: now,
                            FIELD_ATTEMPTS: attempts + 1,
                            FIELD_EXPIRES_AT: now + IDEMPOTENCY_TTL,
                        },
                    )
                    return "claimed", None
                txn.set(
                    ref,
                    {
                        FIELD_STATUS: STATUS_RUNNING,
                        FIELD_TOOL: tool_name,
                        FIELD_CMID: cmid,
                        FIELD_USER_ID: user_id,
                        FIELD_OWNER: owner,
                        FIELD_CREATED_AT: now,
                        FIELD_UPDATED_AT: now,
                        FIELD_LEASE_UNTIL: now + CLAIM_LEASE,
                        FIELD_ATTEMPTS: 1,
                        FIELD_EXPIRES_AT: now + IDEMPOTENCY_TTL,
                    },
                )
                return "claimed", None

            return _txn(transaction)

        claim_status, stored = await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.error(
            "tool_idempotency: claim unavailable, preserving action execution",
            {
                "user_id": user_id,
                "cmid": cmid,
                "tool": tool_name,
                "error_type": type(exc).__name__,
            },
        )
        return await handler(input_data)

    if claim_status == STATUS_DONE and isinstance(stored, dict):
        logger.info(
            "tool_idempotency: duplicate side effect suppressed",
            {
                "user_id": user_id,
                "cmid": cmid,
                "tool": tool_name,
            },
        )
        if not cmid.startswith("voice:"):
            await turn_store.record_completed_tool(
                user_id,
                cmid,
                tool=tool_name,
                result=stored,
            )
        return stored
    if claim_status == STATUS_RUNNING:
        return {
            "ok": False,
            "error": True,
            "code": "action_in_progress",
            "retryable": True,
            "user_message": "That action is still in progress. I won't run it twice.",
        }
    if claim_status in {STATUS_UNKNOWN, "conflict"}:
        return {
            "ok": False,
            "error": True,
            "code": "action_outcome_unknown",
            "retryable": False,
            "user_message": (
                "I can't verify whether that action completed, so I won't repeat it automatically."
            ),
        }

    # We own the claim: run the tool for real.
    try:
        result = await handler(input_data)
    except BaseException as exc:
        await _mark_unknown(ref, owner=owner, error_type=type(exc).__name__)
        raise

    if isinstance(result, dict) and _succeeded(result):
        persisted = await _persist_result(ref, owner=owner, result=result)
        if not persisted:
            return {
                "ok": False,
                "error": True,
                "code": "action_receipt_unavailable",
                "retryable": False,
                "user_message": (
                    "The action may have completed, but I couldn't save proof. "
                    "I won't repeat it automatically."
                ),
            }
        # Record on the turn doc so completion.py can synthesize a confirmation without
        # re-running the LLM (and never regenerate a turn that already did real work).
        if not cmid.startswith("voice:"):
            await turn_store.record_completed_tool(
                user_id,
                cmid,
                tool=tool_name,
                result=result,
            )
    else:
        # A handled tool error (returned, not raised): release so it can be retried.
        await _release(ref)
    return result


async def get_turn_receipts(user_id: str, cmid: str) -> dict[str, dict[str, Any]]:
    """Return the stored successful result per side-effecting tool for this turn.

    Reads the disposable idempotency claims (keyed by ``(cmid, tool, args)``) that
    ``run_idempotent`` persisted with ``STATUS_DONE``. Lets completion.py ground a
    synthesized confirmation and hydrate its reminder card from the ACTUAL tool receipt
    rather than asserting an action from a tool name alone. Filters ``client_message_id``
    (an equality query, so it rides the automatic single-field index — no composite index)
    and screens status in memory. Only runs on the rare background-completion path, so the
    extra read is negligible. Fail-open: returns ``{}`` on any read error.
    """
    if not cmid:
        return {}

    turn = await turn_store.get_turn(user_id, cmid)
    owning_receipts = (turn or {}).get(turn_store.FIELD_TOOL_RECEIPTS)
    if isinstance(owning_receipts, dict):
        return {
            str(tool): result
            for tool, result in owning_receipts.items()
            if isinstance(result, dict)
        }

    def _read() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        query = (
            admin_firestore()
            .collection(COLLECTION)
            .where(filter=FieldFilter(FIELD_CMID, "==", cmid))
        )
        for snap in query.stream():
            row = snap.to_dict() or {}
            if row.get(FIELD_USER_ID) != user_id:
                continue
            if row.get(FIELD_STATUS) != STATUS_DONE:
                continue
            tool = str(row.get(FIELD_TOOL) or "")
            result = row.get(FIELD_RESULT)
            if tool and isinstance(result, dict) and tool not in out:
                out[tool] = result
        return out

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn(
            "tool_idempotency: receipt read failed (fail-open)",
            {
                "user_id": user_id,
                "cmid": cmid,
                "error": str(exc),
            },
        )
        return {}


async def _persist_result(
    ref: fs.DocumentReference,
    *,
    owner: str,
    result: dict[str, Any],
) -> bool:
    now = datetime.now(UTC)

    try:

        def _write() -> bool:
            transaction = admin_firestore().transaction()

            @fs.transactional
            def _txn(txn: fs.Transaction) -> bool:
                snap = ref.get(transaction=txn)
                row = snap.to_dict() if snap.exists else {}
                if (
                    not isinstance(row, dict)
                    or row.get(FIELD_STATUS) != STATUS_RUNNING
                    or row.get(FIELD_OWNER) != owner
                ):
                    return False
                txn.update(
                    ref,
                    {
                        FIELD_RESULT: result,
                        FIELD_STATUS: STATUS_DONE,
                        FIELD_UPDATED_AT: now,
                        FIELD_EXPIRES_AT: now + IDEMPOTENCY_TTL,
                    },
                )
                return True

            return _txn(transaction)

        return await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error(
            "tool_idempotency: result store failed",
            {"error_type": type(exc).__name__},
        )
        await _mark_unknown(ref, owner=owner, error_type="receipt_write_failed")
        return False


async def _mark_unknown(
    ref: fs.DocumentReference,
    *,
    owner: str,
    error_type: str,
) -> None:
    try:

        def _write() -> None:
            transaction = admin_firestore().transaction()

            @fs.transactional
            def _txn(txn: fs.Transaction) -> None:
                snap = ref.get(transaction=txn)
                row = snap.to_dict() if snap.exists else {}
                if not isinstance(row, dict) or row.get(FIELD_OWNER) != owner:
                    return
                txn.update(
                    ref,
                    {
                        FIELD_STATUS: STATUS_UNKNOWN,
                        FIELD_UPDATED_AT: datetime.now(UTC),
                        "error_type": error_type,
                    },
                )

            _txn(transaction)

        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error(
            "tool_idempotency: unknown outcome persistence failed",
            {"error_type": type(exc).__name__},
        )


async def _release(ref: fs.DocumentReference) -> None:
    try:
        await asyncio.to_thread(ref.delete)
    except Exception as exc:
        logger.warn("tool_idempotency: release failed", {"error": str(exc)})
