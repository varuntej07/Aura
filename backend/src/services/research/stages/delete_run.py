"""Drive an explicit deletion to completion. The other post-terminal kind.

Firestore's native TTL does NOT delete subcollections. Expiring only the run document
would leave its ledger, coordinator, stages, plans, sources, claims and audit events
behind as orphans that nothing ever reads and nothing ever removes. Every run-owned
document therefore carries the same ``expires_at``, and an explicit user deletion still
runs this exact-subtree walk rather than trusting TTL, because TTL timing is measured in
days and a user who pressed delete expects it gone.

The work itself lives in ``deletion.drain_deletion``, which is resumable: it records the
collection and cursor it reached, so a stage that dies halfway through 500 claims picks
up where it stopped instead of restarting. This stage is the bounded driver around it.
"""

from __future__ import annotations

from ....lib.logger import logger
from .. import fields as F
from .base import NextJob, StageContext, StageResult, StageResultKind

# Batches per stage invocation. Bounded so one deletion cannot occupy the stage for its
# full 150s budget and starve the queue; an unfinished drain reschedules itself.
DRAIN_BATCHES = 20


async def run(ctx: StageContext) -> StageResult:
    # Imported inside the function, not at module scope: store imports stages.base,
    # so a module-level import here would close a cycle (store -> stages -> registry
    # -> this module -> store) and leave store half-initialised.
    from .. import deletion

    deleted_total = 0
    complete = False
    for _ in range(DRAIN_BATCHES):
        progress = await deletion.drain_deletion(ctx.uid, ctx.run_id)
        deleted_total += int(getattr(progress, "deleted", 0) or 0)
        if getattr(progress, "complete", False):
            complete = True
            break
        # Deletion is the one place a cancelled or terminal run keeps working, so the
        # cancel check here is about not hogging the worker, not about spend.
        if ctx.is_cancelled is not None and await ctx.is_cancelled():
            break

    if complete:
        logger.info(
            "research.delete_run: subtree deleted",
            {"run_id": ctx.run_id, "deleted": deleted_total},
        )
        return StageResult(
            kind=StageResultKind.DONE,
            stage_outputs={"deleted": deleted_total, "complete": True},
        )

    # Not finished. Reschedule at the next wave so the id differs from this one, which
    # keeps the deterministic job id unique and escapes the Cloud Tasks tombstone window.
    return StageResult(
        kind=StageResultKind.DONE,
        next_jobs=(
            NextJob(stage_kind=F.STAGE_DELETE_RUN, wave=ctx.wave + 1),
        ),
        stage_outputs={"deleted": deleted_total, "complete": False},
    )
