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

from google.cloud import firestore as fs
from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import turn_store

ToolResult = dict[str, Any]

# The tools whose effects are externally visible and must not run twice. Mirrors the
# user-requested, state-changing tools in shared/tools.py. Read-only tools (web_surf,
# list_*, query_memory, get_*) are absent: re-running them on a regen is harmless.
SIDE_EFFECTING_TOOLS = frozenset({
    "set_reminder",
    "cancel_reminder",
    "create_calendar_event",
    "send_email",
    "store_memory",
    "track_topic",
    "cancel_tracker",
    "report_feedback",
})

COLLECTION = "tool_idempotency"
FIELD_RESULT = "result"
FIELD_STATUS = "status"
FIELD_TOOL = "tool"
FIELD_CMID = "client_message_id"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"
STATUS_RUNNING = "running"
STATUS_DONE = "done"

# Claim docs are disposable once the turn can no longer be regenerated. Native Firestore
# TTL on `expires_at` reaps them (set the policy alongside the chat_turns one).
IDEMPOTENCY_TTL = timedelta(days=2)


def _key(cmid: str, tool: str, input_data: dict[str, Any]) -> str:
    blob = json.dumps(input_data, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"{cmid}:{tool}:{digest}"


async def run_idempotent(
    user_id: str,
    cmid: str,
    tool_name: str,
    input_data: dict[str, Any],
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
) -> ToolResult:
    """Run ``handler`` exactly once per (cmid, tool, args). On a repeat, return the stored
    result. Fails OPEN: if the idempotency store is unreachable, run the tool normally
    (a rare duplicate is better than dropping a user-requested action)."""
    key = _key(cmid, tool_name, input_data)
    now = datetime.now(UTC)

    try:
        ref = admin_firestore().collection(COLLECTION).document(key)

        def _claim() -> tuple[bool, Any]:
            transaction = admin_firestore().transaction()

            @fs.transactional
            def _txn(txn: fs.Transaction) -> tuple[bool, Any]:
                snap = ref.get(transaction=txn)
                if snap.exists:
                    return False, (snap.to_dict() or {}).get(FIELD_RESULT)
                txn.set(ref, {
                    FIELD_STATUS: STATUS_RUNNING,
                    FIELD_TOOL: tool_name,
                    FIELD_CMID: cmid,
                    FIELD_CREATED_AT: now,
                    FIELD_EXPIRES_AT: now + IDEMPOTENCY_TTL,
                })
                return True, None

            return _txn(transaction)

        claimed, stored = await asyncio.to_thread(_claim)
    except Exception as exc:
        logger.warn("tool_idempotency: claim failed, running tool unguarded (fail-open)", {
            "user_id": user_id, "cmid": cmid, "tool": tool_name, "error": str(exc),
        })
        result = await handler(input_data)
        if isinstance(result, dict) and not result.get("error"):
            await turn_store.record_completed_tool(
                user_id, cmid, tool=tool_name, result=result,
            )
        return result

    if not claimed:
        logger.info("tool_idempotency: duplicate side effect suppressed", {
            "user_id": user_id, "cmid": cmid, "tool": tool_name,
        })
        if isinstance(stored, dict):
            await turn_store.record_completed_tool(
                user_id, cmid, tool=tool_name, result=stored,
            )
            return stored
        # Winner is still running (rare concurrent case) or the result wasn't stored:
        # return a benign confirmation rather than re-running the side effect.
        return {"already_done": True, "user_message": "Already took care of that one."}

    # We own the claim: run the tool for real.
    try:
        result = await handler(input_data)
    except Exception:
        # Release the claim so a genuine retry can run; let the caller map the error.
        await _release(ref)
        raise

    if isinstance(result, dict) and not result.get("error"):
        await _persist_result(ref, result)
        # Record on the turn doc so completion.py can synthesize a confirmation without
        # re-running the LLM (and never regenerate a turn that already did real work).
        await turn_store.record_completed_tool(
            user_id, cmid, tool=tool_name, result=result,
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
        logger.warn("tool_idempotency: receipt read failed (fail-open)", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })
        return {}


async def _persist_result(ref: fs.DocumentReference, result: dict[str, Any]) -> None:
    def _write() -> None:
        ref.set({FIELD_RESULT: result, FIELD_STATUS: STATUS_DONE}, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        # Dedup still holds via the claim doc's existence; the dup path just returns the
        # benign confirmation instead of the exact stored result.
        logger.warn("tool_idempotency: result store failed", {"error": str(exc)})


async def _release(ref: fs.DocumentReference) -> None:
    try:
        await asyncio.to_thread(ref.delete)
    except Exception as exc:
        logger.warn("tool_idempotency: release failed", {"error": str(exc)})
