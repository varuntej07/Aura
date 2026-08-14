"""Typed run budgets. Data, not configuration.

Every number here is a SAFETY CEILING, not an expected usage figure and not a cost
estimate. None of them has been benchmarked: the architecture document is explicit that
token, extraction, latency and Firestore costs stay hypotheses until a real corpus of
quick runs has been measured. Treat a preset as the point past which a run is refused,
never as a prediction of what a run will consume.

These live as a typed model rather than as environment settings on purpose. A ceiling
that can be edited per-deploy is a ceiling nobody can reason about, and the backend
forbids feature flags, so there is no `*_ENABLED` escape hatch here either. The two
values that genuinely belong in settings, because they are spend controls an operator
must be able to move without a deploy, are the project-day cost cap and the provider
credentials. Everything in this module is a product decision.

Why so many separate dimensions rather than one dollar figure: they fail differently.
`extracts_max` bounds documents, but a basic Firecrawl scrape is one credit per page
while a PDF costs one credit PER PDF PAGE, so 15 documents can be 15 credits or 300.
`page_credits_max` is the ceiling that actually holds when a wave lands on filings.
Likewise `model_calls_max` bounds attempts while the token maxima bound what those
attempts may read, and a single enormous page would breach the second long before the
first.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Preset(StrEnum):
    """Run depth. DEEP exists as data and is not offered by any public surface.

    Keeping it in the model rather than deleting it means the engine looks up a preset
    it already understands when deep is eventually approved. Enabling it is then a
    product and API change with its own release, not a configuration flip, which is the
    entire reason it is not expressed as a flag.
    """

    QUICK = "quick"
    DEEP = "deep"


# The only preset a request may name in phase one. A request carrying anything else is
# refused with FAIL_DEPTH_NOT_AVAILABLE rather than silently downgraded, so a user who
# asked for deep work is never quietly given shallow work and charged for it.
PUBLIC_PRESETS: tuple[Preset, ...] = (Preset.QUICK,)


class RunBudget(BaseModel):
    """Hard ceilings for one run. Every field is checked before a billable call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Discovery and acquisition.
    searches_max: int = Field(gt=0)
    extracts_max: int = Field(gt=0)
    # Provider credits, which is NOT the same as documents: PDFs bill per PDF page.
    page_credits_max: int = Field(gt=0)
    bytes_max: int = Field(gt=0)

    # Model work. Attempts and tokens are bounded separately because one oversized
    # document breaches the token ceiling without touching the attempt ceiling.
    model_calls_max: int = Field(gt=0)
    model_input_tokens_max: int = Field(gt=0)
    model_output_tokens_max: int = Field(gt=0)

    # Time. wall_clock_s is the whole run; per_stage_s bounds one stage body and must
    # stay far under both the Cloud Tasks dispatch deadline and Cloud Run's own limit,
    # so a stage that hangs is killed by us rather than by infrastructure.
    wall_clock_s: int = Field(gt=0)
    per_stage_s: int = Field(gt=0)

    # Shape of the search/read loop.
    read_waves_max: int = Field(gt=0)
    fanout_max: int = Field(gt=0)
    results_per_search: int = Field(gt=0, le=20)  # Brave's own per-request ceiling

    # The only ceiling denominated in money. Every other field bounds a COUNT, and counts
    # are not interchangeable: one expert-tier synthesis and one Brave query are both "a
    # unit" and differ by four orders of magnitude in price. A run could therefore stay
    # inside every count ceiling and still cost far more than intended, which is why the
    # project-day cap was the only dollar boundary that existed and it was shared across
    # every run in the project.
    #
    # PROVISIONAL AND NOT RELEASEABLE. See the note on QUICK below.
    cost_microusd_max: int = Field(gt=0)


# --- provisional, NOT RELEASEABLE ------------------------------------------------------
#
# These two figures are DERIVED, not chosen. Each is the arithmetic worst case the preset's
# own count ceilings already permit, priced at the list rates in ``usage.MODEL_RATES`` and
# the Brave and Firecrawl unit prices in ``metering``:
#
#   quick: 24 model attempts x 60_000  = 1_440_000
#           6 searches       x  5_000  =    30_000
#          25 page credits   x  1_000  =    25_000   -> 1_495_000, rounded to 1_500_000
#   deep:  80 x 60_000 + 18 x 5_000 + 100 x 1_000    -> 4_990_000, rounded to 5_000_000
#
# Being derived is what makes them safe to ship as an enforcement mechanism and useless as
# an economic bound: a ceiling equal to what the count ceilings already allow refuses
# nothing that the counts would not have refused anyway. Its job today is that the
# mechanism exists, is transactional, and is settled from the same provider receipts as
# the project-day cap, so lowering it later is one number rather than new code.
#
# THE REAL VALUE IS AN OBSERVED p95 FROM THE 30-QUERY ECONOMICS CORPUS, and it is not
# measured yet. No route that can create a run may ship until it is. Do not treat either
# number below as approved product economics, and do not quote them as a per-run cost.
QUICK_COST_MICROUSD_MAX = 1_500_000
DEEP_COST_MICROUSD_MAX = 5_000_000

BUDGET_PRESETS: dict[Preset, RunBudget] = {
    Preset.QUICK: RunBudget(
        searches_max=6,
        extracts_max=15,
        page_credits_max=25,
        bytes_max=25_000_000,
        model_calls_max=24,
        model_input_tokens_max=300_000,
        model_output_tokens_max=30_000,
        wall_clock_s=240,
        per_stage_s=150,
        read_waves_max=1,
        fanout_max=8,
        results_per_search=10,
        cost_microusd_max=QUICK_COST_MICROUSD_MAX,
    ),
    Preset.DEEP: RunBudget(
        searches_max=18,
        extracts_max=60,
        page_credits_max=100,
        bytes_max=100_000_000,
        model_calls_max=80,
        model_input_tokens_max=1_200_000,
        model_output_tokens_max=100_000,
        wall_clock_s=1200,
        per_stage_s=240,
        read_waves_max=3,
        fanout_max=12,
        results_per_search=15,
        cost_microusd_max=DEEP_COST_MICROUSD_MAX,
    ),
}

# Weighted credits, so quick and deep draw on ONE product allowance rather than two
# independent daily counters. Two counters would let a user exhaust both rows and
# exceed the intended total spend, which is the failure the weighting exists to close.
RESEARCH_CREDIT_COST: dict[Preset, int] = {
    Preset.QUICK: 1,
    Preset.DEEP: 5,
}

# One at a time. The simplest possible concurrency bound for phase one, and the one
# that makes the per-run ceilings above also function as per-user ceilings.
MAX_ACTIVE_RUNS_PER_USER = 1

# Retries per external unit. A stage that has already spent a provider credit and
# crashed re-spends at most once, which bounds the deliberate trade against
# commit-before-call (that alternative risks charging for work never done).
MAX_RETRIES_PER_UNIT = 1

# Bounded repair, borrowing the caps already proven in reactive/agent.py rather than
# inventing new ones for the same job.
TOTAL_REPAIR_CAP = 2
LLM_REPLAN_CAP = 1

# At most two clarification rounds, and the second only when the first answer creates a
# NEW material ambiguity. More rounds read as an interrogation and measurably lose the
# user before any work has been done.
MAX_CLARIFICATION_ROUNDS = 2
# A parked run holds no reserved budget and has consumed no credit, so it can afford to
# wait a day for an answer before terminating itself.
CLARIFICATION_TTL_S = 24 * 60 * 60


def budget_for(preset: Preset) -> RunBudget:
    """The preset's ceilings. Falls back to QUICK rather than raising.

    An unknown preset resolving to the SMALLEST budget is the safe direction: the worst
    case is a run that degrades to a partial brief, where raising would mean an
    admitted run dying after its credit was already debited.
    """
    return BUDGET_PRESETS.get(preset, BUDGET_PRESETS[Preset.QUICK])


def credit_cost(preset: Preset) -> int:
    """Weighted credits this preset debits, once, in the admission transaction."""
    return RESEARCH_CREDIT_COST.get(preset, RESEARCH_CREDIT_COST[Preset.QUICK])
