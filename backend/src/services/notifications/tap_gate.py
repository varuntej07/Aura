"""LLM tap-worthiness gate — the last check before a PROACTIVE push goes out.

Every proactive winner the drain selects must earn its tap here: one cheap Gemini
Flash judgment asking "is this specific to THIS person, does it open a real
curiosity gap or offer a clear next step?". Tuned to a BALANCED bar — it kills
generic filler, not borderline-good copy.

The deterministic destination contract and model judgment both fail closed: an
optional interruption whose value cannot be established is safer as silence.
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

_CONTENT_TOKEN = re.compile(r"[a-z0-9]+")
_LOW_INFORMATION_TOKENS = frozenset({
    "a", "about", "after", "and", "are", "at", "back", "can", "for", "from",
    "here", "i", "in", "is", "it", "more", "of", "on", "the", "this", "to",
    "up", "want", "we", "with", "you", "your",
})
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
) -> tuple[bool, str, str, str]:
    destination, payoff, has_artifact, interactive = _destination_context(proposal)
    if not destination or not payoff:
        return False, "no_destination_payoff", destination, payoff
    if has_artifact or interactive:
        return True, "destination_present", destination, payoff

    data = proposal.data or {}
    opening = str(
        data.get("opening_chat_message") or data.get("initial_message") or ""
    ).strip()
    push = f"{proposal.title} {proposal.body}".strip()
    incremental = _meaningful_tokens(opening) - _meaningful_tokens(push)
    similarity = SequenceMatcher(None, push.casefold(), opening.casefold()).ratio()
    if similarity >= 0.72 or len(incremental) < 4:
        return False, "repeats_push_after_tap", destination, payoff
    return True, "incremental_destination", destination, payoff


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
    has_payoff, payoff_reason, destination, payoff = _has_incremental_tap_value(proposal)
    if not has_payoff:
        return False, payoff_reason
    prompt = tap_gate_user_prompt(
        title=proposal.title,
        body=proposal.body,
        source=proposal.source,
        destination=destination,
        payoff=payoff,
    )
    try:
        raw = await asyncio.wait_for(
            get_model_provider().cheap(
                prompt, system=TAP_GATE_SYSTEM_PROMPT, temperature=0.0
            ),
            timeout=_TAP_GATE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("tap_gate: judge unavailable, failing closed (drop)", {
            "source": proposal.source, "error": str(exc),
        })
        return False, "gate_unavailable"
    return _parse(str(raw))
