"""Single silence presence nudge (voice/recorder.py).

The nudge fires once on LiveKit's 45-second away event. It is screen-aware when
a fresh desktop frame exists and otherwise stays a light check-in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.agent.voice.recorder import (
    FIRST_AWAY_NUDGE_INSTRUCTIONS,
    FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS,
    VoiceSessionRecorder,
)


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


async def test_without_frame_uses_plain_checkin():
    session = _FakeSession()
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == [FIRST_AWAY_NUDGE_INSTRUCTIONS]


async def test_with_fresh_frame_uses_screen_instructions():
    session = _FakeSession()
    rec = _make_recorder(session, screen_frames=_FakeFrameStore(has_frame=True))
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == [FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS]


async def test_skipped_when_agent_not_listening():
    session = _FakeSession(agent_state="speaking")
    rec = _make_recorder(session)
    rec._on_user_state(SimpleNamespace(new_state="away"))
    await _drain_tasks()
    assert session.replies == []


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


def test_nudge_instructions_stay_open_ended():
    for text in (
        FIRST_AWAY_NUDGE_INSTRUCTIONS,
        FIRST_AWAY_NUDGE_SCREEN_INSTRUCTIONS,
    ):
        assert "Vary the wording" in text or "vary the wording" in text
