"""Entitlement and the weighted research credit. The gate in front of paid work.

Two real bugs in the existing entitlement module shape this file, both verified in
source, and both are the reason this is not a two-line call:

1. ``resolve_effective_tier({})`` returns ``"pro"`` (`entitlement.py:107`). That
   permissiveness is deliberate there, to survive the sub-second race before
   ``ensure_entitlement_doc`` has written a new user's document. Gating expensive
   research on it alone would hand free runs to any uid whose document does not exist
   yet, so ``ensure_entitlement_doc`` is awaited FIRST and ``{}`` is never the input.

2. ``has_active_paid_subscription`` and ``resolve_effective_tier`` DISAGREE about trial
   users. The former requires a tier in PAID_TIERS and returns False for
   ``status="trialing"``; the latter returns ``"pro"`` for that same document. They are
   not interchangeable. Research uses the resolved tier, because reverse-trial users are
   intended to get the feature, so calling the boolean helper here would silently
   exclude exactly the cohort the trial exists to convert.

Weighted credits rather than separate quick and deep counters: two independent daily
counters let a user exhaust both rows and exceed the intended total spend. One weighted
allowance cannot be gamed that way.

Failure direction is fail-closed everywhere. An entitlement outage returns 503 and no
run. Research is expensive enough that handing it out during an outage is worse than
refusing it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...lib.logger import logger
from ..entitlement import (
    EntitlementUnavailableError,
    ensure_entitlement_doc,
    resolve_effective_tier,
)
from . import fields as F
from .budget import MAX_ACTIVE_RUNS_PER_USER, Preset, credit_cost
from .store import AdmissionResult, admit_run, count_active_runs

# ---------------------------------------------------------------------------------
# PHASE 0 PENDING. These numbers are NOT approved and are NOT derived from
# subscription economics. The architecture document is explicit that per-tier daily
# credits must come out of the 30-query benchmark plus a product decision on gross
# margin, and that the old per-run dollar guesses are not approval-grade.
#
# They exist so admission is exercisable end to end now. Replace before any pilot.
# ---------------------------------------------------------------------------------
RESEARCH_DAILY_CREDITS: dict[str, int] = {
    "pro": 10,
    "companion": 5,
    "starter": 3,
    # Not a paid tier. Present so an unexpected tier string can never read as generous.
    "free": 0,
}

# Any tier we do not recognise gets nothing rather than the first row's allowance.
UNKNOWN_TIER_CREDITS = 0


@dataclass(frozen=True)
class EntitlementDecision:
    """The resolved right to start one run, before any transaction runs."""

    allowed: bool
    tier: str
    daily_credits: int
    credit_weight: int
    # A machine code the desktop already knows how to parse, plus the HTTP status the
    # eventual route should use. Kept here so the route stays a thin translation.
    code: str = ""
    http_status: int = 200


async def resolve_entitlement(uid: str, preset: Preset) -> EntitlementDecision:
    """Resolve tier, allowance and weight. Never fails open.

    Deliberately a separate step from the admission transaction: this does network reads,
    and a network read inside a Firestore transaction would hold the transaction open
    across an unbounded wait.
    """
    weight = credit_cost(preset)
    try:
        doc = await ensure_entitlement_doc(uid)
    except EntitlementUnavailableError:
        # 503, never fail-open. An outage must not hand out metered research.
        logger.warn("research.credits: entitlement unavailable, refusing", {"uid_hashed": True})
        return EntitlementDecision(
            allowed=False,
            tier="",
            daily_credits=0,
            credit_weight=weight,
            code="entitlement_unavailable",
            http_status=503,
        )

    tier = resolve_effective_tier(doc or {})
    allowance = RESEARCH_DAILY_CREDITS.get(tier, UNKNOWN_TIER_CREDITS)
    if allowance <= 0:
        return EntitlementDecision(
            allowed=False,
            tier=tier,
            daily_credits=allowance,
            credit_weight=weight,
            code=F.RESEARCH_PAID_CODE,
            http_status=402,
        )
    return EntitlementDecision(
        allowed=True, tier=tier, daily_credits=allowance, credit_weight=weight
    )


async def admit(
    uid: str,
    run_id: str,
    *,
    plan_version: int,
    preset: Preset,
    correlation_id: str = "",
) -> AdmissionResult:
    """Resolve entitlement, bound concurrency, then take the one credit-debiting txn.

    Order matters. Entitlement and the active-run bound are checked before the
    transaction so a refusal costs one read rather than a contended write. The credit
    itself is debited only inside ``store.admit_run``, keyed on the deterministic run id,
    which is the single place a research credit can ever be taken.
    """
    decision = await resolve_entitlement(uid, preset)
    if not decision.allowed:
        return AdmissionResult(
            admitted=False, run_id=run_id, state="", code=decision.code
        )

    # One active run per user. The simplest possible concurrency bound, and the reason
    # the per-run ceilings also function as per-user ceilings this phase.
    try:
        active = await count_active_runs(uid)
    except Exception as exc:
        logger.error(
            "research.credits: active-run count failed, failing closed",
            {"run_id": run_id, "error": str(exc)},
        )
        return AdmissionResult(
            admitted=False, run_id=run_id, state="", code=F.FAIL_METER_UNAVAILABLE
        )
    if active > MAX_ACTIVE_RUNS_PER_USER:
        return AdmissionResult(
            admitted=False, run_id=run_id, state="", code="research_run_in_progress"
        )

    return await admit_run(
        uid,
        run_id,
        plan_version=plan_version,
        daily_credit_allowance=decision.daily_credits,
        credit_weight=decision.credit_weight,
        preset=str(preset),
        correlation_id=correlation_id,
    )


async def entitlement_still_valid(uid: str) -> bool:
    """Mid-run re-check, called by admit_stage before each billable external call.

    A lapse mid-run stops spending rather than finishing a run the user no longer pays
    for. An outage returns False, matching the fail-closed rule: the run degrades to a
    partial brief instead of continuing to spend blind.
    """
    try:
        doc = await ensure_entitlement_doc(uid)
    except EntitlementUnavailableError:
        return False
    except Exception:
        return False
    tier = resolve_effective_tier(doc or {})
    return RESEARCH_DAILY_CREDITS.get(tier, UNKNOWN_TIER_CREDITS) > 0
