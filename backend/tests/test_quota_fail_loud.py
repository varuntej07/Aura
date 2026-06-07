"""
Fail-loud coverage for Gemini quota/credits exhaustion.

When the Gemini prepay credits deplete, every embedding call returns 429
RESOURCE_EXHAUSTED. Embeddings have no non-Gemini fallback, so the content pool
stops refreshing and signal-engine notifications dry up while every tick still
returns 200 — the "zero rows looks healthy" trap that hid a real outage.

These pin two guards:
  1. content_ingest._embed_and_write logs a loud ERROR (naming the cause) and
     re-raises on a quota-exhausted embedding failure — and stays quiet for
     unrelated errors.
  2. scoring_loop.run_tick logs a loud WARNING when a tick sends 0 notifications
     AND the content pool is empty (ingest starved), but not when the pool has
     content (nothing simply cleared threshold — a normal outcome).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.signal_engine import content_ingest, scoring_loop
from src.services.signal_engine.content_pool import CandidateInput


def _fake_candidate() -> CandidateInput:
    return CandidateInput(
        source="hackernews",
        category="tech",
        title="A neat new compiler",
        body="It does interesting things.",
        url="https://example.com/a",
    )


class _QuotaError(Exception):
    """Stands in for the google-genai 429 the embedder raises when credits run out."""

    code = 429


async def test_embed_and_write_screams_on_quota_exhausted(monkeypatch):
    """A 429/credits-depleted embedding failure logs a loud ERROR and re-raises."""
    async def failing_add(_candidates):
        raise _QuotaError("429 RESOURCE_EXHAUSTED: prepayment credits are depleted")

    monkeypatch.setattr(content_ingest, "add_candidates", failing_add)

    with patch.object(content_ingest.logger, "error") as mock_error:
        with pytest.raises(_QuotaError):
            await content_ingest._embed_and_write([_fake_candidate()])

    exhausted_logs = [c for c in mock_error.call_args_list if "EXHAUSTED" in str(c)]
    assert len(exhausted_logs) == 1


async def test_embed_and_write_reraises_other_errors_without_quota_log(monkeypatch):
    """An unrelated failure must still propagate but NOT be mislabeled as a quota outage."""
    async def failing_add(_candidates):
        raise ValueError("malformed candidate")

    monkeypatch.setattr(content_ingest, "add_candidates", failing_add)

    with patch.object(content_ingest.logger, "error") as mock_error:
        with pytest.raises(ValueError):
            await content_ingest._embed_and_write([_fake_candidate()])

    assert not any("EXHAUSTED" in str(c) for c in mock_error.call_args_list)


def _patch_tick_dependencies(monkeypatch):
    """Stub run_tick's external touch points so only the pool-empty guard is exercised."""
    monkeypatch.setattr(
        scoring_loop.feature_store,
        "list_active_user_ids",
        AsyncMock(return_value=["uid-1"]),
    )

    async def _no_send(user_id, models, summary):
        return None  # the tick processes a user but sends nothing

    monkeypatch.setattr(scoring_loop, "_score_one_user", _no_send)
    monkeypatch.setattr(scoring_loop, "get_model_provider", lambda: MagicMock())
    monkeypatch.setattr(scoring_loop.posthog_client, "flush", AsyncMock())


async def test_run_tick_warns_when_zero_sends_and_pool_empty(monkeypatch):
    _patch_tick_dependencies(monkeypatch)
    monkeypatch.setattr(scoring_loop, "has_any_candidate", AsyncMock(return_value=False))

    with patch.object(scoring_loop.logger, "warn") as mock_warn:
        summary = await scoring_loop.run_tick()

    assert summary.notifications_sent == 0
    pool_warnings = [c for c in mock_warn.call_args_list if "pool is EMPTY" in str(c)]
    assert len(pool_warnings) == 1


async def test_run_tick_no_pool_warn_when_pool_has_content(monkeypatch):
    """Pool has content but nothing cleared threshold — normal, must stay quiet."""
    _patch_tick_dependencies(monkeypatch)
    monkeypatch.setattr(scoring_loop, "has_any_candidate", AsyncMock(return_value=True))

    with patch.object(scoring_loop.logger, "warn") as mock_warn:
        await scoring_loop.run_tick()

    pool_warnings = [c for c in mock_warn.call_args_list if "pool is EMPTY" in str(c)]
    assert pool_warnings == []


# ---------------------------------------------------------------------------
# ModelProvider._call_gemini logging contract
#
# The retry loop used to emit a WARNING per attempt that dropped the underlying
# error message, so a depleted-credits outage was indistinguishable from a
# transient blip and buried under hundreds of identical lines. These pin:
#   - a quota-exhausted give-up logs ONE loud ERROR naming the cause
#   - a non-quota transient failure does NOT get mislabeled as a quota outage
#   - per-attempt backoff is DEBUG (not WARN) and still carries the real error
# ---------------------------------------------------------------------------

from src.services import model_provider as mp  # noqa: E402


class _OverloadError(Exception):
    """A transient 503 — retryable, but NOT a credits/quota outage."""

    code = 503


def _gemini_provider_raising(monkeypatch, exc: Exception) -> mp.ModelProvider:
    """A ModelProvider whose Gemini client always raises `exc`, with backoff sleeps skipped."""
    provider = mp.ModelProvider()
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = exc
    monkeypatch.setattr(provider, "_get_gemini_client", lambda: fake_client)
    monkeypatch.setattr(mp.asyncio, "sleep", AsyncMock())
    return provider


async def test_call_gemini_screams_on_quota_exhausted(monkeypatch):
    """429 RESOURCE_EXHAUSTED with no fallback → one loud ERROR + re-raise."""
    provider = _gemini_provider_raising(
        monkeypatch,
        _QuotaError("429 RESOURCE_EXHAUSTED: prepay credits depleted"),
    )

    with patch.object(mp.logger, "error") as mock_error, \
         patch.object(mp.logger, "warn") as mock_warn, \
         patch.object(mp.logger, "debug") as mock_debug:
        with pytest.raises(_QuotaError):
            await provider._call_gemini(
                model_id="gemini-2.5-flash",
                fallback_chain=[],
                prompt="hi",
                system=None,
                temperature=0.5,
            )

    exhausted = [c for c in mock_error.call_args_list if "EXHAUSTED" in str(c)]
    assert len(exhausted) == 1
    # Flood reduction: per-attempt backoff is DEBUG, so no WARNs on this path.
    assert mock_warn.call_args_list == []
    # The dropped field is restored: the real error rides the DEBUG backoff lines.
    assert any("RESOURCE_EXHAUSTED" in str(c) for c in mock_debug.call_args_list)


async def test_call_gemini_transient_error_not_labeled_quota(monkeypatch):
    """A 503 overload must propagate but NOT be mislabeled as a credits outage."""
    provider = _gemini_provider_raising(
        monkeypatch,
        _OverloadError("503 UNAVAILABLE: the model is overloaded"),
    )

    with patch.object(mp.logger, "error") as mock_error:
        with pytest.raises(_OverloadError):
            await provider._call_gemini(
                model_id="gemini-2.5-flash",
                fallback_chain=[],
                prompt="hi",
                system=None,
                temperature=0.5,
            )

    assert not any("EXHAUSTED" in str(c) for c in mock_error.call_args_list)
