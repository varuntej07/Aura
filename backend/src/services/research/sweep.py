"""Crash recovery. Firestore is the truth; every delivery mechanism is a hint.

Five independent passes, each per-run isolated so one poisoned run can never abort
recovery for everyone else. This is the analogue of ``_run_meeting_job_sweep`` and would
eventually be registered at the free ``now_minute % 5 == 4`` scheduler slot (0, 1, 2 and
3 are taken by calendar renewal, the reactive outbox, meetings and chat turns).

**Phase two does not register it.** The function exists and is directly callable for
inspection; wiring it into ``handlers/scheduler.py`` is phase three work, after the
queue and indexes exist. Registering it now would mean a deployed revision quietly
sweeping collections whose indexes have not been created.

The passes, and the exact failure each one exists to undo:

  A  outbox redelivery      a stage committed but its dispatch never happened
  B  stale stage recovery   a worker died holding a lease
  C  clarification expiry   a parked run nobody ever answered
  D  stuck fan-out          a child that will never complete, so the join never fires
  E  deletion drain         a deletion receipt that stopped partway

Pass B is the one that must be read carefully. Clearing a dead lease is not enough: the
retry has to mint a NEW Cloud Task name, because Cloud Tasks reserves a completed task's
name for roughly an hour and a re-enqueue under the old name is silently swallowed. That
is what bumping the attempt and re-arming the outbox row accomplishes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import deletion as deletion_mod
from . import fields as F
from . import ledger as ledger_mod
from . import store
from . import tasks as tasks_mod
from .stages.registry import POST_TERMINAL_KINDS, may_run_post_terminal

# Per-pass caps. The sweep runs on a shared scheduler tick, so every pass is bounded
# rather than allowed to grow with the collection.
OUTBOX_LIMIT = 100
STALE_STAGE_LIMIT = 50
CLARIFICATION_LIMIT = 50
COORD_LIMIT = 50
DELETION_LIMIT = 20
PROJECT_RECEIPT_LIMIT = 50


@dataclass
class SweepReport:
    outbox: dict[str, int] = field(default_factory=dict)
    stale_stages_recovered: int = 0
    stale_stages_terminal: int = 0
    clarifications_expired: int = 0
    coords_collapsed: int = 0
    deletions_advanced: int = 0
    deletions_completed: int = 0
    project_receipts_settled: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "outbox": self.outbox,
            "stale_stages_recovered": self.stale_stages_recovered,
            "stale_stages_terminal": self.stale_stages_terminal,
            "clarifications_expired": self.clarifications_expired,
            "coords_collapsed": self.coords_collapsed,
            "deletions_advanced": self.deletions_advanced,
            "deletions_completed": self.deletions_completed,
            "project_receipts_settled": self.project_receipts_settled,
            "errors": self.errors,
        }


async def run_sweep(*, limit: int = OUTBOX_LIMIT) -> SweepReport:
    """One full recovery pass. Never raises: a sweep that dies stops all recovery."""
    report = SweepReport()
    now = datetime.now(UTC)

    for name, coro in (
        ("outbox", _pass_outbox(report, limit)),
        ("stale_stages", _pass_stale_stages(report, now)),
        ("clarifications", _pass_clarification_expiry(report, now)),
        ("coords", _pass_stuck_fanout(report, now)),
        ("deletions", _pass_deletions(report)),
        ("project_receipts", _pass_project_receipts(report)),
    ):
        try:
            await coro
        except Exception as exc:
            report.errors += 1
            logger.error(
                "research.sweep: pass failed",
                {"pass": name, "error": str(exc), "error_code": "research_sweep_failed",
                 "alert": True},
            )
    logger.info("research.sweep: complete", {**report.as_dict(), "metric": "research_sweep"})
    return report


async def _pass_outbox(report: SweepReport, limit: int) -> None:
    """A. Redeliver committed-but-undelivered stages."""
    result = await tasks_mod.dispatch_pending(limit=limit)
    report.outbox = result.as_dict()


async def _pass_stale_stages(report: SweepReport, now: datetime) -> None:
    """B. Recover stages whose worker died holding the lease.

    Past the attempt cap a stage goes terminal, PARTIAL when the run holds evidence and
    FAILED only when it holds none, and its reservation is released so the units are not
    held forever by a process that no longer exists.
    """
    now_iso = now.isoformat()

    def _query() -> list[tuple[str, str, str, dict[str, Any]]]:
        rows: list[tuple[str, str, str, dict[str, Any]]] = []
        query = (
            admin_firestore()
            .collection_group(F.STAGES_SUBCOLLECTION)
            .where(F.STAGE_STATE, "in", list(F.STAGE_ACTIVE_STATES))
            .where(F.STAGE_DEADLINE_AT, "<=", now_iso)
            .limit(STALE_STAGE_LIMIT)
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            deadline = str(data.get(F.STAGE_DEADLINE_AT, ""))
            if not deadline or deadline > now_iso:
                continue
            try:
                run_ref = snap.reference.parent.parent
                run_id = run_ref.id
                uid = run_ref.parent.parent.id
            except Exception:
                continue
            rows.append((uid, run_id, snap.id, data))
        return rows

    for uid, run_id, stage_id, stage in await asyncio.to_thread(_query):
        try:
            recovered, created = await _recover_stage(uid, run_id, stage_id, stage, now)
        except Exception as exc:
            report.errors += 1
            logger.error(
                "research.sweep: stage recovery failed",
                {"run_id": run_id, "stage_id": stage_id, "error": str(exc)},
            )
            continue
        # A notification this pass created is delivered here rather than waiting for the
        # NEXT sweep five minutes later, which for a run that already blew its wall clock
        # is the difference between a late toast and a useless one.
        for created_id in created:
            try:
                await tasks_mod.dispatch_job(uid, created_id)
            except Exception as exc:
                logger.warn(
                    "research.sweep: dispatch of recovery job failed, left due",
                    {"run_id": run_id, "stage_id": created_id, "error": str(exc)},
                )
        if recovered == "retry":
            report.stale_stages_recovered += 1
        elif recovered:
            report.stale_stages_terminal += 1


async def _recover_stage(
    uid: str, run_id: str, stage_id: str, stage: dict[str, Any], now: datetime
) -> tuple[str, list[str]]:
    """Re-arm or terminate one dead stage, then free whatever it was holding.

    Returns the outcome plus any job ids this recovery created, which the caller
    delivers. The reservation is now freed inside the transaction, so there is nothing
    left to do after it commits.
    """
    now_iso = now.isoformat()
    created: list[str] = []

    def _run() -> str:
        db = admin_firestore()
        run_ref = store._run_ref(uid, run_id)
        stage_ref = store._stages_ref(uid, run_id).document(stage_id)
        job_ref = store._jobs_ref(uid).document(stage_id)
        outbox_ref = store._outbox_ref(uid).document(stage_id)
        ledger_ref = store._ledger_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> str:
            # Firestore retries a contended transaction by re-running this body, so the
            # captured list has to be reset or a retry appends the same job id twice.
            created.clear()
            stage_snap = stage_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            outbox_snap = outbox_ref.get(transaction=txn)
            ledger_snap = ledger_ref.get(transaction=txn)
            if not stage_snap.exists or not run_snap.exists:
                return ""
            current = stage_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            ledger = (ledger_snap.to_dict() or {}) if ledger_snap.exists else {}
            current_attempt = int(current.get(F.STAGE_ATTEMPT, 0))
            transition = stale_recovery_transition(current_attempt)
            deletion_active = store._deletion_active(
                store._read_deletion_receipt(txn, uid, run_id)
            )
            receipt_day = str(current.get(F.STAGE_PROJECT_RECEIPT_DAY, ""))
            receipt_id = str(current.get(F.STAGE_PROJECT_RECEIPT_ID, ""))
            receipt_ref = None
            budget_ref = None
            receipt_snap = None
            budget_snap = None
            if receipt_day and receipt_id:
                budget_ref = ledger_mod._project_budget_ref(receipt_day)
                receipt_ref = ledger_mod._project_receipt_ref(receipt_day, receipt_id)
                budget_snap = budget_ref.get(transaction=txn)
                receipt_snap = receipt_ref.get(transaction=txn)
            # Re-check under the transaction: the stage may have completed or been
            # re-leased between the query and here.
            if current.get(F.STAGE_STATE) not in F.STAGE_ACTIVE_STATES:
                return ""
            if str(current.get(F.STAGE_DEADLINE_AT, "")) > now_iso:
                return ""

            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            expires_at = str(run.get(F.EXPIRES_AT, ""))
            blocked = store._blocked_reason(run)

            # Free the dead stage's reservation HERE, in the same transaction that
            # declares it dead. Doing it afterwards left the stage dispatchable while its
            # units were still reserved: a redelivery could claim the stage and reserve
            # against the same stage_id in that window, and the trailing release would
            # then wipe the new owner's live grant instead of the dead one's.
            held = dict((ledger.get(F.LEDGER_RESERVATIONS) or {}).get(stage_id) or {})
            ledger_patch, _released_units = store._commit_units(
                ledger,
                stage_id,
                held,
                cost_microusd=int(
                    (ledger.get(F.LEDGER_COST_RESERVATIONS) or {}).get(stage_id, 0)
                ),
                cost_known=False,
            )
            if ledger_patch:
                txn.update(
                    ledger_ref,
                    dict(ledger_patch, **{F.LEDGER_UPDATED_AT: now_iso}),
                )
            if receipt_snap is not None and receipt_snap.exists:
                receipt_data = receipt_snap.to_dict() or {}
                if receipt_data.get(F.RECEIPT_STATE) == F.RECEIPT_RESERVED:
                    estimate = int(receipt_data.get(F.RECEIPT_ESTIMATE_MICROUSD, 0))
                    budget_data = (
                        budget_snap.to_dict() or {}
                        if budget_snap is not None and budget_snap.exists
                        else {}
                    )
                    txn.set(budget_ref, {
                        F.PROJECT_RESERVED_MICROUSD: max(
                            0,
                            int(budget_data.get(F.PROJECT_RESERVED_MICROUSD, 0)) - estimate,
                        ),
                        F.PROJECT_ACTUAL_MICROUSD: int(
                            budget_data.get(F.PROJECT_ACTUAL_MICROUSD, 0)
                        ) + estimate,
                        F.UPDATED_AT: now_iso,
                    }, merge=True)
                    txn.update(receipt_ref, {
                        F.RECEIPT_STATE: F.RECEIPT_SETTLED,
                        F.RECEIPT_ACTUAL_MICROUSD: estimate,
                        F.RECEIPT_COST_KNOWN: False,
                        F.UPDATED_AT: now_iso,
                    })

            # A DELIVERY stage crashed holding its lease on a run that is legitimately
            # terminal. `_blocked_reason` returns terminal for that run, so treating
            # `blocked` as a universal stop abandoned exactly the stage whose job is to
            # tell the user their research finished - and nothing else would ever create
            # another one, because the run is absorbing. Asked per kind instead.
            stage_kind = str(current.get(F.STAGE_KIND, stage.get(F.STAGE_KIND, "")))
            if blocked and may_run_post_terminal(
                stage_kind,
                run,
                deletion_active=deletion_active,
            ):
                blocked = ""

            if blocked or transition["outcome"] == "attempt_cap_exhausted":
                has_evidence = (
                    int(run.get(F.CLAIM_COUNT, 0)) > 0
                    or int(run.get(F.SOURCE_COUNT, 0)) > 0
                )
                terminal = F.STATE_PARTIAL if has_evidence else F.STATE_FAILED
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_ABANDONED if blocked else F.STAGE_FAILED,
                        F.LEASE_TOKEN_HASH: "",
                        F.STAGE_DEADLINE_AT: "",
                        F.LAST_ERROR_CODE: blocked or F.FAIL_ATTEMPT_CAP,
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(
                    job_ref,
                    {F.JOB_STATE: F.STAGE_FAILED, F.LAST_ERROR_CODE: blocked or
                     F.FAIL_ATTEMPT_CAP, F.DISPATCH_DUE_AT: store.DISPATCH_NEVER,
                     F.UPDATED_AT: now_iso},
                )
                store._retire_outbox(txn, uid, stage_id, now_iso)
                # Exhausting a DELIVERY stage must not touch the run, exactly as in
                # store.fail_stage: downgrading a READY brief to PARTIAL because its toast
                # could not be delivered tells the user their research is incomplete when
                # the only thing that failed was the message about it. It also must not
                # mint a notification for the failure of a notification.
                is_delivery = stage_kind in POST_TERMINAL_KINDS
                if not blocked and not is_delivery:
                    txn.update(
                        run_ref,
                        {
                            F.STATE: terminal,
                            F.FAILURE_CODE: F.FAIL_ATTEMPT_CAP,
                            F.STATE_REVISION: gcloud_firestore.Increment(1),
                            F.UPDATED_AT: now_iso,
                        },
                    )
                    # This run just went terminal without ever reaching finalize, which
                    # is the only stage that creates a notification. Without this the
                    # brief (or the failure) is durable and the user is never told, and
                    # a terminal run refuses all later work so nothing retries.
                    # A blocked run is skipped: it is cancelled or deleting, and
                    # notify_result deliberately has no copy for either.
                    created.append(
                        store.create_terminal_notify_job(
                            txn,
                            uid=uid,
                            run_id=run_id,
                            terminal_state=terminal,
                            wave=int(current.get(F.WAVE, 0)),
                            now_iso=now_iso,
                            expires_at=expires_at,
                            correlation_id=str(run.get(F.CORRELATION_ID, "")),
                            causation_id=stage_id,
                            plan_version=int(run.get(F.ADMITTED_PLAN_VERSION, 0)),
                        )
                    )
                outcome = blocked or terminal
            else:
                # Recovery re-arms the execution. claim_stage alone allocates the next
                # execution attempt; dispatch_attempts supplies task-name generations.
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_PENDING,
                        F.LEASE_TOKEN_HASH: "",
                        F.STAGE_DEADLINE_AT: "",
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(
                    job_ref,
                    {
                        F.JOB_STATE: F.STAGE_PENDING,
                        F.LEASE_TOKEN_HASH: "",
                        F.UPDATED_AT: now_iso,
                    },
                )
                row = {
                    F.OUTBOX_STATE: F.OUTBOX_RETRY,
                    F.DISPATCH_DUE_AT: now_iso,
                    F.UPDATED_AT: now_iso,
                }
                if outbox_snap.exists:
                    txn.update(outbox_ref, row)
                else:
                    store._txn_create(
                        txn,
                        outbox_ref,
                        dict(
                            store._outbox_row(
                                uid=uid,
                                run_id=run_id,
                                stage_id=stage_id,
                                now_iso=now_iso,
                                expires_at=expires_at,
                                correlation_id=str(run.get(F.CORRELATION_ID, "")),
                            ),
                            **row,
                        ),
                    )
                # Hand the re-armed stage back so the caller can dispatch it NOW rather
                # than at the next tick. A stale-stage pass that rescued a stage at
                # minute 4 and left it for minute 9 was, against a 240-second run,
                # rescuing it into a run that had already expired.
                created.append(stage_id)
                outcome = "retry"

            txn.update(run_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now_iso})
            store._audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="stage_recovered",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=stage_id,
                stage_kind=stage_kind,
                attempt=current_attempt,
                reason_code=outcome,
                units=held,
            )
            return outcome

        return _execute(transaction)

    outcome = await asyncio.to_thread(_run)
    return outcome, list(created) if outcome else []


def stale_recovery_transition(attempt: int) -> dict[str, Any]:
    """Pure description of the attempt transition used by stale recovery.

    Recovery never allocates an execution number. It either re-arms the same stored
    number for claim_stage to allocate once, or declares the cap exhausted.
    """
    current = max(0, int(attempt))
    exhausted = current >= store.STAGE_ATTEMPT_CAP
    return {
        "crashed_execution_attempt": current,
        "recovered_stage_attempt": current,
        "next_execution_attempt": None if exhausted else current + 1,
        "outcome": "attempt_cap_exhausted" if exhausted else "retry_dispatchable",
    }


async def _pass_clarification_expiry(report: SweepReport, now: datetime) -> None:
    """C. Terminate runs parked on an unanswered question past its 24-hour TTL.

    Cancelled, not failed, and crucially with NO credit consumed: the run never reached
    admission, so there is nothing to refund and nothing to charge.
    """
    now_iso = now.isoformat()

    def _query() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        query = (
            admin_firestore()
            .collection_group(F.SUBCOLLECTION)
            .where(F.STATE, "==", F.STATE_AWAITING_CLARIFICATION)
            .where(F.PENDING_QUESTION_EXPIRES_AT, "<=", now_iso)
            .limit(CLARIFICATION_LIMIT)
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            try:
                uid = snap.reference.parent.parent.id
            except Exception:
                continue
            rows.append((uid, str(data.get(F.RUN_ID, snap.id))))
        return rows

    for uid, run_id in await asyncio.to_thread(_query):
        try:
            expired = await _expire_clarification(uid, run_id, now_iso)
        except Exception as exc:
            report.errors += 1
            logger.error(
                "research.sweep: clarification expiry failed",
                {"run_id": run_id, "error": str(exc)},
            )
            continue
        if expired:
            report.clarifications_expired += 1


async def _expire_clarification(uid: str, run_id: str, now_iso: str) -> bool:
    def _run() -> bool:
        db = admin_firestore()
        run_ref = store._run_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> bool:
            snap = run_ref.get(transaction=txn)
            if not snap.exists:
                return False
            run = snap.to_dict() or {}
            if run.get(F.STATE) != F.STATE_AWAITING_CLARIFICATION:
                return False
            expires = str(run.get(F.PENDING_QUESTION_EXPIRES_AT, ""))
            if not expires or expires > now_iso:
                return False
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            txn.update(
                run_ref,
                {
                    F.STATE: F.STATE_CANCELLED,
                    F.FAILURE_CODE: F.FAIL_CLARIFICATION_TIMEOUT,
                    F.PENDING_QUESTION: {},
                    F.PENDING_QUESTION_EXPIRES_AT: "",
                    F.STATE_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                },
            )
            store._audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="clarification_expired",
                occurred_at=now_iso,
                expires_at=str(run.get(F.EXPIRES_AT, "")),
                prior_state=F.STATE_AWAITING_CLARIFICATION,
                next_state=F.STATE_CANCELLED,
                reason_code=F.FAIL_CLARIFICATION_TIMEOUT,
            )
            return True

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def _pass_stuck_fanout(report: SweepReport, now: datetime) -> None:
    """D. Rescue a wave whose child will never complete.

    Collapsing ``expected`` down to ``completed`` and claiming the join is what turns a
    permanently dead child into a smaller corroboration set rather than a hung run. The
    abandoned sources are recorded as extraction gaps so the brief stays honest about
    what it could not read.
    """
    now_iso = now.isoformat()

    def _query() -> list[tuple[str, str, str, dict[str, Any]]]:
        rows: list[tuple[str, str, str, dict[str, Any]]] = []
        query = (
            admin_firestore()
            .collection_group(F.COORD_SUBCOLLECTION)
            .where(F.COORD_JOIN_CLAIMED, "==", False)
            .where(F.COORD_DEADLINE_AT, "<=", now_iso)
            .limit(COORD_LIMIT)
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            try:
                run_ref = snap.reference.parent.parent
                uid = run_ref.parent.parent.id
            except Exception:
                continue
            rows.append((uid, run_ref.id, snap.id, data))
        return rows

    for uid, run_id, wave_id, _coord in await asyncio.to_thread(_query):
        try:
            # The queried snapshot is only a candidate; _collapse_wave re-reads and
            # re-checks everything under its own transaction.
            join_job_id = await _collapse_wave(uid, run_id, wave_id, now_iso)
        except Exception as exc:
            report.errors += 1
            logger.error(
                "research.sweep: fan-out collapse failed",
                {"run_id": run_id, "wave": wave_id, "error": str(exc)},
            )
            continue
        if join_job_id:
            report.coords_collapsed += 1
            # Deliver the join now. The whole point of collapsing a stuck wave is to
            # unstick it, and leaving the join for the NEXT sweep meant a wave rescued
            # at minute 4 did not move until minute 9, against a run whose entire wall
            # clock is 240 seconds.
            try:
                await tasks_mod.dispatch_job(uid, join_job_id)
            except Exception as exc:
                logger.warn(
                    "research.sweep: collapsed join dispatch failed, left due",
                    {"run_id": run_id, "stage_id": join_job_id, "error": str(exc)},
                )


async def _collapse_wave(uid: str, run_id: str, wave_id: str, now_iso: str) -> str:
    """Collapse one stuck wave and return the join job id it created, or "".

    The id has to travel out so the caller can deliver it; it is created inside this
    transaction and nothing else knows it exists.
    """

    def _run() -> str:
        db = admin_firestore()
        run_ref = store._run_ref(uid, run_id)
        coord_ref = store._coord_ref(uid, run_id, wave_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> str:
            coord_snap = coord_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            if not coord_snap.exists or not run_snap.exists:
                return ""
            current = coord_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            if current.get(F.COORD_JOIN_CLAIMED):
                return ""  # a real last child won the race after all
            if str(current.get(F.COORD_DEADLINE_AT, "")) > now_iso:
                return ""
            if store._blocked_reason(run):
                return ""  # a cancelled wave must never resurrect itself

            completed = int(current.get(F.COORD_COMPLETED, 0))
            expected = int(current.get(F.COORD_EXPECTED, 0))
            wave = int(current.get(F.WAVE, 0))
            join_job_id = str(
                current.get(F.COORD_JOIN_JOB_ID)
                or store.stage_id_for(F.STAGE_READ_JOIN, run_id, wave)
            )
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            expires_at = str(run.get(F.EXPIRES_AT, ""))

            txn.update(
                coord_ref,
                {
                    F.COORD_EXPECTED: completed,
                    F.COORD_JOIN_CLAIMED: True,
                    "collapsed_at": now_iso,
                    "abandoned_children": max(0, expected - completed),
                    F.UPDATED_AT: now_iso,
                },
            )
            store._create_job_triplet(
                txn,
                uid=uid,
                run_id=run_id,
                stage_id=join_job_id,
                stage_kind=F.STAGE_READ_JOIN,
                wave=wave,
                ordinal="0",
                payload={
                    "plan_version": int(run.get(F.ADMITTED_PLAN_VERSION, 0)),
                    "collapsed": True,
                    "gap_reason": F.FAIL_EXTRACTION_FAILED,
                },
                now_iso=now_iso,
                expires_at=expires_at,
                correlation_id=str(run.get(F.CORRELATION_ID, "")),
                causation_id=wave_id,
            )
            txn.update(run_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now_iso})
            store._audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="fanout_collapsed",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=join_job_id,
                stage_kind=F.STAGE_READ_JOIN,
                reason_code=F.FAIL_EXTRACTION_FAILED,
                units={"expected": expected, "completed": completed},
            )
            return join_job_id

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def _pass_project_receipts(report: SweepReport) -> None:
    """F. Close project-wallet reservations whose stage died holding them.

    This is the crash half of "every exit after reservation settles or releases it". The
    engine settles in a finally, which covers every way a stage can END; it cannot cover a
    process that stops existing. The receipt carries its own day, amount and deadline
    precisely so this pass can close it later without guessing any of the three.
    """
    report.project_receipts_settled = await ledger_mod.sweep_project_receipts(
        limit=PROJECT_RECEIPT_LIMIT
    )


async def _pass_deletions(report: SweepReport) -> None:
    """E. Advance every deletion receipt that still has work to do."""
    for uid, run_id in await deletion_mod.pending_deletions(limit=DELETION_LIMIT):
        try:
            progress = await deletion_mod.drain_deletion(uid, run_id)
        except Exception as exc:
            report.errors += 1
            logger.error(
                "research.sweep: deletion drain failed",
                {"run_id": run_id, "error": str(exc)},
            )
            continue
        report.deletions_advanced += 1
        if progress.complete:
            report.deletions_completed += 1
