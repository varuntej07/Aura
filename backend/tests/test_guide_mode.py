from __future__ import annotations

import asyncio
import json
import time

from livekit.agents import llm as lk_llm

from src.agent.voice.guide_mode import GuideCoordinator
from src.agent.voice.guide_supervisor import GuideSupervisorAgent
from src.agent.voice.interview import ConversationOwner, VoiceSessionState
from src.agent.voice.screen_frames import ScreenFrame, ScreenFrameStore
from src.prompts import GUIDE_INSTRUCTIONS

GUIDE_SESSION_ID = "a" * 32
USER_ID = "u" * 28


class _Speech:
    def __await__(self):
        return asyncio.sleep(0).__await__()


class _Session:
    def __init__(self) -> None:
        self.agent_state = "listening"
        self.user_state = "listening"
        self.calls: list[dict] = []
        self.says: list[str] = []
        self.handlers: dict[str, list] = {}
        self.interrupts = 0
        self.userdata = VoiceSessionState()
        self.current_agent = None

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def interrupt(self, *, force: bool) -> None:
        assert force
        self.interrupts += 1

    def generate_reply(self, **kwargs):
        self.calls.append(kwargs)
        return _Speech()

    async def say(self, text: str) -> None:
        # Present only to catch a regression: the proactive path must never speak
        # a canned apology like "I can't see the current screen".
        self.says.append(text)

    def update_agent(self, agent) -> None:
        previous = self.current_agent
        self.current_agent = agent

        async def _handoff() -> None:
            if previous is not None and hasattr(previous, "on_exit"):
                await previous.on_exit()
            if hasattr(agent, "on_enter"):
                await agent.on_enter()

        asyncio.create_task(_handoff())


class _Participant:
    def __init__(self) -> None:
        self.published: list[tuple[bytes, bool]] = []

    async def publish_data(self, payload: bytes, *, reliable: bool) -> None:
        self.published.append((payload, reliable))


class _Room:
    def __init__(self) -> None:
        self.local_participant = _Participant()


class _Buddy:
    def __init__(self, session: _Session) -> None:
        self._session = session
        self._chat_ctx = lk_llm.ChatContext()
        self.updates = 0
        self.resume_epoch: int | None = None
        self.resume_ready: asyncio.Future[bool] | None = None

    @property
    def chat_ctx(self) -> lk_llm.ChatContext:
        return self._chat_ctx

    async def update_chat_ctx(self, chat_ctx: lk_llm.ChatContext) -> None:
        self._chat_ctx = chat_ctx
        self.updates += 1

    async def prepare_guide_resume(
        self,
        chat_ctx: lk_llm.ChatContext,
        ownership_epoch: int | None,
        ready: asyncio.Future[bool],
    ) -> None:
        await self.update_chat_ctx(chat_ctx)
        self.resume_epoch = ownership_epoch
        self.resume_ready = ready

    async def on_enter(self) -> None:
        committed = True
        if self.resume_epoch is not None:
            committed = self._session.userdata.guide.commit_idle(self.resume_epoch)
        if self.resume_ready is not None and not self.resume_ready.done():
            self.resume_ready.set_result(committed)
        self.resume_epoch = None
        self.resume_ready = None


class _TaskRuntime:
    def __init__(self) -> None:
        self.activations: list[dict] = []
        self.deactivations: list[bool] = []
        self.failure_handler = None
        self.cancelled = 0
        self.speech_in_progress = False

    def bind_failure_handler(self, handler) -> None:
        self.failure_handler = handler

    def activate(self, **kwargs) -> None:
        self.activations.append(kwargs)

    def cancel_generation(self) -> None:
        self.cancelled += 1

    async def deactivate(self, *, cancelled: bool) -> None:
        self.deactivations.append(cancelled)

    async def close(self) -> None:
        return None

    def note_activity(self) -> None:
        return None

    async def on_user_speech_start(self) -> None:
        return None

    async def on_agent_state(self, _state: str) -> None:
        return None


def _frame(
    sequence: int,
    *,
    session_id: str = GUIDE_SESSION_ID,
    change: str = "1",
) -> ScreenFrame:
    return ScreenFrame(
        jpeg_bytes=b"jpeg",
        attributes={
            "frame_id": f"{session_id}:{sequence}",
            "frame_seq": str(sequence),
            "mode": "guide",
            "change": change,
        },
        received_at_monotonic=time.monotonic(),
    )


def _coordinator():
    session = _Session()
    buddy = _Buddy(session)
    session.current_agent = buddy
    room = _Room()
    task_runtime = _TaskRuntime()
    screen_frames = ScreenFrameStore(session_id="voice-session", user_id=USER_ID)
    guide = GuideCoordinator(
        session=session,
        buddy=buddy,
        room=room,
        session_id="voice-session",
        user_id=USER_ID,
        task_runtime=task_runtime,
        screen_frames=screen_frames,
        display_name="there",
    )
    return guide, session, buddy, room, task_runtime


def _control(
    *,
    active: bool = True,
    generation: int = 1,
    session_id=GUIDE_SESSION_ID,
    protocol_version: int = 2,
):
    return {
        "type": "guide.mode",
        "active": active,
        "guide_session_id": session_id if active else None,
        "generation": generation,
        "protocol_version": protocol_version,
    }


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _acks(room: _Room) -> list[dict]:
    return [json.loads(payload) for payload, _ in room.local_participant.published]


async def test_control_requires_authenticated_participant_and_valid_session_id():
    guide, _, buddy, _, _ = _coordinator()
    assert not guide.apply_control(_control(), "someone-else")
    assert not guide.apply_control(_control(session_id="bad"), USER_ID)
    assert guide.apply_control(_control(), USER_ID)
    assert not guide.apply_control(_control(), USER_ID)
    await _settle()
    assert isinstance(guide._session.current_agent, GuideSupervisorAgent)
    assert guide._session.userdata.owner is ConversationOwner.GUIDE


async def test_protocol_v2_ack_is_published_only_after_supervisor_and_runtime_activate():
    guide, _, buddy, room, runtime = _coordinator()
    assert guide.apply_control(_control(protocol_version=2), USER_ID)
    assert room.local_participant.published == []

    await _settle()

    assert isinstance(guide._session.current_agent, GuideSupervisorAgent)
    assert runtime.activations == [
        {
            "guide_session_id": GUIDE_SESSION_ID,
            "protocol_version": 2,
            "resume_task_id": None,
        }
    ]
    assert _acks(room) == [
        {
            "type": "guide.mode_ack",
            "payload": {
                "active": True,
                "generation": 1,
                "guide_session_id": GUIDE_SESSION_ID,
                "protocol_version": 2,
                "reason": None,
            },
        }
    ]


async def test_runtime_failure_fails_closed_and_acknowledges_the_same_generation():
    guide, session, buddy, room, runtime = _coordinator()
    assert guide.apply_control(_control(protocol_version=2), USER_ID)
    await _settle()
    room.local_participant.published.clear()
    runtime.speech_in_progress = True

    assert runtime.failure_handler is not None
    runtime.failure_handler("planning_unavailable")
    await _settle()

    assert not guide.is_active()
    assert session.current_agent is buddy
    assert session.userdata.owner is ConversationOwner.BUDDY
    assert runtime.cancelled >= 1
    assert session.interrupts == 1
    assert _acks(room) == [
        {
            "type": "guide.mode_ack",
            "payload": {
                "active": False,
                "generation": 1,
                "guide_session_id": GUIDE_SESSION_ID,
                "protocol_version": 2,
                "reason": "planning_unavailable",
            },
        }
    ]


async def test_changed_frame_fires_one_terse_pointed_nudge():
    guide, session, buddy, room, _ = _coordinator()
    assert guide.apply_control(_control(), USER_ID)
    guide.start()
    guide.submit_frame(_frame(8, change="1"))
    await _settle()
    await guide.close()

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["tools"] == []
    assert call["instructions"] == GUIDE_INSTRUCTIONS
    assert isinstance(call["user_input"], lk_llm.ChatMessage)
    assert any(
        isinstance(part, lk_llm.ImageContent)
        for part in call["user_input"].content
    )
    assert isinstance(session.current_agent, GuideSupervisorAgent)
    assert session.says == []
    # The frame is also acked so the desktop handshake advances.
    frame_acks = [ack for ack in _acks(room) if ack["type"] == "guide.frame_ack"]
    assert frame_acks == [
        {
            "type": "guide.frame_ack",
            "payload": {
                "frame_id": f"{GUIDE_SESSION_ID}:8",
                "frame_seq": 8,
                "accepted": True,
                "rejection_reason": None,
                "newest_frame_id": f"{GUIDE_SESSION_ID}:8",
            },
        }
    ]


async def test_forced_static_frame_is_acked_but_not_spoken():
    guide, session, _, room, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    guide.submit_frame(_frame(5, change="0"))
    await _settle()
    await guide.close()

    assert session.calls == []
    assert session.says == []
    frame_acks = [ack for ack in _acks(room) if ack["type"] == "guide.frame_ack"]
    assert len(frame_acks) == 1
    assert frame_acks[0]["payload"]["frame_id"] == f"{GUIDE_SESSION_ID}:5"


async def test_every_accepted_frame_is_acked():
    guide, _, _, room, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    guide.submit_frame(_frame(1, change="0"))
    await _settle()
    guide.submit_frame(_frame(2, change="0"))
    await _settle()
    await guide.close()

    seqs = [
        ack["payload"]["frame_seq"]
        for ack in _acks(room)
        if ack["type"] == "guide.frame_ack"
    ]
    assert seqs == [1, 2]


async def test_non_guide_and_wrong_session_frames_are_ignored():
    guide, session, _, room, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    normal = _frame(1)
    normal.attributes["mode"] = "turn"
    guide.submit_frame(normal)
    guide.submit_frame(_frame(2, session_id="b" * 32))
    await _settle()
    await guide.close()

    assert session.calls == []
    rejected = [
        ack
        for ack in _acks(room)
        if ack["type"] == "guide.frame_ack" and not ack["payload"]["accepted"]
    ]
    assert [ack["payload"]["rejection_reason"] for ack in rejected] == [
        "wrong_mode",
        "wrong_session",
    ]


async def test_redelivered_frame_is_reacked_without_speaking_twice():
    guide, session, _, room, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    frame = _frame(3, change="1")
    guide.submit_frame(frame)
    await _settle()
    guide.submit_frame(frame)
    await _settle()
    await guide.close()

    assert len(session.calls) == 1
    frame_acks = [ack for ack in _acks(room) if ack["type"] == "guide.frame_ack"]
    assert len(frame_acks) == 2


async def test_two_rapid_changes_fire_one_nudge():
    guide, session, _, _, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    guide.submit_frame(_frame(4, change="1"))
    await _settle()
    guide.submit_frame(_frame(5, change="1"))
    await _settle()
    await guide.close()

    # Debounced by _PROACTIVE_MIN_INTERVAL_S; both frames still get acked.
    assert len(session.calls) == 1


async def test_nudge_suppressed_right_after_a_spoken_turn():
    guide, session, _, _, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    guide.note_user_turn("What should I click?")
    guide.submit_frame(_frame(6, change="1"))
    await _settle()
    await guide.close()

    # Within the post-turn quiet window: the spoken turn already answered them.
    assert session.calls == []


async def test_stale_frame_is_acked_but_not_spoken():
    guide, session, _, room, _ = _coordinator()
    guide.apply_control(_control(), USER_ID)
    guide.start()
    stale = _frame(10, change="1")
    stale.received_at_monotonic = time.monotonic() - 16
    guide.submit_frame(stale)
    await _settle()
    await guide.close()

    assert session.calls == []
    assert session.says == []
    frame_acks = [ack for ack in _acks(room) if ack["type"] == "guide.frame_ack"]
    assert len(frame_acks) == 1


async def test_disarm_hands_back_to_the_same_buddy():
    guide, session, buddy, _, _ = _coordinator()
    guide.apply_control(_control(active=True, generation=1), USER_ID)
    await _settle()
    assert isinstance(session.current_agent, GuideSupervisorAgent)
    guide.apply_control(_control(active=False, generation=2), USER_ID)
    await _settle()
    assert session.current_agent is buddy
    assert session.userdata.owner is ConversationOwner.BUDDY


async def test_old_images_are_stripped_before_next_guide_frame():
    guide, _, buddy, _, _ = _coordinator()
    buddy._chat_ctx.items.append(
        lk_llm.ChatMessage(
            role="user",
            content=[
                "old",
                lk_llm.ImageContent(image="data:image/jpeg;base64,QUFB"),
            ],
        )
    )
    guide.apply_control(_control(), USER_ID)
    guide.start()
    guide.submit_frame(_frame(4, change="1"))
    await _settle()
    await guide.close()

    assert isinstance(guide._session.current_agent, GuideSupervisorAgent)
    message = guide._session.current_agent.chat_ctx.items[0]
    assert isinstance(message, lk_llm.ChatMessage)
    assert not any(isinstance(part, lk_llm.ImageContent) for part in message.content)
