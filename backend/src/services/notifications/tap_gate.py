"""LLM tap-worthiness gate — the last check before a PROACTIVE push goes out.

Every proactive winner the drain selects must earn its tap here: one cheap Gemini
Flash judgment asking "is this specific to THIS person, does it open a real
curiosity gap or offer a clear next step?". Tuned to a BALANCED bar — it kills
generic filler, not borderline-good copy.

Fails OPEN (sends) on any error or timeout: the producer already framed the copy
with its own quality gate, so a judge outage must never silence notifications
(CLAUDE.md: an infra failure must never look like "nothing worth sending").
"""

from __future__ import annotations

import asyncio
import json
import re

from ...lib.logger import logger
from ..model_provider import get_model_provider
from .proposal import NotificationProposal

# A judge that takes longer than this isn't worth blocking a send on — fail open.
_TAP_GATE_TIMEOUT_S = 6.0

_SYSTEM = """\
You are the final quality gate for a push notification from Buddy, a warm AI companion \
who is genuinely into this person's life. Decide if THIS notification is worth \
interrupting them for.

Return ONLY JSON: {"worthy": true or false, "reason": "<=8 words"}

Approve (worthy=true) when the notification is specific, opens a genuine curiosity gap \
or offers a clearly useful next step, and reads like a friend who knows this person.

Reject (worthy=false) ONLY when it is generic filler, a bare headline with no hook, \
vague, clickbait, or could be sent to literally anyone.

Be BALANCED: if it's a reasonable, specific, on-topic message, APPROVE it. Reject only \
clearly low-value sends — when unsure, approve. Silence is better than spam, but a good \
message earning a tap is the goal."""


def _parse(raw: str) -> tuple[bool, str]:
    """Parse the judge JSON. Defaults to worthy=True on any malformed output — the
    gate must never turn a parse hiccup into a silenced notification."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return True, "unparseable_allow"
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return True, "unparseable_allow"
    worthy = data.get("worthy")
    reason = str(data.get("reason", "")).strip()[:60]
    # Only an explicit false rejects; anything else (missing/odd) errs toward sending.
    return (worthy is not False), (reason or ("ok" if worthy is not False else "low_value"))


async def passes(proposal: NotificationProposal) -> tuple[bool, str]:
    """``(worthy, reason)`` for one proactive proposal. Fails OPEN on error/timeout."""
    prompt = (
        f"Title: {proposal.title}\n"
        f"Body: {proposal.body}\n"
        f"Notification kind: {proposal.source}\n\n"
        "Is this worth a tap?"
    )
    try:
        raw = await asyncio.wait_for(
            get_model_provider().cheap(prompt, system=_SYSTEM, temperature=0.0),
            timeout=_TAP_GATE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("tap_gate: judge unavailable, failing open (send)", {
            "source": proposal.source, "error": str(exc),
        })
        return True, "gate_unavailable"
    return _parse(str(raw))
