"""The one place that decides whether a speculative reply may be reused.

LiveKit's AgentSession generates a reply from the raw transcript BEFORE
``on_user_turn_completed`` runs, then keeps it only when four things are still
true afterwards (agent_activity.py, ``_PreemptiveGeneration`` handling): the
transcript, the chat context (``ChatContext.is_equivalent``), ``Agent.tools``
and ``tool_choice`` are all unchanged. Any mutation the hook makes throws the
speculation away and pays the full cold model round trip instead.

So the hook must mutate deliberately, and every mutation must be nameable. This
module owns that vocabulary. It is intentionally a closed enum rather than free
text: the decision is read back from logs to attribute latency, and an
open-ended reason string makes that impossible to aggregate.

Nothing here touches transcript wording. The decision is derived only from which
structural mutations the turn actually applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SpeculationDecision(StrEnum):
    """Why one finalized turn did or did not reuse its speculative reply."""

    # The hook changed nothing. The speculation stands and is spoken as-is.
    UNCHANGED = "unchanged"
    # A pixel frame was attached. Unavoidable: the frame label changes
    # new_message.text_content, and an imageless speculation is the wrong reply
    # for an image turn regardless of what the SDK would allow.
    SCREEN_FRAME_ADDED = "screen_frame_added"
    # Structured UI context had to be appended at finalization rather than
    # already being in the context the speculation snapshotted.
    SCREEN_CONTEXT_ADDED = "screen_context_added"
    # Structured context landed after end-of-turn, too late to be injected
    # early. Distinct from the above so late uploads are separately visible.
    CONTEXT_ARRIVED_LATE = "context_arrived_late"
    # A relevant memory subgraph was rendered into the turn.
    GRAPH_CONTEXT_CHANGED = "graph_context_changed"
    # The context compactor rewrote the item list for this turn.
    CONTEXT_COMPACTED = "context_compacted"
    # The exposed tool set differed between the speculative and finalized
    # passes (Guide arm/disarm mid-turn, or fresh-frame availability flipping).
    TOOL_POLICY_CHANGED = "tool_policy_changed"
    # Guide Mode owns the turn; its runtime generates instead of this path.
    GUIDE_ACTIVE = "guide_active"
    # The speculative pass emitted a side-effecting tool call, which the
    # execution gate refused because that pass is not a finalized turn. Reusing
    # it would speak the refusal, so the turn is invalidated and the finalized
    # pass performs the action for real.
    SPECULATIVE_WRITE_TOOL = "speculative_write_tool"
    # A finalized turn selected a tool that can mutate or present state. Always
    # regenerate it under finalized authorization instead of racing a blocked
    # speculative call against end-of-turn finalization.
    FINALIZED_SIDE_EFFECT = "finalized_side_effect"
    # A historical screen block was collapsed because this turn received no
    # replacement. The context changed, so the old speculation cannot survive.
    STALE_SCREEN_CONTEXT_REMOVED = "stale_screen_context_removed"


@dataclass(frozen=True, slots=True)
class TurnMutations:
    """What ``on_user_turn_completed`` actually did to this turn."""

    guide_active: bool = False
    context_compacted: bool = False
    graph_context_appended: bool = False
    structured_context_appended: bool = False
    structured_context_arrived_late: bool = False
    screen_frame_attached: bool = False
    tool_policy_changed: bool = False
    speculative_write_attempt: bool = False
    finalized_side_effect: bool = False
    stale_screen_context_collapsed: bool = False

    def applied(self) -> tuple[str, ...]:
        """Names of every mutation that fired, for the decision log line."""
        return tuple(
            name
            for name in (
                "guide_active",
                "context_compacted",
                "graph_context_appended",
                "structured_context_appended",
                "structured_context_arrived_late",
                "screen_frame_attached",
                "tool_policy_changed",
                "speculative_write_attempt",
                "finalized_side_effect",
                "stale_screen_context_collapsed",
            )
            if getattr(self, name)
        )


# Most consequential first. A turn can trip several of these at once; the log
# still carries the full ``applied()`` tuple, so collapsing to one decision
# loses nothing and keeps the metric aggregatable.
_PRIORITY: tuple[tuple[str, SpeculationDecision], ...] = (
    ("guide_active", SpeculationDecision.GUIDE_ACTIVE),
    ("finalized_side_effect", SpeculationDecision.FINALIZED_SIDE_EFFECT),
    # Ranked above everything else that could also be true, because this one is
    # about the turn being ACTED on correctly rather than merely being fresh.
    ("speculative_write_attempt", SpeculationDecision.SPECULATIVE_WRITE_TOOL),
    ("screen_frame_attached", SpeculationDecision.SCREEN_FRAME_ADDED),
    ("context_compacted", SpeculationDecision.CONTEXT_COMPACTED),
    ("graph_context_appended", SpeculationDecision.GRAPH_CONTEXT_CHANGED),
    ("structured_context_arrived_late", SpeculationDecision.CONTEXT_ARRIVED_LATE),
    ("structured_context_appended", SpeculationDecision.SCREEN_CONTEXT_ADDED),
    (
        "stale_screen_context_collapsed",
        SpeculationDecision.STALE_SCREEN_CONTEXT_REMOVED,
    ),
    ("tool_policy_changed", SpeculationDecision.TOOL_POLICY_CHANGED),
)


def decide(mutations: TurnMutations) -> SpeculationDecision:
    """Collapse the applied mutations into exactly one bounded reason."""
    for field_name, decision in _PRIORITY:
        if getattr(mutations, field_name):
            return decision
    return SpeculationDecision.UNCHANGED


def is_reusable(decision: SpeculationDecision) -> bool:
    """Only a turn that mutated nothing can keep its speculative reply."""
    return decision is SpeculationDecision.UNCHANGED
