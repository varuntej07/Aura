"""LLM tap-worthiness gate — the last check before a PROACTIVE push goes out.

Every proactive winner the drain selects must earn its tap here: one cheap Gemini
Flash judgment asking "is this specific to THIS person, does it open a real
curiosity gap or offer a clear next step?". Tuned to a BALANCED bar — it kills
generic filler, not borderline-good copy.

The deterministic half is a STRUCTURAL precondition only (a push must have a
destination and a payoff); it no longer vetoes on redundancy, because that
silently outranked the judge it fronts. A model VERDICT of "not worthy" drops.
A model that is merely UNREACHABLE returns REASON_GATE_UNAVAILABLE, and the drain
holds rather than drops — infrastructure uncertainty is not a value judgment.
"""

from __future__ import annotations

import asyncio
import json
import re
from difflib import SequenceMatcher

from ...lib.logger import logger
from ...prompts import TAP_GATE_SYSTEM_PROMPT, tap_gate_user_prompt
from ..model_provider import get_model_provider
from .proposal import NotificationProposal

# This runs in the background proactive drain, so allow enough headroom for a
# cold Gemini request while keeping the gate bounded below the one-minute tick.
_TAP_GATE_TIMEOUT_S = 15.0

# Infrastructure failure, NOT a verdict on the copy. The drain holds on this reason
# instead of dropping, so a slow judge cannot discard a good notification.
REASON_GATE_UNAVAILABLE = "gate_unavailable"

_CONTENT_TOKEN = re.compile(r"[a-z0-9]+")
_LOW_INFORMATION_TOKENS = frozenset({
    "a", "about", "after", "and", "are", "at", "back", "can", "for", "from",
    "here", "i", "in", "is", "it", "more", "of", "on", "the", "this", "to",
    "up", "want", "we", "with", "you", "your",
})
# Redundancy between the push and its chat seed. These no longer DROP a proposal;
# they only decide whether the judge is told the opener adds nothing.
_REDUNDANT_SIMILARITY = 0.72
_MIN_NOVEL_TOKENS = 4

_ARTIFACT_KEYS = (
    "briefing_date",
    "content_id",
    "research_id",
    "meeting_id",
    "reminder_id",
)


def _meaningful_tokens(value: str) -> set[str]:
    return {
        token
        for token in _CONTENT_TOKEN.findall(value.casefold())
        if token not in _LOW_INFORMATION_TOKENS
    }


def _destination_context(
    proposal: NotificationProposal,
) -> tuple[str, str, bool, bool]:
    data = proposal.data or {}
    has_chat_seed = bool(data.get("opening_chat_message") or data.get("initial_message"))
    destination = str(data.get("deep_link") or ("chat" if has_chat_seed else ""))
    opening = str(
        data.get("opening_chat_message") or data.get("initial_message") or ""
    ).strip()
    url = str(data.get("url") or "").strip()
    artifact = next(
        (
            f"{key}={data[key]}"
            for key in _ARTIFACT_KEYS
            if str(data.get(key) or "").strip()
        ),
        "",
    )
    replies = str(data.get("suggested_replies") or "").strip()
    interactive = bool(
        str(data.get("thread_id") or "").strip() and replies not in {"", "[]"}
    )
    payoff_parts = [part for part in (url, artifact, opening) if part]
    if interactive:
        payoff_parts.append(f"reply options={replies[:240]}")
    return (
        destination or ("url" if url else ""),
        " | ".join(payoff_parts),
        bool(url or artifact),
        interactive,
    )


def _has_incremental_tap_value(
    proposal: NotificationProposal,
) -> tuple[bool, str, str, str, str]:
    """Structural precondition only: a push must have a destination and a payoff.

    Redundancy between the push copy and the chat seed is NOT decided here. It is
    measured and handed to the judge as one more observation, because a hard-coded
    similarity threshold silently outranked the model it fronts: a producer that
    deliberately seeds the chat with its own opener (so Buddy continues its line
    instead of starting blank) scores as maximally redundant by construction, and
    was dropped before the judge ever saw it. See NOTIFICATION_ABSTRACTION_AUDIT
    finding 9.
    """
    destination, payoff, has_artifact, interactive = _destination_context(proposal)
    if not destination or not payoff:
        return False, "no_destination_payoff", destination, payoff, ""
    if has_artifact or interactive:
        return True, "destination_present", destination, payoff, ""

    data = proposal.data or {}
    opening = str(
        data.get("opening_chat_message") or data.get("initial_message") or ""
    ).strip()
    push = f"{proposal.title} {proposal.body}".strip()
    incremental = _meaningful_tokens(opening) - _meaningful_tokens(push)
    similarity = SequenceMatcher(None, push.casefold(), opening.casefold()).ratio()
    if similarity >= _REDUNDANT_SIMILARITY or len(incremental) < _MIN_NOVEL_TOKENS:
        return (
            True,
            "redundant_destination",
            destination,
            payoff,
            "The chat opener restates the push copy and adds no new information.",
        )
    return True, "incremental_destination", destination, payoff, ""


def _parse(raw: str) -> tuple[bool, str]:
    """Parse judge JSON; malformed or ambiguous output cannot authorize a push."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return False, "unparseable_reject"
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return False, "unparseable_reject"
    worthy = data.get("worthy")
    reason = str(data.get("reason", "")).strip()[:60]
    # Only an explicit true authorizes an optional interruption.
    return (worthy is True), (reason or ("ok" if worthy is True else "low_value"))


async def passes(proposal: NotificationProposal) -> tuple[bool, str]:
    """``(worthy, reason)`` for one proactive proposal. Fails closed."""
    has_payoff, payoff_reason, destination, payoff, redundancy = (
        _has_incremental_tap_value(proposal)
    )
    if not has_payoff:
        return False, payoff_reason
    prompt = tap_gate_user_prompt(
        title=proposal.title,
        body=proposal.body,
        source=proposal.source,
        destination=destination,
        payoff=payoff,
        redundancy=redundancy,
    )
    try:
        raw = await asyncio.wait_for(
            get_model_provider().cheap(
                prompt, system=TAP_GATE_SYSTEM_PROMPT, temperature=0.0
            ),
            timeout=_TAP_GATE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.error("tap_gate: judge unavailable, holding for the next drain", {
            "source": proposal.source, "error": str(exc),
        })
        return False, REASON_GATE_UNAVAILABLE
    return _parse(str(raw))
