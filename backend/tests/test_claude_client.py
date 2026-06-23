"""
Fallback coverage for the main text-chat client (src/services/claude_client.py).

The streaming chat path escalates Sonnet -> Haiku and, if the whole Anthropic chain is down
BEFORE any token streamed, hands off to the cross-provider Gemini hop. These pin:
  - a model that fails before any text -> escalate to the Anthropic fallback model, user
    still gets text + done, no error
  - a failure AFTER a token has streamed -> propagate as ONE error event, no fallback
  - the whole Anthropic chain exhausted before any token -> delegate to the Gemini hop
  - both providers down -> exactly one error event
  - the non-streaming send_text_turn escalates Sonnet -> Haiku
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import anthropic

from src.services.claude_client import ClaudeClient
from src.config.settings import settings


# --- fakes -----------------------------------------------------------------

def _rate_limit() -> anthropic.RateLimitError:
    return anthropic.RateLimitError("429", response=MagicMock(), body={})


def _usage() -> MagicMock:
    u = MagicMock()
    u.input_tokens = 1
    u.output_tokens = 1
    u.cache_read_input_tokens = 0
    u.cache_creation_input_tokens = 0
    return u


def _text_delta_event(text: str) -> MagicMock:
    ev = MagicMock()
    ev.type = "content_block_delta"
    ev.delta = MagicMock()
    ev.delta.type = "text_delta"
    ev.delta.text = text
    return ev


def _end_turn_event() -> MagicMock:
    ev = MagicMock()
    ev.type = "message_delta"
    ev.delta = MagicMock()
    ev.delta.stop_reason = "end_turn"
    return ev


def _final_response(stop_reason: str = "end_turn", content=None) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content or []
    resp.usage = _usage()
    return resp


class _FakeStream:
    """Stand-in for the Anthropic streaming async-context-manager."""

    def __init__(self, events=None, final=None, enter_exc=None, final_exc=None):
        self._events = events or []
        self._final = final if final is not None else _final_response()
        self._enter_exc = enter_exc
        self._final_exc = final_exc

    async def __aenter__(self):
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *_a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        if self._final_exc is not None:
            raise self._final_exc
        return self._final


def _client_with_streams(stream_side_effect: list) -> tuple[ClaudeClient, MagicMock]:
    tool_executor = MagicMock()
    tool_executor.execute = AsyncMock()
    client = ClaudeClient(tool_executor)
    inner = MagicMock()
    inner.messages.stream = MagicMock(side_effect=stream_side_effect)
    client._client = inner
    return client, inner


async def _collect(agen) -> list:
    return [e async for e in agen]


# --- streaming fallback ----------------------------------------------------

async def test_stream_escalates_to_fallback_model_before_any_text(monkeypatch):
    """Sonnet fails its retries before any token; Haiku then streams the reply."""
    success = _FakeStream(events=[_text_delta_event("Hi from fallback"), _end_turn_event()])
    side_effects = [
        _FakeStream(enter_exc=_rate_limit()),
        _FakeStream(enter_exc=_rate_limit()),
        _FakeStream(enter_exc=_rate_limit()),
        success,
    ]
    client, inner = _client_with_streams(side_effects)
    monkeypatch.setattr("src.services.claude_client.asyncio.sleep", AsyncMock())

    events = await _collect(
        client.send_text_turn_stream(system_prompt="sys", user_content="hi")
    )

    types = [e["type"] for e in events]
    assert "error" not in types
    assert "done" in types
    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "Hi from fallback"
    # 3 sonnet attempts + 1 haiku; the 4th call used the fallback model.
    assert inner.messages.stream.call_count == 4
    assert inner.messages.stream.call_args_list[-1].kwargs["model"] == settings.ANTHROPIC_CHAT_MODEL_FALLBACK


async def test_stream_failure_after_text_started_propagates_no_fallback(monkeypatch):
    """Once a token has streamed, a failure becomes one error event — no model switch."""
    long_text = "x" * 90  # > _NARRATION_MAX_CHARS, so it commits and streams immediately
    failing_after_text = _FakeStream(
        events=[_text_delta_event(long_text)], final_exc=_rate_limit()
    )
    client, inner = _client_with_streams([failing_after_text])
    monkeypatch.setattr("src.services.claude_client.asyncio.sleep", AsyncMock())

    events = await _collect(
        client.send_text_turn_stream(system_prompt="sys", user_content="hi")
    )

    types = [e["type"] for e in events]
    assert types.count("error") == 1
    assert any(e["type"] == "text_delta" for e in events)
    assert inner.messages.stream.call_count == 1  # no retry/fallback after text_started


async def test_stream_delegates_to_gemini_when_anthropic_chain_exhausted(monkeypatch):
    """Both Anthropic models down before any token -> the Gemini hop serves the reply."""
    side_effects = [_FakeStream(enter_exc=_rate_limit()) for _ in range(6)]  # 3 sonnet + 3 haiku
    client, inner = _client_with_streams(side_effects)
    monkeypatch.setattr("src.services.claude_client.asyncio.sleep", AsyncMock())

    async def _fake_gemini(**_kwargs):
        yield {"type": "text_delta", "delta": "from gemini"}
        yield {"type": "done", "metadata": {"tool_names": []}}

    called = {}

    def _spy(**kwargs):
        called["kwargs"] = kwargs
        return _fake_gemini(**kwargs)

    with patch("src.services.claude_client.stream_gemini_chat_fallback", _spy):
        events = await _collect(
            client.send_text_turn_stream(system_prompt="sys", user_content="hi")
        )

    assert inner.messages.stream.call_count == 6
    assert called  # the hop was invoked
    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "from gemini"
    assert events[-1]["type"] == "done"
    assert [e["type"] for e in events].count("error") == 0


async def test_stream_both_providers_down_single_error(monkeypatch):
    """Anthropic chain down AND the Gemini hop fails -> exactly one error event."""
    side_effects = [_FakeStream(enter_exc=_rate_limit()) for _ in range(6)]
    client, _inner = _client_with_streams(side_effects)
    monkeypatch.setattr("src.services.claude_client.asyncio.sleep", AsyncMock())

    async def _fake_gemini_error(**_kwargs):
        yield {"type": "error", "message": "gemini down too"}

    with patch("src.services.claude_client.stream_gemini_chat_fallback", _fake_gemini_error):
        events = await _collect(
            client.send_text_turn_stream(system_prompt="sys", user_content="hi")
        )

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1


# --- non-streaming fallback ------------------------------------------------

async def test_send_text_turn_escalates_to_fallback_model(monkeypatch):
    """Non-streaming: Sonnet 429s through retries, Haiku returns the answer."""
    tool_executor = MagicMock()
    tool_executor.execute = AsyncMock()
    client = ClaudeClient(tool_executor)

    block = MagicMock()
    block.type = "text"
    block.text = "answer from haiku"
    final = MagicMock()
    final.stop_reason = "end_turn"
    final.content = [block]
    final.usage = _usage()

    inner = MagicMock()
    inner.messages.create = AsyncMock(
        side_effect=[_rate_limit(), _rate_limit(), _rate_limit(), final]
    )
    client._client = inner
    monkeypatch.setattr("src.services.claude_client.asyncio.sleep", AsyncMock())

    result = await client.send_text_turn(system_prompt="sys", user_content="hi")

    assert result["text"] == "answer from haiku"
    assert inner.messages.create.call_count == 4
    assert inner.messages.create.call_args_list[-1].kwargs["model"] == settings.ANTHROPIC_CHAT_MODEL_FALLBACK
