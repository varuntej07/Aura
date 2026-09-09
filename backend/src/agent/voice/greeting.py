"""Situational session opener.

``BuddyAgent.on_enter`` waits at most ``settings.VOICE_GREETING_SEED_BUDGET_S``
for this line before falling back to ``FALLBACK_GREETING``. ``voice_agent``
starts the task right after ``gather_session_context``, so the call overlaps the
pipeline build and normally resolves long before ``on_enter`` runs. Fail-open
everywhere: any error or timeout returns "" and the fallback speaks.

Two things about this module are load-bearing and were both measured rather than
reasoned about.

**The digest is what creates variety, not temperature.** Given only the time of
day, the model returns "hey, good morning!" six times out of six at temperature
0.7 and four out of six at 1.0. Given recency, mood and what they have been
talking about, it returns six distinct openers out of six. So a thin digest does
not produce a greeting generator, it produces a constant with a model call in
front of it - which is the exact bug this module exists to fix. Everything cheap
that is already in ``SessionContext`` therefore goes in, capped.

**The model is pinned, and the pin is a latency decision.** Measured medians on
this task, warm: flash-lite 0.39s, Claude Haiku 4.5 0.80s, gemini-2.5-flash 0.52s
but with a 3.59s outlier, gemini-3.8-flash 2.64s (it rejects minimal thinking
outright with a 400 and its cheapest working level is far past the budget).
Only flash-lite clears ``VOICE_GREETING_SEED_BUDGET_S`` with real headroom. Do
not switch this to a bigger model without re-measuring: a model that misses the
budget does not degrade the greeting, it silently restores the hardcoded one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from ...lib.logger import logger
from ...prompts import VOICE_OPENER_SYSTEM_PROMPT
from ...services.model_provider import get_model_provider
from .context import SessionContext

# Fastest model measured on this task (0.39s median, 0.40s max). Pinned rather
# than riding TIER_CHEAP so a tier change elsewhere cannot quietly push the
# opener past the budget and bring the canned greeting back.
OPENER_MODEL = "gemini-2.5-flash-lite"
# Above the 0.40s measured max, below the 0.9s budget: a hung provider call gets
# abandoned by the model layer before the budget has to paper over it.
_OPENER_ATTEMPT_TIMEOUT_S = 0.8
# 1.0 rather than the 0.7 default. It is a small part of the variety story next
# to digest richness, but it is free.
_OPENER_TEMPERATURE = 1.0

# Cost control. text_chat_context and graph_context are unbounded, and the opener
# runs once per session on every user, so the digest is capped per field and in
# total instead of forwarding whole context blocks.
_MAX_FIELD_CHARS = 320
_MAX_DIGEST_CHARS = 1400


def _clip(value: object) -> str:
    text = str(value or "").strip()
    return text[:_MAX_FIELD_CHARS]


def _time_of_day(session_context: SessionContext) -> str:
    """Local weekday and clock time, which give a cold user something to vary on.

    Falls back to worker-local time when the profile carries no usable zone; a
    slightly wrong hour costs a duller hello, never a wrong statement, because
    the opener never says the time out loud.
    """
    tz_name = str((session_context.profile or {}).get("timezone") or "").strip()
    try:
        now = datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.now()
    except Exception:
        now = datetime.now()
    return f"Local time: {now.strftime('%I:%M %p on %A').lstrip('0')}."


def _age_phrase(session_context: SessionContext) -> str:
    """Bare duration, e.g. "about 3 weeks ago". Empty when the age is unknown.

    Split out from _since_last_session so the same phrase can be attached TO a fact
    ("Last conversation, about 3 weeks ago: ...") instead of only standing beside it.
    A duration on its own line names nothing, which is the whole reason the opener
    could not tell a fresh fact from a stale one.
    """
    raw = str(session_context.last_session_at or "").strip()
    if not raw:
        return ""
    try:
        then = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
        hours = (now - then).total_seconds() / 3600.0
    except Exception:
        return ""
    if hours < 0:
        return ""
    if hours < 1:
        return "less than an hour ago"
    if hours < 24:
        return f"about {int(round(hours))} hours ago"
    days = hours / 24.0
    if days < 14:
        return f"about {int(round(days))} days ago"
    return f"about {int(round(days / 7))} weeks ago"


def _since_last_session(session_context: SessionContext) -> str:
    """How long since the last call, in words. 'Yesterday' and 'three weeks ago'
    open very differently, and this is the cheapest situational signal there is."""
    phrase = _age_phrase(session_context)
    return f"Last call: {phrase}." if phrase else ""


def build_opener_digest(session_context: SessionContext) -> str:
    """Everything cheap and already-fetched that could colour a hello.

    Never empty: the time of day always survives, so the model is never asked to
    greet from nothing.
    """
    mood = " ".join(
        part
        for part in (
            _clip(session_context.dominant_tone),
            _clip(session_context.dominant_emotion),
        )
        if part
    ).strip()
    # Substantive = something the opener could actually REFER to. The last-call
    # timestamp is deliberately not in this list: it is context for tone ("about
    # three weeks ago" opens differently) but it names nothing to ask about, and
    # counting it as history is what let the fabrication back in. A user with a
    # timestamp and no summary got "still recovering from that marathon?".
    # Age rides ON the fact, not beside it. The digest used to carry one free-floating
    # "Last call: about 3 weeks ago" line and then a pile of undated facts, so the
    # model had no way to tell a thing that happened yesterday from a thing that
    # happened in March - and the instruction above it says to pick one and call back
    # to it. That is how a live 2026-09-09 session opened with "how's the party
    # planning going?" about a party long over. Choosing a stale item was the
    # INSTRUCTED behaviour; nothing in the digest made staleness visible.
    #
    # Only last_session_at is actually known here. Everything else is labelled undated
    # rather than silently presented as current: memory rows are ordered by updated_at
    # in fetchers but the timestamp is dropped before formatting, and the aura profile
    # carries none at all. Saying "age unknown" is cheap and true; implying freshness
    # is neither.
    age = _age_phrase(session_context)
    dated_last_call = f", {age}" if age else ", age unknown"
    substantive = [
        session_context.last_session_summary
        and f"Last conversation{dated_last_call}: "
        f"{_clip(session_context.last_session_summary)}",
        session_context.text_chat_context
        and f"Recent app chat: {_clip(session_context.text_chat_context)}",
        session_context.memory_summary
        and "What Buddy remembers, age unknown, could be months old: "
        f"{_clip(session_context.memory_summary)}",
        session_context.aura_summary
        and f"Who they are, age unknown: {_clip(session_context.aura_summary)}",
        mood and f"Mood lately, age unknown: {mood}",
    ]
    substantive = [part for part in substantive if part]
    # Now attached to the last-conversation line above when there is one, so it is only
    # emitted on its own when that line is absent and something else is substantive.
    recency = "" if session_context.last_session_summary else _since_last_session(
        session_context
    )
    # State the absence rather than leaving a hole. Given only a clock reading the
    # model fabricated a shared past - "still at it with that old lawnmower?",
    # "still out there chasing those birds?" - to a user it had never spoken to.
    # Same shape as the incident behind _AURA_PRODUCT_TRUTH: nothing in the prompt
    # was wrong, nothing was THERE, so plausible fiction filled the gap. An explicit
    # no-history fact is what makes "never invent details" obeyable, and it is
    # cheaper than any wording that tries to argue the model out of it.
    if not substantive:
        # Drop recency too. "Last call: about 3 days ago" with nothing attached is
        # still a hook, and the model reached for it: "still chasing that big win?".
        # Recency earns its place only when there is something real to colour.
        recency = ""
        substantive = [
            "No history: nothing is on record about this person or any past call."
        ]
    parts = [_time_of_day(session_context), recency, *substantive]
    return "\n".join(part for part in parts if part)[:_MAX_DIGEST_CHARS]


def start_opener_task(
    session_context: SessionContext, *, session_id: str, user_id: str
) -> "asyncio.Task[str]":
    """Kick off the opener LLM call; resolves to "" on any failure."""

    async def _generate() -> str:
        try:
            digest = build_opener_digest(session_context)
            opener = await get_model_provider().cheap(
                digest,
                system=VOICE_OPENER_SYSTEM_PROMPT,
                model=OPENER_MODEL,
                temperature=_OPENER_TEMPERATURE,
                attempt_timeout_s=_OPENER_ATTEMPT_TIMEOUT_S,
            )
            line = str(opener or "").strip().strip('"')
            # NONE is no longer instructed, but an older cached response or a
            # confused generation should still fall back rather than say it.
            if not line or line.upper() == "NONE" or len(line) > 120:
                return ""
            return line
        except Exception as exc:
            logger.warn("VoiceSession: seeded opener failed open", {
                "session_id": session_id,
                "user_id": user_id,
                "error": str(exc),
            })
            return ""

    return asyncio.create_task(_generate(), name=f"voice-opener-{session_id[:8]}")


async def resolve_opener(task: "asyncio.Task[str] | None", budget_s: float) -> str:
    """Wait briefly for the opener; "" means use the fallback greeting.

    A "" here is worth noticing rather than shrugging at: it means the session
    opened on the hardcoded line, which is indistinguishable from the bug this
    module fixes unless it is logged.
    """
    if task is None:
        logger.warn("VoiceSession: opener task missing, using fallback greeting")
        return ""
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=budget_s)
    except (TimeoutError, asyncio.CancelledError):
        logger.warn("VoiceSession: opener missed the budget, using fallback greeting", {
            "budget_s": budget_s,
            "model": OPENER_MODEL,
        })
        return ""
    except Exception:
        return ""


async def prewarm_opener_model() -> None:
    """Warm the provider client so the first session after a deploy is not cold.

    Measured: the first call in a fresh process took 3.85s against a 0.39s warm
    median, and that gap is HTTP client and telemetry init rather than inference.
    Without this the first session after every deploy or scale-up silently gets
    the fallback greeting.
    """
    try:
        await get_model_provider().cheap(
            "hi",
            system="Reply with the single word ok.",
            model=OPENER_MODEL,
            attempt_timeout_s=_OPENER_ATTEMPT_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("VoiceWorker: opener prewarm failed", {"error": str(exc)})
