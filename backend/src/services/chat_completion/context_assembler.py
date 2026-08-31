"""Authoritative layered context assembly for Desktop text conversations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ...lib.logger import logger
from ...shared.context_summary import estimate_tokens
from .. import desktop_chat_store

CONTEXT_ASSEMBLER_VERSION = "2026-08-16.1"

# This is a prompt budget, not a turn-count guess. The selector measures the actual
# stored text and keeps complete user/assistant exchanges. CHAT_HISTORY_WINDOW remains
# only the compatibility ceiling for released-client fallback history.
RECENT_VERBATIM_TOKEN_BUDGET = 6_000
MIN_RECENT_EXCHANGES = 1
MAX_CANONICAL_MESSAGES = desktop_chat_store.MAX_MESSAGE_PAGE_SIZE


@dataclass(frozen=True, slots=True)
class AssembledTextContext:
    history: list[dict[str, Any]]
    conversation_summary: str
    source: str
    estimated_recent_tokens: int
    summarized_through_seq: int
    oldest_recent_seq: int | None
    context_gap_detected: bool = False


def _content_tokens(text: str) -> int:
    return estimate_tokens(len(text)) + 6


def _complete_exchanges(
    messages: list[dict[str, Any]], *, exclude_message_id: str = ""
) -> list[list[dict[str, Any]]]:
    """Return complete user/assistant exchanges in canonical sequence order.

    A pending user message is intentionally omitted because it is supplied separately as
    the current model input. Orphan assistant records are also omitted; manufacturing a
    prompt for a missing user message is less truthful than logging the storage defect.
    """
    normalized: list[dict[str, Any]] = []
    for message in messages:
        message_id = str(message.get(desktop_chat_store.FIELD_MESSAGE_ID) or "")
        client_message_id = str(
            message.get(desktop_chat_store.FIELD_CLIENT_MESSAGE_ID) or ""
        )
        if exclude_message_id in {message_id, client_message_id}:
            continue
        role = str(message.get(desktop_chat_store.FIELD_ROLE) or "")
        text = str(message.get(desktop_chat_store.FIELD_TEXT) or "").strip()
        if not text:
            continue
        normalized.append({
            "role": role,
            "content": text,
            "seq": int(message.get(desktop_chat_store.FIELD_SEQ, 0)),
            "client_message_id": client_message_id,
        })

    assistants = {
        str(item["client_message_id"]): item
        for item in normalized
        if item["role"] == desktop_chat_store.ROLE_ASSISTANT
        and item["client_message_id"]
    }
    exchanges: list[list[dict[str, Any]]] = []
    for user in normalized:
        if user["role"] != desktop_chat_store.ROLE_USER:
            continue
        assistant = assistants.get(str(user["client_message_id"]))
        if assistant is not None:
            exchanges.append([user, assistant])
    return exchanges


def select_recent_exchanges(
    messages: list[dict[str, Any]],
    *,
    exclude_message_id: str = "",
    token_budget: int = RECENT_VERBATIM_TOKEN_BUDGET,
    max_messages: int | None = None,
) -> list[dict[str, Any]]:
    """Select a measured recent tail without splitting an exchange."""
    exchanges = _complete_exchanges(messages, exclude_message_id=exclude_message_id)
    if not exchanges:
        return []
    message_ceiling = max_messages or max(2, len(messages))
    exchange_ceiling = max(1, message_ceiling // 2)
    selected: list[list[dict[str, Any]]] = []
    tokens = 0
    for exchange in reversed(exchanges):
        exchange_tokens = sum(_content_tokens(str(item["content"])) for item in exchange)
        must_keep = len(selected) < MIN_RECENT_EXCHANGES
        if selected and not must_keep and tokens + exchange_tokens > token_budget:
            break
        if len(selected) >= exchange_ceiling:
            break
        selected.append(exchange)
        tokens += exchange_tokens
    return [item for exchange in reversed(selected) for item in exchange]


def _model_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": str(item["role"]), "content": str(item["content"])}
        for item in messages
    ]


async def assemble_desktop_context(
    user_id: str,
    conversation_id: str,
    *,
    current_message_id: str,
    fallback_history: list[dict[str, Any]],
) -> AssembledTextContext:
    """Build thread context from Firestore, failing open to released-client history."""
    if not desktop_chat_store.is_valid_id(conversation_id):
        return AssembledTextContext(
            history=fallback_history,
            conversation_summary="",
            source="client_fallback_invalid_conversation",
            estimated_recent_tokens=sum(
                _content_tokens(str(item.get("content") or ""))
                for item in fallback_history
            ),
            summarized_through_seq=-1,
            oldest_recent_seq=None,
        )
    try:
        state, page = await asyncio.gather(
            desktop_chat_store.get_context_state(user_id, conversation_id),
            desktop_chat_store.list_messages(
                user_id, conversation_id, limit=MAX_CANONICAL_MESSAGES
            ),
        )
        messages, older_cursor = page
    except Exception as exc:
        logger.warn(
            "text_context: canonical assembly failed, using client fallback",
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "error_type": type(exc).__name__,
            },
        )
        return AssembledTextContext(
            history=fallback_history,
            conversation_summary="",
            source="client_fallback_read_failure",
            estimated_recent_tokens=sum(
                _content_tokens(str(item.get("content") or ""))
                for item in fallback_history
            ),
            summarized_through_seq=-1,
            oldest_recent_seq=None,
        )

    state = state or {}
    watermark = int(state.get(desktop_chat_store.FIELD_SUMMARIZED_THROUGH_SEQ, -1))
    summary = str(state.get(desktop_chat_store.FIELD_CONTEXT_SUMMARY) or "")
    selected = select_recent_exchanges(
        messages, exclude_message_id=current_message_id
    )

    # Never create a missing middle while background compaction catches up. Any complete
    # canonical exchange newer than the summary watermark remains verbatim even when it
    # temporarily exceeds the target budget. Once the summary advances, the same selector
    # naturally returns to its measured recent tail.
    complete = [
        item
        for exchange in _complete_exchanges(messages, exclude_message_id=current_message_id)
        for item in exchange
    ]
    unsummarized = [item for item in complete if int(item["seq"]) > watermark]
    budget_override_applied = False
    if selected and unsummarized and int(unsummarized[0]["seq"]) < int(selected[0]["seq"]):
        selected = unsummarized
        budget_override_applied = True

    oldest_available = int(complete[0]["seq"]) if complete else None
    gap = bool(
        older_cursor
        and oldest_available is not None
        and oldest_available > watermark + 1
    )
    if gap:
        logger.error(
            "text_context: unsummarized canonical history exceeds assembly page",
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "summarized_through_seq": watermark,
                "oldest_available_seq": oldest_available,
                "page_limit": MAX_CANONICAL_MESSAGES,
            },
        )

    history = _model_history(selected)
    estimated_tokens = sum(
        _content_tokens(str(item.get("content") or "")) for item in history
    )
    if budget_override_applied and estimated_tokens > 3 * RECENT_VERBATIM_TOKEN_BUDGET:
        # The keep-verbatim override is correct (no missing middle), but a prompt
        # 3x over budget means the background compactor has been stalling for many
        # turns — the situation is self-inflicted growth, so heal it here instead
        # of waiting for the post-turn task that has evidently not been landing.
        # The compaction lease makes a concurrent attempt safe; deferred import
        # because text_compaction imports select_recent_exchanges from this module.
        logger.error(
            "text_context: budget override runaway, forcing compaction",
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "estimated_recent_tokens": estimated_tokens,
                "token_budget": RECENT_VERBATIM_TOKEN_BUDGET,
                "summarized_through_seq": watermark,
                "unsummarized_messages": len(unsummarized),
            },
        )
        from . import text_compaction

        asyncio.create_task(
            text_compaction.maybe_compact(user_id, conversation_id),
            name=f"chat-compact-runaway-{conversation_id[:8]}",
        )
    logger.info(
        "text_context: assembled",
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "source": "firestore",
            "history_messages": len(history),
            "estimated_recent_tokens": estimated_tokens,
            "summarized_through_seq": watermark,
            "context_gap_detected": gap,
            "version": CONTEXT_ASSEMBLER_VERSION,
        },
    )
    return AssembledTextContext(
        history=history,
        conversation_summary=summary,
        source="firestore",
        estimated_recent_tokens=estimated_tokens,
        summarized_through_seq=watermark,
        oldest_recent_seq=(int(selected[0]["seq"]) if selected else None),
        context_gap_detected=gap,
    )
