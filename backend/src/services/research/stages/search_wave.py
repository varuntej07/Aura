"""Discover sources, then fan out one reader per source.

This stage ends by returning FANOUT, which makes the advance transaction write the
coordinator, every child job and every outbox row TOGETHER with the state change. That
single-transaction property is what the whole fan-out safety argument rests on: a crash
cannot leave children with no coordinator to join into, and cannot leave a coordinator
waiting on children that were never created.

Sources are created with a deterministic id derived from the canonical URL, so a URL
already seen in an earlier wave collides on create and is not read twice. Paying
Firecrawl twice for the same page is the exact waste that guard exists to prevent.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ....agents.data_fetchers.brave_search import brave_search
from ....lib.logger import logger
from .. import fields as F
from ..domain_class import BATCH_MAX as DOMAIN_BATCH_MAX
from ..domain_class import (
    apply_primary_source_role,
    classify_domains,
    registrable_domain,
)
from ..metering import (
    INPUT_TOKENS_PER_CALL,
    OUTPUT_TOKENS_PER_CALL,
    StageMeter,
    merge_actuals,
    meter_models,
    provider_cost_microusd,
)
from ..policy_table import NON_CORROBORATING_CLASSES, SourceClass
from ..sanitize import plain_text
from ..url_policy import evaluate_url
from .base import NextJob, StageContext, StageResult, StageResultKind


# Brave recency tokens, chosen from the composed policy's max age. A hard-recency policy
# that accepts nothing older than 90 days should not be handed year-old results and then
# forced to discard them: filtering at the search saves the extract spend entirely.
def _recency_for(max_age_days: int) -> str:
    if max_age_days <= 0:
        return "any"
    if max_age_days <= 1:
        return "fresh"
    if max_age_days <= 7:
        return "past_week"
    if max_age_days <= 31:
        return "past_month"
    if max_age_days <= 366:
        return "past_year"
    return "any"


def _fanout_limits(ctx: StageContext) -> dict[str, int]:
    """How many read children each remaining unit can actually pay for.

    Each entry is ``remaining budget for that unit // what one child reserves of it``,
    so the minimum across the dict is the widest honest wave. Computed from the RUN's
    remaining headroom rather than from this stage's own grant, because the children
    reserve against the run, not against their parent.

    Deliberately integer floor division: a wave of "3.9 children" is a wave of 3, and
    rounding up is how a ceiling stops being one.
    """
    from ..engine import STAGE_UNIT_REQUESTS

    per_child = STAGE_UNIT_REQUESTS.get(F.STAGE_READ_SOURCE, {})
    budget = ctx.budget
    limits: dict[str, int] = {}
    for unit, per in per_child.items():
        per_n = int(per)
        if per_n <= 0:
            continue
        attr = F.UNIT_BUDGET_ATTR.get(unit)
        if not attr:
            continue
        # The parent's own grant is not the constraint; the run's ceiling is. Using the
        # run ceiling here over-estimates slightly when earlier stages have already spent,
        # and the ledger's own partial grant is the backstop that corrects it. What it
        # cannot do any more is IGNORE a unit entirely, which is what produced a wave
        # reserving 64 page credits against a ceiling of 25.
        limits[unit] = max(0, int(getattr(budget, attr, 0)) // per_n)
    return limits


def source_id_for(canonical_url: str) -> str:
    """Deterministic per-URL id. The dedupe key across waves and across retries."""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]


def _rank(source_class: SourceClass, policy: dict[str, Any]) -> int:
    """Read order. Lower sorts first.

    ``discouraged`` never BANS a source, it deprioritises it and disqualifies it from
    satisfying corroboration. Only aggregators and quarantined pages are excluded from
    counting outright. Banning outright would quietly turn a thin-evidence topic into an
    empty brief, when a clearly-labelled weak source plus a named gap is more useful.
    """
    preferred = set(policy.get("preferred_source_classes") or ())
    discouraged = set(policy.get("discouraged_source_classes") or ())
    if source_class.value in preferred:
        return 0
    if source_class in NON_CORROBORATING_CLASSES:
        return 3
    if source_class.value in discouraged:
        return 2
    return 1


async def run(ctx: StageContext) -> StageResult:
    plan = ctx.plan or {}
    policy = dict(plan.get("effective_policy") or {})
    recency_rule = dict(policy.get("recency") or {})
    queries = [q for q in (plan.get("seed_queries") or []) if str(q).strip()]
    if not queries:
        queries = [str(plan.get("objective", "")).strip()]
    queries = [q for q in queries if q]

    # Never more searches than were actually granted. A degraded grant runs a smaller
    # wave and reports the shortfall rather than quietly overspending. Compared
    # numerically rather than by truthiness: `if granted` and `granted > 0` differ only at
    # zero, and zero is the one value that has to refuse.
    allowed_searches = max(0, min(int(ctx.grant.get(F.UNIT_SEARCHES) or 0), len(queries)))
    if allowed_searches <= 0:
        return StageResult(
            kind=StageResultKind.DONE,
            next_state=F.STATE_VERIFYING,
            stage_outputs={"skipped": True, "reason": F.FAIL_BUDGET_EXHAUSTED},
        )

    recency = _recency_for(int(recency_rule.get("max_age_days", 0) or 0))
    per_search = int(ctx.budget.results_per_search)

    seen: dict[str, dict[str, Any]] = {}
    rejected = 0
    searches_run = 0
    for query in queries[:allowed_searches]:
        # Cancellation is a WRITE, not an interrupt, so a stage checks it between
        # external calls. Without this a cancelled run keeps buying searches until its
        # 150s bound expires.
        if ctx.is_cancelled is not None and await ctx.is_cancelled():
            break
        try:
            result = await brave_search(
                query,
                uid=ctx.uid,
                recency=recency,
                count=per_search,
                feature="research_search_wave",
            )
        except Exception as exc:
            # Brave already degrades network faults to an empty result; this catches the
            # misconfiguration case. One failed query must not lose the whole wave.
            logger.warn(
                "research.search_wave: search failed",
                {"run_id": ctx.run_id, "error": str(exc),
                 "error_code": "research_search_failed"},
            )
            continue
        searches_run += 1
        ctx.record_spend(
            {F.UNIT_SEARCHES: 1},
            provider_cost_microusd({F.UNIT_SEARCHES: 1}),
        )
        for item in result.get("sources") or []:
            raw_url = str(item.get("url") or "").strip()
            if not raw_url:
                continue
            verdict = await evaluate_url(raw_url)
            if not verdict.allowed:
                # A forbidden URL is rejected BEFORE any provider call, so a private or
                # credential-bearing target never reaches Firecrawl at all.
                rejected += 1
                continue
            canonical = verdict.canonical_url
            if canonical in seen:
                continue
            seen[canonical] = {
                "url": canonical,
                "title": plain_text(str(item.get("title") or ""), max_chars=300),
                "snippet": plain_text(str(item.get("description") or item.get("text") or ""),
                                      max_chars=400),
                "domain": registrable_domain(canonical),
                "query": query,
            }

    if not seen:
        # No usable source is not a failure. Verification and synthesis still run and
        # produce a partial brief naming what could not be found.
        return StageResult(
            kind=StageResultKind.DONE,
            next_state=F.STATE_VERIFYING,
            run_updates={F.FAILURE_CODE: F.FAIL_NO_SOURCE_FOUND},
            stage_outputs={"searches": searches_run, "rejected": rejected, "found": 0},
            actuals={F.UNIT_SEARCHES: searches_run, F.UNIT_MODEL_CALLS: 0},
            # Finding nothing still cost the searches that found nothing.
            cost_microusd=provider_cost_microusd({F.UNIT_SEARCHES: searches_run}),
        )

    # One batched classification for every new domain, cached globally afterwards. Input
    # is domain, title and snippet only: page bodies have not been fetched yet, and would
    # not be used for this even if they had been.
    #
    # Metered around classify_domains rather than around a provider call, because the
    # model call lives inside it and a wide wave batches into SEVERAL calls. Counting a
    # flat 1 per stage under-reported exactly the case that costs most.
    meter = StageMeter()
    # Between the searches and the classification, for the same reason the loop above
    # checks between searches: classify_domains is a model call, and a run cancelled
    # during the last search should not pay for it.
    if ctx.is_cancelled is not None and await ctx.is_cancelled():
        return StageResult(
            kind=StageResultKind.DONE,
            stage_outputs={"cancelled": True, "searches": searches_run,
                           "found": len(seen)},
            actuals={F.UNIT_SEARCHES: searches_run},
            cost_microusd=provider_cost_microusd({F.UNIT_SEARCHES: searches_run}),
        )
    # logical_calls tracks the BATCHES classify_domains will make, so a wide wave is not
    # starved by a single-call attempt budget. One batch per BATCH_MAX domains.
    domain_rows = [
        {"domain": row["domain"], "title": row["title"], "snippet": row["snippet"]}
        for row in seen.values()
        if row["domain"]
    ]
    batches = max(1, -(-len(domain_rows) // DOMAIN_BATCH_MAX))
    async with meter_models(
        meter,
        run_id=ctx.run_id,
        stage_kind=ctx.stage_kind,
        logical_calls=batches,
        ctx=ctx,
        reserved_input_tokens_per_attempt=INPUT_TOKENS_PER_CALL,
        reserved_output_tokens_per_attempt=OUTPUT_TOKENS_PER_CALL,
    ):
        classes = await classify_domains([
            {"domain": row["domain"], "title": row["title"], "snippet": row["snippet"]}
            for row in seen.values()
            if row["domain"]
        ], max_output_tokens=OUTPUT_TOKENS_PER_CALL)
    # Run-relative, applied AFTER the shared cache is read and never written back to it.
    # The cache stores what a publisher is; this decides what it is to this question.
    classes = apply_primary_source_role(classes, list(plan.get("entities") or []))

    ordered = sorted(
        seen.values(),
        key=lambda row: _rank(classes.get(row["domain"], SourceClass.UNKNOWN), policy),
    )
    # How wide this wave may actually go, computed against EVERY unit a child will
    # reserve, not just extracts.
    #
    # Bounding on extracts alone was the bug that made quick's page-credit ceiling
    # unenforceable. Eight children each reserving READ_PAGE_CREDIT_RESERVE = 8 credits
    # collectively reserve 64 against a page_credits_max of 25, so the ledger handed the
    # first three their full grant and the rest a short one, and a "degraded" wave was the
    # NORMAL outcome of a healthy run. The binding constraint has to be computed before
    # the children are created, not discovered afterwards by five of them running small.
    limits = _fanout_limits(ctx)
    fanout = max(0, min(len(ordered), int(ctx.budget.fanout_max), *limits.values()))
    # Printed, because a silently truncated wave reads exactly like a wave that found
    # fewer sources. Naming the binding unit is the difference between "the web was thin"
    # and "the budget was".
    binding = min(limits, key=lambda unit: limits[unit]) if limits else ""
    logger.info(
        "research.search_wave: fan-out computed",
        {
            "run_id": ctx.run_id,
            "candidates": len(ordered),
            "fanout_max": int(ctx.budget.fanout_max),
            "max_by_unit": limits,
            "binding_unit": binding,
            "chosen": fanout,
        },
    )
    chosen = ordered[:fanout]
    if not chosen:
        return StageResult(
            kind=StageResultKind.DONE,
            next_state=F.STATE_VERIFYING,
            run_updates={F.FAILURE_CODE: F.FAIL_BUDGET_EXHAUSTED},
            stage_outputs={"searches": searches_run, "found": len(seen), "chosen": 0},
            actuals=merge_actuals(
                {F.UNIT_SEARCHES: searches_run}, meter.as_actuals()
            ),
            cost_microusd=meter.cost_microusd
            + provider_cost_microusd({F.UNIT_SEARCHES: searches_run}),
            cost_known=not meter.cost_incomplete,
        )

    child_wave = ctx.wave + 1
    documents: dict[str, dict[str, dict[str, Any]]] = {F.SOURCES_SUBCOLLECTION: {}}
    jobs: list[NextJob] = []
    for row in chosen:
        sid = source_id_for(row["url"])
        source_class = classes.get(row["domain"], SourceClass.UNKNOWN)
        documents[F.SOURCES_SUBCOLLECTION][sid] = {
            "source_id": sid,
            "url": row["url"],
            "domain": row["domain"],
            "title": row["title"],
            "snippet": row["snippet"],
            "source_class": source_class.value,
            "discovered_by_query": row["query"],
            "wave": child_wave,
            "state": "pending",
        }
        jobs.append(
            NextJob(
                stage_kind=F.STAGE_READ_SOURCE,
                wave=child_wave,
                # The source id IS the ordinal, which is what makes each child's stage_id
                # unique without a shared counter to contend on.
                ordinal=sid,
                payload={"source_id": sid, "url": row["url"], "title": row["title"]},
            )
        )

    return StageResult(
        kind=StageResultKind.FANOUT,
        next_state=F.STATE_READING,
        next_jobs=tuple(jobs),
        expected_children=len(jobs),
        documents=documents,
        stage_outputs={
            "searches": searches_run,
            "found": len(seen),
            "rejected": rejected,
            "chosen": len(jobs),
            "recency": recency,
        },
        actuals=merge_actuals({F.UNIT_SEARCHES: searches_run}, meter.as_actuals()),
        cost_microusd=meter.cost_microusd
        + provider_cost_microusd({F.UNIT_SEARCHES: searches_run}),
        cost_known=not meter.cost_incomplete,
    )
