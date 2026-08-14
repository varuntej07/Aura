"""Reassemble a finished wave and decide what happens next. Pure coordination.

No provider call and no spend, which is why the engine grants this stage no units. Its
only job is to look at what the wave produced and choose between another search wave and
verification.

It is reached by exactly one route: the last child's completion transaction claims the
join and creates this job in the same commit. A crash between that commit and dispatch
leaves a durable outbox row for the sweeper, so the join cannot be lost, and the
``join_claimed`` flag plus the deterministic job id mean it cannot fire twice.
"""

from __future__ import annotations

from .. import fields as F
from ..policy_table import NON_CORROBORATING_CLASSES, SourceClass
from .base import NextJob, StageContext, StageResult, StageResultKind


async def run(ctx: StageContext) -> StageResult:
    # Imported inside the function, not at module scope: store imports stages.base,
    # so a module-level import here would close a cycle (store -> stages -> registry
    # -> this module -> store) and leave store half-initialised.
    from .. import store

    sources = await store.list_documents(ctx.uid, ctx.run_id, F.SOURCES_SUBCOLLECTION)
    plan = ctx.plan or {}

    usable = [row for row in sources if row.get("candidate_count")]
    read_attempted = [row for row in sources if row.get("state") in ("read", "unusable")]
    candidate_total = sum(int(row.get("candidate_count") or 0) for row in usable)

    # Which sub-questions have nothing pointing at them yet. This is the only input to
    # the "search again" decision, so an extra wave is bought for coverage rather than
    # for volume.
    covered: set[str] = set()
    for row in usable:
        try:
            source_class = SourceClass(str(row.get("source_class") or ""))
        except ValueError:
            source_class = SourceClass.UNKNOWN
        if source_class in NON_CORROBORATING_CLASSES:
            continue
        for candidate in row.get("candidate_claims") or []:
            covered.update(
                str(item)
                for item in (candidate.get("eligible_sub_question_ids") or ())
                if str(item)
            )
    must_answer = [
        str(item.get("sub_question_id"))
        for item in (plan.get("sub_questions") or [])
        if item.get("must_answer")
    ]
    uncovered = [sub_id for sub_id in must_answer if sub_id not in covered]

    outputs = {
        "sources_seen": len(sources),
        "sources_read": len(read_attempted),
        "sources_usable": len(usable),
        "candidates": candidate_total,
        "uncovered_must_answer": len(uncovered),
    }

    next_wave = ctx.wave + 1
    # Another wave only when ALL of: a must-answer is still uncovered, the preset allows
    # more waves, and the ledger still has searches. Quick's read_waves_max is 1, so in
    # phase one this branch is unreachable by budget and exists for the deep preset.
    if uncovered and next_wave <= int(ctx.budget.read_waves_max) and not ctx.degraded:
        return StageResult(
            kind=StageResultKind.DONE,
            next_state=F.STATE_SEARCHING,
            next_jobs=(NextJob(stage_kind=F.STAGE_SEARCH_WAVE, wave=next_wave),),
            stage_outputs=dict(outputs, decision="another_wave"),
        )

    return StageResult(
        kind=StageResultKind.DONE,
        next_state=F.STATE_VERIFYING,
        next_jobs=(NextJob(stage_kind=F.STAGE_VERIFY, wave=ctx.wave),),
        stage_outputs=dict(outputs, decision="verify"),
    )
