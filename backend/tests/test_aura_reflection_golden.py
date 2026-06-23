"""
Golden eval for the reflection PROMPT (quality, not plumbing).

These call the real reflection model (BALANCED tier = Claude Haiku) on the three worked
examples that motivated the narrative data model, and assert the structured patch carries
the connected meaning a flat interest can't. Run this before shipping any change to
_REFLECTION_SYSTEM_PROMPT or the reflect tier.

Skipped automatically when ANTHROPIC_API_KEY is unset (CI without secrets / local dev),
so it never blocks the deterministic suite. The plumbing that APPLIES a patch is covered
deterministically in test_aura_reflection.py; this covers what the model PRODUCES.

Run explicitly:  ANTHROPIC_API_KEY=... pytest tests/test_aura_reflection_golden.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from src.config.settings import settings
from src.services.aura_reflection import reflect_session

pytestmark = pytest.mark.skipif(
    not settings.ANTHROPIC_API_KEY,
    reason="reflection runs on the BALANCED tier (Claude Haiku); needs ANTHROPIC_API_KEY",
)

_CAREER_TURNS = [
    {"role": "user", "text": "i just wrote a blog post on tensor parallelism"},
    {"role": "assistant", "text": "nice, what's the angle?"},
    {"role": "user", "text": "how to split a model across GPUs. i'm hoping it helps me land "
                            "a software dev engineer role at Annapurna Labs, the AWS chip team"},
    {"role": "user", "text": "what should i do to be an ideal candidate, and stay useful "
                            "long term by building projects?"},
]

_FIFA_TURNS = [
    {"role": "user", "text": "can you send me updates about the FIFA World Cup?"},
    {"role": "assistant", "text": "sure, want score alerts or daily recaps?"},
    {"role": "user", "text": "daily recaps, especially when a big team is playing"},
]


def _has(text: str, needle: str) -> bool:
    return needle.lower() in (text or "").lower()


def test_golden_career_storyline_fuses_blog_and_goal():
    patch = asyncio.run(reflect_session(_CAREER_TURNS))
    assert patch is not None
    # The VALUE is the connected narrative: the blog serves the Annapurna goal.
    assert any(_has(s.summary, "annapurna") for s in patch.storylines), \
        f"no storyline mentioned Annapurna: {[s.summary for s in patch.storylines]}"
    # The goal-serving work should read as goal_instrumental, not a bare durable interest.
    assert any(s.kind == "goal_instrumental" for s in patch.storylines), \
        f"expected a goal_instrumental storyline, got: {[(s.summary, s.kind) for s in patch.storylines]}"


def test_golden_career_infers_drive_trait():
    patch = asyncio.run(reflect_session(_CAREER_TURNS))
    assert patch is not None
    # Wanting to be an ideal candidate + stay useful long-term = a drive/vision signal.
    assert patch.traits, "expected at least one inferred trait from the career session"
    joined = " ".join(t.name.lower() for t in patch.traits)
    assert any(k in joined for k in ("passion", "vision", "driven", "ambit", "growth", "motivat")), \
        f"expected a drive/vision trait, got: {[t.name for t in patch.traits]}"


def test_golden_fifa_is_event_driven_not_a_passion():
    patch = asyncio.run(reflect_session(_FIFA_TURNS))
    assert patch is not None
    # FIFA must be classified event_driven (a happening), via a storyline kind and/or an
    # interest_kind_op — NOT a durable "loves FIFA".
    storyline_event = any(s.kind == "event_driven" for s in patch.storylines)
    op_event = any(o.kind == "event_driven" for o in patch.interest_kind_ops)
    assert storyline_event or op_event, (
        "expected FIFA World Cup to be classified event_driven; "
        f"storylines={[(s.summary, s.kind) for s in patch.storylines]} "
        f"ops={[(o.subject, o.kind) for o in patch.interest_kind_ops]}"
    )
