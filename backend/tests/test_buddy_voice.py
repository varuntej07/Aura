"""Contract: every proactive notification framer speaks in Buddy's shared voice.

The persona is not otherwise enforced, so a new framer (or a careless edit) can
silently drift back to a flat, source-centric "content bot" voice — the exact
regression that produced the 2/10 "Found an article on Hacker news... Might be
useful" push. This test fails CI the moment a proactive framer stops injecting
BUDDY_VOICE_CORE, the same way test_funnel_event_contract guards the funnel keys.

It also pins the split: tap-through pushes (signal engine, icebreaker) carry the
curiosity-gap rules; the thread follow-up framer (which asks a question, not a
tap) must NOT, so it never turns salesy.
"""

from __future__ import annotations

from src.prompts import (
    BUDDY_CONTENT_PUSH_RULES,
    BUDDY_VOICE_CORE,
    CALENDAR_PREP_SYSTEM_PROMPT,
    HABIT_NUDGE_SYSTEM_PROMPT,
    ICEBREAKER_SYSTEM_PROMPT,
    RE_ENGAGEMENT_SYSTEM_PROMPT,
    SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT,
    THREAD_FRAMER_SYSTEM_PROMPT,
)


def test_signal_framer_uses_core_and_content_push_voice():
    assert BUDDY_VOICE_CORE in SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT
    assert BUDDY_CONTENT_PUSH_RULES in SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT


def test_icebreaker_framer_uses_core_and_content_push_voice():
    assert BUDDY_VOICE_CORE in ICEBREAKER_SYSTEM_PROMPT
    assert BUDDY_CONTENT_PUSH_RULES in ICEBREAKER_SYSTEM_PROMPT


def test_thread_framer_uses_core_voice_but_not_content_push():
    # Threads ask a curious question; they must NOT use the tap-through CTA rules,
    # or Buddy starts "selling" instead of being curious.
    assert BUDDY_VOICE_CORE in THREAD_FRAMER_SYSTEM_PROMPT
    assert BUDDY_CONTENT_PUSH_RULES not in THREAD_FRAMER_SYSTEM_PROMPT


def test_engagement_agents_use_core_voice():
    for prompt in (
        RE_ENGAGEMENT_SYSTEM_PROMPT,
        HABIT_NUDGE_SYSTEM_PROMPT,
        CALENDAR_PREP_SYSTEM_PROMPT,
    ):
        assert BUDDY_VOICE_CORE in prompt
