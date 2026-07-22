"""Two-tier silence presence (voice/recorder.py).

Tier 1 fires on LiveKit's away event: playful and screen-aware when a fresh
desktop frame exists, a light check-in otherwise. Tier 2 escalates after
VOICE_AWAY_SECOND_NUDGE_S total silence with a memory-pull re-engage, and is
cancelled by any user activity. Both are LLM-framed instructions, never canned
lines.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.agent.voice.recorder import (
    FIRST_AWAY_NUDGE_INSTRUCTIONS,
    FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS,
    SECOND_AWAY_NUDGE_INSTRUCTIONS,
    VoiceSessionRecorder,
)
from src.config.settings import settings


class _FakeSession:
    def __init__(self, *, agent_state: str = "listening", user_state: str = "away") -> None:
        self.agent_state = agent_state
        self.user_state = user_state
        self.replies: list[str] = []

    async def generate_reply(self, *, instructions: str) -> None:
        self.replies.append(instructions)


class _FakeFrameStore:
    def __init__(self, has_frame: bool) -> None:
        self._has_frame = has_frame

    async def fresh_frame(self):
        return object() if self._has_frame else None


def _make_recorder(session: _FakeSession, screen_frames=None) -> VoiceSessionRecorder:
    return VoiceSessionRecorder(
        session=session,
        ctx=SimpleNamespace(),
        session_id="sess-1",
        user_id="user-1",
        user_tier="free",
        screen_frames=screen_frames,
    )


async def _drain_tasks() -> None:
    # Let fire-and-forget nudge tasks run to completion.
    for _ in range(3):
        await asyncio.sleep(0)


async def test_tier1_without_frame_uses_plain_checkin():
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == [FIRST_AWAY_NUDGE_INSTRUCTIONS]
    rec._cancel_second_away_nudge()


async def test_tier1_with_fresh_frame_uses_screen_instructions():
    session = _FakeSession()
    rec = _make_recorder(session, screen_frames=_FakeFrameStore(has_frame=True))
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == [FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS]
    rec._cancel_second_away_nudge()


async def test_tier1_skipped_when_agent_not_listening():
    session = _FakeSession(agent_state="speaking")
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == []
    assert rec._second_away_nudge_task is None


async def test_tier2_fires_after_escalation_delay_when_still_away(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_AWAY_FIRST_NUDGE_S", 0.0)
    monkeypatch.setattr(settings, "VOICE_AWAY_SECOND_NUDGE_S", 0.01)
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await asyncio.sleep(0.05)
    assert session.replies[0] == FIRST_AWAY_NUDGE_INSTRUCTIONS
    assert session.replies[1] == SECOND_AWAY_NUDGE_INSTRUCTIONS


async def test_tier2_cancelled_when_user_returns(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_AWAY_FIRST_NUDGE_S", 0.0)
    monkeypatch.setattr(settings, "VOICE_AWAY_SECOND_NUDGE_S", 0.05)
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    rec._on_user_state(SimpleNamespace(new_state="listening"))
    await asyncio.sleep(0.1)
    assert session.replies == [FIRST_AWAY_NUDGE_INSTRUCTIONS]


async def test_tier2_rechecks_away_state_at_fire_time(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_AWAY_FIRST_NUDGE_S", 0.0)
    monkeypatch.setattr(settings, "VOICE_AWAY_SECOND_NUDGE_S", 0.01)
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    # The user came back but no state event reached the recorder (race): the
    # timer's own re-check must still refuse to fire.
    session.user_state = "listening"
    await asyncio.sleep(0.05)
    assert session.replies == [FIRST_AWAY_NUDGE_INSTRUCTIONS]


async def test_repeated_away_events_nudge_only_once_per_silence():
    # LiveKit re-emits "away" after every agent turn while the user stays quiet.
    # Buddy must check in ONCE, not once per re-emit (the "why do you keep
    # talking" loop).
    session = _FakeSession()
    rec = _make_recorder(session)
    for _ in range(5):
        rec._on_user_state(SimpleNamespace(new_state="away"))
        await _drain_tasks()
    assert session.replies == [FIRST_AWAY_NUDGE_INSTRUCTIONS]
    rec._cancel_second_away_nudge()


async def test_listening_blip_does_not_reopen_nudging():
    # A transient "listening" state between agent turns (no real user speech)
    # must NOT re-open nudging; only a final transcript does.
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    rec._on_user_state(SimpleNamespace(new_state="listening"))
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == [FIRST_AWAY_NUDGE_INSTRUCTIONS]
    rec._cancel_second_away_nudge()


async def test_final_user_transcript_reopens_nudging():
    # After the user actually speaks, the next silence span may check in again.
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    rec._on_user_transcript(SimpleNamespace(transcript="hey", is_final=True))
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == [
        FIRST_AWAY_NUDGE_INSTRUCTIONS,
        FIRST_AWAY_NUDGE_INSTRUCTIONS,
    ]
    rec._cancel_second_away_nudge()


def test_tier_instructions_stay_open_ended_and_distinct():
    # Guard against a future edit collapsing the tiers back into one stock line.
    assert FIRST_AWAY_NUDGE_INSTRUCTIONS != SECOND_AWAY_NUDGE_INSTRUCTIONS
    for text in (
        FIRST_AWAY_NUDGE_INSTRUCTIONS,
        FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS,
        SECOND_AWAY_NUDGE_INSTRUCTIONS,
    ):
        assert "Vary the wording" in text or "vary the wording" in text
