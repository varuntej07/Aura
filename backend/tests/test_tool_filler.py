"""Coverage for the slow-tool spoken filler (voice/tool_filler.py).

Pins the contracts that keep the filler safe on the live speech scheduler:
  - only tools in the slow set speak; fast tools stay silent (no added latency);
  - say() is called with allow_interruptions=True and add_to_chat_ctx=False
    (interruptible, never pollutes the LLM context);
  - one filler per dedupe window, so chained tool rounds don't stack "one sec";
  - the thinking-state guard: a speculative (preemptive) generation that never
    commits must NOT speak, one that commits late still speaks;
  - the llm_node tee passes every chunk through untouched and in order.
"""

from __future__ import annotations

import asyncio

from livekit.agents import llm as lk_llm

from src.agent import buddy_agent as buddy_agent_module
from src.agent.voice import tool_filler as tool_filler_module
from src.agent.voice.tool_filler import (
    SLOW_TOOL_THINKING_PHRASES,
    ToolFillerSpeaker,
)


class _FakeSession:
    """Just enough AgentSession surface for the speaker: state + say capture."""

    def __init__(self, agent_state: str = "thinking") -> None:
        self.agent_state = agent_state
        self.say_calls: list[tuple[str, dict]] = []

    async def say(self, phrase: str, **kwargs) -> None:
        self.say_calls.append((phrase, kwargs))


def _speaker(session: _FakeSession) -> ToolFillerSpeaker:
    return ToolFillerSpeaker(session=session, session_id="sess-test", user_id="uid-test")


async def _drain(speaker: ToolFillerSpeaker) -> None:
    await asyncio.gather(*speaker._speak_tasks, return_exceptions=True)


# ---------------------------------------------------------------- lookup


async def test_slow_tool_speaks_a_listed_phrase_with_safe_kwargs():
    session = _FakeSession(agent_state="thinking")
    speaker = _speaker(session)
    speaker.speak_for_tool("web_surf")
    await _drain(speaker)

    assert len(session.say_calls) == 1
    phrase, kwargs = session.say_calls[0]
    assert phrase in SLOW_TOOL_THINKING_PHRASES["web_surf"]
    assert kwargs == {"allow_interruptions": True, "add_to_chat_ctx": False}


async def test_fast_tool_stays_silent():
    session = _FakeSession(agent_state="thinking")
    speaker = _speaker(session)
    speaker.speak_for_tool("set_reminder")
    speaker.speak_for_tool("store_memory")
    speaker.speak_for_tool("save_screen_item")
    await _drain(speaker)

    assert session.say_calls == []
    assert speaker._speak_tasks == set()


# ---------------------------------------------------------------- dedupe


async def test_second_filler_inside_dedupe_window_is_silent():
    session = _FakeSession(agent_state="thinking")
    speaker = _speaker(session)
    speaker.speak_for_tool("web_surf")
    speaker.speak_for_tool("query_memory")
    await _drain(speaker)

    assert len(session.say_calls) == 1


async def test_filler_fires_again_after_dedupe_window(monkeypatch):
    monkeypatch.setattr(tool_filler_module, "_FILLER_DEDUP_WINDOW_S", 0.0)
    session = _FakeSession(agent_state="thinking")
    speaker = _speaker(session)
    speaker.speak_for_tool("web_surf")
    await _drain(speaker)
    speaker.speak_for_tool("query_memory")
    await _drain(speaker)

    assert len(session.say_calls) == 2


# ---------------------------------------------------------------- thinking guard


async def test_discarded_preemptive_generation_never_speaks(monkeypatch):
    monkeypatch.setattr(tool_filler_module, "_WAIT_FOR_THINKING_CAP_S", 0.15)
    session = _FakeSession(agent_state="listening")  # never commits
    speaker = _speaker(session)
    speaker.speak_for_tool("web_surf")
    await _drain(speaker)

    assert session.say_calls == []


async def test_late_committed_generation_still_speaks():
    session = _FakeSession(agent_state="listening")
    speaker = _speaker(session)
    speaker.speak_for_tool("web_surf")

    async def _commit_soon() -> None:
        await asyncio.sleep(0.1)
        session.agent_state = "thinking"

    await asyncio.gather(_commit_soon(), _drain(speaker))
    assert len(session.say_calls) == 1


async def test_say_failure_never_raises():
    class _BrokenSession(_FakeSession):
        async def say(self, phrase: str, **kwargs) -> None:
            raise RuntimeError("tts down")

    speaker = _speaker(_BrokenSession(agent_state="thinking"))
    speaker.speak_for_tool("web_surf")
    await _drain(speaker)  # must not raise


# ---------------------------------------------------------------- llm_node tee


class _StubAgent:
    """Bare self for BuddyAgent._speak_filler_on_tool_calls: speaker preset, and
    the real _maybe_fire_tool_filler bound so the tee's trigger path is exercised."""

    _maybe_fire_tool_filler = buddy_agent_module.BuddyAgent._maybe_fire_tool_filler

    def __init__(self, speaker) -> None:
        self._tool_filler_speaker = speaker
        self._session_id = "sess-test"
        self._user_id = "uid-test"


class _RecordingSpeaker:
    def __init__(self) -> None:
        self.tool_names: list[str] = []

    def speak_for_tool(self, tool_name: str) -> None:
        self.tool_names.append(tool_name)


def _text_chunk(text: str) -> lk_llm.ChatChunk:
    return lk_llm.ChatChunk(id="c", delta=lk_llm.ChoiceDelta(content=text))


def _tool_chunk(name: str) -> lk_llm.ChatChunk:
    return lk_llm.ChatChunk(
        id="c",
        delta=lk_llm.ChoiceDelta(
            tool_calls=[lk_llm.FunctionToolCall(name=name, arguments="{}", call_id="call-1")]
        ),
    )


async def _run_tee(chunks: list[object]) -> tuple[list[object], list[str]]:
    speaker = _RecordingSpeaker()
    stub = _StubAgent(speaker)

    async def _stream():
        for chunk in chunks:
            yield chunk

    tee = buddy_agent_module.BuddyAgent._speak_filler_on_tool_calls(stub, _stream())
    emitted = [item async for item in tee]
    return emitted, speaker.tool_names


async def test_tee_passes_every_chunk_through_untouched_and_in_order():
    chunks = [_text_chunk("hey "), _tool_chunk("web_surf"), _text_chunk("done")]
    emitted, _ = await _run_tee(chunks)
    assert emitted == chunks  # same objects, same order


async def test_tee_fires_speaker_for_named_tool_calls_only():
    emitted, tool_names = await _run_tee(
        [_text_chunk("hello"), _tool_chunk("web_surf"), _tool_chunk("query_memory")]
    )
    assert tool_names == ["web_surf", "query_memory"]
    assert len(emitted) == 3


async def test_tee_ignores_plain_strings_and_textless_chunks():
    chunks = ["bare string", lk_llm.ChatChunk(id="c", delta=None)]
    emitted, tool_names = await _run_tee(chunks)
    assert emitted == chunks
    assert tool_names == []
