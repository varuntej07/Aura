"""Output guardrails — the safety peer layer between an agent's proposal and the send.

Every proposal an agent produces passes through here before the orchestrator routes
it to the funnel. This is the deterministic, zero-cost floor: it catches the failure
modes that make a notification embarrassing rather than merely low-quality (the
tap-gate in the funnel is the separate, LLM-judged QUALITY bar; this is the SAFETY
bar). A blocked proposal is DROPPED loudly, never sent.

Checks (all deterministic, so they can never themselves fail a send into silence):
  * a visible push needs a non-empty title AND body (data-only payloads are exempt —
    they render their own UI client-side);
  * the body must fit a sane push length;
  * the copy must not leak an unrendered template, a raw ``None``/``null``, or a model
    refusal ("I can't help with that") — all signs the framing step silently broke.

This is intentionally NOT an LLM call: an output guardrail that itself costs a model
round-trip and can time out would reintroduce the very fragility it exists to prevent.
LLM-judged worthiness already lives in the funnel's tap-gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...lib.logger import logger
from ..notifications.proposal import NotificationProposal

# FCM hard-truncates very long bodies; keep well under, copy this long is already a bug.
MAX_BODY_CHARS = 500
MAX_TITLE_CHARS = 120

# Lowercased fragments that, as (or dominating) the body, mean the framing step broke:
# an unrendered placeholder, a serialized null, or a model refusal that leaked through.
_BROKEN_MARKERS = (
    "{{", "}}", "{0}", "{1}", "<placeholder", "todo:", "lorem ipsum",
    "i can't help", "i cannot help", "i'm unable to", "as an ai",
)
_NULL_BODIES = {"", "none", "null", "n/a", "undefined"}


@dataclass
class GuardVerdict:
    allow: bool
    reason: str = ""


def check_proposal(proposal: NotificationProposal) -> GuardVerdict:
    """Deterministic output safety check. Returns allow/deny + a reason."""
    title = (proposal.title or "").strip()
    body = (proposal.body or "").strip()

    # data-only payloads render their own UI client-side; they carry no title/body.
    if not proposal.data_only:
        if not body:
            return GuardVerdict(False, "empty_body")
        if not title:
            return GuardVerdict(False, "empty_title")

    if body:
        if body.casefold() in _NULL_BODIES:
            return GuardVerdict(False, "null_body")
        if len(body) > MAX_BODY_CHARS:
            return GuardVerdict(False, "body_too_long")
        low = body.casefold()
        for marker in _BROKEN_MARKERS:
            if marker in low:
                return GuardVerdict(False, f"broken_copy:{marker.strip()}")

    if title and len(title) > MAX_TITLE_CHARS:
        return GuardVerdict(False, "title_too_long")

    return GuardVerdict(True, "ok")


def filter_proposals(proposals: list[NotificationProposal]) -> list[NotificationProposal]:
    """Pass-through the safe proposals; drop + loudly log the unsafe ones."""
    allowed: list[NotificationProposal] = []
    for proposal in proposals:
        verdict = check_proposal(proposal)
        if verdict.allow:
            allowed.append(proposal)
        else:
            logger.error("guardrails: proposal blocked before send", {
                "user_id": proposal.user_id,
                "source": proposal.source,
                "reason": verdict.reason,
                "title_preview": (proposal.title or "")[:40],
            })
    return allowed
