"""The stage registry. Every stage kind resolves here, or the engine refuses to run it.

Phase two ships stubs. Every entry below is a pure `async def run(ctx) -> StageResult`
that performs no I/O, calls no provider, and returns `NOT_IMPLEMENTED`. The engine can
therefore be driven end to end, and every durability property (lease, replay, steal,
cancel, join, budget, deletion) can be proven, before a single provider call exists.

Why stubs return NOT_IMPLEMENTED rather than a plausible fake transition: a stub that
invented `next_state=searching` would make the run LOOK like it worked, and a phase-four
regression could then hide behind a green phase-two inspection. An inert stage that
stops the run is honest, and the engine records it as such.

Replacing one is a two-line change: write `stages/search_wave.py` with the same
signature and swap its entry here. Nothing else in the engine moves, which is the point
of the seam.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .. import fields as F
from . import (
    classify_plan,
    delete_run,
    finalize,
    notify_result,
    read_join,
    read_source,
    search_wave,
    synthesize,
    verify,
)
from .base import StageContext, StageResult, StageResultKind

StageFn = Callable[[StageContext], Awaitable[StageResult]]


class StageNotRegisteredError(LookupError):
    """An unknown stage kind reached the engine.

    Raised rather than defaulted. A stage kind is written by our own advance
    transaction, so an unknown one means the job doc and the code disagree, and
    silently picking a default would run the wrong work against a real budget.
    """

    def __init__(self, stage_kind: str) -> None:
        super().__init__(f"no stage registered for kind {stage_kind!r}")
        self.stage_kind = stage_kind


def _stub(stage_kind: str) -> StageFn:
    """Build an inert body for one stage kind.

    Consumes nothing, so `actuals` stays empty and the advance transaction releases the
    entire grant back to the ledger. A stubbed run therefore reconciles to zero spend,
    which is what makes budget inspection meaningful this phase.
    """

    async def run(ctx: StageContext) -> StageResult:
        return StageResult(
            kind=StageResultKind.NOT_IMPLEMENTED,
            stage_outputs={
                "stub": True,
                "stage_kind": stage_kind,
                "attempt": ctx.attempt,
                "degraded": ctx.degraded,
            },
        )

    run.__name__ = f"stub_{stage_kind}"
    run.__doc__ = f"Phase-two stub for {stage_kind}. No I/O, no provider, no spend."
    return run


# Every kind the advance transaction can name. Keeping the table exhaustive is what
# lets StageNotRegisteredError mean "code drift", not "not built yet".
REGISTRY: dict[str, StageFn] = {
    F.STAGE_CLASSIFY_PLAN: classify_plan.run,
    F.STAGE_SEARCH_WAVE: search_wave.run,
    F.STAGE_READ_SOURCE: read_source.run,
    F.STAGE_READ_JOIN: read_join.run,
    F.STAGE_VERIFY: verify.run,
    F.STAGE_SYNTHESIZE: synthesize.run,
    F.STAGE_FINALIZE: finalize.run,
    # Delivery kinds. These may legitimately act AFTER a run is result-terminal, under
    # their own idempotent receipts, and can never reopen research work.
    F.STAGE_NOTIFY_RESULT: notify_result.run,
    F.STAGE_DELETE_RUN: delete_run.run,
}

# Kinds that MAY run once the run has reached a terminal result state. Membership here is
# necessary and not sufficient: what each one is actually allowed to do is decided by
# ``may_run_post_terminal`` below, per kind.
POST_TERMINAL_KINDS = (F.STAGE_NOTIFY_RESULT, F.STAGE_DELETE_RUN)

# Result states a notification has copy for. A cancelled run is deliberately absent: the
# user performed the cancellation, so telling them about it is noise, not news.
_NOTIFIABLE_STATES = (
    F.STATE_READY,
    F.STATE_PARTIAL,
    F.STATE_FAILED,
    F.STATE_AWAITING_CLARIFICATION,
)


def may_run_post_terminal(
    stage_kind: str, run: dict[str, object], *, deletion_active: bool = False
) -> bool:
    """Whether this delivery kind may act on this run right now.

    One blanket exemption for both kinds was wrong in both directions, and the two failures
    are unrelated to each other:

      * ``notify_result`` was exempt from the whole block check, so it would still send on
        a run the user had cancelled, and on a run whose deletion had already begun. A
        toast about research that no longer exists is worse than no toast.
      * ``delete_run`` was exempt on any terminal run, including one with no deletion
        receipt at all, so a stray or replayed delete job could drain a run nobody asked
        to delete.

    Splitting them is what lets each carry the condition that actually makes it safe: a
    notification needs a result worth reporting, and a deletion needs a live receipt
    authorising it.
    """
    if stage_kind == F.STAGE_NOTIFY_RESULT:
        if run.get(F.CANCEL_REQUESTED_AT):
            return False
        if str(run.get(F.DELETION_STATE) or "") or deletion_active:
            return False
        return str(run.get(F.STATE) or "") in _NOTIFIABLE_STATES
    if stage_kind == F.STAGE_DELETE_RUN:
        # An active receipt is the ONLY authority to delete. Terminality is not one.
        return bool(deletion_active)
    return False


def get_stage(stage_kind: str) -> StageFn:
    """Resolve one stage body, or raise. Never falls back to a default."""
    try:
        return REGISTRY[stage_kind]
    except KeyError:
        raise StageNotRegisteredError(stage_kind) from None


def is_stub(stage_kind: str) -> bool:
    """True while this kind is still inert. Phase four flips these one at a time."""
    fn = REGISTRY.get(stage_kind)
    return bool(fn) and getattr(fn, "__name__", "").startswith("stub_")
