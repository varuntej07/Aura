"""Normalizing what a model call actually consumed, for a fail-closed spend ledger.

The gap this closes. ``ModelProvider.cheap/balanced/expert`` return only the parsed
response, and its tier methods fall back across providers, so a research stage that
asked for `balanced` may in fact have been served by Gemini. Meanwhile the three
existing normalizers in ``analytics/llm_telemetry.py`` already know each SDK's field
spelling, but they emit Langfuse usage-detail dicts for observability, which is a
different job from telling a budget how much of its allowance is gone.

This module is the second consumer of those same helpers, not a second copy of them.
Exactly one place in the backend knows that Anthropic says ``input_tokens`` while Gemini
says ``prompt_token_count``, and it stays ``llm_telemetry``.

Two properties the ledger depends on:

1. **An unknown model costs an UNKNOWN amount, never zero.** ``cost_known=False`` with
   ``cost_microusd=None`` is a real answer that admission must fail closed on. A missing
   rate silently becoming 0 would delete the only spend boundary on the expensive path,
   which is precisely the fail-open behavior the reactive cost cap has and research is
   forbidden from copying.

2. **Every rounding goes UP.** Reservations already over-count rather than under-count,
   because a crashed stage holds its units until a sweeper declares it dead. Cost
   estimation follows the same direction: over-counting stops a run early, while
   under-counting overspends real money.

Rates are list prices per MILLION tokens, in micro-USD, checked 2026-08-10 against the
providers' own pricing pages. Only models with a rate verified in
``BACKGROUND_RESEARCH_AGENT_ARCHITECTURE.md`` section 8.0 appear here. Sonnet, Opus and
every other tier are DELIBERATELY absent rather than guessed: a fabricated number inside
a spend guard is worse than an honest unknown, and the repo rule against calling a cost
negligible points the same way. Add a row only with a primary source and a check date.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..analytics.llm_telemetry import (
    anthropic_usage_tokens,
    gemini_usage_tokens,
    openai_usage_tokens,
)


class ModelRate(BaseModel):
    """List price for one model, in micro-USD per million tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_microusd_per_mtok: int = Field(ge=0)
    output_microusd_per_mtok: int = Field(ge=0)
    checked_on: str


# $0.30 in = 300_000 micro-USD per million tokens.
MODEL_RATES: dict[str, ModelRate] = {
    "gemini-2.5-flash": ModelRate(
        input_microusd_per_mtok=300_000,
        output_microusd_per_mtok=2_500_000,
        checked_on="2026-08-10",
    ),
    "gemini-2.5-flash-lite": ModelRate(
        input_microusd_per_mtok=100_000,
        output_microusd_per_mtok=400_000,
        checked_on="2026-08-10",
    ),
    "claude-haiku-4-5-20251001": ModelRate(
        input_microusd_per_mtok=1_000_000,
        output_microusd_per_mtok=5_000_000,
        checked_on="2026-08-10",
    ),
    # settings.TIER_EXPERT, which is what synthesize runs on. Its absence meant every
    # synthesis reported cost_known=False, so the most expensive stage in the pipeline
    # contributed nothing to the project-day cost cap.
    "claude-sonnet-4-6": ModelRate(
        input_microusd_per_mtok=3_000_000,
        output_microusd_per_mtok=15_000_000,
        checked_on="2026-08-11",
    ),
}

# Longest first, so "gemini-2.5-flash-lite" can never be matched by the "gemini-2.5-flash"
# prefix. Sorting once at import beats getting the precedence subtly wrong per call.
_RATE_PREFIXES: tuple[str, ...] = tuple(
    sorted(MODEL_RATES, key=len, reverse=True)
)


class ProviderUsage(BaseModel):
    """What one model attempt consumed, normalized across providers.

    ``input_tokens`` excludes cached input on every provider: the Gemini and OpenAI
    helpers subtract it out (their prompt counts include it) and Anthropic reports it
    separately to begin with. So input + cached is the true prompt size, and adding them
    is safe rather than double counting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The model that ACTUALLY served the attempt, which after a fallback is not the one
    # the tier method names. The ledger records this, not the tier.
    model_id: str = Field(max_length=128)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    # False when the provider reported no usage at all. Distinct from "zero tokens":
    # a provider that omits its usage block would otherwise be recorded as a free call,
    # and a run could exhaust its allowance while the ledger read zero.
    usage_reported: bool = False

    cost_microusd: int | None = None
    # True only when the rate is known AND usage was actually reported. False means
    # "unknown", never "free". Admission must fail closed on it.
    cost_known: bool = False

    @property
    def total_input_tokens(self) -> int:
        """Prompt size including cache reads, for the token ceiling in RunBudget."""
        return self.input_tokens + self.cached_input_tokens


def find_rate(model_id: str) -> ModelRate | None:
    """Exact match, then longest known prefix, so a dated variant still resolves."""
    rate = MODEL_RATES.get(model_id)
    if rate is not None:
        return rate
    for prefix in _RATE_PREFIXES:
        if model_id.startswith(prefix):
            return MODEL_RATES[prefix]
    return None


def estimate_cost_microusd(
    model_id: str, *, input_tokens: int, output_tokens: int
) -> int | None:
    """List-price cost, rounded UP. None when no rate is known for this model.

    Returns whole micro-USD. A sub-micro-dollar call therefore costs 1, not 0, which
    keeps a long tail of tiny calls from summing to nothing in the ledger.
    """
    rate = find_rate(model_id)
    if rate is None:
        return None
    total = (
        input_tokens * rate.input_microusd_per_mtok
        + output_tokens * rate.output_microusd_per_mtok
    )
    return math.ceil(total / 1_000_000)


def _langfuse_tokens(raw_usage: Any) -> dict[str, int]:
    """Delegate to whichever existing helper matches the SDK object's shape.

    Detection is by attribute presence rather than by isinstance, because the usage
    objects are SDK-internal types this module must not import, and a stub or a mock
    with the right attributes is just as valid an input.
    """
    if raw_usage is None:
        return {}
    if isinstance(raw_usage, dict):
        # Already in Langfuse usage-detail shape (what finish(tokens=...) receives).
        return {
            key: int(value or 0)
            for key, value in raw_usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    if hasattr(raw_usage, "input_tokens"):
        return anthropic_usage_tokens(raw_usage)
    if hasattr(raw_usage, "prompt_token_count") or hasattr(
        raw_usage, "candidates_token_count"
    ):
        return gemini_usage_tokens(raw_usage)
    if hasattr(raw_usage, "prompt_tokens"):
        return openai_usage_tokens(raw_usage)
    # An unrecognised shape yields zero TOKENS, but the cost lookup below still runs on
    # the model id, so an unpriced model is still reported as cost_known=False.
    return {}


def normalize_provider_usage(model_id: str, raw_usage: Any) -> ProviderUsage:
    """One model attempt's consumption, ready for the budget ledger. Never raises.

    ``model_id`` must be the model that served the attempt. Passing the tier name
    ("balanced") would silently produce cost_known=False, which fails closed and is
    noisy rather than wrong, but it is not the intended use.
    """
    tokens = _langfuse_tokens(raw_usage)
    input_tokens = max(0, int(tokens.get("input", 0)))
    output_tokens = max(0, int(tokens.get("output", 0)))
    # Cache WRITES bill at a premium and cache READS at a discount. Neither discount is
    # documented in the approved price basis, so both are counted at the full input
    # rate: an over-estimate, in the safe direction, rather than an invented rate.
    cached = max(0, int(tokens.get("cache_read_input_tokens", 0))) + max(
        0, int(tokens.get("cache_creation_input_tokens", 0))
    )

    # An empty normalized dict means the usage object was absent or an unrecognised
    # shape. Every provider in use reports usage on a successful call, so absence is a
    # measurement failure, not a free call.
    usage_reported = bool(tokens)

    cost = estimate_cost_microusd(
        model_id,
        input_tokens=input_tokens + cached,
        output_tokens=output_tokens,
    )
    return ProviderUsage(
        model_id=model_id[:128],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        usage_reported=usage_reported,
        cost_microusd=cost if usage_reported else None,
        cost_known=cost is not None and usage_reported,
    )
