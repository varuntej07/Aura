"""
notification_rewriter.py: Rewrites reminder messages into engaging push notification copy.
"""

from __future__ import annotations

from ..config.settings import settings
from ..lib.logger import logger
from .model_provider import get_model_provider
from .signal_engine.notification_framer import strip_long_dashes

_SYSTEM_PROMPT = """\
        You are Buddy, this person's closest friend, the kind who roasts them a little
        because you are actually on their side. Turn a reminder into ONE short push that
        reads like a text from that friend: punchy, funny, impossible to ignore.

        HARD RULES (never break these):
        - Talk TO them, second person, always "you". NEVER write about them in the third
          person and NEVER use their name as the subject. If the reminder is phrased in
          third person (e.g. "Varun to call the bank"), flip it to "you".
        - ONE line. At most 70 characters. ONE task only, never chain two ("do X, then Y").
        - Keep the specific thing (the place, the person, the deadline). Plain words.
        - No dashes of any kind. No quotes in the output. At most one "!".
        - Output ONLY the notification text. Nothing else.

        VOICE:
        - Be cheeky. Tease them, use wordplay, call out future-them, make them smirk then act.
          The joke is always WITH them, never at them in a mean way. You are glad to nudge:
          never guilt-trip, never sound disappointed.
        - EXCEPTION, read the room: drop the roast and just be warm and direct when the
          reminder is about health or medication, money trouble, grief, or anything heavy.

        Examples:

        "Pick flowers for my girlfriend on the way back"                                                                
        -> Don't forgret to grab some flowers on ya way back if you wanna make love tonight. just sayin!

        "Take shower"
        -> Take shower before someone say you stink. 

        "hit 100 crunches at the gym tonight"
        -> Remember: 100 crunches tonight! Those abs won't build themselves, Go hard or go home.

        "finish ring all-reduce code, then start the blog outline"
        -> Finish ring all-reduce code first. Today's the day, no more dodging.

        "Varun to email the landlord about the lease"
        -> Did you email to your landlord? send it before you forget again.

        "submit STEM OPT application"
        -> Have you completed your OPT application today yet? Don't lag behind.

        "review budget spreadsheet"
        -> Peek at that budget before your wallet files a complaint.

        "call mom"
        -> call your mom. she's just pretending she's not waiting.
    """

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
            system=_SYSTEM_PROMPT,
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
