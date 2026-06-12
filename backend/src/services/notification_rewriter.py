"""
notification_rewriter.py: Rewrites reminder messages into engaging push notification copy.
"""

from __future__ import annotations

import asyncio
import random

import anthropic
from anthropic.types import TextBlock
from langfuse import observe

from ..config.settings import settings
from ..lib.logger import logger

_MAX_RETRIES = 2
_BASE_DELAY_S = 1.0  # exponential backoff: 1s, 2s
_TIMEOUT_S = 15.0    # short budget; notification copy is non-critical

# Anthropic exceptions that are worth retrying (transient / server-side)
_RETRYABLE_ERRORS = (
    anthropic.RateLimitError,        # 429
    anthropic.APIConnectionError,    # network blip (includes APITimeoutError)
    anthropic.InternalServerError,   # 500 / 529
)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=_TIMEOUT_S,
        )
    return _client


_SYSTEM_PROMPT = """\
        You are Buddy, the user's best friend. Turn a reminder into a short push notification \
        that sounds like a friend casually reminding them. Plain, warm, simple.

        Rules:
        - Keep it very simple. Say the task in plain words, like you would in a text.
        - One short line. Max 70 characters.
        - No dashes. No quotes in the output.
        - Do not sound like an app or assistant.
        - Do not add drama, stakes, or clever lines. Just the friendly nudge.
        - Output only the notification text. Nothing else.

        Examples:

        "Take medication"
        -> "Alright buddy, its that time to take your meds now."

        "Hit 100 crunches at the gym tonight"
        -> "Hey, those 100 crunches tonight. You got this."

        "Pick flowers for my girlfriend on the way back"
        -> "Don't forgret to grab some flowers on th way back if you wanna make love tonight. just sayin!"

        "Complete STEM OPT application"
        -> "Quick one, let's get that OPT application done today. Don't procrastinate on this"

        "Review budget spreadsheet"
        -> "Time to look over the budget before you run out of cash. better later than never, buddy!!"
    """


@observe(name="notification_rewrite")
async def rewrite_reminder_notification(message: str) -> str:
    """Rewrite a reminder message into engaging push notification copy """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = await _get_client().messages.create(
                model=settings.TIER_EXPERT,
                max_tokens=60,
                system=_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": f"Reminder: {message}"},
                ],
            )
            block = response.content[0]
            if isinstance(block, TextBlock):
                rewritten = block.text.strip()
            else:
                rewritten = message
            logger.info("notification_rewriter: rewrote reminder", {
                "model": settings.TIER_EXPERT,
                "original_len": len(message),
                "rewritten_len": len(rewritten),
                "rewritten_preview": rewritten[:60],
                "attempt": attempt,
            })
            return rewritten
        except _RETRYABLE_ERRORS as exc:
            if attempt == _MAX_RETRIES:
                logger.warn("notification_rewriter: retries exhausted, using original message", {
                    "model": settings.TIER_EXPERT,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                return message
            delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warn("notification_rewriter: retryable error, backing off", {
                "model": settings.TIER_EXPERT,
                "attempt": attempt,
                "delay_s": round(delay, 2),
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            await asyncio.sleep(delay)
        except Exception as exc:
            logger.warn("notification_rewriter: failed, using original message", {
                "model": settings.TIER_EXPERT,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            return message
    return message
