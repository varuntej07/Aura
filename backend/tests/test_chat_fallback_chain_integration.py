"""
True end-to-end integration test for the full text-chat fallback waterfall:
Sonnet -> Haiku -> Gemini Flash -> GPT-4.1-mini.

Unlike test_claude_client.py / test_gemini_chat_fallback.py / test_openai_chat_fallback.py
(each of which mocks the NEXT hop's function directly to pin one module's own contract in
isolation), this test mocks ONLY the three real network-facing SDK entry points (Anthropic's
messages.stream, Gemini's client, OpenAI's client) and lets the REAL claude_client ->
gemini_chat_fallback -> openai_chat_fallback call chain execute untouched. That is what
actually proves the cross-module wiring (kwarg names, message/tool threading) is correct,
not just each module's isolated contract.

Scenario: Sonnet and Haiku both fail with the exact real-world "insufficient credit balance"
shape (the reported bug), Gemini is ALSO down before any token, and GPT-4.1-mini is the one
provider still up. The user must still get a normal streamed reply.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import anthropic

from src.services.claude_client import ClaudeClient


def _insufficient_credit_balance() -> anthropic.BadRequestError:
    body = {
        "error": {
            "type": "invalid_request_error",
            "message": (
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits."
            ),
        }
    }
    return anthropic.BadRequestError(f"Error code: 400 - {body}", response=MagicMock(), body=body)


class _FakeAnthropicStream:
    """Stand-in for the Anthropic streaming async-context-manager: raises on __aenter__,
    simulating the API call itself failing before any token streamed."""

    def __init__(self, enter_exc: Exception):
        self._enter_exc = enter_exc

    async def __aenter__(self):
        raise self._enter_exc

    async def __aexit__(self, *_a):
        return False


class _FakeOpenAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _openai_text_chunk(text: str) -> MagicMock:
    delta = MagicMock()
    delta.content = text
    delta.tool_calls = None
    choice = MagicMock()
    choice.delta = delta
    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


async def test_full_chain_sonnet_haiku_gemini_down_gpt_serves_reply(monkeypatch):
    # 1. Anthropic: both Sonnet and Haiku fail immediately with the real billing-outage
    #    shape (non-retryable, so exactly one call per model, no backoff).
    anthropic_inner = MagicMock()
    anthropic_inner.messages.stream = MagicMock(
        side_effect=[
            _FakeAnthropicStream(_insufficient_credit_balance()),
            _FakeAnthropicStream(_insufficient_credit_balance()),
        ]
    )
    tool_executor = MagicMock()
    tool_executor.execute = AsyncMock()
    client = ClaudeClient(tool_executor)
    client._client = anthropic_inner
    monkeypatch.setattr("src.services.claude_client.asyncio.sleep", AsyncMock())

    # 2. Gemini: the REAL gemini_chat_fallback module runs; only its network-facing
    #    client construction is faked, and it fails before any token.
    gemini_provider = MagicMock()
    gemini_provider._get_gemini_client.side_effect = RuntimeError("gemini quota exhausted")
    monkeypatch.setattr(
        "src.services.gemini_chat_fallback.get_model_provider", lambda: gemini_provider
    )

    # 3. OpenAI: the REAL openai_chat_fallback module runs; only its network-facing
    #    client is faked, and it succeeds.
    openai_client = MagicMock()
    openai_client.chat.completions.create = AsyncMock(
        return_value=_FakeOpenAIStream([_openai_text_chunk("Hey, I'm still here!")])
    )
    monkeypatch.setattr(
        "src.services.openai_chat_fallback._get_openai_client", lambda: openai_client
    )

    events = [
        e async for e in client.send_text_turn_stream(system_prompt="sys", user_content="hi")
    ]

    # Every provider boundary was actually exercised, not skipped.
    assert anthropic_inner.messages.stream.call_count == 2
    gemini_provider._get_gemini_client.assert_called_once()
    openai_client.chat.completions.create.assert_awaited_once()

    types_seen = [e["type"] for e in events]
    assert "error" not in types_seen
    assert types_seen[-1] == "done"
    text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
    assert text == "Hey, I'm still here!"
