"""
Tests for src/services/gemini_chat_fallback.py — the cross-provider chat hop.

Covers: Anthropic<->Gemini message translation, a text-only turn, a tool-call turn that
executes a tool and surfaces a reminder card, the clarification sentinel, and the error path.
The Gemini client is faked (no network); generate_content_stream is awaited and yields chunks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from src.services.gemini_chat_fallback import (
    stream_gemini_chat_fallback,
    _anthropic_messages_to_gemini_contents,
)
from src.shared.tools import claude_tool_definitions


# --- fakes -----------------------------------------------------------------

async def _achunks(chunks):
    for chunk in chunks:
        yield chunk


def _text_chunk(text: str) -> MagicMock:
    part = MagicMock()
    part.text = text
    part.function_call = None
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    return chunk


def _fc_chunk(name: str, args: dict) -> MagicMock:
    fc = MagicMock()
    fc.name = name
    fc.args = args
    part = MagicMock()
    part.text = None
    part.function_call = fc
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    return chunk


def _provider_streaming(side_effect):
    """A fake ModelProvider whose Gemini client's generate_content_stream is awaitable
    and returns the supplied async iterators (or raises, if side_effect is an exception)."""
    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = AsyncMock(side_effect=side_effect)
    provider = MagicMock()
    provider._get_gemini_client.return_value = fake_client
    return provider


async def _collect(**kwargs) -> list:
    return [e async for e in stream_gemini_chat_fallback(**kwargs)]


# --- translation -----------------------------------------------------------

def test_translation_roles_and_part_types():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "set_reminder", "input": {"m": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "{'id': 'r1'}"},
        ]},
    ]
    contents = _anthropic_messages_to_gemini_contents(messages)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert any(getattr(p, "function_call", None) for p in contents[1].parts)
    assert any(getattr(p, "function_response", None) for p in contents[2].parts)


# --- streaming behaviour ---------------------------------------------------

async def test_text_only_turn_streams_text_and_done():
    provider = _provider_streaming([_achunks([_text_chunk("hello there")])])
    te = MagicMock()
    te.execute = AsyncMock()
    with patch("src.services.gemini_chat_fallback.get_model_provider", return_value=provider):
        events = await _collect(
            tool_executor=te,
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "hello there"
    assert events[-1]["type"] == "done"
    te.execute.assert_not_called()


async def test_tool_call_executes_and_captures_reminder():
    provider = _provider_streaming([
        _achunks([_fc_chunk("set_reminder", {"message": "call mom"})]),
        _achunks([_text_chunk("done!")]),
    ])
    te = MagicMock()
    te.execute = AsyncMock(return_value={"id": "r1"})
    with patch("src.services.gemini_chat_fallback.get_model_provider", return_value=provider):
        events = await _collect(
            tool_executor=te,
            system_prompt="sys",
            messages=[{"role": "user", "content": "remind me to call mom"}],
            tools=claude_tool_definitions(),
        )

    te.execute.assert_awaited_once()
    assert te.execute.await_args.args[0] == "set_reminder"
    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "done!"
    done = events[-1]
    assert done["type"] == "done"
    assert done["metadata"].get("reminder") == {"id": "r1"}
    assert "set_reminder" in done["metadata"]["tool_names"]


async def test_clarification_sentinel_emits_ui_and_done():
    provider = _provider_streaming([
        _achunks([_fc_chunk("set_reminder", {"message": "x"})]),
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
    with patch("src.services.gemini_chat_fallback.get_model_provider", return_value=provider):
        events = await _collect(
            tool_executor=te,
            system_prompt="sys",
            messages=[{"role": "user", "content": "remind me"}],
            tools=claude_tool_definitions(),
        )

    assert any(e["type"] == "clarification_ui" and e["clarification_id"] == "c1" for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["metadata"].get("awaiting_clarification") is True


async def test_stream_error_yields_single_error_event():
    provider = _provider_streaming(RuntimeError("gemini exploded"))
    te = MagicMock()
    te.execute = AsyncMock()
    with patch("src.services.gemini_chat_fallback.get_model_provider", return_value=provider):
        events = await _collect(
            tool_executor=te,
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
    assert len(events) == 1
    assert events[0]["type"] == "error"
