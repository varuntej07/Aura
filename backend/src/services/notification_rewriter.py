"""
notification_rewriter.py: Rewrites reminder messages into engaging push notification copy.

Returns a title AND a body. The title used to be the constant "Buddy Reminder" set at
the call site, which burned the bold line the eye lands on first: one user received it
34 times in 60 days and never once learned anything from it. The model now names the
actual subject there, and the body carries the nudge.
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple

from ..prompts import NOTIFICATION_REWRITER_SYSTEM_PROMPT

from ..config.settings import settings
from ..lib.logger import logger
from .model_provider import get_model_provider
from .signal_engine.notification_framer import strip_long_dashes

# The model is told these caps but occasionally overshoots or wraps a line in quotes;
# _normalise is the deterministic guarantee (the old prompt had none, which is how a
# 185-char third-person reminder shipped). These mirror the caps stated in the prompt.
_BODY_MAX_CHARS = 90
_TITLE_MAX_CHARS = 40
_WRAP_QUOTE_CHARS = "\"'“”‘’"

# Titles that name nothing. If the model echoes one back it has not written a title,
# so the caller keeps its own default rather than shipping the dead line again.
_DEAD_TITLES = {"buddy reminder", "buddy", "reminder", "reminders", "heads up", "alarm"}


class ReminderCopy(NamedTuple):
    """Push copy for one reminder. ``title`` is empty when the model gave nothing
    usable, and the caller then keeps whatever default it already had."""

    title: str
    body: str


def _normalise(text: str, limit: int = _BODY_MAX_CHARS) -> str:
    """Enforce the hard format the prompt asks for but the model can ignore: a single
    line, no wrapping quotes, no long dashes, within ``limit`` on a word boundary."""
    cleaned = strip_long_dashes(text or "")
    # Collapse any line breaks / runs of whitespace into single spaces.
    cleaned = " ".join(cleaned.split())
    # Drop a matched pair of wrapping quotes the model sometimes adds around the whole line.
    if len(cleaned) >= 2 and cleaned[0] in _WRAP_QUOTE_CHARS and cleaned[-1] in _WRAP_QUOTE_CHARS:
        cleaned = cleaned[1:-1].strip()
    if len(cleaned) <= limit:
        return cleaned
    # Over the cap: cut at the last word boundary that fits, then trim trailing punctuation.
    truncated = cleaned[:limit]
    if " " in truncated:
        truncated = truncated[: truncated.rfind(" ")]
    return truncated.rstrip(" ,;:.")


def _parse(raw: str) -> ReminderCopy | None:
    """Read the {"title","body"} object out of the model's reply.

    Tolerates markdown fences and leading prose. Returns None when there is no usable
    body, which is the only field a reminder cannot ship without.
    """
    text = (raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    body = _normalise(str(data.get("body", "")))
    if not body:
        return None
    title = _normalise(str(data.get("title", "")), _TITLE_MAX_CHARS)
    if title.strip().lower().rstrip(":.!") in _DEAD_TITLES:
        title = ""
    return ReminderCopy(title=title, body=body)


async def rewrite_reminder_notification(message: str) -> ReminderCopy:
    """Rewrite a reminder message into engaging push notification copy.

    Routes through model_provider.cheap() (Gemini Flash): notification copy is
    non-critical background work, so it runs on the cheapest tier and inherits that
    tier's full fallback chain (Flash -> Flash-Lite -> Haiku) + 3 retries + timeout.
    On a total failure (whole chain exhausted, or unparseable output) it degrades to
    the normalised original message with an empty title, so a reminder still fires
    with usable copy and is never dropped.
    """
    try:
        result = await get_model_provider().cheap(
            f"Reminder: {message}",
            system=NOTIFICATION_REWRITER_SYSTEM_PROMPT,
        )
        copy = _parse(str(result))
        if copy is None:
            # Unparseable or empty output would ship a blank push: use the original.
            logger.warn("notification_rewriter: unusable model output, using original", {
                "raw_preview": str(result)[:120],
            })
            return ReminderCopy(title="", body=_normalise(message))
        logger.info("notification_rewriter: rewrote reminder", {
            "model": settings.TIER_CHEAP,
            "original_len": len(message),
            "rewritten_len": len(copy.body),
            "has_title": bool(copy.title),
            "title_preview": copy.title[:40],
            "rewritten_preview": copy.body[:60],
        })
        return copy
    except Exception as exc:
        logger.warn("notification_rewriter: failed, using original message", {
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return ReminderCopy(title="", body=_normalise(message))
