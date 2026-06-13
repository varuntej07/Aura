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

from src.services.buddy_voice import BUDDY_CONTENT_PUSH_RULES, BUDDY_VOICE_CORE


def test_signal_framer_uses_core_and_content_push_voice():
    from src.services.signal_engine.notification_framer import _FRAMER_SYSTEM_PROMPT

    assert BUDDY_VOICE_CORE in _FRAMER_SYSTEM_PROMPT
    assert BUDDY_CONTENT_PUSH_RULES in _FRAMER_SYSTEM_PROMPT


def test_icebreaker_framer_uses_core_and_content_push_voice():
    from src.services.icebreaker.icebreaker_framer import _ICEBREAKER_SYSTEM_PROMPT

    assert BUDDY_VOICE_CORE in _ICEBREAKER_SYSTEM_PROMPT
    assert BUDDY_CONTENT_PUSH_RULES in _ICEBREAKER_SYSTEM_PROMPT


def test_thread_framer_uses_core_voice_but_not_content_push():
    # Threads ask a curious question; they must NOT use the tap-through CTA rules,
    # or Buddy starts "selling" instead of being curious.
    from src.services.threads.thread_framer import _FRAMER_SYSTEM_PROMPT

    assert BUDDY_VOICE_CORE in _FRAMER_SYSTEM_PROMPT
    assert BUDDY_CONTENT_PUSH_RULES not in _FRAMER_SYSTEM_PROMPT


def test_engagement_agents_use_core_voice():
    from src.services.engagement.agents.calendar_prep import _SYSTEM_PROMPT as calendar_prompt
    from src.services.engagement.agents.habit_nudge import _SYSTEM_PROMPT as habit_prompt
    from src.services.engagement.agents.re_engagement import _SYSTEM_PROMPT as re_engagement_prompt

    for prompt in (re_engagement_prompt, habit_prompt, calendar_prompt):
        assert BUDDY_VOICE_CORE in prompt
