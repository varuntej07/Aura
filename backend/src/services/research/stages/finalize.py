"""Commit the terminal result and its notification intent in ONE transaction.

This exists as a separate stage for one reason. If the terminal state committed first and
the notification job were created afterwards, a crash in between would leave a durable,
correct result that the user is never told about, and terminal states refuse later
research work so nothing would ever retry it. The brief would sit there, finished and
invisible.

Returning TERMINAL with a ``notify_result`` job attached makes the advance transaction
write the state, the job document and the outbox row together. A crash after that commit
is recovered by the outbox sweeper, which redelivers exactly once.

``notify_result`` is one of the two kinds allowed to run AFTER a run is result-terminal
(``registry.POST_TERMINAL_KINDS``). It carries its own idempotent receipt and cannot
reopen research work.
"""

from __future__ import annotations

from .. import fields as F
from .base import NextJob, StageContext, StageResult, StageResultKind


async def run(ctx: StageContext) -> StageResult:
    from .. import store

    # Synthesis decided this; finalize does not re-derive it. One owner for the decision
    # means the state in the brief and the state on the run cannot disagree.
    terminal_state = str(ctx.payload.get("terminal_state") or F.STATE_PARTIAL)
    if terminal_state not in (F.STATE_READY, F.STATE_PARTIAL):
        terminal_state = F.STATE_PARTIAL

    # The last line before READY becomes durable: a run holding no cited claim can never
    # be ready, whatever the payload says.
    #
    # Synthesis already refuses to report complete without at least one surviving
    # evidence-backed statement, and this is the same rule enforced by the stage that
    # actually commits the state. It is worth duplicating because READY is absorbing:
    # once it commits, nothing reopens the run, so a wrong READY is permanent and a
    # zero-evidence one contradicts the single promise the artifact makes.
    downgraded = False
    run_doc: dict | None = None
    if terminal_state == F.STATE_READY:
        run_doc = await store.get_run(ctx.uid, ctx.run_id) or {}
        brief = dict(run_doc.get(F.BRIEF) or {})
        if not (brief.get("cited_claim_ids") or []):
            terminal_state = F.STATE_PARTIAL
            downgraded = True

    # A run bound to a Notion destination delivers FIRST and notifies from the
    # deliver stage's receipt, so the notification (and the spoken line built
    # from the same fields) can truthfully say "saved" or "saving failed"
    # instead of racing the write. Runs without a delivery keep today's path.
    if run_doc is None:
        run_doc = await store.get_run(ctx.uid, ctx.run_id) or {}
    successor_kind = (
        F.STAGE_NOTION_DELIVER
        if run_doc.get(F.DELIVERY)
        else F.STAGE_NOTIFY_RESULT
    )

    return StageResult(
        kind=StageResultKind.TERMINAL,
        next_state=terminal_state,
        next_jobs=(
            NextJob(
                stage_kind=successor_kind,
                wave=ctx.wave,
                payload={"terminal_state": terminal_state},
            ),
        ),
        run_updates=(
            {F.FAILURE_CODE: F.FAIL_NO_SOURCE_FOUND} if downgraded else {}
        ),
        stage_outputs={
            "terminal_state": terminal_state,
            "downgraded_no_evidence": downgraded,
        },
    )
