"""
Tests for src/services/openai_chat_fallback.py — the third-tier (GPT) chat hop.

Covers: Anthropic<->OpenAI message translation (including the tool_result-must-be-a-
separate-message split OpenAI requires that Gemini/Anthropic don't), a text-only turn, a
tool-call turn that executes a tool and surfaces a reminder card, the clarification
sentinel, and the error path -- this is the LAST tier, so its own failure must yield
exactly one FRIENDLY error event, never the raw provider exception text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.services import openai_chat_fallback
from src.services.chat_error_copy import CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE
from src.services.openai_chat_fallback import (
    _anthropic_messages_to_openai_messages,
    stream_openai_chat_fallback,
)
from src.shared.tools import claude_tool_definitions

# openai_chat_fallback._client (the module-level AsyncOpenAI singleton) is reset
# by the autouse conftest.py fixture reset_openai_chat_fallback_client.


# --- fakes -------------------------------------------------------------------

def _text_chunk(text: str) -> MagicMock:
    delta = MagicMock()
    delta.content = text
    delta.tool_calls = None
    choice = MagicMock()
    choice.delta = delta
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


def _tool_call_chunk(
    index: int, tool_id: str | None, name: str | None, arguments: str | None
) -> MagicMock:
    delta = MagicMock()
    delta.content = None
    tc = MagicMock()
    tc.index = index
    tc.id = tool_id
    func = MagicMock()
    func.name = name
    func.arguments = arguments
    tc.function = func
    delta.tool_calls = [tc]
    choice = MagicMock()
    choice.delta = delta
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


class _FakeOpenAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _client_streaming(side_effect) -> MagicMock:
    """A fake AsyncOpenAI client whose chat.completions.create is awaitable and
    returns the supplied async iterators (or raises, if side_effect is an exception)."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=side_effect)
    return client


async def _collect(monkeypatch, client, **kwargs) -> list:
    monkeypatch.setattr(openai_chat_fallback, "_get_openai_client", lambda: client)
    return [e async for e in stream_openai_chat_fallback(**kwargs)]


# --- translation ---------------------------------------------------------------

def test_translation_assistant_tool_call_and_separate_tool_message():
    messages = [
        {"role": "user", "content": "remind me to call mom"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me set that up"},
            {"type": "tool_use", "id": "t1", "name": "set_reminder", "input": {"m": "call mom"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "{'id': 'r1'}"},
        ]},
    ]
    translated = _anthropic_messages_to_openai_messages(messages)

    assert translated[0] == {"role": "user", "content": "remind me to call mom"}
    assert translated[1]["role"] == "assistant"
    assert translated[1]["content"] == "let me set that up"
    assert translated[1]["tool_calls"][0]["id"] == "t1"
    assert translated[1]["tool_calls"][0]["function"]["name"] == "set_reminder"
    # The tool_result became its OWN standalone message (OpenAI's requirement),
    # not embedded inside a user-role message.
    assert translated[2] == {
        "role": "tool", "tool_call_id": "t1", "content": "{'id': 'r1'}",
    }


def test_translation_drops_unknown_tool_result_with_no_matching_call():
    # A tool_result whose tool_use_id was never seen (shouldn't happen in practice,
    # but must not crash or fabricate a dangling tool message).
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ghost", "content": "x"},
        ]},
    ]
    assert _anthropic_messages_to_openai_messages(messages) == []


# --- streaming behaviour ---------------------------------------------------------

async def test_text_only_turn_streams_text_and_done(monkeypatch):
    client = _client_streaming([_FakeOpenAIStream([_text_chunk("hello there")])])
    te = MagicMock()
    te.execute = AsyncMock()

    events = await _collect(
        monkeypatch, client,
        tool_executor=te, system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}], tools=[],
    )

    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "hello there"
    assert events[-1]["type"] == "done"
    te.execute.assert_not_called()


async def test_tool_call_executes_and_captures_reminder(monkeypatch):
    reminder_args = '{"message": "call mom"}'
    client = _client_streaming([
        _FakeOpenAIStream([_tool_call_chunk(0, "call_1", "set_reminder", reminder_args)]),
        _FakeOpenAIStream([_text_chunk("done!")]),
    ])
    te = MagicMock()
    te.execute = AsyncMock(return_value={"id": "r1"})

    events = await _collect(
        monkeypatch, client,
        tool_executor=te, system_prompt="sys",
        messages=[{"role": "user", "content": "remind me to call mom"}],
        tools=claude_tool_definitions(),
    )

    te.execute.assert_awaited_once()
    assert te.execute.await_args.args[0] == "set_reminder"
    assert te.execute.await_args.args[1] == {"message": "call mom"}
    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "done!"
    done = events[-1]
    assert done["type"] == "done"
    assert done["metadata"].get("reminder") == {"id": "r1"}
    assert "set_reminder" in done["metadata"]["tool_names"]


async def test_tool_call_malformed_json_arguments_does_not_crash(monkeypatch):
    client = _client_streaming([
        _FakeOpenAIStream([_tool_call_chunk(0, "call_1", "set_reminder", "{not valid json")]),
        _FakeOpenAIStream([_text_chunk("ok")]),
    ])
    te = MagicMock()
    te.execute = AsyncMock(return_value={"id": "r1"})

    events = await _collect(
        monkeypatch, client,
        tool_executor=te, system_prompt="sys",
        messages=[{"role": "user", "content": "remind me"}],
        tools=claude_tool_definitions(),
    )

    te.execute.assert_awaited_once_with("set_reminder", {})
    assert events[-1]["type"] == "done"


async def test_clarification_sentinel_emits_ui_and_done(monkeypatch):
    client = _client_streaming([
        _FakeOpenAIStream([_tool_call_chunk(0, "call_1", "set_reminder", "{}")]),
    ])
    clarification = {
        "__clarification__": True,
        "clarification_id": "c1",
        "question": "when?",
        "options": ["now", "later"],
        "multi_select": False,
    }
    te = MagicMock()
    te.execute = AsyncMock(return_value=clarification)

    events = await _collect(
        monkeypatch, client,
        tool_executor=te, system_prompt="sys",
        messages=[{"role": "user", "content": "remind me"}],
        tools=claude_tool_definitions(),
    )

    assert any(e["type"] == "clarification_ui" and e["clarification_id"] == "c1" for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["metadata"].get("awaiting_clarification") is True


# --- error path: last tier, must never leak raw text -----------------------------

async def test_stream_error_yields_friendly_message_not_raw_text(monkeypatch):
    client = _client_streaming(RuntimeError("gpt exploded: sk-proj-abc123 invalid"))
    te = MagicMock()
    te.execute = AsyncMock()

    events = await _collect(
        monkeypatch, client,
        tool_executor=te, system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}], tools=[],
    )

    assert len(events) == 1
    assert events[0] == {"type": "error", "message": CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE}
    assert "gpt exploded" not in events[0]["message"]
    assert "sk-proj" not in events[0]["message"]


async def test_missing_api_key_yields_friendly_message_not_raise(monkeypatch):
    monkeypatch.setattr(openai_chat_fallback.settings, "OPENAI_API_KEY", "")
    te = MagicMock()
    te.execute = AsyncMock()

    events = [
        e async for e in stream_openai_chat_fallback(
            tool_executor=te, system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}], tools=[],
        )
    ]

    assert events == [{"type": "error", "message": CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE}]
