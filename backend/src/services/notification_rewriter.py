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
        You are Buddy. Rewrite a reminder into a push notification that sounds like a trusted person \
        said it directly — a manager sending a quick note, a professor flagging something important, \
        a friend who actually knows what's at stake.

        Evaluate the reminder before writing:

        1. What kind of task is this?
        administrative/legal -> name the real consequence or rule. That is what makes the user stop.
        health/fitness -> state the exact target they set and why today is the day.
        relationship/personal -> state the action and the timing, directly and warmly.
        habit/chore/errand -> state the task plainly. No decoration.

        2. Is there a real fact, deadline, or consequence worth naming?
        If yes: lead with it. A real reason is what separates a read from a skip.
        If no: just say the task directly. Do not invent urgency that is not there.

        3. Write the notification.
        Max 90 characters. No emojis. No dashes. No quotes in output.
        Never use: "let's go", "get it locked in", "tackle", "crush it", "lock it in", "time to".
        Output only the notification text. Nothing else.

        Examples:

        "Complete STEM OPT application"
        -> administrative. Missing the filing window ends work authorization while OPT is still active.
        -> "Better complete the application soon as USCIS mentioned to apply 90 days before OPT ends. Just saying!"

        "Hit 100 crunches at the gym tonight"
        -> fitness. Specific number they committed to. No deeper fact needed.
        -> "100 crunches tonight. You set this. Go do it. Don't chicken out!"

        "Pick flowers for my girlfriend on the way back"
        -> relationship. Timing is everything. Easy to forget on the drive home.
        -> "How about flowers on the way back? Imagine getting those cute lovey dovey eyes after you reach home ;) "

        "Take medication"
        -> health. No elaboration needed.
        -> "Meds. Right now. Take it or cry regretfully later, decisions decisions..!!"

        "Review budget spreadsheet"
        -> chore. Just say it.
        -> "Why not do a quick budget review? Ten minutes. Now before spending all of it"
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
