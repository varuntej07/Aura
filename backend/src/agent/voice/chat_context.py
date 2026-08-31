"""Queries over a LiveKit ``ChatContext`` that several voice modules need.

Each of these was written out separately in three or four places (Buddy, the
Guide supervisor, the Guide task runtime, the action policy), which is how the
same "walk backwards to the last user message" loop ended up existing in three
shapes with three return types. They are pure reads: nothing here mutates the
context, and nothing here looks at what the user actually said.
"""

from __future__ import annotations

from livekit.agents import llm as lk_llm


def latest_user_message(chat_ctx: lk_llm.ChatContext) -> lk_llm.ChatMessage | None:
    """The most recent user message, or None when the context holds none."""
    for item in reversed(chat_ctx.items):
        if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
            return item
    return None


def latest_user_text(chat_ctx: lk_llm.ChatContext) -> str:
    """The most recent user message's text, stripped. "" when there is none."""
    message = latest_user_message(chat_ctx)
    return (message.text_content or "").strip() if message is not None else ""


def latest_user_index(chat_ctx: lk_llm.ChatContext) -> int:
    """Index of the most recent user message, or -1.

    Callers slice from ``index + 1`` to reach "everything that happened after the
    user last spoke", so -1 correctly yields the whole context.
    """
    latest = -1
    for index, item in enumerate(chat_ctx.items):
        if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
            latest = index
    return latest
