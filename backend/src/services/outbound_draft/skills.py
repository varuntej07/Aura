"""Bounded writing-skill instructions selected by structured tool input.

The conversational model chooses ``skill_id`` through the tool schema. This
module never inspects user prose to infer intent. Only the selected skill's
instructions are added to the drafting call, keeping prompt cost and latency
off unrelated turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeGuard, cast

WritingSkillId = Literal["general", "linkedin_post", "tweet", "email"]

GENERAL_SKILL_ID: WritingSkillId = "general"
WRITING_SKILL_IDS: tuple[WritingSkillId, ...] = (
    GENERAL_SKILL_ID,
    "linkedin_post",
    "tweet",
    "email",
)


@dataclass(frozen=True)
class WritingSkill:
    skill_id: WritingSkillId
    title: str
    instructions: str


_SKILLS: dict[WritingSkillId, WritingSkill] = {
    "general": WritingSkill(
        skill_id="general",
        title="Draft",
        instructions=(
            "Write the exact copy the user needs for the visible destination. "
            "Follow the user's requested purpose, audience, tone, and constraints."
        ),
    ),
    "linkedin_post": WritingSkill(
        skill_id="linkedin_post",
        title="LinkedIn post",
        instructions=(
            "Write a credible LinkedIn post in the user's own voice. Open with a "
            "specific observation, tension, or moment rather than a generic hook. "
            "Develop one clear idea with concrete details and natural paragraph "
            "breaks. Prefer first-person experience when the brief supports it. "
            "Do not invent traction, metrics, customers, partnerships, outcomes, "
            "or personal experiences. Avoid corporate filler, engagement bait, "
            "fake quotations, and a stack of one-line fragments. Use hashtags only "
            "when the user asks for them. Return an approval-ready draft only and "
            "never imply that it has been published."
        ),
    ),
    "tweet": WritingSkill(
        skill_id="tweet",
        title="Tweet",
        instructions=(
            "Write one concise standalone tweet in the user's voice. Put the main "
            "point early, use concrete language, and make every sentence earn its "
            "space. Stay within 280 characters unless the user explicitly requests "
            "a thread. Do not invent facts, metrics, reactions, or experiences. "
            "Avoid engagement bait, unnecessary hashtags, and forced slang. Return "
            "only approval-ready copy and never imply that it has been posted."
        ),
    ),
    "email": WritingSkill(
        skill_id="email",
        title="Email draft",
        instructions=(
            "Write a complete email or email reply in the user's voice. Preserve "
            "the requested intent, commitments, boundaries, names, dates, and factual "
            "details. Match the relationship and formality visible in the context. "
            "Lead with the purpose, keep the body easy to scan, and include a clear "
            "next step when one is warranted. A subject explicitly requested by the "
            "user is part of the artifact despite the general no-subject rule. Do "
            "not invent facts or commitments. Return approval-ready copy only and "
            "never send it."
        ),
    ),
}


def is_writing_skill_id(value: object) -> TypeGuard[WritingSkillId]:
    """Validate a model-supplied enum value without interpreting user text."""
    return isinstance(value, str) and value in WRITING_SKILL_IDS


def get_writing_skill(skill_id: str) -> WritingSkill:
    """Return the selected skill, falling back only for stale stored drafts."""
    if skill_id not in WRITING_SKILL_IDS:
        return _SKILLS[GENERAL_SKILL_ID]
    return _SKILLS[cast(WritingSkillId, skill_id)]
