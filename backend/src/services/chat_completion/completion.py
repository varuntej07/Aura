"""Finish a chat turn that the client disconnected from, then push the reply.

Triggered by a delayed Cloud Task (POST /internal/chat/complete) enqueued when the turn
started, with the per-minute scheduler sweep as a backstop. Flow:

  claim the turn (transaction; only one worker wins) ->
    - already did real work (a tool ran) -> synthesize a confirmation, NO LLM call
    - has attachments -> cannot faithfully regenerate -> fail (client shows retry)
    - otherwise -> regenerate the turn with the same prompt and tools, store the answer
  -> push "Buddy replied" via the orchestrator (committed lane).

Regeneration reuses the SAME client_message_id, so the per-turn tool idempotency guard
(tool_idempotency.py) stops any side effect the live run already committed from firing
again. ``send_email`` is additionally excluded from regeneration outright: it is
irreversible and has no dedup, so a turn that asked to send mail is never auto-sent in the
background.
"""

from __future__ import annotations

from typing import Any

from ...lib.logger import logger
from ...shared.tools import REMINDER_RECEIPT_EXISTING, reminder_ui_payload
from .. import desktop_chat_store
from ..action_intent_policy import blocked_write_reasons_for_text_turn
from ..claude_client import ClaudeClient
from ..tool_executor import (
    ToolExecutor,
    excluded_tools_for_chat_surface,
    resolve_chat_surface_allowed_tools,
)
from . import context_assembler, tool_idempotency, turn_store
from .prompt_builder import build_turn_system_blocks, build_user_content

# Tools whose effect cannot be safely reproduced by a fresh, non-deterministic LLM run.
_REGEN_EXCLUDED_TOOLS = frozenset({"send_email", "start_research"})

# Warm, deterministic confirmations used when a side effect already ran on the live turn,
# so we never re-call the LLM (zero cost, zero risk of a double side effect).
_TOOL_CONFIRMATIONS: dict[str, str] = {
    "set_reminder": "All set, I locked that reminder in for you.",
    "create_calendar_event": "Done, I added that to your calendar.",
    "update_calendar_event": "Done, I updated that event for you.",
    "send_email": "Sent that email for you.",
    "track_topic": "Got it, I'll keep an eye on that and ping you with updates.",
    "store_memory": "Noted, I'll hold onto that.",
    "delete_memory": "Done, I forgot that one.",
    "cancel_reminder": "Done, I cleared that reminder.",
    "cancel_tracker": "Done, I stopped tracking that one.",
    "report_feedback": "Thanks, I passed that along.",
    "save_screen_item": "Done, I saved that screenshot to Screen Saves.",
    "start_research": (
        "I started the research. You can keep working, and I'll notify you when the "
        "sourced brief is ready."
    ),
}

_PREVIEW_MAX_CHARS = 140

# Terminal copy for a desktop turn that cannot be finished. Written into the canonical
# transcript so a restart shows an honest failed turn with Retry instead of a message
# that appears to have vanished.
_DESKTOP_FAILED_TEXT = "Aura could not finish this message. Try sending it again."
_DESKTOP_ATTACHMENT_FAILED_TEXT = (
    "I lost the screen you shared with that message. Capture the screen and ask again."
)


async def _store_desktop_answer(
    user_id: str,
    conversation_id: str,
    cmid: str,
    *,
    text: str,
    status: str,
    reminder: dict[str, Any] | None = None,
) -> bool:
    """Persist this turn's terminal answer to the canonical desktop transcript.

    Returns whether the answer now has a durable home. ``duplicate`` counts as success:
    the foreground stream already stored its own answer under the same deterministic id,
    so the write was unnecessary rather than failed, and the user never sees two replies.

    Called BEFORE turn_store.mark_complete/mark_failed in every branch, because the
    recovery record is the only durable copy of the answer until this lands. A non-desktop
    turn has no transcript to write and is trivially durable.
    """
    if not conversation_id:
        return True
    result = await desktop_chat_store.put_assistant_message(
        user_id, conversation_id, cmid, text=text, status=status, reminder=reminder,
    )
    return result != desktop_chat_store.RESULT_ERROR


def _leave_repairable(user_id: str, cmid: str, branch: str) -> str:
    """Abandon this attempt WITHOUT terminalizing the recovery record.

    The turn stays ``regenerating`` with its claim timestamp, so once
    COMPLETION_CLAIM_LEASE (4 minutes) lapses the backstop sweep re-claims and retries it;
    list_stuck_turns already scans that status. MAX_ATTEMPTS bounds the whole thing at two
    tries. The cost is that a transcript-write failure burns one attempt, which is the right
    trade: marking the turn complete here would strip the recovery fields and leave the
    answer with no durable home and nothing to retry it.

    Repair is NOT immediate. The sweep is gated at ``now_minute % 5 == 3`` in
    handlers/scheduler.py and only looks at turns older than a 5-minute cutoff, so a repair
    lands roughly 5 to 10 minutes out. Any client waiting on one has to keep checking past
    that window, not just past CHAT_COMPLETION_DELAY_SECONDS.
    """
    logger.warn("chat_completion: canonical transcript write failed, left repairable", {
        "user_id": user_id, "cmid": cmid, "branch": branch,
    })
    return "transcript_write_failed"


async def complete_turn(
    user_id: str, cmid: str, session_id: str | None = None
) -> str:
    """Finish a backgrounded turn if it is still pending. Returns a short status string
    for logging. Idempotent and safe to call more than once (the claim transaction makes
    a second call a no-op)."""
    if not user_id or not cmid:
        return "bad_request"

    turn = await turn_store.claim_for_completion(user_id, cmid)
    if turn is None:
        # Already delivered by the foreground stream (client_complete), already completed
        # by a prior task, out of attempts, or never recorded. Nothing to do.
        logger.info("chat_completion: nothing to complete", {"user_id": user_id, "cmid": cmid})
        return "noop"

    session_id = session_id or str(turn.get(turn_store.FIELD_SESSION_ID) or "")
    completed_tools: list[str] = list(turn.get(turn_store.FIELD_COMPLETED_TOOLS) or [])
    # Read before any mark_complete/mark_failed below: both DELETE_FIELD `surface`, so
    # asking afterwards would always come back empty and silently skip the transcript.
    surface = str(turn.get(turn_store.FIELD_SURFACE) or "app")
    desktop_conversation_id = (
        session_id
        if surface == desktop_chat_store.SURFACE
        and desktop_chat_store.is_valid_id(session_id)
        and desktop_chat_store.is_valid_id(cmid)
        else ""
    )

    # The live run already did real, side-effecting work before disconnecting. Do NOT
    # re-run the LLM (it might phrase or act differently); just confirm what happened.
    # `completed_tools` is authoritative (a tool lands there only after it commits without
    # error), so every confirmation line is grounded in a real action, never an LLM claim.
    if completed_tools:
        receipts = await tool_idempotency.get_turn_receipts(user_id, cmid)
        confirmed_tools = [tool for tool in completed_tools if tool in receipts]
        effective_tools = [
            tool
            for tool in confirmed_tools
            if not (
                tool == "set_reminder"
                and isinstance(receipts.get(tool), dict)
                and receipts[tool].get("receipt_status") == REMINDER_RECEIPT_EXISTING
            )
        ]
    else:
        effective_tools = []

    if effective_tools:
        answer = (
            _synthesize_confirmation(effective_tools)
            if effective_tools
            else "I couldn't verify that action, so I won't pretend it went through."
        )
        # Hydrate the reminder card from the actual receipt so a backgrounded reminder
        # arrives as a card, not text-only, matching the live stream's `reminder` payload.
        reminder = reminder_ui_payload(receipts.get("set_reminder"), effective_tools)
        if not await _store_desktop_answer(
            user_id, desktop_conversation_id, cmid,
            text=answer, status=desktop_chat_store.STATUS_COMPLETE, reminder=reminder,
        ):
            return _leave_repairable(user_id, cmid, "synthesized")
        await turn_store.mark_complete(
            user_id, cmid, answer_text=answer, completed_tools=effective_tools,
            reminder=reminder, pushed=True,
        )
        await _push_reply(user_id, cmid, session_id, answer)
        logger.info("chat_completion: synthesized from completed tools", {
            "user_id": user_id, "cmid": cmid, "tools": effective_tools,
            "reminder_card": reminder is not None,
        })
        return "synthesized"

    # Attachment turns were stored text-only (base64 would blow the doc limit), so a regen
    # would answer a different question. Fail rather than mislead; the client offers retry.
    if turn.get(turn_store.FIELD_HAS_ATTACHMENTS):
        # No screenshot is kept anywhere, by design, so there is nothing to regenerate
        # from. Say so plainly instead of answering a question we can no longer see.
        if not await _store_desktop_answer(
            user_id, desktop_conversation_id, cmid,
            text=_DESKTOP_ATTACHMENT_FAILED_TEXT,
            status=desktop_chat_store.STATUS_FAILED,
        ):
            return _leave_repairable(user_id, cmid, "skipped_attachments")
        await turn_store.mark_failed(user_id, cmid)
        logger.info("chat_completion: skipped (had attachments)", {"user_id": user_id, "cmid": cmid})
        return "skipped_attachments"

    answer, reminder, tools = await _regenerate(turn, user_id, cmid)
    if not answer.strip():
        if not await _store_desktop_answer(
            user_id, desktop_conversation_id, cmid,
            text=_DESKTOP_FAILED_TEXT, status=desktop_chat_store.STATUS_FAILED,
        ):
            return _leave_repairable(user_id, cmid, "failed_empty")
        await turn_store.mark_failed(user_id, cmid)
        logger.warn("chat_completion: regeneration produced no answer", {
            "user_id": user_id, "cmid": cmid,
        })
        return "failed_empty"

    if not await _store_desktop_answer(
        user_id, desktop_conversation_id, cmid,
        text=answer, status=desktop_chat_store.STATUS_COMPLETE, reminder=reminder,
    ):
        return _leave_repairable(user_id, cmid, "regenerated")
    await turn_store.mark_complete(
        user_id, cmid, answer_text=answer, completed_tools=tools, reminder=reminder, pushed=True
    )
    await _push_reply(user_id, cmid, session_id, answer)
    logger.info("chat_completion: regenerated and pushed", {
        "user_id": user_id, "cmid": cmid, "answer_len": len(answer), "tools": tools,
    })
    return "regenerated"


async def _regenerate(
    turn: dict[str, Any], user_id: str, cmid: str
) -> tuple[str, dict[str, Any] | None, list[str]]:
    """Re-run the chat turn server-side, consuming the stream to completion. Returns
    (answer_text, reminder_payload_or_None, tool_names)."""
    message = str(turn.get(turn_store.FIELD_MESSAGE) or "")
    history = list(turn.get(turn_store.FIELD_HISTORY) or [])
    tier = str(turn.get(turn_store.FIELD_TIER) or "pro")
    notification_reason = str(turn.get(turn_store.FIELD_NOTIFICATION_REASON) or "")
    surface = str(turn.get(turn_store.FIELD_SURFACE) or "app")
    surface_allowed_tools = resolve_chat_surface_allowed_tools(surface)

    # Same conversation summary the live turn would have used. Without this a
    # regenerated answer is built from a strictly smaller context than the one
    # the user's original attempt had, so a turn that only failed on delivery
    # comes back visibly worse informed than it should be.
    session_id = str(turn.get(turn_store.FIELD_SESSION_ID) or "")
    conversation_summary = ""
    if surface == "desktop" and session_id:
        assembled_context = await context_assembler.assemble_desktop_context(
            user_id,
            session_id,
            current_message_id=cmid,
            fallback_history=history,
        )
        history = assembled_context.history
        conversation_summary = assembled_context.conversation_summary

    system_blocks = await build_turn_system_blocks(
        user_id, message, notification_reason,
        conversation_summary=conversation_summary,
    )
    user_content = build_user_content(message, [])

    tool_executor = ToolExecutor(
        user_id,
        created_via="text",
        client_message_id=cmid,
        session_id=str(turn.get(turn_store.FIELD_SESSION_ID) or ""),
        allowed_tools=surface_allowed_tools,
        blocked_write_reasons=blocked_write_reasons_for_text_turn(message),
        user_tier=tier,
        product_surface=surface,
    )
    claude = ClaudeClient(tool_executor)

    parts: list[str] = []
    reminder: dict[str, Any] | None = None
    tools: list[str] = []
    try:
        async for ev in claude.send_text_turn_stream(
            system_prompt=system_blocks,
            user_content=user_content,
            history=history,
            is_agent=False,
            extra_excluded_tools=(
                _REGEN_EXCLUDED_TOOLS | excluded_tools_for_chat_surface(surface)
            ),
        ):
            etype = ev.get("type")
            if etype == "text_delta":
                parts.append(str(ev.get("delta", "")))
            elif etype == "done":
                metadata = ev.get("metadata") or {}
                tools = list(metadata.get("tool_names") or [])
                if metadata.get("reminder"):
                    reminder = metadata["reminder"]
            elif etype == "error":
                logger.warn("chat_completion: stream error during regeneration", {
                    "user_id": user_id, "cmid": cmid, "message": ev.get("message"),
                })
    except Exception as exc:
        logger.exception("chat_completion: regeneration crashed", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })

    return "".join(parts), reminder_ui_payload(reminder, tools), tools


def _synthesize_confirmation(tools: list[str]) -> str:
    """Confirm EVERY side effect that committed this turn, in order, deduped.

    A turn can run more than one side-effecting tool (e.g. set_reminder + track_topic);
    confirming only the first silently misrepresents the rest. Each line maps to a tool
    already known to have succeeded, so the composite stays truthful."""
    lines: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        if tool in seen or tool not in _TOOL_CONFIRMATIONS:
            continue
        seen.add(tool)
        lines.append(_TOOL_CONFIRMATIONS[tool])
    if not lines:
        return "Done, I took care of that for you."
    return " ".join(lines)


def _preview(text: str, limit: int = _PREVIEW_MAX_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


async def _push_reply(user_id: str, cmid: str, session_id: str, answer: str) -> None:
    """Fire the 'Buddy replied' push via the orchestrator (committed lane). The push only
    ever fires for a turn the client did not ack, i.e. one the user genuinely left, so no
    extra foreground guard is needed here (the client suppresses the banner if it happens
    to be on this chat when the push lands)."""
    from ..notifications import orchestrator
    from ..notifications.proposal import (
        SOURCE_CHAT_REPLY,
        NotificationProposal,
        ProposalKind,
    )

    proposal = NotificationProposal(
        user_id=user_id,
        source=SOURCE_CHAT_REPLY,
        kind=ProposalKind.COMMITTED,
        # One push per turn: the committed lane's ledger dedup drops a second attempt.
        dedup_key=f"chat_reply:{cmid}",
        title="Buddy replied",
        body=_preview(answer),
        data={
            "notification_type": "chat_reply",
            "session_id": session_id,
            "cmid": cmid,
        },
        notification_type="chat_reply",
        # Replace any older pending reply for the same conversation in the tray.
        collapse_key=f"chat_reply:{session_id or cmid}",
    )
    try:
        await orchestrator.submit(proposal)
    except Exception as exc:
        logger.warn("chat_completion: push submit failed", {
            "user_id": user_id, "cmid": cmid, "error": str(exc),
        })
