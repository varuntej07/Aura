"""Turn provider calls into ledger units. The bridge between spending and recording.

Every research stage reserves units before it may touch a provider, and commits what it
actually used in the same transaction that advances the run. Until this module existed
the second half was fiction: stages reported ``model_calls`` as a COUNT of attempts and
nothing else, so ``model_input_tokens``, ``model_output_tokens`` and ``cost_microusd``
were reserved against, budgeted for, and never once written.

That mattered more than an accounting gap. ``budget.py`` bounds attempts and tokens
separately, on purpose, because "one enormous document breaches the token ceiling without
touching the attempt ceiling". With the token meters always reading zero, the token
ceiling could not fire at all, and the only live bound was the attempt count.

The pieces this composes were already here, unused:

  * ``model_provider.capture_usage`` collects one raw usage object per API ATTEMPT,
    including retries and cross-provider fallback hops.
  * ``usage.normalize_provider_usage`` converts one of those into typed token counts plus
    a list-price cost, and reports ``cost_known=False`` rather than guessing.

Absence of usage is treated as a MEASUREMENT FAILURE, not a free call: every provider in
use reports usage on success, so a missing usage object means we could not see the spend,
not that there was none.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from ..model_provider import attempt_budget, capture_usage
from . import fields as F
from .usage import estimate_cost_microusd, normalize_provider_usage

# Provider attempts one LOGICAL model call may make, across the per-model retry loop and
# every cross-provider fallback hop.
#
# This exists because a reservation has to be made BEFORE the first attempt, and the
# provider's default envelope is not reservable. `_MAX_RETRIES = 3` across an expert-tier
# chain of three models is nine attempts; reserving nine times a read's prompt against
# quick's 300k input ceiling would shrink every wave to nothing, and reserving one attempt
# while up to nine can happen is a ceiling that does not bound what it meters.
#
# Two is not a guess. `budget.MAX_RETRIES_PER_UNIT` is already 1 for exactly this reason:
# a research stage that has spent provider budget and failed re-spends at most once. One
# primary attempt plus one retry-or-fallback is that same rule expressed at the provider
# boundary, and the stage's own attempt cap is the outer retry beyond it.
RESEARCH_ATTEMPT_BUDGET = 2

# Page credits one read may reserve. A deliberate over-reservation: the grant comes back
# short when the ledger cannot cover it, and a short grant makes the stage run smaller
# and report the shortfall, which is a far better failure than silently exceeding the
# ceiling. Kept well under quick's page_credits_max so a single read cannot consume the
# whole run's credit budget on its own.
#
# Lives here rather than in engine.py so the reservation and the cost estimate derived
# from it cannot drift apart; engine imports it from here.
READ_PAGE_CREDIT_RESERVE = 8

# Per-ATTEMPT worst cases used to reserve the token ceilings BEFORE a call rather than
# count them after it. A ceiling checked only after the call it was meant to bound is a
# counter, not a ceiling.
#
# Deliberately NOT a real pre-call token count. Doing that properly means a tokenizer per
# provider and a re-count on every fallback hop, and being wrong there fails in the
# dangerous direction: refusing a call that would have fit. Over-reserving fails in the
# safe one - the grant comes back short, the stage runs smaller, and it says so.
#
# SIZING IS LOAD BEARING, and the constraint is PEAK CONCURRENT reservation, not the sum
# over a run. A fan-out reserves for all its children at once, so with quick's
# fanout_max=8 and model_input_tokens_max=300_000 the read reserve must satisfy
# 8 x READ_INPUT <= 300_000. Set it too high and the ledger silently shrinks every wave
# (8 sources become 5) while looking like a budget working as intended - which is the
# same class of failure as a cap that does not cap, in the opposite direction.
#
# Each figure is roughly 2x the realistic prompt for that stage, at ~4 chars per token.
#
# Small prompts: the user's request, or domain+title+snippet for one wave of results.
INPUT_TOKENS_PER_CALL = 12_000
OUTPUT_TOKENS_PER_CALL = 4_000
# One fetched page, bounded by max_chars (120k chars is ~30k tokens). 8 x 35k = 280k,
# which fits one full wave inside quick's 300k input ceiling and refuses a second.
READ_INPUT_TOKENS_RESERVE = 35_000
# Extraction returns at most 12 short claims plus a summary; nothing like a full page.
READ_OUTPUT_TOKENS_RESERVE = 2_500
# One adjudication compares a handful of excerpts for a single (subject, attribute).
VERIFY_INPUT_TOKENS_PER_CALL = 8_000
# An Adjudication is a verdict plus three short strings, well under 300 tokens. Sized
# separately from OUTPUT_TOKENS_PER_CALL because verify reserves ADJUDICATION_MAX of them
# at once: at the generic 4k figure, times the attempt envelope, the reservation alone
# exceeded quick's whole 30k output ceiling and every verify would have run degraded.
VERIFY_OUTPUT_TOKENS_PER_CALL = 1_000
# Synthesis receives every claim in the run at once, up to CLAIMS_MAX.
SYNTHESIS_INPUT_TOKENS_RESERVE = 40_000
# Bytes one read may fetch. Also caps the reader's max_chars, so the byte ceiling binds
# BEFORE the fetch instead of being discovered afterwards.
READ_BYTES_RESERVE = 2_000_000


@dataclass
class StageMeter:
    """What one stage spent on models, accumulated across every call it made."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0
    # True when at least one attempt could not be priced (unknown model, or the provider
    # returned no usage object). The recorded cost is then a FLOOR, not the total.
    cost_incomplete: bool = False
    models: list[str] = field(default_factory=list)

    def as_actuals(self) -> dict[str, int]:
        """The units to hand back in ``StageResult.actuals``.

        Cached input tokens are folded into the input count deliberately: they were read
        by the model and they bill, at a discount this repo does not have a documented
        rate for, so counting them at full rate over-estimates in the safe direction.
        """
        return {
            F.UNIT_MODEL_CALLS: self.calls,
            F.UNIT_MODEL_INPUT_TOKENS: self.input_tokens,
            F.UNIT_MODEL_OUTPUT_TOKENS: self.output_tokens,
        }


@asynccontextmanager
async def meter_models(
    meter: StageMeter,
    *,
    run_id: str = "",
    stage_kind: str = "",
    logical_calls: int = 1,
    ctx: Any = None,
    reserved_input_tokens_per_attempt: int = 0,
    reserved_output_tokens_per_attempt: int = 0,
) -> AsyncIterator[StageMeter]:
    """Accumulate every provider attempt made inside this block onto ``meter``.

    Re-entrant by accumulation rather than by nesting: a stage that makes several calls
    wraps each one (or wraps them all at once) and the totals add up either way, which is
    what lets verify meter six adjudications onto one stage result.

    It also CAPS the block at ``logical_calls * RESEARCH_ATTEMPT_BUDGET`` provider
    attempts. Metering and bounding belong together: every research provider call already
    runs inside this context manager, so this is the one place where "what was reserved"
    and "what may actually be attempted" can be guaranteed to be the same number. A block
    that fans out into several batched calls (search_wave's domain classification) passes
    its own ``logical_calls`` rather than starving on a single-call budget.
    """
    budget = max(1, int(logical_calls)) * RESEARCH_ATTEMPT_BUDGET
    with capture_usage() as sink, attempt_budget(budget):
        try:
            yield meter
        finally:
            # `finally`, so an exception inside the block still accounts for the attempts
            # it made on the way out. A stage that raises never returns a StageResult, and
            # tokens spent by an attempt that then failed are spent either way.
            before_calls = meter.calls
            before_input = meter.input_tokens
            before_output = meter.output_tokens
            before_cost = meter.cost_microusd
            block_cost_known = True
            for model_id, raw_usage in sink:
                usage = normalize_provider_usage(model_id, raw_usage)
                meter.calls += 1
                if usage.usage_reported:
                    meter.input_tokens += usage.input_tokens + usage.cached_input_tokens
                    meter.output_tokens += usage.output_tokens
                else:
                    # The provider accepted an attempt but did not return a receipt. It
                    # consumes the pre-call token reservation, never zero tokens.
                    meter.input_tokens += max(0, reserved_input_tokens_per_attempt)
                    meter.output_tokens += max(0, reserved_output_tokens_per_attempt)
                if usage.cost_known and usage.cost_microusd is not None:
                    meter.cost_microusd += int(usage.cost_microusd)
                else:
                    # Includes a FAILED attempt, which the provider now reports with no
                    # usage object at all. An attempt that raised still burned a prompt,
                    # so it is recorded as unknown cost rather than as a free call.
                    meter.cost_incomplete = True
                    block_cost_known = False
                if model_id not in meter.models:
                    meter.models.append(model_id)
            if ctx is not None and meter.calls > before_calls:
                # Mirror this block's spend onto the stage context, so it survives an
                # exception on the way out of the block. Without it, the engine's failure
                # path had nothing to commit and released the whole grant, handing the
                # retry units the first attempt had already burned.
                ctx.record_spend(
                    {
                        F.UNIT_MODEL_CALLS: meter.calls - before_calls,
                        F.UNIT_MODEL_INPUT_TOKENS: meter.input_tokens - before_input,
                        F.UNIT_MODEL_OUTPUT_TOKENS: meter.output_tokens - before_output,
                    },
                    meter.cost_microusd - before_cost,
                    cost_known=block_cost_known,
                )
            if meter.cost_incomplete:
                # Loud, because an unpriced call means the project-day cost cap is being
                # evaluated against a number smaller than reality.
                logger.warn(
                    "research.metering: spend recorded without a known price",
                    {
                        "run_id": run_id,
                        "stage_kind": stage_kind,
                        "models": meter.models,
                        "error_code": "research_cost_unknown",
                    },
                )


def provider_cost_microusd(actuals: dict[str, int]) -> int:
    """What the NON-model providers in this stage's actuals cost, in micro-USD.

    ``StageMeter`` only sees model calls, because it is fed by the model provider's usage
    sink. Brave searches and Firecrawl page credits never reached ``cost_microusd`` at
    all, so the project-day cap was reconciled against model spend alone and the two
    providers whose ceilings ``budget.py`` bothers to track separately contributed
    nothing to it.

    Same list prices as the reservation estimates above, so a stage reserves and settles
    against one set of numbers rather than two that can drift.
    """
    searches = max(0, int(actuals.get(F.UNIT_SEARCHES, 0)))
    page_credits = max(0, int(actuals.get(F.UNIT_PAGE_CREDITS, 0)))
    return searches * _SEARCH_MICROUSD + page_credits * _PAGE_CREDIT_MICROUSD


def merge_actuals(*parts: dict[str, int]) -> dict[str, int]:
    """Sum several unit dicts. Used where a stage meters models and provider units."""
    out: dict[str, int] = {}
    for part in parts:
        for unit, value in part.items():
            out[unit] = out.get(unit, 0) + int(value)
    return out


def _model_envelope(stage_kind: str) -> tuple[str, ...]:
    if stage_kind == F.STAGE_SEARCH_WAVE:
        return tuple(dict.fromkeys((
            settings.TIER_CHEAP,
            settings.TIER_CHEAP_FALLBACK,
            settings.TIER_CHEAP_LAST_RESORT,
        )))
    if stage_kind in (F.STAGE_CLASSIFY_PLAN, F.STAGE_READ_SOURCE, F.STAGE_VERIFY):
        return tuple(dict.fromkeys((
            settings.TIER_BALANCED,
            settings.TIER_BALANCED_FALLBACK,
        )))
    if stage_kind == F.STAGE_SYNTHESIZE:
        return tuple(dict.fromkeys((
            settings.TIER_EXPERT,
            settings.TIER_EXPERT_FALLBACK,
            settings.TIER_CHEAP,
        )))
    return ()


def stage_cost_estimate_microusd(
    stage_kind: str, unit_request: dict[str, int]
) -> int:
    """What one attempt of this stage might plausibly cost, in micro-USD.

    Reserved against the PROJECT-day cap before the stage runs, then reconciled against
    the real figure afterwards. It is a reservation, not a prediction: over-estimating
    briefly withholds project headroom, while under-estimating lets the cap be breached,
    so every number here rounds up.

    These are derived from the per-run ceilings in ``budget.py`` and the list prices in
    ``usage.MODEL_RATES`` rather than from measurement, and they should be replaced with
    observed percentiles once a corpus of real quick runs exists. They are deliberately
    NOT settings: a ceiling an operator can move per-deploy is a ceiling nobody can
    reason about, which is the same argument budget.py already makes for itself.
    """
    input_tokens = max(0, int(unit_request.get(F.UNIT_MODEL_INPUT_TOKENS, 0)))
    output_tokens = max(0, int(unit_request.get(F.UNIT_MODEL_OUTPUT_TOKENS, 0)))
    model_cost = 0
    envelope = _model_envelope(stage_kind)
    if envelope and int(unit_request.get(F.UNIT_MODEL_CALLS, 0)) > 0:
        costs = [
            estimate_cost_microusd(
                model_id, input_tokens=input_tokens, output_tokens=output_tokens
            )
            for model_id in envelope
        ]
        if any(cost is None for cost in costs):
            unknown = [model for model, cost in zip(envelope, costs) if cost is None]
            raise ValueError(f"unpriced research model envelope: {unknown}")
        model_cost = max(int(cost or 0) for cost in costs)
    return (
        model_cost
        + max(0, int(unit_request.get(F.UNIT_SEARCHES, 0))) * _SEARCH_MICROUSD
        + max(0, int(unit_request.get(F.UNIT_PAGE_CREDITS, 0)))
        * _PAGE_CREDIT_MICROUSD
    )
# One Brave search.
_SEARCH_MICROUSD = 5_000  # $0.005
# One Firecrawl page credit. A PDF bills one credit PER PAGE, which is why the read
# estimate multiplies by the budget's page-credit ceiling rather than by 1.
_PAGE_CREDIT_MICROUSD = 1_000  # $0.001
