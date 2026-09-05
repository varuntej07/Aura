"""The engine seam. The ONLY module anything outside this package may import.

Nothing outside this file, including future handlers, tools and the scheduler, imports
``tasks.py`` or the store's write paths. That rule is what makes the durability
substrate replaceable: a Temporal implementation would stub exactly two methods,
``advance`` and ``sweep``, and those are called from exactly two places, so deleting the
internal step endpoint and the sweeper hook is the complete removal surface.

``advance`` is the whole per-stage lifecycle, in the fixed order the architecture
requires:

    claim  ──►  admit  ──►  run body  ──►  commit
     lease      budget      bounded       one txn:
     or 200     or partial  wait_for      state + budget + next job + outbox

Every step can refuse, and each refusal has a different meaning. Claim refusing means
someone else owns the stage. Admit refusing means the run should degrade to a partial
brief. The body raising means retry. Only the final transaction makes anything durable.

Phase two runs this against stub stage bodies, so the whole lifecycle is exercisable and
provable without a single provider call existing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ...lib.logger import logger
from . import credits as credits_mod
from . import fields as F
from . import ledger as ledger_mod
from . import metering, store
from . import sweep as sweep_mod
from . import tasks as tasks_mod
from .budget import Preset, budget_for
from .stages.base import NextJob, StageContext, StageResult, StageResultKind
from .stages.registry import (
    POST_TERMINAL_KINDS,
    StageNotRegisteredError,
    get_stage,
    may_run_post_terminal,
)
from .stages.verify import ADJUDICATION_MAX

# What each stage kind reserves before it is allowed to touch a provider. A kind absent
# here reserves nothing, which is correct for the pure-coordination stages: the join and
# the finalizer read what earlier stages already paid for.
#
# Token ceilings are reserved here too, not merely recorded afterwards. budget.py bounds
# attempts and tokens SEPARATELY on purpose, because "one enormous document breaches the
# token ceiling without touching the attempt ceiling" - and a ceiling that is only
# checked after the call it was meant to bound is a counter, not a ceiling. These are
# per-attempt worst cases: over-reserve, let the grant come back short, let the stage run
# smaller and report the shortfall.
# Every model figure below is multiplied by metering.RESEARCH_ATTEMPT_BUDGET, because one
# LOGICAL model call is not one provider attempt. The provider retries and falls across
# providers inside a single await, and each of those hops re-sends the whole prompt. A
# reservation of one attempt against an envelope of several is a ceiling that does not
# bound what it meters, which is why metering caps the envelope at the same number it is
# reserved against: what is reserved before the first attempt is what can actually happen.
_ENVELOPE = metering.RESEARCH_ATTEMPT_BUDGET

STAGE_UNIT_REQUESTS: dict[str, dict[str, int]] = {
    F.STAGE_CLASSIFY_PLAN: {
        F.UNIT_MODEL_CALLS: 1 * _ENVELOPE,
        F.UNIT_MODEL_INPUT_TOKENS: metering.INPUT_TOKENS_PER_CALL * _ENVELOPE,
        F.UNIT_MODEL_OUTPUT_TOKENS: metering.OUTPUT_TOKENS_PER_CALL * _ENVELOPE,
    },
    F.STAGE_SEARCH_WAVE: {
        F.UNIT_SEARCHES: 1,
        F.UNIT_MODEL_CALLS: 1 * _ENVELOPE,
        F.UNIT_MODEL_INPUT_TOKENS: metering.INPUT_TOKENS_PER_CALL * _ENVELOPE,
        F.UNIT_MODEL_OUTPUT_TOKENS: metering.OUTPUT_TOKENS_PER_CALL * _ENVELOPE,
    },
    F.STAGE_READ_SOURCE: {
        F.UNIT_EXTRACTS: 1,
        # NOT 1. A basic scrape is one credit per page, but a PDF bills one credit PER
        # PDF PAGE, which is the exact case budget.py's separate page_credits_max ceiling
        # exists for. Reserving 1 meant a wave landing on filings blew through its credit
        # ceiling with every overage recorded as within-grant.
        F.UNIT_PAGE_CREDITS: metering.READ_PAGE_CREDIT_RESERVE,
        F.UNIT_MODEL_CALLS: 1 * _ENVELOPE,
        # A whole page goes into the prompt, so this is the stage that can breach the
        # token ceiling on a single call. The granted bytes also cap how much of the page
        # is fetched at all - see read_source's max_chars.
        F.UNIT_BYTES: metering.READ_BYTES_RESERVE,
        F.UNIT_MODEL_INPUT_TOKENS: metering.READ_INPUT_TOKENS_RESERVE * _ENVELOPE,
        F.UNIT_MODEL_OUTPUT_TOKENS: metering.READ_OUTPUT_TOKENS_RESERVE * _ENVELOPE,
    },
    # Reserves what the stage can actually spend, not what it usually spends. Verify may
    # run up to ADJUDICATION_MAX adjudications, and reserving 2 against a ceiling of 6
    # meant four real model calls fell outside the grant, where _commit_units clamped
    # them out of the ledger entirely. The reservation and the stage's own ceiling are
    # the same number on purpose: if one moves the other has to.
    F.STAGE_VERIFY: {
        F.UNIT_MODEL_CALLS: ADJUDICATION_MAX * _ENVELOPE,
        F.UNIT_MODEL_INPUT_TOKENS: (
            metering.VERIFY_INPUT_TOKENS_PER_CALL * ADJUDICATION_MAX * _ENVELOPE
        ),
        F.UNIT_MODEL_OUTPUT_TOKENS: (
            metering.VERIFY_OUTPUT_TOKENS_PER_CALL * ADJUDICATION_MAX * _ENVELOPE
        ),
    },
    F.STAGE_SYNTHESIZE: {
        F.UNIT_MODEL_CALLS: 1 * _ENVELOPE,
        # Synthesis is handed every claim in the run, so its prompt is the largest of
        # any single-call stage.
        F.UNIT_MODEL_INPUT_TOKENS: metering.SYNTHESIS_INPUT_TOKENS_RESERVE * _ENVELOPE,
        F.UNIT_MODEL_OUTPUT_TOKENS: metering.OUTPUT_TOKENS_PER_CALL * _ENVELOPE,
    },
}

# The units a stage CANNOT run without. A zero grant on any of these is a refusal, not a
# smaller run.
#
# ``Grant.empty`` only asks whether ANY unit is nonzero, so a read that was granted its
# extract slot and zero page credits, zero bytes and zero model calls still reached
# Firecrawl and then the extraction model. Degrading is right when a stage can do less;
# it is wrong when the stage cannot do the thing it exists to do without spending a unit
# the ledger just refused.
STAGE_MANDATORY_UNITS: dict[str, tuple[str, ...]] = {
    F.STAGE_CLASSIFY_PLAN: (
        F.UNIT_MODEL_CALLS,
        F.UNIT_MODEL_INPUT_TOKENS,
        F.UNIT_MODEL_OUTPUT_TOKENS,
    ),
    F.STAGE_SEARCH_WAVE: (
        F.UNIT_SEARCHES,
        F.UNIT_MODEL_CALLS,
        F.UNIT_MODEL_INPUT_TOKENS,
        F.UNIT_MODEL_OUTPUT_TOKENS,
    ),
    F.STAGE_READ_SOURCE: (
        F.UNIT_EXTRACTS,
        F.UNIT_PAGE_CREDITS,
        F.UNIT_BYTES,
        F.UNIT_MODEL_CALLS,
        F.UNIT_MODEL_INPUT_TOKENS,
        F.UNIT_MODEL_OUTPUT_TOKENS,
    ),
    # Verify can run with zero adjudications: with no competing values there is nothing
    # to adjudicate, and the merge itself is pure code. Its model units are therefore
    # optional, while its ability to write claims is not gated on any unit at all.
    F.STAGE_VERIFY: (),
    F.STAGE_SYNTHESIZE: (
        F.UNIT_MODEL_CALLS,
        F.UNIT_MODEL_INPUT_TOKENS,
        F.UNIT_MODEL_OUTPUT_TOKENS,
    ),
}

# Kinds that may degrade into a partial brief by creating a synthesis job. Synthesize
# itself is absent (it would create a second copy of itself), and so are the delivery
# kinds and the fan-out child, which cannot move the run's state at all.
_DEGRADABLE_TO_SYNTHESIS = (
    F.STAGE_CLASSIFY_PLAN,
    F.STAGE_SEARCH_WAVE,
    F.STAGE_READ_JOIN,
    F.STAGE_VERIFY,
)


class PlanReadUnavailable(RuntimeError):
    """A downstream stage could not positively load its admitted plan."""


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    state: str
    replayed: bool = False


@dataclass(frozen=True)
class _Admission:
    """What the admission gate resolved, so advance does not re-read any of it.

    ``run`` and ``plan`` ride along because the gate already had to fetch the run to
    decide, and the stage body needs both. Returning them is what stopped every stage
    receiving an empty request and an empty plan.
    """

    refusal: StepOutcome | None = None
    grant: ledger_mod.Grant | None = None
    run: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    # The durable receipt this stage holds against the PROJECT-day wallet. Carried out so
    # the commit path can settle the EXACT reservation it opened, on the day it opened it,
    # rather than decrementing whatever day the clock happens to read afterwards.
    receipt: ledger_mod.ProjectReceipt | None = None


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    state: str
    processing_stage: str = ""
    state_revision: int = 0
    failure_code: str = ""
    pending_question: dict[str, Any] = field(default_factory=dict)
    found: bool = True


@dataclass(frozen=True)
class StepRef:
    """Identifies one unit of work. What a Cloud Task body would carry."""

    uid: str
    run_id: str
    stage_id: str


@dataclass(frozen=True)
class StepOutcome:
    """Why a step ended. ``retryable`` decides whether the caller returns 200 or 5xx."""

    disposition: str
    state: str = ""
    stage_kind: str = ""
    retryable: bool = False
    detail: str = ""


@runtime_checkable
class ResearchRunEngine(Protocol):
    """The narrow contract a durability substrate must satisfy."""

    async def start(self, uid: str, spec: dict[str, Any], *, client_run_id: str) -> RunHandle: ...

    async def signal(self, uid: str, run_id: str, signal: dict[str, Any]) -> RunStatus: ...

    async def advance(self, uid: str, step: StepRef) -> StepOutcome: ...

    async def status(self, uid: str, run_id: str) -> RunStatus: ...

    async def list_runs(self, uid: str, *, limit: int = F.LIST_LIMIT) -> list[dict[str, Any]]: ...

    async def detail(self, uid: str, run_id: str) -> dict[str, Any] | None: ...

    async def activity(self, uid: str, run_id: str) -> dict[str, Any] | None: ...

    async def sweep(self, *, limit: int = 100) -> dict[str, Any]: ...


class FirestoreResearchEngine:
    """The phase-two implementation: Firestore state, outbox delivery, stub stages."""

    async def _deliver(self, uid: str, stage_ids: tuple[str, ...] | list[str]) -> None:
        """Hand every freshly created stage to the dispatcher. Best effort, never raises.

        The transaction that created these rows is already durable, so this is delivery,
        not commitment. A failure here leaves the outbox row due and the sweeper picks it
        up, which is the same recovery path a crash between commit and dispatch takes.

        Without this call the sweeper was the ONLY delivery path, and it runs every five
        minutes against a quick run whose whole wall clock is 240 seconds: the run
        expired before its second stage was ever handed to anyone.
        """
        for stage_id in stage_ids:
            try:
                await tasks_mod.dispatch_job(uid, stage_id)
            except Exception as exc:
                logger.warn(
                    "research.engine: inline dispatch failed, left for the sweeper",
                    {"stage_id": stage_id, "error": str(exc),
                     "error_code": "research_inline_dispatch_failed"},
                )

    async def _auto_admit(
        self,
        uid: str,
        run_id: str,
        *,
        plan_version: int,
        preset: Preset,
        correlation_id: str = "",
    ) -> bool:
        """Consume a durable auto-start marker through the one credit transaction."""
        admission = await credits_mod.admit(
            uid,
            run_id,
            plan_version=plan_version,
            preset=preset,
            correlation_id=correlation_id,
        )
        if admission.admitted:
            if admission.first_stage_id and not admission.replayed:
                await self._deliver(uid, (admission.first_stage_id,))
            return True

        # Paid access and the daily user credit cap are durable product refusals. A
        # competing run or an unavailable entitlement/meter can clear later, so those
        # retain the marker and the sweep retries without charging in the meantime.
        terminal = admission.code in {F.RESEARCH_PAID_CODE, F.RESEARCH_CAP_CODE}
        refused = await store.record_auto_admission_refusal(
            uid,
            run_id,
            error_code=admission.code or F.FAIL_METER_UNAVAILABLE,
            terminal=terminal,
            correlation_id=correlation_id,
        )
        await self._deliver(uid, refused.created_job_ids)
        return False

    async def start(
        self, uid: str, spec: dict[str, Any], *, client_run_id: str
    ) -> RunHandle:
        delivery_spec = spec.get("delivery")
        creation = await store.create_run(
            uid,
            client_run_id=client_run_id,
            request_text=str(spec.get("request", "")),
            preset=str(spec.get("preset", Preset.QUICK)),
            origin_surface=str(spec.get("origin_surface", "dashboard")),
            correlation_id=str(spec.get("correlation_id", "")),
            delivery=dict(delivery_spec) if isinstance(delivery_spec, dict) else None,
        )
        # A replayed creation has already been delivered once; re-dispatching is safe
        # (dispatch_job skips a fresh in-flight row) but pointless, so skip it.
        if creation.first_stage_id and not creation.replayed:
            await self._deliver(uid, (creation.first_stage_id,))
        return RunHandle(
            run_id=creation.run_id, state=creation.state, replayed=creation.replayed
        )

    async def signal(self, uid: str, run_id: str, signal: dict[str, Any]) -> RunStatus:
        """Cancel, answer a clarification, or admit. One entry point for user intent."""
        kind = str(signal.get("kind", ""))
        if kind == "cancel":
            _state, task_names = await store.request_cancel(
                uid, run_id, correlation_id=str(signal.get("correlation_id", ""))
            )
            # Best effort, outside the transaction, and the half that was never wired up:
            # request_cancel has always returned these names and nothing consumed them.
            # Claim-time retirement is what makes cancellation CORRECT - a task that fires
            # anyway finds the run blocked and abandons itself - and deleting the
            # not-yet-fired tasks is what makes it CHEAP. On a wide fan-out that is the
            # difference between a cancelled run costing nothing and costing a full wave
            # of Firecrawl credits.
            if task_names:
                from . import cloud_tasks as cloud_tasks_mod

                for task_name in task_names:
                    await cloud_tasks_mod.delete_task(task_name)
            return await self.status(uid, run_id)
        if kind == "answer":
            accepted, _state, resumed_stage_id = await store.answer_clarification(
                uid,
                run_id,
                question_id=str(signal.get("question_id", "")),
                answer=dict(signal.get("answer") or {}),
                correlation_id=str(signal.get("correlation_id", "")),
            )
            # The user is sitting in front of this. Leaving the resumed scope check for
            # the five-minute sweep reads as the answer having been ignored.
            if accepted and resumed_stage_id:
                await self._deliver(uid, (resumed_stage_id,))
            return await self.status(uid, run_id)
        if kind == "admit":
            admission = await credits_mod.admit(
                uid,
                run_id,
                plan_version=int(signal.get("plan_version", 1)),
                preset=Preset(str(signal.get("preset", Preset.QUICK))),
                correlation_id=str(signal.get("correlation_id", "")),
            )
            # admit_run is the one transaction that debits a credit and creates the first
            # search job. Charging the user and then leaving that job undelivered for the
            # sweeper is the worst of both: paid for, and not started.
            if admission.admitted and admission.first_stage_id and not admission.replayed:
                await self._deliver(uid, (admission.first_stage_id,))
            return await self.status(uid, run_id)
        if kind == "delete":
            from . import deletion as deletion_mod

            found, _state = await deletion_mod.request_deletion(
                uid, run_id, correlation_id=str(signal.get("correlation_id", ""))
            )
            if found:
                progress = await deletion_mod.drain_deletion(uid, run_id)
                if not progress.complete:
                    await self.sweep(limit=20)
            return RunStatus(run_id=run_id, state="", found=False)
        raise ValueError(f"unknown research signal kind {kind!r}")

    async def _fail_and_deliver(
        self,
        lease: store.StageLease,
        *,
        error_code: str,
        retryable: bool,
        spent_actuals: dict[str, int] | None = None,
        spent_cost_microusd: int = 0,
        spent_cost_known: bool = True,
    ) -> store.FailOutcome:
        """Fail one stage, then deliver whatever that transaction created OR re-armed.

        The re-armed stage is the part that was missing. ``fail_stage`` committed a retry
        with a jittered due time and returned nothing to deliver, so the only thing that
        would ever pick it up was the five-minute recovery sweep - against a quick run
        whose entire wall clock is 240 seconds. Every scheduled retry was, in practice, a
        run that expired waiting.
        """
        failed = await store.fail_stage(
            lease,
            error_code=error_code,
            retryable=retryable,
            spent_actuals=spent_actuals,
            spent_cost_microusd=spent_cost_microusd,
            spent_cost_known=spent_cost_known,
        )
        await self._deliver(lease.uid, failed.created_job_ids)
        if failed.rearmed_stage_id:
            # dispatch_job reads the row's due date and hands it to Cloud Tasks as the
            # task's schedule_time, so delivering it now honours the backoff rather than
            # discarding it.
            await self._deliver(lease.uid, (failed.rearmed_stage_id,))
        return failed

    async def advance(self, uid: str, step: StepRef) -> StepOutcome:
        """Claim, admit, run, commit. The whole per-stage lifecycle, in that order."""
        lease = await store.claim_stage(uid, step.run_id, step.stage_id)
        if lease is None:
            # Not an error. Another worker owns it, or the run refuses it. Answering
            # success is what stops Cloud Tasks hammering a stage someone else has.
            return StepOutcome(disposition="already_leased", retryable=False)

        admission = await self._admit_stage(lease)
        if admission.refusal is not None:
            return admission.refusal

        grant = admission.grant
        budget = lease.budget
        ctx = StageContext(
            uid=uid,
            run_id=lease.run_id,
            stage_id=lease.stage_id,
            stage_kind=lease.stage_kind,
            wave=lease.wave,
            ordinal=lease.ordinal,
            attempt=lease.attempt,
            admitted_plan_version=lease.admitted_plan_version,
            budget=budget,
            grant=dict(grant.units) if grant else {},
            degraded=bool(grant.degraded) if grant else False,
            # The request the user actually typed, and the plan classify_plan persisted.
            # Both were reaching every stage empty, so the classifier classified "" and
            # search, read, verify and synthesis all ran against an empty policy.
            request_text=str(admission.run.get(F.REQUEST_TEXT, "")),
            plan=dict(admission.plan),
            clarification_answers=tuple(
                admission.run.get(F.CLARIFICATION_ANSWERS) or ()
            ),
            payload=dict(lease.payload),
            correlation_id=lease.correlation_id,
            is_cancelled=lambda: store.is_cancelled(uid, lease.run_id),
        )

        # Every exit from here on settles the project receipt, in a finally, so a path
        # added later cannot silently skip it. `settled` is what stops the finally from
        # settling a receipt an earlier branch already closed with a real figure.
        settled = False

        async def _settle(actual_microusd: int | None, *, released: bool = False) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            await ledger_mod.settle_project_receipt(
                admission.receipt, actual_microusd, released=released
            )

        try:
            try:
                body = get_stage(lease.stage_kind)
            except StageNotRegisteredError as exc:
                # Code drift, not "not built yet". Fail the stage rather than guessing.
                # Nothing ran, so the reservation is genuinely released rather than kept.
                await _settle(0, released=True)
                failed = await self._fail_and_deliver(
                    lease, error_code="stage_not_registered", retryable=False
                )
                return StepOutcome(
                    disposition=failed.outcome or "stage_not_registered",
                    stage_kind=lease.stage_kind,
                    retryable=False,
                    detail=str(exc),
                )

            try:
                # Hard-bounded here rather than by infrastructure, so a hung stage is
                # killed by us well before Cloud Run or the dispatch deadline notice.
                result = await asyncio.wait_for(body(ctx), timeout=budget.per_stage_s)
            except Exception as exc:
                # TimeoutError is an Exception subclass, so one handler covers both and
                # the distinction is only in the failure code the user eventually sees.
                timed_out = isinstance(exc, TimeoutError)
                if not timed_out:
                    logger.error(
                        "research.engine: stage body failed",
                        {"stage_id": lease.stage_id, "stage_kind": lease.stage_kind,
                         "error": str(exc), "error_code": F.FAIL_PROVIDER_UNAVAILABLE},
                    )
                # One unreadable page must not discard evidence from the other fan-out
                # children. Preserve the normal retry, then close an exhausted child as
                # a named source gap so the coordinator can still reach its join.
                if (
                    lease.stage_kind == F.STAGE_READ_SOURCE
                    and lease.attempt >= store.STAGE_ATTEMPT_CAP
                ):
                    source_id = str(lease.payload.get("source_id") or lease.ordinal)
                    error_code = (
                        F.FAIL_WALL_CLOCK_EXPIRED
                        if timed_out
                        else F.FAIL_PROVIDER_UNAVAILABLE
                    )
                    result = StageResult(
                        kind=StageResultKind.DONE,
                        document_updates={
                            F.SOURCES_SUBCOLLECTION: {
                                source_id: {
                                    "state": "failed",
                                    "gap_reason": error_code,
                                }
                            }
                        },
                        stage_outputs={
                            "source_id": source_id,
                            "state": "failed",
                            "gap_reason": error_code,
                        },
                        actuals=dict(ctx.spent),
                        cost_microusd=ctx.spent_cost_microusd,
                        cost_known=ctx.spent_cost_known,
                    )
                    child = await store.complete_child(lease, result)
                    await _settle(
                        int(result.cost_microusd) if result.cost_known else None
                    )
                    await self._deliver(uid, child.created_job_ids)
                    return StepOutcome(
                        disposition=child.disposition,
                        stage_kind=lease.stage_kind,
                        retryable=False,
                    )
                # A stage that raised or timed out still spent whatever it spent before it
                # stopped. ctx carries that running total precisely because there is no
                # StageResult on this path, and settling at None RETAINS the estimate on
                # the project wallet rather than reopening headroom for money that may be
                # gone. Both are the conservative direction, which is the only acceptable
                # direction for a spend guard.
                failed = await self._fail_and_deliver(
                    lease,
                    error_code=(
                        F.FAIL_WALL_CLOCK_EXPIRED if timed_out
                        else F.FAIL_PROVIDER_UNAVAILABLE
                    ),
                    retryable=True,
                    spent_actuals=dict(ctx.spent),
                    spent_cost_microusd=ctx.spent_cost_microusd,
                    spent_cost_known=ctx.spent_cost_known,
                )
                await _settle(None)
                return StepOutcome(
                    disposition=failed.outcome,
                    stage_kind=lease.stage_kind,
                    retryable=False,
                )

            # A fan-out child completes through the join transaction, never through
            # advance: only the join may move the run's state.
            if lease.stage_kind == F.STAGE_READ_SOURCE:
                child = await store.complete_child(lease, result)
                await _settle(
                    int(result.cost_microusd) if result.cost_known else None
                )
                # The join job is created inside that transaction by whichever child
                # happens to be last. It was unreachable before, so a completed wave sat
                # there with nothing delivering the join.
                await self._deliver(uid, child.created_job_ids)
                return StepOutcome(
                    disposition=child.disposition,
                    stage_kind=lease.stage_kind,
                    retryable=False,
                )

            advanced = await store.advance(lease, result)
            await _settle(int(result.cost_microusd) if result.cost_known else None)
            await self._deliver(uid, advanced.created_job_ids)
            if (
                advanced.committed
                and lease.stage_kind == F.STAGE_CLASSIFY_PLAN
                and advanced.state == F.STATE_QUEUED
                and result.run_updates.get(F.AUTO_ADMIT_REQUESTED) is True
            ):
                # Admission is deliberately after the plan commit: store.admit_run reads
                # the committed plan version and atomically debits the credit with the
                # first search job. The durable marker is the crash-recovery handoff.
                try:
                    await self._auto_admit(
                        uid,
                        lease.run_id,
                        plan_version=int(result.run_updates.get(F.CURRENT_PLAN_VERSION, 0)),
                        preset=Preset(lease.preset),
                        correlation_id=lease.correlation_id,
                    )
                except Exception as exc:
                    # The plan and marker are already durable. Returning success prevents
                    # a pointless classifier retry; the sweep owns this recovery now.
                    logger.error(
                        "research.engine: inline auto admission failed, left for sweep",
                        {"run_id": lease.run_id, "error": str(exc),
                         "error_code": "research_auto_admission_failed"},
                    )
                status = await self.status(uid, lease.run_id)
                advanced = replace(advanced, state=status.state)
            return StepOutcome(
                disposition=advanced.disposition,
                state=advanced.state,
                stage_kind=lease.stage_kind,
                retryable=False,
            )
        finally:
            # The backstop. Anything that escapes above - a cancellation landing inside a
            # commit, an unforeseen raise, a future early return - still closes the
            # reservation, conservatively, rather than leaking it until the day rolls over.
            await _settle(None)

    async def _admit_stage(self, lease: store.StageLease) -> _Admission:
        """The gate before any billable external call. A None refusal means proceed.

        Check order is fixed by the architecture's admission table and is not
        rearrangeable: cancel, wall clock, attempt cap, entitlement, project-day cost
        cap, then the per-run reservation. Cheapest and most absolute first, so a
        cancelled run never reaches an entitlement read and an entitlement lapse never
        reaches a provider.
        """
        uid, run_id = lease.uid, lease.run_id
        delivery_kind = lease.stage_kind in POST_TERMINAL_KINDS
        # get_run hides a run once deletion starts, which is right for every caller
        # except the stage whose whole job is to finish that deletion.
        run = await store.get_run(uid, run_id, include_deleted=delivery_kind)
        if run is None:
            await store.advance(lease, StageResult(kind=StageResultKind.DONE))
            return _Admission(
                refusal=StepOutcome(disposition="cancelled", retryable=False)
            )

        # Whether a DELIVERY kind may act is its own question, per kind, not a blanket
        # exemption from the block check. notify_result needs a result worth reporting on
        # a run that is neither cancelled nor deleting; delete_run needs a live deletion
        # receipt and gets its authority from nowhere else.
        deletion_active = await store.deletion_active(uid, run_id)
        permitted = delivery_kind and may_run_post_terminal(
            lease.stage_kind, run, deletion_active=deletion_active
        )
        if store._blocked_reason(run) and not permitted:
            # advance() sees the same block, marks the stage abandoned and commits
            # whatever it proved it spent. Routing through it keeps one owner for that
            # transition.
            await store.advance(lease, StageResult(kind=StageResultKind.DONE))
            return _Admission(
                refusal=StepOutcome(disposition="cancelled", retryable=False)
            )

        try:
            plan = await self._load_plan(uid, run_id, run, lease.stage_kind)
        except PlanReadUnavailable:
            failed = await self._fail_and_deliver(
                lease, error_code=F.FAIL_METER_UNAVAILABLE, retryable=True
            )
            return _Admission(
                refusal=StepOutcome(disposition=failed.outcome, retryable=False)
            )

        if delivery_kind:
            # claim_stage and store.advance both apply the same per-kind predicate; this
            # gate did not apply any, so notify_result and delete_run were claimed and
            # then completed without their body ever running. They reserve nothing, so
            # there is no budget path to fall through to.
            return _Admission(run=run, plan=plan)

        now = datetime.now(UTC)
        deadline = str(run.get(F.DEADLINE_AT, ""))
        if deadline and deadline <= now.isoformat():
            # Exhaustion NEVER produces a failure. Every path out of budget or time
            # routes through synthesis so the user still gets a sourced partial brief.
            return _Admission(
                refusal=await self._degrade_to_partial(lease, F.FAIL_WALL_CLOCK_EXPIRED)
            )

        if lease.attempt > store.STAGE_ATTEMPT_CAP:
            failed = await self._fail_and_deliver(
                lease, error_code=F.FAIL_ATTEMPT_CAP, retryable=False
            )
            return _Admission(
                refusal=StepOutcome(disposition=failed.outcome, retryable=False)
            )

        request = STAGE_UNIT_REQUESTS.get(lease.stage_kind)
        if not request:
            # Pure coordination, nothing billable to gate. It still needs the run and the
            # plan: read_join decides whether to buy another wave from the plan's
            # must-answer list, and finalize reads the run.
            return _Admission(run=run, plan=plan)

        if not await credits_mod.entitlement_still_valid(uid):
            # A lapse mid-run stops spending rather than finishing work nobody pays for.
            return _Admission(
                refusal=await self._degrade_to_partial(lease, F.FAIL_ENTITLEMENT_LAPSED)
            )

        # The project-day ceiling, checked before the per-run reservation because it is
        # the wider boundary: one run staying inside its own budget says nothing about
        # whether the project can afford it today.
        #
        # The reservation is a durable RECEIPT keyed on this stage and this attempt, not
        # an anonymous increment. Two counters could not be settled correctly: the
        # settlement recomputed the day from the clock, so a stage spanning UTC midnight
        # credited one day and debited another, and a bare counter has no record of which
        # stage still owes a settlement, so every non-success exit leaked its hold.
        run_budget = budget_for(Preset(lease.preset))
        estimate = metering.stage_cost_estimate_microusd(lease.stage_kind, request)
        try:
            receipt = await ledger_mod.reserve_project_spend(
                estimate,
                uid=uid,
                run_id=run_id,
                stage_id=lease.stage_id,
                attempt=lease.attempt,
                deadline_at=lease.stage_deadline_at,
            )
        except Exception:
            # Same fail-closed shape as an unreadable per-run meter below. An outage
            # must not remove the only spend boundary; retry WITHOUT a provider call.
            failed = await self._fail_and_deliver(
                lease, error_code=F.FAIL_METER_UNAVAILABLE, retryable=True
            )
            return _Admission(
                refusal=StepOutcome(
                    disposition=failed.outcome,
                    stage_kind=lease.stage_kind,
                    retryable=False,
                )
            )
        if receipt is None:
            return _Admission(
                refusal=await self._degrade_to_partial(lease, F.FAIL_COST_CAP_REACHED)
            )

        try:
            grant = await ledger_mod.reserve(
                uid,
                run_id,
                lease.stage_id,
                budget=run_budget,
                request=request,
                cost_microusd=estimate,
            )
        except ledger_mod.MeterUnavailableError:
            # Fail CLOSED. Retry WITHOUT a provider call: an unreadable meter must never
            # be interpreted as ordinary exhaustion, which would end the run early.
            # Nothing was spent, so the project hold is genuinely released.
            failed = await self._fail_and_deliver(
                lease, error_code=F.FAIL_METER_UNAVAILABLE, retryable=True
            )
            return _Admission(
                refusal=StepOutcome(
                    disposition=failed.outcome,
                    stage_kind=lease.stage_kind,
                    retryable=False,
                )
            )
        except ledger_mod.RunCostCapReached:
            # The run's own dollar ceiling. Not retryable and not a failure: the user gets
            # the sourced partial brief the evidence so far supports.
            return _Admission(
                refusal=await self._degrade_to_partial(lease, F.FAIL_COST_CAP_REACHED)
            )

        # A zero grant on a unit the stage CANNOT work without is a refusal, not a smaller
        # run. `grant.empty` only asked whether ANY unit was nonzero, so a read holding its
        # extract slot and zero page credits, zero bytes and zero model calls still reached
        # Firecrawl and then the extraction model, spending units the ledger had just
        # refused and recording every one of them as an overrun after the fact.
        missing = [
            unit
            for unit in STAGE_MANDATORY_UNITS.get(lease.stage_kind, ())
            if int(request.get(unit, 0)) > 0
            and grant.get(unit) < int(request.get(unit, 0))
        ]
        if grant.empty or missing:
            if missing:
                logger.info(
                    "research.engine: mandatory unit grant was zero, refusing before spend",
                    {"run_id": run_id, "stage_kind": lease.stage_kind,
                     "missing_units": missing},
                )
            # Nothing has been spent yet, so both holds are released rather than retained.
            return _Admission(
                refusal=await self._degrade_to_partial(lease, F.FAIL_BUDGET_EXHAUSTED)
            )

        return _Admission(grant=grant, run=run, plan=plan, receipt=receipt)

    async def _load_plan(
        self, uid: str, run_id: str, run: dict[str, Any], stage_kind: str
    ) -> dict[str, Any]:
        """The plan this stage must run against, pinned to the admitted version.

        Admitted wins over current: a task delayed behind a clarification round has to
        execute against the interpretation the user actually confirmed, which is the same
        fence claim_stage applies to the job's plan_version. Falling back to current is
        for the window before admission, where only classify_plan runs and the plan it is
        about to write does not exist yet.
        """
        version = int(run.get(F.ADMITTED_PLAN_VERSION, 0)) or int(
            run.get(F.CURRENT_PLAN_VERSION, 0)
        )
        if not version:
            if stage_kind == F.STAGE_CLASSIFY_PLAN:
                return {}
            raise PlanReadUnavailable("persisted research plan is missing")
        try:
            plan = await store.get_plan(uid, run_id, version)
            if not plan:
                raise PlanReadUnavailable("persisted research plan is missing")
            return plan
        except PlanReadUnavailable:
            raise
        except Exception as exc:
            # An unreadable plan is not a reason to spend against an empty one. Raise a
            # typed gate failure so admission schedules a bounded retry before any call.
            logger.error(
                "research.engine: plan read failed",
                {"run_id": run_id, "plan_version": version, "error": str(exc),
                 "error_code": "research_plan_read_failed"},
            )
            raise PlanReadUnavailable("persisted research plan read failed") from exc

    async def _degrade_to_partial(
        self, lease: store.StageLease, failure_code: str
    ) -> StepOutcome:
        """Route an exhausted run into synthesis, not into a failure.

        The user asked a question and we have evidence; a partial answer with a named
        gap is the product. Ending here as `failed` would throw away work already paid
        for and tell the user nothing.

        Naming the destination state is not enough: without a job to service it the run
        parked in `synthesizing` forever. Which job depends on who is degrading, and the
        three cases are genuinely different:

          * an ordinary stage buys a partial synthesis;
          * synthesize itself cannot buy a second synthesize, so it goes straight to
            finalize, which owns the terminal commit and the notification;
          * a fan-out child must not set next_state at all. Only the join may move the
            run, so a degrading child completes through the join instead and the wave
            reports the shortfall as a gap.
        """
        if lease.stage_kind == F.STAGE_READ_SOURCE:
            child = await store.complete_child(
                lease,
                StageResult(
                    kind=StageResultKind.DONE,
                    stage_outputs={"degraded_reason": failure_code, "mode": "partial"},
                ),
            )
            await self._deliver(lease.uid, child.created_job_ids)
            return StepOutcome(
                disposition="degraded_to_partial",
                stage_kind=lease.stage_kind,
                retryable=False,
                detail=failure_code,
            )

        if lease.stage_kind in _DEGRADABLE_TO_SYNTHESIS:
            next_jobs = (
                NextJob(
                    stage_kind=F.STAGE_SYNTHESIZE,
                    wave=lease.wave,
                    payload={"mode": "partial"},
                ),
            )
        else:
            # synthesize, finalize, and anything else that already holds a result. The
            # brief is written or unwritable; finalize commits partial and notifies.
            next_jobs = (
                NextJob(
                    stage_kind=F.STAGE_FINALIZE,
                    wave=lease.wave,
                    payload={"terminal_state": F.STATE_PARTIAL},
                ),
            )

        result = StageResult(
            kind=StageResultKind.DONE,
            next_state=F.STATE_SYNTHESIZING,
            next_jobs=next_jobs,
            run_updates={F.FAILURE_CODE: failure_code},
            stage_outputs={"degraded_reason": failure_code, "mode": "partial"},
        )
        advanced = await store.advance(lease, result)
        await self._deliver(lease.uid, advanced.created_job_ids)
        return StepOutcome(
            disposition="degraded_to_partial",
            state=advanced.state,
            stage_kind=lease.stage_kind,
            retryable=False,
            detail=failure_code,
        )

    async def status(self, uid: str, run_id: str) -> RunStatus:
        run = await store.get_run(uid, run_id)
        if run is None:
            return RunStatus(run_id=run_id, state="", found=False)
        return RunStatus(
            run_id=run_id,
            state=str(run.get(F.STATE, "")),
            processing_stage=str(run.get(F.PROCESSING_STAGE, "")),
            state_revision=int(run.get(F.STATE_REVISION, 0)),
            failure_code=str(run.get(F.FAILURE_CODE, "")),
            pending_question=dict(run.get(F.PENDING_QUESTION) or {}),
        )

    async def list_runs(
        self, uid: str, *, limit: int = F.LIST_LIMIT
    ) -> list[dict[str, Any]]:
        return await store.list_runs(uid, limit=limit)

    async def detail(self, uid: str, run_id: str) -> dict[str, Any] | None:
        run = await store.get_run(uid, run_id)
        if run is None:
            return None
        version = int(run.get(F.CURRENT_PLAN_VERSION, 0))
        plan = await store.get_plan(uid, run_id, version) if version else None
        claims = await store.list_documents(uid, run_id, F.CLAIMS_SUBCOLLECTION, limit=200)
        return {"run": run, "plan": plan or {}, "claims": claims}

    async def activity(self, uid: str, run_id: str) -> dict[str, Any] | None:
        run = await store.get_run(uid, run_id)
        if run is None:
            return None
        sources = await store.list_documents(uid, run_id, F.SOURCES_SUBCOLLECTION, limit=50)
        return {"run": run, "sources": sources}

    async def sweep(self, *, limit: int = 100) -> dict[str, Any]:
        report = await sweep_mod.run_sweep(limit=limit)
        result = report.as_dict()
        recovered = 0
        candidates = await store.list_pending_auto_admissions(limit=min(limit, 50))
        for candidate in candidates:
            try:
                admitted = await self._auto_admit(
                    candidate.uid,
                    candidate.run_id,
                    plan_version=candidate.plan_version,
                    preset=Preset(candidate.preset),
                    correlation_id=candidate.correlation_id,
                )
                if admitted:
                    recovered += 1
            except Exception as exc:
                result["errors"] = int(result.get("errors", 0)) + 1
                logger.error(
                    "research.sweep: auto admission recovery failed",
                    {"run_id": candidate.run_id, "error": str(exc),
                     "error_code": "research_auto_admission_recovery_failed"},
                )
        result["auto_admissions_recovered"] = recovered
        return result


_engine: FirestoreResearchEngine | None = None


def get_research_engine() -> ResearchRunEngine:
    """Module singleton, mirroring ``get_task_scheduler()``."""
    global _engine
    if _engine is None:
        _engine = FirestoreResearchEngine()
    return _engine
