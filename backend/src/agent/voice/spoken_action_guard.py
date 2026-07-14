"""Stop spoken side-effect language that the finalized turn did not authorize."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Callable

from .capabilities import Capability

_REMINDER_ACTION_LANGUAGE = re.compile(
    r"\b(?:set|create|schedule|configure|add|make)\b.{0,40}\breminder\b|"
    r"\b(?:what|which|when)\b.{0,45}\breminder\b.{0,30}\b(?:fire|set|schedule)\b|"
    r"\bremind you\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def has_unauthorized_spoken_action(
    text: str,
    capabilities: frozenset[Capability],
) -> bool:
    """Return true when speech invents reminder work outside current policy."""
    return (
        Capability.REMINDER_WRITE not in capabilities
        and bool(_REMINDER_ACTION_LANGUAGE.search(text))
    )


def _chunk_text(item: object) -> str:
    if isinstance(item, str):
        return item
    delta = getattr(item, "delta", None)
    content = getattr(delta, "content", None)
    return content if isinstance(content, str) else ""


async def guard_spoken_action_stream(
    chunks: AsyncIterable[object],
    *,
    capabilities: frozenset[Capability],
    regenerate: Callable[[], AsyncIterable[object]] | None,
    neutral_recovery: str,
    on_blocked: Callable[[], None] | None = None,
) -> AsyncIterable[object]:
    """Hold one sentence at a time, retry once, then fail closed with neutral speech."""
    pending: list[object] = []
    pending_text = ""

    async def _flush() -> AsyncIterable[object]:
        nonlocal pending, pending_text
        if has_unauthorized_spoken_action(pending_text, capabilities):
            if on_blocked is not None:
                on_blocked()
            pending = []
            pending_text = ""
            if regenerate is not None:
                async for retry_item in guard_spoken_action_stream(
                    regenerate(),
                    capabilities=capabilities,
                    regenerate=None,
                    neutral_recovery=neutral_recovery,
                    on_blocked=on_blocked,
                ):
                    yield retry_item
            else:
                yield neutral_recovery
            return
        buffered = pending
        pending = []
        pending_text = ""
        for buffered_item in buffered:
            yield buffered_item

    async for item in chunks:
        text = _chunk_text(item)
        if not text:
            yield item
            continue
        pending.append(item)
        pending_text += text
        if _SENTENCE_END.search(pending_text) or len(pending_text) >= 320:
            blocked = has_unauthorized_spoken_action(pending_text, capabilities)
            async for flushed in _flush():
                yield flushed
            if blocked:
                return

    if pending:
        async for flushed in _flush():
            yield flushed
