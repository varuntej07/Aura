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
from ..claude_client import ClaudeClient
from ..tool_executor import ToolExecutor
from . import turn_store
from .prompt_builder import build_turn_system_blocks, build_user_content

# Tools whose effect cannot be safely reproduced by a fresh, non-deterministic LLM run.
_REGEN_EXCLUDED_TOOLS = frozenset({"send_email"})

# Warm, deterministic confirmations used when a side effect already ran on the live turn,
# so we never re-call the LLM (zero cost, zero risk of a double side effect).
_TOOL_CONFIRMATIONS: dict[str, str] = {
    "set_reminder": "All set, I locked that reminder in for you.",
    "create_calendar_event": "Done, I added that to your calendar.",
    "send_email": "Sent that email for you.",
    "track_topic": "Got it, I'll keep an eye on that and ping you with updates.",
    "store_memory": "Noted, I'll hold onto that.",
    "cancel_reminder": "Done, I cleared that reminder.",
    "cancel_tracker": "Done, I stopped tracking that one.",
    "report_feedback": "Thanks, I passed that along.",
}

_PREVIEW_MAX_CHARS = 140


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

    # The live run already did real, side-effecting work before disconnecting. Do NOT
    # re-run the LLM (it might phrase or act differently); just confirm what happened.
    if completed_tools:
        answer = _synthesize_confirmation(completed_tools)
        await turn_store.mark_complete(
            user_id, cmid, answer_text=answer, completed_tools=completed_tools, pushed=True
        )
        await _push_reply(user_id, cmid, session_id, answer)
        logger.info("chat_completion: synthesized from completed tools", {
            "user_id": user_id, "cmid": cmid, "tools": completed_tools,
        })
        return "synthesized"

    # Attachment turns were stored text-only (base64 would blow the doc limit), so a regen
    # would answer a different question. Fail rather than mislead; the client offers retry.
    if turn.get(turn_store.FIELD_HAS_ATTACHMENTS):
        await turn_store.mark_failed(user_id, cmid)
        logger.info("chat_completion: skipped (had attachments)", {"user_id": user_id, "cmid": cmid})
        return "skipped_attachments"

    answer, reminder, tools = await _regenerate(turn, user_id, cmid)
    if not answer.strip():
        await turn_store.mark_failed(user_id, cmid)
        logger.warn("chat_completion: regeneration produced no answer", {
            "user_id": user_id, "cmid": cmid,
        })
        return "failed_empty"

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

    system_blocks = await build_turn_system_blocks(user_id, message, notification_reason)
    user_content = build_user_content(message, [])

    tool_executor = ToolExecutor(user_id, created_via="text", client_message_id=cmid)
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
            user_tier=tier,
            extra_excluded_tools=_REGEN_EXCLUDED_TOOLS,
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

    return "".join(parts), reminder, tools


def _synthesize_confirmation(tools: list[str]) -> str:
    for tool in tools:
        if tool in _TOOL_CONFIRMATIONS:
            return _TOOL_CONFIRMATIONS[tool]
    return "Done, I took care of that for you."


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
