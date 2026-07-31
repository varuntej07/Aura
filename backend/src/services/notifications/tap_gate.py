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
from ...prompts import TAP_GATE_SYSTEM_PROMPT, tap_gate_user_prompt
from ..model_provider import get_model_provider
from .proposal import NotificationProposal

# This runs in the background proactive drain, so allow enough headroom for a
# cold Gemini request while keeping the gate bounded below the one-minute tick.
_TAP_GATE_TIMEOUT_S = 15.0

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
    prompt = tap_gate_user_prompt(
        title=proposal.title,
        body=proposal.body,
        source=proposal.source,
    )
    try:
        raw = await asyncio.wait_for(
            get_model_provider().cheap(
                prompt, system=TAP_GATE_SYSTEM_PROMPT, temperature=0.0
            ),
            timeout=_TAP_GATE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("tap_gate: judge unavailable, failing open (send)", {
            "source": proposal.source, "error": str(exc),
        })
        return True, "gate_unavailable"
    return _parse(str(raw))
