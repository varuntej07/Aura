"""Outbound draft service: screen-sighted, tone-matched email replies and cold DMs.

The desktop sibling of ``services/keyboard`` - see ``drafter.py`` for the contract.
"""

from .drafter import (
    OutboundDraftResult,
    draft_outbound,
    refine_outbound,
    writing_voice_lines,
)

__all__ = [
    "OutboundDraftResult",
    "draft_outbound",
    "refine_outbound",
    "writing_voice_lines",
]
