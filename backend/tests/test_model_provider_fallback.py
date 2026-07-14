"""
Fallback coverage for model_provider's Anthropic paths.

_call_anthropic (powers balanced()/expert()) and reason_turn() gained a fallback chain,
mirroring the already-tested _call_gemini chain. These pin:
  - _call_anthropic falls over to the next model after retryable retries are exhausted,
    including the cross-provider terminal hop into _call_gemini
  - a 404 (NotFoundError) skips retries and jumps straight to the next model
  - a 400 (BadRequestError) raises immediately and never falls back (fails identically
    on every model)
  - expert() walks Sonnet -> Haiku -> Gemini Flash end to end
  - reason_turn() escalates Sonnet -> Haiku and raises only after the last model
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from src.services import model_provider as mp


# --- helpers ---------------------------------------------------------------

def _text_response(text: str):
    """An Anthropic Messages response carrying a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _rate_limit() -> anthropic.RateLimitError:
    return anthropic.RateLimitError("429", response=MagicMock(), body={})


def _internal_error() -> anthropic.InternalServerError:
    return anthropic.InternalServerError("500", response=MagicMock(), body={})


def _provider_with_anthropic(create_mock: AsyncMock) -> mp.ModelProvider:
    """A ModelProvider whose Anthropic client.messages.create is `create_mock`."""
    provider = mp.ModelProvider()
    client = MagicMock()
    client.messages.create = create_mock
    provider._anthropic = client
    return provider


def _attach_gemini(provider: mp.ModelProvider, text: str, monkeypatch) -> MagicMock:
    """Give the provider a Gemini client whose generate_content returns `text`."""
    g_client = MagicMock()
    g_resp = MagicMock()
    g_resp.text = text
    g_client.models.generate_content.return_value = g_resp
    monkeypatch.setattr(provider, "_get_gemini_client", lambda: g_client)
    return g_client


# --- _call_anthropic fallback chain ---------------------------------------

async def test_call_anthropic_falls_back_to_gemini_after_retries(monkeypatch):
    """Retryable failure on the only Anthropic model -> recurse into the Gemini terminal hop."""
    create = AsyncMock(side_effect=_rate_limit())
    provider = _provider_with_anthropic(create)
    _attach_gemini(provider, "answer from gemini", monkeypatch)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider._call_anthropic(
        model_id="claude-haiku-4-5-20251001",
        fallback_chain=["gemini-2.5-flash"],
        prompt="hi",
        system=None,
        tools=None,
        history=None,
        temperature=0.5,
    )

    assert result == "answer from gemini"
    assert create.await_count == mp._MAX_RETRIES  # primary exhausted its retries first


async def test_call_anthropic_404_skips_retries_to_fallback(monkeypatch):
    """A model-not-found (404) is pointless to retry: jump straight to the next model."""
    create = AsyncMock(side_effect=anthropic.NotFoundError("404", response=MagicMock(), body={}))
    provider = _provider_with_anthropic(create)
    _attach_gemini(provider, "served by fallback", monkeypatch)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider._call_anthropic(
        model_id="claude-bogus",
        fallback_chain=["gemini-2.5-flash"],
        prompt="hi",
        system=None,
        tools=None,
        history=None,
        temperature=0.5,
    )

    assert result == "served by fallback"
    assert create.await_count == 1  # no retries on a 404


async def test_call_anthropic_400_raises_without_fallback(monkeypatch):
    """A malformed request (400) fails the same on every model: raise, never fall back."""
    create = AsyncMock(side_effect=anthropic.BadRequestError("400", response=MagicMock(), body={}))
    provider = _provider_with_anthropic(create)
    g_client = _attach_gemini(provider, "should not be reached", monkeypatch)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    with pytest.raises(anthropic.BadRequestError):
        await provider._call_anthropic(
            model_id="claude-haiku-4-5-20251001",
            fallback_chain=["gemini-2.5-flash"],
            prompt="hi",
            system=None,
            tools=None,
            history=None,
            temperature=0.5,
        )

    assert create.await_count == 1
    g_client.models.generate_content.assert_not_called()


async def test_call_anthropic_no_chain_raises(monkeypatch):
    """With an empty chain, an exhausted retryable failure raises (nothing to fall back to)."""
    create = AsyncMock(side_effect=_rate_limit())
    provider = _provider_with_anthropic(create)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    with pytest.raises(anthropic.RateLimitError):
        await provider._call_anthropic(
            model_id="claude-haiku-4-5-20251001",
            fallback_chain=[],
            prompt="hi",
            system=None,
            tools=None,
            history=None,
            temperature=0.5,
        )
    assert create.await_count == mp._MAX_RETRIES


async def test_expert_walks_sonnet_haiku_then_gemini(monkeypatch):
    """expert() chain Sonnet -> Haiku -> Gemini Flash: both Claude models down -> Gemini serves."""
    create = AsyncMock(side_effect=_rate_limit())  # every Claude attempt 429s
    provider = _provider_with_anthropic(create)
    _attach_gemini(provider, "gemini rescued it", monkeypatch)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider.expert("a hard question")

    assert result == "gemini rescued it"
    # Sonnet x _MAX_RETRIES + Haiku x _MAX_RETRIES before the Gemini hop.
    assert create.await_count == 2 * mp._MAX_RETRIES


async def test_balanced_falls_back_to_gemini(monkeypatch):
    """balanced() (Haiku) falls back to Gemini Flash when Haiku is down."""
    create = AsyncMock(side_effect=_rate_limit())
    provider = _provider_with_anthropic(create)
    _attach_gemini(provider, "balanced via gemini", monkeypatch)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider.balanced("classify this")

    assert result == "balanced via gemini"
    assert create.await_count == mp._MAX_RETRIES


async def test_balanced_images_ride_into_anthropic_blocks():
    """balanced(images=...) builds Anthropic base64 image blocks before the text."""
    # A tiny valid-base64 payload; the screen-demo endpoint sends real JPEGs.
    image_b64 = "aGVsbG8="
    create = AsyncMock(return_value=_text_response("i can see it"))
    provider = _provider_with_anthropic(create)

    result = await provider.balanced(
        "what is on screen",
        images=[{"media_type": "image/jpeg", "data": image_b64}],
    )

    assert result == "i can see it"
    assert create.await_args is not None
    content = create.await_args.kwargs["messages"][-1]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["data"] == image_b64
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[-1] == {"type": "text", "text": "what is on screen"}


async def test_balanced_images_survive_the_gemini_fallback_hop(monkeypatch):
    """The vision payload must not be dropped when Haiku falls over to Gemini."""
    image_b64 = "aGVsbG8="
    create = AsyncMock(side_effect=_rate_limit())
    provider = _provider_with_anthropic(create)
    g_client = _attach_gemini(provider, "gemini saw it", monkeypatch)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider.balanced(
        "what is on screen",
        images=[{"media_type": "image/jpeg", "data": image_b64}],
    )

    assert result == "gemini saw it"
    contents = g_client.models.generate_content.call_args.kwargs["contents"]
    # Image part first (from_bytes), prompt last — the image crossed providers.
    assert len(contents) == 2
    assert contents[-1] == "what is on screen"


async def test_call_anthropic_recovers_on_fallback_model(monkeypatch):
    """Primary 429s through its retries; the fallback Anthropic model then succeeds."""
    create = AsyncMock(side_effect=[_rate_limit(), _rate_limit(), _rate_limit(),
                                    _text_response("haiku saved it")])
    provider = _provider_with_anthropic(create)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider._call_anthropic(
        model_id="claude-sonnet-4-6",
        fallback_chain=["claude-haiku-4-5-20251001"],
        prompt="hi",
        system=None,
        tools=None,
        history=None,
        temperature=0.5,
    )

    assert result == "haiku saved it"
    assert create.await_count == mp._MAX_RETRIES + 1  # 3 sonnet + 1 haiku


# --- reason_turn escalation -----------------------------------------------

def _stream_cm(final_message=None, exc: Exception | None = None):
    """A mock `messages.stream(...)` async-context-manager. get_final_message either returns
    `final_message` or raises `exc`."""
    stream_obj = MagicMock()
    if exc is not None:
        stream_obj.get_final_message = AsyncMock(side_effect=exc)
    else:
        stream_obj.get_final_message = AsyncMock(return_value=final_message)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream_obj)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _provider_with_stream(stream_side_effect: list) -> tuple[mp.ModelProvider, MagicMock]:
    provider = mp.ModelProvider()
    inner = MagicMock()
    inner.messages.stream = MagicMock(side_effect=stream_side_effect)
    base = MagicMock()
    base.with_options.return_value = inner
    provider._anthropic = base
    return provider, inner


async def test_reason_turn_escalates_to_fallback_model(monkeypatch):
    """Sonnet stream fails through its retries; Haiku then returns the message."""
    sentinel = MagicMock()
    side_effects = [
        _stream_cm(exc=_internal_error()),
        _stream_cm(exc=_internal_error()),
        _stream_cm(exc=_internal_error()),
        _stream_cm(final_message=sentinel),
    ]
    provider, inner = _provider_with_stream(side_effects)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    result = await provider.reason_turn([{"role": "user", "content": "x"}])

    assert result is sentinel
    assert inner.messages.stream.call_count == mp._MAX_RETRIES + 1  # 3 sonnet + 1 haiku


async def test_reason_turn_both_models_down_raises(monkeypatch):
    """Both Sonnet and Haiku exhausted -> raise after the last model, no infinite loop."""
    side_effects = [_stream_cm(exc=_internal_error()) for _ in range(2 * mp._MAX_RETRIES)]
    provider, inner = _provider_with_stream(side_effects)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())

    with pytest.raises(anthropic.InternalServerError):
        await provider.reason_turn([{"role": "user", "content": "x"}])

    assert inner.messages.stream.call_count == 2 * mp._MAX_RETRIES
