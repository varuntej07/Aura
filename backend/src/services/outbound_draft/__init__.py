"""Outbound draft service: screen-sighted, tone-matched email replies and cold DMs.

The desktop sibling of ``services/keyboard`` - see ``drafter.py`` for the contract.
"""

from .drafter import (
    OutboundDraftResult,
    draft_outbound,
    refine_outbound,
    writing_voice_lines,
)
from .skills import (
    GENERAL_SKILL_ID,
    WRITING_SKILL_IDS,
    WritingSkill,
    WritingSkillId,
    get_writing_skill,
    is_writing_skill_id,
)

__all__ = [
    "OutboundDraftResult",
    "draft_outbound",
    "refine_outbound",
    "writing_voice_lines",
    "GENERAL_SKILL_ID",
    "WRITING_SKILL_IDS",
    "WritingSkill",
    "WritingSkillId",
    "get_writing_skill",
    "is_writing_skill_id",
]
