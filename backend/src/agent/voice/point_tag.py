"""[POINT:x,y:label[:screenN]] tag extraction from the voice LLM output stream.

The screen-sight prompt teaches the model to append one coordinate tag after its
spoken reply when pointing at something on screen would help ("[POINT:640,360:the
save button]", or "[POINT:none]" when nothing needs pointing). Coordinates are in
the pixel space of the screenshot the model saw this turn.

The tag must NEVER reach TTS, the client captions, or the recorded transcript, so
it is stripped at the single point all three consume: a ``BuddyAgent.llm_node``
override wraps the LLM output stream with :func:`filter_point_tags`. A streaming
holdback buffer catches tags split across chunk boundaries (the model may emit
"[POI" and "NT:640,360:save]" separately). Parsed targets fire a callback; the
agent publishes the first one per reply as an ``element.point`` data-channel
event the desktop client animates.

Fail-open like text_sanitizer.py: malformed tags pass through the regular text
path untouched, a broken filter yields the raw stream, and nothing here may
raise into the pipeline.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass

from livekit.agents import get_job_context
from livekit.agents import llm as lk_llm

from ...lib.logger import logger

# Matches clicky's grammar: [POINT:none] | [POINT:x,y] | [POINT:x,y:label]
# | [POINT:x,y:label:screenN]. The label may not contain ']' or ':'.
POINT_TAG_PATTERN = re.compile(
    r"\[POINT:(?:none|(?P<x>\d+)\s*,\s*(?P<y>\d+)"
    r"(?::(?P<label>[^\]:]+?))?(?::screen(?P<screen>\d+))?)\]"
)

_TAG_PREFIX = "[POINT:"


@dataclass
class PointTarget:
    """One parsed pointing target, in the screenshot's pixel space."""

    x: int
    y: int
    label: str
    screen: int | None


def extract_point_tags(text: str) -> tuple[str, list[PointTarget]]:
    """Strip every complete [POINT:...] tag from ``text``.

    Returns the cleaned text and the parsed targets ([POINT:none] strips to
    nothing and yields no target). Pure and deterministic.
    """
    targets: list[PointTarget] = []

    def _consume(match: re.Match) -> str:
        x = match.group("x")
        y = match.group("y")
        if x is not None and y is not None:
            targets.append(
                PointTarget(
                    x=int(x),
                    y=int(y),
                    label=(match.group("label") or "").strip(),
                    screen=int(s) if (s := match.group("screen")) else None,
                )
            )
        return ""

    cleaned = POINT_TAG_PATTERN.sub(_consume, text)
    return cleaned, targets


def holdback_start(text: str) -> int:
    """The index from which the tail of ``text`` could still become a tag.

    Everything before this index is safe to emit; the tail must wait for more
    chunks. Returns ``len(text)`` when nothing needs holding.
    """
    bracket = text.rfind("[")
    if bracket == -1:
        return len(text)
    tail = text[bracket:]
    if len(tail) < len(_TAG_PREFIX):
        return bracket if _TAG_PREFIX.startswith(tail) else len(text)
    if tail.startswith(_TAG_PREFIX) and "]" not in tail:
        return bracket
    return len(text)


def _chunk_text(item: object) -> str | None:
    """The text a ChatChunk carries, or None when the item carries none."""
    if isinstance(item, str):
        return item
    if isinstance(item, lk_llm.ChatChunk):
        delta = item.delta
        if delta is not None and delta.content:
            return delta.content
    return None


def _with_text(item: object, text: str) -> object:
    """A copy of ``item`` carrying ``text`` instead of its original content."""
    if isinstance(item, str):
        return text
    assert isinstance(item, lk_llm.ChatChunk) and item.delta is not None
    return item.model_copy(update={"delta": item.delta.model_copy(update={"content": text})})


async def filter_point_tags(
    chunks: AsyncIterable[object],
    *,
    on_point: Callable[[PointTarget], None],
) -> AsyncIterator[object]:
    """Wrap the llm_node output stream, stripping [POINT:...] tags as they form.

    Text-bearing items flow through a holdback buffer; everything else
    (tool-call chunks, sentinels, usage chunks) passes through untouched and
    flushes the buffer first so ordering is preserved. ``on_point`` fires once
    per complete coordinate tag and must not raise.
    """
    pending = ""

    def _drain() -> str:
        """Process complete tags in the buffer and cut the emittable prefix."""
        nonlocal pending
        cleaned, targets = extract_point_tags(pending)
        for target in targets:
            try:
                on_point(target)
            except Exception as exc:
                logger.warn("VoiceSession: point callback failed", {"error": str(exc)})
        cut = holdback_start(cleaned)
        emit, pending = cleaned[:cut], cleaned[cut:]
        return emit

    async for item in chunks:
        text = _chunk_text(item)
        if text is None:
            # Non-text item: flush held text first so nothing is reordered.
            if pending:
                emit = _drain()
                if emit:
                    yield emit
            yield item
            continue
        pending += text
        emit = _drain()
        if emit or not isinstance(item, str):
            # ChatChunks are re-emitted even when their text was fully held so
            # ids/usage stay visible downstream; bare strings only when non-empty.
            yield _with_text(item, emit)

    if pending:
        cleaned, targets = extract_point_tags(pending)
        for target in targets:
            try:
                on_point(target)
            except Exception as exc:
                logger.warn("VoiceSession: point callback failed", {"error": str(exc)})
        if cleaned.startswith(_TAG_PREFIX):
            # An unterminated tag at stream end is model garbage, never speech;
            # dropping it beats TTS reading "bracket point six four zero" aloud.
            logger.warn("VoiceSession: dropped unterminated point tag", {
                "tail": cleaned[:48],
            })
        elif cleaned:
            yield cleaned


async def publish_element_point(
    target: PointTarget,
    *,
    frame_id: str,
    session_id: str,
    user_id: str,
) -> None:
    """Push the parsed target down the data channel for the overlay to animate.

    Payload shape matches VoiceServerEvent.fromJson on the client:
    {type: 'element.point', payload: {x, y, label, screen, frame_id}}. The
    frame_id names the screenshot the coordinates live in, so the client maps
    against that frame's monitor geometry. Fail-soft: a lost point event costs
    an animation, never the reply.
    """
    try:
        room = get_job_context().room
        payload = json.dumps({
            "type": "element.point",
            "payload": {
                "x": target.x,
                "y": target.y,
                "label": target.label,
                "screen": target.screen,
                "frame_id": frame_id,
            },
        }).encode("utf-8")
        await room.local_participant.publish_data(payload, reliable=True)
        logger.info("VoiceSession: element.point published", {
            "session_id": session_id,
            "user_id": user_id,
            "x": target.x,
            "y": target.y,
            "label": target.label,
            "frame_id": frame_id,
        })
    except Exception as exc:
        logger.warn("VoiceSession: element.point publish failed", {
            "session_id": session_id, "user_id": user_id, "error": str(exc),
        })
