"""
Coverage for the Anthropic depleted-credits classification (model_provider).

Anthropic reports an empty prepaid balance as HTTP 400 invalid_request_error
("Your credit balance is too low..."), the same status a malformed request
gets. Found live 2026-07-08: every Buddy Drafts call died on that 400 and the
cross-provider fallback chain never engaged, because 400s were classified as
"fails identically everywhere, never fall back". These tests pin the split:
billing 400s fall back down the chain (the terminal Gemini hop runs on
separate billing), genuinely malformed 400s still fail fast.
"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from src.services.model_provider import ModelProvider, is_quota_exhausted


def _bad_request(message: str) -> anthropic.BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError(message, response=response, body=None)


_CREDIT_400 = _bad_request(
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too low to "
    "access the Anthropic API. Please go to Plans & Billing to upgrade or "
    "purchase credits.'}}"
)

_MALFORMED_400 = _bad_request(
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'messages: text content blocks must "
    "be non-empty'}}"
)


def test_is_quota_exhausted_matches_credit_balance_400():
    assert is_quota_exhausted(_CREDIT_400) is True


def test_is_quota_exhausted_ignores_genuine_bad_request():
    assert is_quota_exhausted(_MALFORMED_400) is False


class _FakeAnthropicClient:
    def __init__(self, exc: Exception):
        self._exc = exc
        parent = self

        class _Messages:
            async def create(self, **kwargs):
                raise parent._exc

        self.messages = _Messages()


def _provider_raising(monkeypatch, exc: Exception) -> tuple[ModelProvider, dict]:
    provider = ModelProvider()
    monkeypatch.setattr(
        provider, "_get_anthropic_client", lambda: _FakeAnthropicClient(exc)
    )
    fallback_calls: dict = {}

    async def _fake_call(**kwargs):
        fallback_calls.update(kwargs)
        return "fallback text"

    monkeypatch.setattr(provider, "_call", _fake_call)
    return provider, fallback_calls


async def test_credit_400_falls_back_down_the_chain(monkeypatch):
    provider, fallback_calls = _provider_raising(monkeypatch, _CREDIT_400)

    result = await provider._call_anthropic(
        model_id="claude-sonnet-4-6",
        fallback_chain=["gemini-2.5-flash"],
        prompt="draft it",
        system="sys",
        tools=None,
        images=[{"media_type": "image/jpeg", "data": "ZmFrZQ=="}],
        history=None,
        temperature=0.7,
    )

    assert result == "fallback text"
    assert fallback_calls["model_id"] == "gemini-2.5-flash"
    assert fallback_calls["fallback_chain"] == []
    # The frame survives the hop (the Gemini path is vision-capable).
    assert fallback_calls["images"] == [{"media_type": "image/jpeg", "data": "ZmFrZQ=="}]


async def test_credit_400_with_empty_chain_raises(monkeypatch):
    provider, fallback_calls = _provider_raising(monkeypatch, _CREDIT_400)

    with pytest.raises(anthropic.BadRequestError):
        await provider._call_anthropic(
            model_id="gemini-terminal-was-last",
            fallback_chain=[],
            prompt="draft it",
            system=None,
            tools=None,
            images=None,
            history=None,
            temperature=0.7,
        )
    assert fallback_calls == {}


async def test_malformed_400_never_falls_back(monkeypatch):
    provider, fallback_calls = _provider_raising(monkeypatch, _MALFORMED_400)

    with pytest.raises(anthropic.BadRequestError):
        await provider._call_anthropic(
            model_id="claude-sonnet-4-6",
            fallback_chain=["gemini-2.5-flash"],
            prompt="draft it",
            system=None,
            tools=None,
            images=None,
            history=None,
            temperature=0.7,
        )
    assert fallback_calls == {}  # a real bad request must not multiply cost