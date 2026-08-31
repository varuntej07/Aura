"""Estimated LLM prices for the cost ledger.

These are ESTIMATES for per-user spend visibility, never billing truth: the
provider invoice is the only real number. Prices are USD per million tokens,
prefix-matched on the model id so dated snapshots (claude-haiku-4-5-20251001)
resolve to their family row. An unknown model still gets its tokens counted by
the ledger; only the µUSD estimate stays 0, with one warn per process so a new
model shows up in logs instead of silently pricing at zero.

Rates verified 2026-08-30 (Anthropic first-party API; OpenAI and Gemini public
list prices). Update this table when a provider changes list pricing or a new
model enters `config/settings.py` fallback chains.
"""

from __future__ import annotations

from ...lib.logger import logger

# (input, cached_input, output) USD per 1M tokens. Longest matching prefix wins
# so "gemini-2.5-flash-lite" never resolves to the "gemini-2.5-flash" row.
_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "claude-haiku-4-5": (1.00, 0.10, 5.00),
    "claude-sonnet-4-6": (3.00, 0.30, 15.00),
    "claude-opus-4-8": (5.00, 0.50, 25.00),
    "gemini-2.5-flash-lite": (0.10, 0.025, 0.40),
    "gemini-2.5-flash": (0.30, 0.075, 2.50),
}

_warned_unknown_models: set[str] = set()


def estimate_microusd(model: str, tokens: dict[str, int]) -> int:
    """Estimate cost in micro-USD for one generation's Langfuse-shaped usage.

    ``tokens`` uses the usage-detail names the telemetry layer already emits:
    ``input`` (uncached), ``output``, ``cache_read_input_tokens``, and
    ``cache_creation_input_tokens``. Cache writes are billed near 1.25x input
    by Anthropic and ~1x elsewhere; 1.25x is used uniformly because
    overestimating a write beats undercounting spend.
    """
    normalized = (model or "").strip().casefold()
    rates = None
    best_prefix_len = -1
    for prefix, candidate in _PRICES_PER_MTOK.items():
        if normalized.startswith(prefix) and len(prefix) > best_prefix_len:
            rates = candidate
            best_prefix_len = len(prefix)
    if rates is None:
        if normalized and normalized not in _warned_unknown_models:
            _warned_unknown_models.add(normalized)
            logger.warn(
                "llm_pricing: unknown model, cost estimated at 0",
                {"model": normalized},
            )
        return 0
    input_rate, cached_rate, output_rate = rates
    usd = (
        int(tokens.get("input", 0) or 0) * input_rate
        + int(tokens.get("cache_read_input_tokens", 0) or 0) * cached_rate
        + int(tokens.get("cache_creation_input_tokens", 0) or 0) * input_rate * 1.25
        + int(tokens.get("output", 0) or 0) * output_rate
    ) / 1_000_000
    return round(usd * 1_000_000)
