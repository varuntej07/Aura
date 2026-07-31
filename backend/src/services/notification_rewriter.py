"""
notification_rewriter.py: Rewrites reminder messages into engaging push notification copy.
"""

from __future__ import annotations

from ..prompts import NOTIFICATION_REWRITER_SYSTEM_PROMPT

from ..config.settings import settings
from ..lib.logger import logger
from .model_provider import get_model_provider
from .signal_engine.notification_framer import strip_long_dashes

# The model is told <=70 chars but occasionally overshoots or wraps the line in quotes;
# _normalise is the deterministic guarantee (the old prompt had none, which is how a
# 185-char third-person reminder shipped). 70 mirrors the cap stated in the prompt.
_REMINDER_MAX_CHARS = 70
_WRAP_QUOTE_CHARS = "\"'“”‘’"


def _normalise(text: str) -> str:
    """Enforce the hard format the prompt asks for but the model can ignore: a single
    line, no wrapping quotes, no long dashes, at most 70 chars on a word boundary."""
    cleaned = strip_long_dashes(text or "")
    # Collapse any line breaks / runs of whitespace into single spaces.
    cleaned = " ".join(cleaned.split())
    # Drop a matched pair of wrapping quotes the model sometimes adds around the whole line.
    if len(cleaned) >= 2 and cleaned[0] in _WRAP_QUOTE_CHARS and cleaned[-1] in _WRAP_QUOTE_CHARS:
        cleaned = cleaned[1:-1].strip()
    if len(cleaned) <= _REMINDER_MAX_CHARS:
        return cleaned
    # Over the cap: cut at the last word boundary that fits, then trim trailing punctuation.
    truncated = cleaned[:_REMINDER_MAX_CHARS]
    if " " in truncated:
        truncated = truncated[: truncated.rfind(" ")]
    return truncated.rstrip(" ,;:.")


async def rewrite_reminder_notification(message: str) -> str:
    """Rewrite a reminder message into engaging push notification copy.

    Routes through model_provider.cheap() (Gemini Flash): notification copy is
    non-critical background work, so it runs on the cheapest tier and inherits that
    tier's full fallback chain (Flash -> Flash-Lite -> Haiku) + 3 retries + timeout.
    On a total failure (whole chain exhausted) it degrades to the normalised original
    message, so a reminder still fires with usable copy and is never dropped."""
    try:
        result = await get_model_provider().cheap(
            f"Reminder: {message}",
            system=NOTIFICATION_REWRITER_SYSTEM_PROMPT,
        )
        rewritten = _normalise(result.strip())
        if not rewritten:
            # Empty model output would ship a blank push — fall back to the original.
            return _normalise(message)
        logger.info("notification_rewriter: rewrote reminder", {
            "model": settings.TIER_CHEAP,
            "original_len": len(message),
            "rewritten_len": len(rewritten),
            "rewritten_preview": rewritten[:60],
        })
        return rewritten
    except Exception as exc:
        logger.warn("notification_rewriter: failed, using original message", {
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return _normalise(message)
