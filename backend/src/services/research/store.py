"""Durable state for one research run. Firestore transactions, leases, and the join.

This is the research analogue of ``services/meetings/store.py`` and follows its shape
deliberately: the same ``asyncio.to_thread`` + ``@transactional`` idiom, the same
create-only primitive, the same hashed-lease-token discipline, the same audit envelope.
It does NOT import that package. The two engines share a pattern, not code, so a change
to meeting synthesis can never alter research durability.

The one invariant everything else rests on: a stage's state transition, its budget
commit, the next job document and the next outbox row all commit in ONE transaction.
If that transaction commits, the work is durably recorded and the next step is durably
scheduled. If it does not, nothing happened. There is no window in which a run has
advanced but forgotten what to do next.

Replay safety comes from three independent layers, strongest first:

1. The lease token. A replayed advance carries a token that no longer matches the stage
   document, so it cannot write at all.
2. ``create()`` on deterministic ids. Even a replay that somehow held a valid lease
   collides on the job, outbox and audit documents simultaneously.
3. Terminal states are absorbing. A late task on a finished run does no work and reports
   success, so Cloud Tasks stops retrying it.

Timestamps are ISO-8601 STRINGS throughout, matching the meetings engine, because every
freshness and deadline comparison here is then a plain lexicographic compare that works
identically in Firestore range queries and in Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from .budget import CLARIFICATION_TTL_S, Preset, RunBudget, budget_for
from .stages.base import StageResult, StageResultKind

# Extra head-room on top of the stage's own wall clock before another worker may steal
# the lease. Sized so a stage that is merely slow is never stolen from mid-flight, while
# a stage whose process died is recoverable within one sweeper interval.
LEASE_MARGIN_S = 120

# How many attempts one stage gets before it goes terminal. Matches the meetings engine's
# max_attempts rather than inventing a second retry philosophy.
STAGE_ATTEMPT_CAP = 2


# --- references ------------------------------------------------------------------


def _runs_ref(uid: str) -> Any:
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.SUBCOLLECTION)
    )


def _run_ref(uid: str, run_id: str) -> Any:
    return _runs_ref(uid).document(run_id)


def _ledger_ref(uid: str, run_id: str) -> Any:
    return _run_ref(uid, run_id).collection(F.LEDGER_SUBCOLLECTION).document(F.LEDGER_BUDGET_DOC)


def _coord_ref(uid: str, run_id: str, wave_id: str) -> Any:
    return _run_ref(uid, run_id).collection(F.COORD_SUBCOLLECTION).document(wave_id)


def _stages_ref(uid: str, run_id: str) -> Any:
    return _run_ref(uid, run_id).collection(F.STAGES_SUBCOLLECTION)


def _plans_ref(uid: str, run_id: str) -> Any:
    return _run_ref(uid, run_id).collection(F.PLANS_SUBCOLLECTION)


def _audit_ref(uid: str, run_id: str) -> Any:
    return _run_ref(uid, run_id).collection(F.AUDIT_SUBCOLLECTION)


def _sub_ref(uid: str, run_id: str, subcollection: str) -> Any:
    """Any run-owned subcollection by name.

    Generic rather than one helper per collection, because the two callers that matter
    are both name-driven: the deletion drain walks RUN_OWNED_SUBCOLLECTIONS, and advance
    writes whatever a stage put in StageResult.documents.
    """
    return _run_ref(uid, run_id).collection(subcollection)


def _jobs_ref(uid: str) -> Any:
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.JOBS_SUBCOLLECTION)
    )


def _outbox_ref(uid: str) -> Any:
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.JOB_OUTBOX_SUBCOLLECTION)
    )


def _deletions_ref(uid: str) -> Any:
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.DELETIONS_SUBCOLLECTION)
    )


def _usage_ref(uid: str, day: str) -> Any:
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.USAGE_SUBCOLLECTION)
        .document(f"{F.USAGE_DOC_PREFIX}{day}")
    )


def _project_budget_ref(day: str) -> Any:
    return admin_firestore().collection(F.PROJECT_BUDGET_COLLECTION).document(day)


def _project_receipt_ref(day: str, receipt_id: str) -> Any:
    """One stage attempt's project-spend receipt, under the day it was reserved against.

    Nested under the day document rather than keyed by run, because the settlement has to
    find the SAME day it credited. A receipt read back at 00:01 still names the day it was
    written at 23:59, which is the whole reason the aggregate counter could not be settled
    correctly on its own.
    """
    return (
        _project_budget_ref(day)
        .collection(F.PROJECT_RECEIPTS_SUBCOLLECTION)
        .document(receipt_id)
    )


def project_receipt_id(stage_id: str, attempt: int) -> str:
    """Idempotency key for a project-spend reservation: the stage AND its attempt.

    Keyed on the attempt as well as the stage because a redelivery of the same attempt
    must reuse its reservation, while a genuine retry is new work that will spend again.
    """
    return f"{stage_id}-a{int(attempt)}"


# --- errors ----------------------------------------------------------------------


class ResearchIntegrityError(RuntimeError):
    """A durable invariant was violated. Carries a stable code, never a vendor string."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- identity --------------------------------------------------------------------


def run_id_for(uid: str, client_run_id: str) -> str:
    """Deterministic run id. The same client_run_id always names the same run.

    This is the outermost idempotency layer: a retried create collides on the run
    document itself, so a double-submitted chat turn or a redelivered voice tool call
    returns the existing run instead of starting, and charging for, a second one.
    """
    return hashlib.sha256(f"{uid}:{client_run_id}".encode()).hexdigest()[:24]


def stage_id_for(stage_kind: str, run_id: str, wave: int, ordinal: str = "0") -> str:
    """Deterministic stage id, which is also the job id and the outbox row id.

    One identity across three documents means a replayed advance collides on all three
    at once. ``ordinal`` is the source id for a fan-out child and "0" otherwise, so
    children are unique without a shared counter to contend on.
    """
    return f"{stage_kind}-{run_id}-w{wave}-{ordinal}"


def _day_key(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def _actor_hash(actor_identity: str) -> str:
    return hashlib.sha256(actor_identity.encode("utf-8")).hexdigest()


def _txn_create(txn: Any, ref: Any, value: dict[str, Any]) -> None:
    """Create-only write inside a transaction.

    ``create`` is what makes a replayed advance collide instead of silently overwriting
    a live document. Older in-repo fakes expose only ``set``; falling back keeps those
    usable, at the cost of losing the collision guard in that environment only.
    """
    create = getattr(txn, "create", None)
    if callable(create):
        create(ref, value)
    else:
        txn.set(ref, value)


# --- audit -----------------------------------------------------------------------


def _audit_event(
    txn: Any,
    *,
    uid: str,
    run_id: str,
    sequence: int,
    event_type: str,
    occurred_at: str,
    expires_at: str,
    actor_type: str = "server",
    actor_identity: str = "juno-backend",
    stage_id: str = "",
    stage_kind: str = "",
    attempt: int = 0,
    lease_token: str = "",
    prior_state: str = "",
    next_state: str = "",
    reason_code: str = "",
    plan_version: int = 0,
    units: dict[str, Any] | None = None,
    correlation_id: str = "",
) -> str:
    """Append one immutable audit record, inside the caller's transaction.

    The raw lease token is NEVER persisted, only its sha256. An audit trail that stored
    live tokens would hand anyone with read access the ability to forge a lease match
    and write to a stage another worker owns.
    """
    event_id = uuid.uuid4().hex
    envelope: dict[str, Any] = {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "recorded_at": datetime.now(UTC).isoformat(),
        "actor_type": actor_type,
        "actor_identity_hash": _actor_hash(actor_identity),
        F.RUN_ID: run_id,
        F.STAGE_ID: stage_id,
        F.STAGE_KIND: stage_kind,
        "attempt": attempt,
        F.LEASE_TOKEN_HASH: _actor_hash(lease_token) if lease_token else "",
        "prior_state": prior_state,
        "next_state": next_state,
        "reason_code": reason_code,
        "plan_version": plan_version,
        # Unit counts only. Never request text, page body, excerpt or claim text.
        "units": dict(units or {}),
        "schema_version": F.AUDIT_SCHEMA_VERSION,
        "policy_version": F.POLICY_TABLE_VERSION,
        F.CORRELATION_ID: correlation_id or event_id,
        F.EXPIRES_AT: expires_at,
    }
    _txn_create(txn, _audit_ref(uid, run_id).document(event_id), envelope)
    return event_id


# --- handles ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunCreation:
    """Result of create_run. ``replayed`` distinguishes 200 from 202 at the API later."""

    run_id: str
    state: str
    replayed: bool
    first_stage_id: str = ""


@dataclass(frozen=True)
class AdmissionResult:
    """Result of the one transaction that debits a research credit."""

    admitted: bool
    run_id: str
    state: str
    # Populated on refusal: a stable machine code the desktop already knows how to parse.
    code: str = ""
    credits_used: int = 0
    credits_remaining: int = 0
    first_stage_id: str = ""
    replayed: bool = False


@dataclass(frozen=True)
class PendingAutoAdmission:
    """A planned run whose durable auto-start marker has not been consumed yet."""

    uid: str
    run_id: str
    plan_version: int
    preset: str
    correlation_id: str = ""


@dataclass(frozen=True)
class AutoAdmissionRefusal:
    """State left after recording an automatic admission refusal."""

    state: str
    created_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageLease:
    """Proof that this process, and only this process, owns one stage right now.

    ``lease_token`` lives in memory and is never persisted; only its hash reaches
    Firestore. Every later write re-checks the hash, so a worker whose lease expired and
    was stolen mid-stage cannot commit stale work over the new owner's.
    """

    uid: str
    run_id: str
    stage_id: str
    stage_kind: str
    wave: int
    ordinal: str
    attempt: int
    lease_token: str
    stage_deadline_at: str
    admitted_plan_version: int
    preset: str
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    stolen: bool = False

    @property
    def budget(self) -> RunBudget:
        return budget_for(Preset(self.preset) if self.preset else Preset.QUICK)


@dataclass(frozen=True)
class AdvanceOutcome:
    """What one advance transaction actually did."""

    committed: bool
    # "advanced" | "lost_lease" | "cancelled" | "terminal" | "duplicate"
    disposition: str
    state: str = ""
    created_job_ids: tuple[str, ...] = ()
    released_units: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ChildOutcome:
    """What one child-completion transaction did, and what it left to deliver.

    ``created_job_ids`` carries the join job when this child happened to be the last one.
    It is a tuple for symmetry with AdvanceOutcome, so the engine has one delivery path
    rather than two shapes to special-case.
    """

    # "duplicate" | "abandoned" | "ok" | "join_claimed"
    disposition: str
    created_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailOutcome:
    """What one fail_stage transaction did.

    ``created_job_ids`` carries the notification job when the failure was terminal. Like
    the join, it is created inside the transaction and would otherwise be invisible to
    the caller that has to deliver it.

    ``rearmed_stage_id`` and ``rearmed_due_at`` carry a scheduled RETRY back out, for the
    same reason and to fix a worse version of the same bug. The retry was durable in the
    outbox and invisible to the caller, so nothing delivered it and it waited for the
    five-minute recovery sweep inside a run whose entire wall clock is 240 seconds. A
    retry that cannot run before its own run expires is not a retry.
    """

    # "lost_lease" | "retry_scheduled" | "delivery_failed" | STATE_PARTIAL | STATE_FAILED
    outcome: str
    created_job_ids: tuple[str, ...] = ()
    rearmed_stage_id: str = ""
    rearmed_due_at: str = ""


# Written onto a job and its outbox row the moment the stage reaches a terminal state.
# The outbox sweep selects on `dispatch_due_at <= now`, so a completed row has to leave
# that range or it competes for the query's limit forever and can mask newer pending
# work indefinitely. An empty string will NOT do: "" <= now_iso is true for strings.
DISPATCH_NEVER = "9999-12-31T00:00:00+00:00"


def _deletion_active(receipt: dict[str, Any] | None) -> bool:
    """True when a deletion receipt exists and still has work to do.

    The ONLY authority a ``delete_run`` stage has. Read transactionally alongside the run
    so a receipt that completed between the query and the write cannot license a second
    drain of a subtree that is already gone.
    """
    if not receipt:
        return False
    return str(receipt.get(F.DELETION_RECEIPT_STATE) or "") in (
        F.DELETION_PENDING,
        F.DELETION_RUNNING,
    )


def _read_deletion_receipt(txn: Any, uid: str, run_id: str) -> dict[str, Any]:
    """This run's deletion receipt, read inside the caller's transaction, or {}."""
    snap = _deletions_ref(uid).document(run_id).get(transaction=txn)
    return (snap.to_dict() or {}) if snap.exists else {}


async def deletion_active(uid: str, run_id: str) -> bool:
    """Non-transactional read of the same authority, for the admission gate.

    Advisory here and binding inside ``claim_stage`` and ``advance``, which re-read it
    under their own transaction. A stale read costs one refused admission, never a
    deletion that ran without a receipt.
    """

    def _run() -> bool:
        snap = _deletions_ref(uid).document(run_id).get()
        return _deletion_active((snap.to_dict() or {}) if snap.exists else {})

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        # Fail closed: no readable receipt means no authority to delete.
        logger.warn(
            "research.store: deletion receipt read failed, treating as inactive",
            {"run_id": run_id, "error": str(exc)},
        )
        return False


def _lease_matches(stage: dict[str, Any], lease: StageLease) -> bool:
    """Fence check. All four conditions, every time, before any write.

    Attempt is part of the fence because a stolen-and-restolen stage would otherwise let
    an ancient worker match on state alone.
    """
    return (
        stage.get(F.STAGE_STATE) == F.STAGE_LEASED
        and int(stage.get(F.STAGE_ATTEMPT, -1)) == lease.attempt
        and stage.get(F.LEASE_TOKEN_HASH) == _actor_hash(lease.lease_token)
    )


def _blocked_reason(run: dict[str, Any]) -> str:
    """Why this run refuses new research work, or "" when it accepts it."""
    if run.get(F.CANCEL_REQUESTED_AT):
        return "cancelled"
    if run.get(F.DELETION_STATE):
        return "deleting"
    if run.get(F.STATE) in F.TERMINAL_STATES:
        return "terminal"
    return ""


# --- job + outbox rows -----------------------------------------------------------


def _job_doc(
    *,
    uid: str,
    run_id: str,
    stage_id: str,
    stage_kind: str,
    wave: int,
    ordinal: str,
    payload: dict[str, Any],
    now_iso: str,
    expires_at: str,
    correlation_id: str,
    causation_id: str,
) -> dict[str, Any]:
    return {
        F.JOB_ID: stage_id,
        F.JOB_KIND: stage_kind,
        F.JOB_USER_ID: uid,
        F.JOB_RUN_ID: run_id,
        F.STAGE_ID: stage_id,
        F.STAGE_KIND: stage_kind,
        F.WAVE: wave,
        "ordinal": ordinal,
        "payload": dict(payload),
        F.JOB_STATE: F.STAGE_PENDING,
        F.STAGE_ATTEMPT: 0,
        F.LEASE_TOKEN_HASH: "",
        F.STAGE_DEADLINE_AT: "",
        F.DISPATCH_DUE_AT: now_iso,
        F.TASK_NAME: "",
        F.LAST_ERROR_CODE: "",
        F.CREATED_AT: now_iso,
        F.UPDATED_AT: now_iso,
        F.CORRELATION_ID: correlation_id,
        F.CAUSATION_ID: causation_id,
        F.EXPIRES_AT: expires_at,
    }


def _outbox_row(
    *,
    uid: str,
    run_id: str,
    stage_id: str,
    now_iso: str,
    expires_at: str,
    correlation_id: str,
) -> dict[str, Any]:
    return {
        F.OUTBOX_ID: stage_id,
        F.JOB_ID: stage_id,
        F.JOB_USER_ID: uid,
        F.JOB_RUN_ID: run_id,
        F.OUTBOX_STATE: F.OUTBOX_PENDING,
        F.DISPATCH_DUE_AT: now_iso,
        F.DISPATCH_ATTEMPTS: 0,
        F.TASK_NAME: "",
        F.LAST_DISPATCHED_AT: "",
        F.CREATED_AT: now_iso,
        F.UPDATED_AT: now_iso,
        F.CORRELATION_ID: correlation_id,
        F.EXPIRES_AT: expires_at,
    }


def _stage_doc(
    *,
    run_id: str,
    stage_id: str,
    stage_kind: str,
    wave: int,
    ordinal: str,
    now_iso: str,
    expires_at: str,
) -> dict[str, Any]:
    return {
        F.STAGE_ID: stage_id,
        F.STAGE_KIND: stage_kind,
        F.JOB_RUN_ID: run_id,
        F.WAVE: wave,
        "ordinal": ordinal,
        F.STAGE_STATE: F.STAGE_PENDING,
        F.STAGE_ATTEMPT: 0,
        F.LEASE_TOKEN_HASH: "",
        F.STAGE_DEADLINE_AT: "",
        F.CREATED_AT: now_iso,
        F.UPDATED_AT: now_iso,
        F.EXPIRES_AT: expires_at,
    }


def create_terminal_notify_job(
    txn: Any,
    *,
    uid: str,
    run_id: str,
    terminal_state: str,
    wave: int,
    now_iso: str,
    expires_at: str,
    correlation_id: str,
    causation_id: str,
    plan_version: int = 0,
) -> str:
    """Attach a notification to a run that went terminal WITHOUT reaching finalize.

    ``finalize`` exists so the terminal state and its delivery intent commit together,
    and it is the only stage that creates a notify job. Three other paths reach a
    terminal state and bypass it entirely: the attempt-cap branch of ``fail_stage``, the
    sweeper's stale-stage recovery, and clarification expiry. A run that ends down any of
    those is finished, absorbing, and silent: terminal states refuse later research work,
    so nothing retries and the user is simply never told.

    The ordinal is ``terminal`` rather than ``0`` so this can never collide with the job
    finalize would create for the same wave. ``_txn_create`` raises on collision, and a
    collision here would abort the whole termination transaction, which is a far worse
    outcome than the duplicate it is guarding against. A genuine duplicate is harmless:
    notify_result's dedup_key carries state_revision, so both resolve to one toast.
    """
    stage_id = stage_id_for(F.STAGE_NOTIFY_RESULT, run_id, wave, "terminal")
    _create_job_triplet(
        txn,
        uid=uid,
        run_id=run_id,
        stage_id=stage_id,
        stage_kind=F.STAGE_NOTIFY_RESULT,
        wave=wave,
        ordinal="terminal",
        payload={"terminal_state": terminal_state, "plan_version": plan_version},
        now_iso=now_iso,
        expires_at=expires_at,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    return stage_id


def _retire_outbox(txn: Any, uid: str, stage_id: str, now_iso: str) -> None:
    """Take a finished stage's outbox row out of the sweep's selection range.

    ``dispatch_pending`` queries ``dispatch_due_at <= now`` and only then filters the
    state in Python, so a completed row still matches the query and still consumes one of
    the limited slots. Enough of them and the oldest completed rows permanently mask
    newer pending work, and the newer stage is never delivered at all.

    Pushing due_at out to DISPATCH_NEVER removes the row from the range at the source,
    which needs no extra composite index. The state filter in dispatch_pending stays as
    defence for rows written before this existed.
    """
    txn.update(
        _outbox_ref(uid).document(stage_id),
        {
            F.OUTBOX_STATE: F.OUTBOX_DONE,
            F.DISPATCH_DUE_AT: DISPATCH_NEVER,
            F.UPDATED_AT: now_iso,
        },
    )


def _create_job_triplet(
    txn: Any,
    *,
    uid: str,
    run_id: str,
    stage_id: str,
    stage_kind: str,
    wave: int,
    ordinal: str,
    payload: dict[str, Any],
    now_iso: str,
    expires_at: str,
    correlation_id: str,
    causation_id: str,
) -> None:
    """Create the stage, job and outbox documents for one future unit of work.

    All three use create(), all three share one id. That is the second replay layer:
    even a caller holding a valid lease cannot create the same next step twice.
    """
    _txn_create(
        txn,
        _stages_ref(uid, run_id).document(stage_id),
        _stage_doc(
            run_id=run_id,
            stage_id=stage_id,
            stage_kind=stage_kind,
            wave=wave,
            ordinal=ordinal,
            now_iso=now_iso,
            expires_at=expires_at,
        ),
    )
    _txn_create(
        txn,
        _jobs_ref(uid).document(stage_id),
        _job_doc(
            uid=uid,
            run_id=run_id,
            stage_id=stage_id,
            stage_kind=stage_kind,
            wave=wave,
            ordinal=ordinal,
            payload=payload,
            now_iso=now_iso,
            expires_at=expires_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
        ),
    )
    _txn_create(
        txn,
        _outbox_ref(uid).document(stage_id),
        _outbox_row(
            uid=uid,
            run_id=run_id,
            stage_id=stage_id,
            now_iso=now_iso,
            expires_at=expires_at,
            correlation_id=correlation_id,
        ),
    )


# --- run creation ----------------------------------------------------------------


async def create_run(
    uid: str,
    *,
    client_run_id: str,
    request_text: str,
    preset: str = str(Preset.QUICK),
    origin_surface: str = "dashboard",
    correlation_id: str = "",
    delivery: dict[str, str] | None = None,
) -> RunCreation:
    """Create a draft run and its scope-check job, idempotently. Debits NO credit.

    ``delivery`` binds a Notion destination at creation, from the user's own
    spoken words, and is immutable afterwards: changing the destination is a
    new run, never an edit. finalize routes a run holding it through the
    notion_deliver stage instead of straight to notify_result.

    Draft creation deliberately runs entitlement-free and credit-free. It exists so the
    user gets an acknowledgement in under a second and so the scope check itself
    survives a process crash. The expensive work cannot exist yet: only the later
    admission transaction may create a search job.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    expires_at = (now + timedelta(days=F.RETENTION_DAYS)).isoformat()
    run_id = run_id_for(uid, client_run_id)
    first_stage_id = stage_id_for(F.STAGE_CLASSIFY_PLAN, run_id, 0)

    def _run() -> RunCreation:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> RunCreation:
            snap = run_ref.get(transaction=txn)
            if snap.exists:
                existing = snap.to_dict() or {}
                # The idempotent path. No second classifier job, no second run, and
                # crucially no second credit: admission has its own receipt.
                return RunCreation(
                    run_id=run_id,
                    state=str(existing.get(F.STATE, F.STATE_PLANNING)),
                    replayed=True,
                    first_stage_id=first_stage_id,
                )

            txn.set(
                run_ref,
                {
                    F.RUN_ID: run_id,
                    F.CLIENT_RUN_ID: client_run_id,
                    F.REQUEST_TEXT: request_text,
                    F.PRESET: preset,
                    F.ORIGIN_SURFACE: origin_surface,
                    **({F.DELIVERY: dict(delivery)} if delivery else {}),
                    F.REQUEST_REVISION: 0,
                    F.CURRENT_PLAN_VERSION: 0,
                    F.ADMITTED_PLAN_VERSION: 0,
                    F.AUTO_ADMIT_REQUESTED: False,
                    F.CLARIFICATION_ANSWERS: [],
                    F.CLARIFICATION_ROUNDS: 0,
                    F.STATE: F.STATE_PLANNING,
                    F.PROCESSING_STAGE: F.STAGE_CLASSIFY_PLAN,
                    F.STATE_REVISION: 0,
                    F.AUDIT_SEQUENCE: 1,
                    F.FAILURE_CODE: "",
                    F.SOURCE_COUNT: 0,
                    F.CLAIM_COUNT: 0,
                    F.CREATED_AT: now_iso,
                    F.UPDATED_AT: now_iso,
                    F.EXPIRES_AT: expires_at,
                    F.CORRELATION_ID: correlation_id,
                },
            )
            # The hot counter gets its own document from the start, so a status read of
            # the run never contends with a budget write from a running stage.
            txn.set(
                _ledger_ref(uid, run_id),
                {
                    F.LEDGER_USED: {},
                    F.LEDGER_RESERVED: {},
                    F.LEDGER_RESERVATIONS: {},
                    F.LEDGER_COST_MICROUSD: 0,
                    F.LEDGER_UPDATED_AT: now_iso,
                    F.EXPIRES_AT: expires_at,
                },
            )
            _create_job_triplet(
                txn,
                uid=uid,
                run_id=run_id,
                stage_id=first_stage_id,
                stage_kind=F.STAGE_CLASSIFY_PLAN,
                wave=0,
                ordinal="0",
                payload={},
                now_iso=now_iso,
                expires_at=expires_at,
                correlation_id=correlation_id,
                causation_id=client_run_id,
            )
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=1,
                event_type="run_created",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=first_stage_id,
                stage_kind=F.STAGE_CLASSIFY_PLAN,
                next_state=F.STATE_PLANNING,
                reason_code="draft_created",
                correlation_id=correlation_id,
            )
            return RunCreation(
                run_id=run_id,
                state=F.STATE_PLANNING,
                replayed=False,
                first_stage_id=first_stage_id,
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def get_run(
    uid: str, run_id: str, *, include_deleted: bool = False
) -> dict[str, Any] | None:
    """Read one run. Hidden and deleting runs are invisible unless asked for.

    ``include_deleted`` exists for exactly one caller: the delete_run stage, whose job is
    to finish the drain the DELETION_STATE flag marks. Hiding the run from it would mean
    the deletion driver could never read the run it is deleting.
    """

    def _run() -> dict[str, Any] | None:
        snap = _run_ref(uid, run_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        # A run the user deleted must never reappear, even before the drain finishes.
        if not include_deleted and (data.get(F.HIDDEN_AT) or data.get(F.DELETION_STATE)):
            return None
        return data

    return await asyncio.to_thread(_run)


async def get_coordinator(
    uid: str, run_id: str, wave_id: str
) -> dict[str, Any] | None:
    """One fan-out coordinator. READ ONLY, and a hint rather than an authority.

    Exists so a child can cheaply ask "has my wave already joined?" before spending on a
    provider. The binding check stays inside ``complete_child``, which reads the same
    document in the transaction that increments the counter; this read is deliberately
    outside any transaction and may be stale.
    """

    def _run() -> dict[str, Any] | None:
        snap = _coord_ref(uid, run_id, wave_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    return await asyncio.to_thread(_run)


async def list_runs(uid: str, *, limit: int = F.LIST_LIMIT) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        query = _runs_ref(uid).order_by(F.CREATED_AT, direction="DESCENDING").limit(limit)
        rows: list[dict[str, Any]] = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get(F.HIDDEN_AT) or data.get(F.DELETION_STATE):
                continue
            rows.append(data)
        return rows

    return await asyncio.to_thread(_run)


async def count_active_runs(uid: str) -> int:
    """Active runs, for the one-at-a-time admission bound."""

    def _run() -> int:
        total = 0
        for snap in _runs_ref(uid).stream():
            data = snap.to_dict() or {}
            if data.get(F.HIDDEN_AT) or data.get(F.DELETION_STATE):
                continue
            if data.get(F.STATE) in F.ACTIVE_STATES:
                total += 1
        return total

    return await asyncio.to_thread(_run)


# --- plans -----------------------------------------------------------------------


async def write_plan(
    uid: str,
    run_id: str,
    *,
    plan: dict[str, Any],
    correlation_id: str = "",
) -> int:
    """Persist one generated interpretation as a NEW version. Never mutates a prior one.

    Versioning rather than overwriting is what makes "what did the user actually
    confirm" answerable after the fact: admission pins a version, and a clarification
    answer produces the next one alongside it rather than editing history.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> int:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> int:
            run_snap = run_ref.get(transaction=txn)
            if not run_snap.exists:
                raise ResearchIntegrityError("run_not_found", "run does not exist")
            run = run_snap.to_dict() or {}
            blocked = _blocked_reason(run)
            if blocked:
                raise ResearchIntegrityError(f"run_{blocked}", f"run is {blocked}")

            version = int(run.get(F.CURRENT_PLAN_VERSION, 0)) + 1
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            expires_at = str(run.get(F.EXPIRES_AT, ""))
            body = dict(plan)
            body["plan_version"] = version
            body["request_revision"] = int(run.get(F.REQUEST_REVISION, 0))
            body[F.CREATED_AT] = now_iso
            body[F.EXPIRES_AT] = expires_at

            _txn_create(txn, _plans_ref(uid, run_id).document(str(version)), body)
            txn.update(
                run_ref,
                {
                    F.CURRENT_PLAN_VERSION: version,
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="plan_written",
                occurred_at=now_iso,
                expires_at=expires_at,
                plan_version=version,
                reason_code="plan_version_created",
                correlation_id=correlation_id,
            )
            return version

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def list_documents(
    uid: str, run_id: str, subcollection: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Read every document in one run-owned subcollection. READ ONLY.

    Stages are forbidden from making Firestore STATE TRANSITIONS, not from reading. The
    distinction matters here: ``verify`` has to see all twelve sources' candidate claims
    at once to merge them, and threading that through a job payload would put a
    multi-kilobyte evidence blob inside a Cloud Task body. What stages still cannot do is
    write a transition, which is why this returns plain dicts and no reference.
    """

    def _run() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for snap in _sub_ref(uid, run_id, subcollection).limit(limit).stream():
            data = snap.to_dict() or {}
            data.setdefault("doc_id", snap.id)
            out.append(data)
        return out

    return await asyncio.to_thread(_run)


async def get_plan(uid: str, run_id: str, version: int) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        snap = _plans_ref(uid, run_id).document(str(version)).get()
        return (snap.to_dict() or {}) if snap.exists else None

    return await asyncio.to_thread(_run)


# --- clarification ---------------------------------------------------------------


async def answer_clarification(
    uid: str,
    run_id: str,
    *,
    question_id: str,
    answer: dict[str, Any],
    correlation_id: str = "",
) -> tuple[bool, str, str]:
    """Record a clarification answer and re-arm the scope check. Idempotent by design.

    Returns (accepted, state, resumed_stage_id). The stage id travels out so the caller
    can deliver it: without that, answering a question left the resumed scope check
    sitting in the outbox until the next five-minute sweep, which for a user who just
    typed an answer is indistinguishable from the app ignoring them.

    The pending question id is cleared on use, so a replayed answer fails the match and
    returns the current state instead of resuming twice. Even if that check were
    bypassed, the resume job id is deterministic and its create() would collide.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> tuple[bool, str, str]:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> tuple[bool, str, str]:
            run_snap = run_ref.get(transaction=txn)
            if not run_snap.exists:
                return False, "run_not_found", ""
            run = run_snap.to_dict() or {}
            blocked = _blocked_reason(run)
            if blocked:
                return False, str(run.get(F.STATE, blocked)), ""
            if run.get(F.STATE) != F.STATE_AWAITING_CLARIFICATION:
                return False, str(run.get(F.STATE, "")), ""
            pending = run.get(F.PENDING_QUESTION) or {}
            if str(pending.get("question_id", "")) != question_id:
                # Replay, or an answer to a question we already superseded.
                return False, str(run.get(F.STATE, "")), ""

            revision = int(run.get(F.REQUEST_REVISION, 0)) + 1
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            expires_at = str(run.get(F.EXPIRES_AT, ""))
            answers = list(run.get(F.CLARIFICATION_ANSWERS) or [])
            answers.append(
                {"question_id": question_id, "answer": answer, "answered_at": now_iso}
            )
            resume_stage_id = stage_id_for(F.STAGE_CLASSIFY_PLAN, run_id, revision)

            txn.update(
                run_ref,
                {
                    F.STATE: F.STATE_PLANNING,
                    F.PROCESSING_STAGE: F.STAGE_CLASSIFY_PLAN,
                    F.REQUEST_REVISION: revision,
                    F.CLARIFICATION_ANSWERS: answers,
                    F.PENDING_QUESTION: {},
                    F.PENDING_QUESTION_EXPIRES_AT: "",
                    F.STATE_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                },
            )
            _create_job_triplet(
                txn,
                uid=uid,
                run_id=run_id,
                stage_id=resume_stage_id,
                stage_kind=F.STAGE_CLASSIFY_PLAN,
                wave=revision,
                ordinal="0",
                payload={"resumed_from_question": question_id},
                now_iso=now_iso,
                expires_at=expires_at,
                correlation_id=correlation_id,
                causation_id=question_id,
            )
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="clarification_answered",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=resume_stage_id,
                stage_kind=F.STAGE_CLASSIFY_PLAN,
                prior_state=F.STATE_AWAITING_CLARIFICATION,
                next_state=F.STATE_PLANNING,
                reason_code="clarification_answered",
                correlation_id=correlation_id,
            )
            return True, F.STATE_PLANNING, resume_stage_id

        return _execute(transaction)

    return await asyncio.to_thread(_run)


# --- admission -------------------------------------------------------------------


async def admit_run(
    uid: str,
    run_id: str,
    *,
    plan_version: int,
    daily_credit_allowance: int,
    credit_weight: int,
    preset: str,
    correlation_id: str = "",
) -> AdmissionResult:
    """The ONE transaction that debits a research credit and starts paid work.

    Entitlement resolution happens before this call, in ``credits.py``, because it is a
    network read that must not sit inside a transaction. What lands here is the already
    resolved allowance plus the weight to debit.

    Everything that makes double-charging impossible is in this function:
      * the credit receipt is written onto the run, so a replay sees it and skips;
      * the receipt, the usage increment, the state change and the first search job all
        commit together, so a partial admission cannot exist;
      * the plan version is pinned here, so a task delayed behind a later clarification
        round is refused by every stage rather than running against a newer plan.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    day = _day_key(now)
    first_stage_id = stage_id_for(F.STAGE_SEARCH_WAVE, run_id, 1)

    def _run() -> AdmissionResult:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        usage_ref = _usage_ref(uid, day)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> AdmissionResult:
            run_snap = run_ref.get(transaction=txn)
            usage_snap = usage_ref.get(transaction=txn)
            if not run_snap.exists:
                return AdmissionResult(False, run_id, "", code="run_not_found")
            run = run_snap.to_dict() or {}
            blocked = _blocked_reason(run)
            if blocked:
                return AdmissionResult(
                    False, run_id, str(run.get(F.STATE, "")), code=f"run_{blocked}"
                )

            # Idempotent replay. The receipt is the proof a credit was already taken for
            # this exact run, and it is keyed on a deterministic run_id.
            if run.get(F.CREDIT_RECEIPT):
                return AdmissionResult(
                    admitted=True,
                    run_id=run_id,
                    state=str(run.get(F.STATE, "")),
                    credits_used=int(run.get(F.CREDIT_WEIGHT, 0)),
                    first_stage_id=first_stage_id,
                    replayed=True,
                )

            usage = usage_snap.to_dict() or {}
            used = int(usage.get("credits", 0))
            if used + credit_weight > daily_credit_allowance:
                return AdmissionResult(
                    admitted=False,
                    run_id=run_id,
                    state=str(run.get(F.STATE, "")),
                    code=F.RESEARCH_CAP_CODE,
                    credits_used=used,
                    credits_remaining=max(0, daily_credit_allowance - used),
                )

            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            expires_at = str(run.get(F.EXPIRES_AT, ""))
            budget = budget_for(Preset(preset) if preset else Preset.QUICK)
            deadline_at = (now + timedelta(seconds=budget.wall_clock_s)).isoformat()
            receipt = f"{run_id}:{day}:{credit_weight}"

            txn.set(
                usage_ref,
                {
                    "credits": gcloud_firestore.Increment(credit_weight),
                    "runs": gcloud_firestore.Increment(1),
                    F.UPDATED_AT: now_iso,
                },
                merge=True,
            )
            txn.update(
                run_ref,
                {
                    F.STATE: F.STATE_QUEUED,
                    F.PROCESSING_STAGE: F.STAGE_SEARCH_WAVE,
                    F.FAILURE_CODE: "",
                    F.ADMITTED_PLAN_VERSION: plan_version,
                    F.AUTO_ADMIT_REQUESTED: False,
                    F.CREDIT_RECEIPT: receipt,
                    F.CREDIT_WEIGHT: credit_weight,
                    # The active wall clock starts HERE, not at draft creation, so plan
                    # classification and any admission retry do not eat the run.
                    F.DEADLINE_AT: deadline_at,
                    F.PENDING_QUESTION: {},
                    F.PENDING_QUESTION_EXPIRES_AT: "",
                    F.STATE_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                },
            )
            _create_job_triplet(
                txn,
                uid=uid,
                run_id=run_id,
                stage_id=first_stage_id,
                stage_kind=F.STAGE_SEARCH_WAVE,
                wave=1,
                ordinal="0",
                payload={"plan_version": plan_version},
                now_iso=now_iso,
                expires_at=expires_at,
                correlation_id=correlation_id,
                causation_id=receipt,
            )
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="run_admitted",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=first_stage_id,
                stage_kind=F.STAGE_SEARCH_WAVE,
                prior_state=str(run.get(F.STATE, "")),
                next_state=F.STATE_QUEUED,
                reason_code="credit_debited",
                plan_version=plan_version,
                units={"credits": credit_weight},
                correlation_id=correlation_id,
            )
            return AdmissionResult(
                admitted=True,
                run_id=run_id,
                state=F.STATE_QUEUED,
                credits_used=used + credit_weight,
                credits_remaining=max(0, daily_credit_allowance - used - credit_weight),
                first_stage_id=first_stage_id,
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def list_pending_auto_admissions(*, limit: int = 50) -> list[PendingAutoAdmission]:
    """Return only new-contract queued runs carrying the durable auto-start marker.

    Legacy queued runs have no marker and are deliberately invisible here. That keeps a
    rollout from charging an old request its owner reasonably believed was abandoned.
    """

    def _run() -> list[PendingAutoAdmission]:
        rows: list[PendingAutoAdmission] = []
        query = (
            admin_firestore()
            .collection_group(F.SUBCOLLECTION)
            .where(F.AUTO_ADMIT_REQUESTED, "==", True)
            .limit(max(1, min(100, limit)))
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            if (
                data.get(F.STATE) != F.STATE_QUEUED
                or int(data.get(F.ADMITTED_PLAN_VERSION, 0)) > 0
            ):
                continue
            try:
                uid = snap.reference.parent.parent.id
            except Exception:
                continue
            rows.append(PendingAutoAdmission(
                uid=uid,
                run_id=str(data.get(F.RUN_ID) or snap.id),
                plan_version=int(data.get(F.CURRENT_PLAN_VERSION, 0)),
                preset=str(data.get(F.PRESET) or Preset.QUICK),
                correlation_id=str(data.get(F.CORRELATION_ID) or ""),
            ))
        return rows

    return await asyncio.to_thread(_run)


async def record_auto_admission_refusal(
    uid: str,
    run_id: str,
    *,
    error_code: str,
    terminal: bool,
    correlation_id: str = "",
) -> AutoAdmissionRefusal:
    """Persist an automatic admission refusal without inventing a second debit path."""
    now_iso = datetime.now(UTC).isoformat()

    def _run() -> AutoAdmissionRefusal:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> AutoAdmissionRefusal:
            snap = run_ref.get(transaction=txn)
            if not snap.exists:
                return AutoAdmissionRefusal("")
            run = snap.to_dict() or {}
            state = str(run.get(F.STATE) or "")
            if (
                not run.get(F.AUTO_ADMIT_REQUESTED)
                or int(run.get(F.ADMITTED_PLAN_VERSION, 0)) > 0
                or state != F.STATE_QUEUED
            ):
                return AutoAdmissionRefusal(state)

            if not terminal and str(run.get(F.FAILURE_CODE) or "") == error_code:
                return AutoAdmissionRefusal(state)

            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            next_state = F.STATE_FAILED if terminal else F.STATE_QUEUED
            updates: dict[str, Any] = {
                F.STATE: next_state,
                F.FAILURE_CODE: error_code,
                F.STATE_REVISION: gcloud_firestore.Increment(1),
                F.AUDIT_SEQUENCE: sequence,
                F.UPDATED_AT: now_iso,
            }
            created_job_ids: tuple[str, ...] = ()
            if terminal:
                updates.update({
                    F.PROCESSING_STAGE: "",
                    F.AUTO_ADMIT_REQUESTED: False,
                })
                notify_id = create_terminal_notify_job(
                    txn,
                    uid=uid,
                    run_id=run_id,
                    terminal_state=F.STATE_FAILED,
                    wave=0,
                    now_iso=now_iso,
                    expires_at=str(run.get(F.EXPIRES_AT) or ""),
                    correlation_id=correlation_id,
                    causation_id=f"auto-admission:{run_id}:{error_code}",
                    plan_version=int(run.get(F.CURRENT_PLAN_VERSION, 0)),
                )
                created_job_ids = (notify_id,)

            txn.update(run_ref, updates)
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type=(
                    "auto_admission_refused" if terminal else "auto_admission_deferred"
                ),
                occurred_at=now_iso,
                expires_at=str(run.get(F.EXPIRES_AT) or ""),
                prior_state=state,
                next_state=next_state,
                reason_code=error_code,
                plan_version=int(run.get(F.CURRENT_PLAN_VERSION, 0)),
                correlation_id=correlation_id,
            )
            return AutoAdmissionRefusal(next_state, created_job_ids)

        return _execute(transaction)

    return await asyncio.to_thread(_run)


# --- leases ----------------------------------------------------------------------


async def claim_stage(uid: str, run_id: str, stage_id: str) -> StageLease | None:
    """Acquire or steal one stage's lease. Returns None when the caller must stand down.

    None is not an error. It means another worker legitimately owns this stage, or the
    run no longer wants it. The HTTP handler answers 200 on None precisely so Cloud
    Tasks stops retrying; the lease TTL, not the retry, is the recovery path.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    lease_token = uuid.uuid4().hex + uuid.uuid4().hex  # 256 bits, never persisted raw

    def _run() -> StageLease | None:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        stage_ref = _stages_ref(uid, run_id).document(stage_id)
        job_ref = _jobs_ref(uid).document(stage_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> StageLease | None:
            stage_snap = stage_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            job_snap = job_ref.get(transaction=txn)
            deletion_active = _deletion_active(_read_deletion_receipt(txn, uid, run_id))
            if not stage_snap.exists or not run_snap.exists:
                return None
            stage = stage_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            job = job_snap.to_dict() or {}
            stage_kind = str(stage.get(F.STAGE_KIND, ""))

            # Absorbing states. A delivery kind may act after a terminal result only when
            # its OWN condition holds: notify_result needs a result worth reporting on a
            # run that is neither cancelled nor deleting, and delete_run needs a live
            # deletion receipt. Membership in POST_TERMINAL_KINDS alone used to be enough,
            # which let a cancelled run still toast and let a stray delete job drain a run
            # nobody had asked to delete.
            from .stages.registry import may_run_post_terminal

            blocked = _blocked_reason(run)
            if blocked and not may_run_post_terminal(
                stage_kind, run, deletion_active=deletion_active
            ):
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_ABANDONED,
                        F.LAST_ERROR_CODE: blocked,
                        F.UPDATED_AT: now_iso,
                    },
                )
                # Retire the job and outbox rows too. Marking only the stage left the
                # outbox row PENDING with a due date in the past, so every sweep for the
                # next ten days re-dispatched a stage that immediately abandons itself
                # again. Cancelling a wide fan-out created one such row per child.
                if job_snap.exists:
                    txn.update(
                        job_ref,
                        {
                            F.JOB_STATE: F.STAGE_ABANDONED,
                            F.LAST_ERROR_CODE: blocked,
                            F.DISPATCH_DUE_AT: DISPATCH_NEVER,
                            F.UPDATED_AT: now_iso,
                        },
                    )
                    _retire_outbox(txn, uid, stage_id, now_iso)
                return None

            state = str(stage.get(F.STAGE_STATE, ""))
            if state in (F.STAGE_DONE, F.STAGE_ABANDONED, F.STAGE_FAILED):
                return None

            retry_due = str(job.get(F.NEXT_ATTEMPT_AT, ""))
            if retry_due and retry_due > now_iso:
                return None

            deadline = str(stage.get(F.STAGE_DEADLINE_AT, ""))
            if state == F.STAGE_LEASED and deadline > now_iso:
                # A live lease held by someone else. Stand down.
                return None
            stolen = state == F.STAGE_LEASED

            # Plan fence. A task delayed behind a clarification round must never execute
            # against an interpretation the user did not confirm.
            admitted_version = int(run.get(F.ADMITTED_PLAN_VERSION, 0))
            payload = dict(job.get("payload") or {})
            job_version = int(payload.get("plan_version", admitted_version))
            if admitted_version and job_version and job_version != admitted_version:
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_FAILED,
                        F.LAST_ERROR_CODE: "stale_plan_version",
                        F.UPDATED_AT: now_iso,
                    },
                )
                # Same as the block above: a stage fenced off by a superseded plan is
                # never going to run, so its delivery rows must stop being due or the
                # sweeper redelivers a permanently dead stage until TTL.
                if job_snap.exists:
                    txn.update(
                        job_ref,
                        {
                            F.JOB_STATE: F.STAGE_FAILED,
                            F.LAST_ERROR_CODE: "stale_plan_version",
                            F.DISPATCH_DUE_AT: DISPATCH_NEVER,
                            F.UPDATED_AT: now_iso,
                        },
                    )
                    _retire_outbox(txn, uid, stage_id, now_iso)
                return None

            attempt = int(stage.get(F.STAGE_ATTEMPT, 0)) + 1
            preset = str(run.get(F.PRESET, str(Preset.QUICK)))
            budget = budget_for(Preset(preset) if preset else Preset.QUICK)
            stage_deadline = (
                now + timedelta(seconds=budget.per_stage_s + LEASE_MARGIN_S)
            ).isoformat()
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1

            lease_fields = {
                F.STAGE_STATE: F.STAGE_LEASED,
                F.STAGE_ATTEMPT: attempt,
                F.LEASE_TOKEN_HASH: _actor_hash(lease_token),
                F.STAGE_DEADLINE_AT: stage_deadline,
                F.UPDATED_AT: now_iso,
            }
            txn.update(stage_ref, lease_fields)
            if job_snap.exists:
                txn.update(job_ref, dict(lease_fields, **{F.JOB_STATE: F.STAGE_LEASED}))
            txn.update(run_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now_iso})
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="stage_stolen" if stolen else "stage_leased",
                occurred_at=now_iso,
                expires_at=str(run.get(F.EXPIRES_AT, "")),
                stage_id=stage_id,
                stage_kind=stage_kind,
                attempt=attempt,
                lease_token=lease_token,
                prior_state=state,
                next_state=F.STAGE_LEASED,
                reason_code="lease_stolen" if stolen else "lease_acquired",
                plan_version=admitted_version,
            )
            return StageLease(
                uid=uid,
                run_id=run_id,
                stage_id=stage_id,
                stage_kind=stage_kind,
                wave=int(stage.get(F.WAVE, 0)),
                ordinal=str(stage.get("ordinal", "0")),
                attempt=attempt,
                lease_token=lease_token,
                stage_deadline_at=stage_deadline,
                admitted_plan_version=admitted_version,
                preset=preset,
                correlation_id=str(run.get(F.CORRELATION_ID, "")),
                payload=payload,
                stolen=stolen,
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def heartbeat_stage(lease: StageLease) -> bool:
    """Extend a live lease. False means the lease was lost and the caller must stop."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> bool:
        db = admin_firestore()
        stage_ref = _stages_ref(lease.uid, lease.run_id).document(lease.stage_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> bool:
            snap = stage_ref.get(transaction=txn)
            if not snap.exists:
                return False
            stage = snap.to_dict() or {}
            if not _lease_matches(stage, lease):
                return False
            budget = lease.budget
            txn.update(
                stage_ref,
                {
                    F.STAGE_DEADLINE_AT: (
                        now + timedelta(seconds=budget.per_stage_s + LEASE_MARGIN_S)
                    ).isoformat(),
                    F.UPDATED_AT: now_iso,
                },
            )
            return True

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def is_cancelled(uid: str, run_id: str) -> bool:
    """Cheap re-read for a stage body's inner loop, between external calls.

    This is the third of the four cancellation checkpoints. Without it a 240s stage
    could keep spending its whole extract grant after the user already cancelled.
    """

    def _run() -> bool:
        snap = _run_ref(uid, run_id).get()
        if not snap.exists:
            return True
        data = snap.to_dict() or {}
        return bool(_blocked_reason(data))

    return await asyncio.to_thread(_run)


# --- advance ---------------------------------------------------------------------


def _commit_units(
    ledger: dict[str, Any],
    stage_id: str,
    actuals: dict[str, int],
    *,
    cost_microusd: int = 0,
    cost_known: bool = True,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Move this stage's reservation from reserved to used, freeing the remainder.

    Returns the ledger patch plus what was released, purely as dict math so the caller
    can apply it inside its own transaction. Keeping this pure is what lets advance
    commit state and budget together instead of in two racing transactions.

    ``cost_microusd`` is settled here for the same reason the units are: the run's dollar
    reservation was opened in the same transaction as its unit reservation, and closing
    the two in different transactions would leave a window where the run holds dollars
    for a stage that has already finished.
    """
    reservations = dict(ledger.get(F.LEDGER_RESERVATIONS) or {})
    granted = dict(reservations.pop(stage_id, {}) or {})
    used = dict(ledger.get(F.LEDGER_USED) or {})
    reserved = dict(ledger.get(F.LEDGER_RESERVED) or {})
    released: dict[str, int] = {}

    overrun: dict[str, int] = {}
    for unit, grant in granted.items():
        grant_n = int(grant)
        actual = max(0, int(actuals.get(unit, 0)))
        # The RESERVATION arithmetic is clamped to the grant, because releasing more than
        # was reserved would invent budget. The RECORD is not: clamping what a stage
        # actually consumed made over-use vanish from the ledger entirely, which is the
        # one direction a cost guard must never fail in. Verify reserving 2 model calls
        # and making 6 recorded 2, and the four extra were free as far as every later
        # check could tell.
        billed = min(actual, grant_n)
        used[unit] = int(used.get(unit, 0)) + actual
        reserved[unit] = max(0, int(reserved.get(unit, 0)) - grant_n)
        if grant_n - billed:
            released[unit] = grant_n - billed
        if actual > grant_n:
            overrun[unit] = actual - grant_n

    # Units consumed with no reservation still get recorded. That should not happen, but
    # under-recording spend is the one direction a cost guard must never fail in.
    for unit, actual in actuals.items():
        if unit not in granted and int(actual) > 0:
            used[unit] = int(used.get(unit, 0)) + int(actual)
            overrun[unit] = overrun.get(unit, 0) + int(actual)

    # The dollar side of the same reservation. Released whatever this stage actually cost,
    # and floored at zero so a settlement can never invent run headroom.
    cost_reservations = dict(ledger.get(F.LEDGER_COST_RESERVATIONS) or {})
    held_cost = int(cost_reservations.pop(stage_id, 0) or 0)
    reserved_cost = max(0, int(ledger.get(F.LEDGER_RESERVED_MICROUSD, 0)) - held_cost)
    settled_cost = max(0, int(cost_microusd))
    if not cost_known:
        settled_cost = max(settled_cost, held_cost)
    actual_cost = int(ledger.get(F.LEDGER_COST_MICROUSD, 0)) + settled_cost

    patch = {
        F.LEDGER_USED: used,
        F.LEDGER_RESERVED: reserved,
        F.LEDGER_RESERVATIONS: reservations,
        F.LEDGER_RESERVED_MICROUSD: reserved_cost,
        F.LEDGER_COST_RESERVATIONS: cost_reservations,
        F.LEDGER_COST_MICROUSD: actual_cost,
    }
    if overrun:
        # Cumulative, and separate from `used`, so "we spent more than we granted" stays
        # answerable from the ledger alone rather than requiring an audit-log replay.
        totals = dict(ledger.get(F.LEDGER_OVERRUN) or {})
        for unit, count in overrun.items():
            totals[unit] = int(totals.get(unit, 0)) + int(count)
        patch[F.LEDGER_OVERRUN] = totals
    return patch, released


def _read_project_settlement(txn: Any, stage: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    day = str(stage.get(F.STAGE_PROJECT_RECEIPT_DAY, ""))
    receipt_id = str(stage.get(F.STAGE_PROJECT_RECEIPT_ID, ""))
    if not day or not receipt_id:
        return None, None, None, None
    budget_ref = admin_firestore().collection(F.PROJECT_BUDGET_COLLECTION).document(day)
    receipt_ref = budget_ref.collection(F.PROJECT_RECEIPTS_SUBCOLLECTION).document(receipt_id)
    return (
        budget_ref,
        receipt_ref,
        budget_ref.get(transaction=txn),
        receipt_ref.get(transaction=txn),
    )


def _settle_project_in_txn(
    txn: Any,
    settlement: tuple[Any, Any, Any, Any],
    actual_microusd: int,
    *,
    cost_known: bool,
    now_iso: str,
) -> None:
    budget_ref, receipt_ref, budget_snap, receipt_snap = settlement
    if receipt_snap is None or not receipt_snap.exists:
        return
    receipt = receipt_snap.to_dict() or {}
    if receipt.get(F.RECEIPT_STATE) != F.RECEIPT_RESERVED:
        return
    estimate = int(receipt.get(F.RECEIPT_ESTIMATE_MICROUSD, 0))
    actual = max(0, int(actual_microusd)) if cost_known else estimate
    budget = budget_snap.to_dict() or {} if budget_snap and budget_snap.exists else {}
    txn.set(budget_ref, {
        F.PROJECT_RESERVED_MICROUSD: max(
            0, int(budget.get(F.PROJECT_RESERVED_MICROUSD, 0)) - estimate
        ),
        F.PROJECT_ACTUAL_MICROUSD: int(
            budget.get(F.PROJECT_ACTUAL_MICROUSD, 0)
        ) + actual,
        F.UPDATED_AT: now_iso,
    }, merge=True)
    txn.update(receipt_ref, {
        F.RECEIPT_STATE: F.RECEIPT_SETTLED,
        F.RECEIPT_ACTUAL_MICROUSD: actual,
        F.RECEIPT_COST_KNOWN: cost_known,
        F.UPDATED_AT: now_iso,
    })


async def advance(lease: StageLease, result: StageResult) -> AdvanceOutcome:
    """Commit one stage's outcome: state, budget, next job and outbox row. One txn.

    Ordering inside is fixed and load-bearing. The lease is verified first, so a worker
    whose lease was stolen writes nothing at all rather than partially. The run's block
    state is verified second, so a cancellation that landed during the stage body turns
    this into an abandonment that releases budget instead of a transition that spends it.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> AdvanceOutcome:
        db = admin_firestore()
        run_ref = _run_ref(lease.uid, lease.run_id)
        stage_ref = _stages_ref(lease.uid, lease.run_id).document(lease.stage_id)
        job_ref = _jobs_ref(lease.uid).document(lease.stage_id)
        ledger_ref = _ledger_ref(lease.uid, lease.run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> AdvanceOutcome:
            stage_snap = stage_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            ledger_snap = ledger_ref.get(transaction=txn)
            # Read with the rest, before any write: a delete_run stage's authority to act
            # is the receipt, and it has to be read in the same transaction as the write
            # it authorises.
            deletion_active = _deletion_active(
                _read_deletion_receipt(txn, lease.uid, lease.run_id)
            )
            if not stage_snap.exists or not run_snap.exists:
                return AdvanceOutcome(False, "lost_lease")
            stage = stage_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            ledger = ledger_snap.to_dict() or {}
            project_settlement = _read_project_settlement(txn, stage)

            if stage.get(F.STAGE_STATE) == F.STAGE_DONE:
                # Redelivery of an already-committed stage.
                return AdvanceOutcome(False, "duplicate", state=str(run.get(F.STATE, "")))
            if not _lease_matches(stage, lease):
                # Stolen or superseded. Write NOTHING; the new owner is authoritative.
                return AdvanceOutcome(False, "lost_lease", state=str(run.get(F.STATE, "")))

            expires_at = str(run.get(F.EXPIRES_AT, ""))
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            blocked = _blocked_reason(run)

            from .stages.registry import may_run_post_terminal

            if blocked and not may_run_post_terminal(
                lease.stage_kind, run, deletion_active=deletion_active
            ):
                # Cancelled mid-stage. The run is already cancelled from the user's view,
                # but whatever this stage had already spent before the cancellation landed
                # is still spent: releasing the whole grant refunded real Brave, Firecrawl
                # and token spend, and handed those units to the next thing that asked.
                # Commit what was proven consumed, release only the remainder.
                patch, released = _commit_units(
                    ledger,
                    lease.stage_id,
                    result.actuals,
                    cost_microusd=int(result.cost_microusd),
                    cost_known=result.cost_known,
                )
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_ABANDONED,
                        F.LAST_ERROR_CODE: blocked,
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(
                    job_ref,
                    {
                        F.JOB_STATE: F.STAGE_ABANDONED,
                        F.DISPATCH_DUE_AT: DISPATCH_NEVER,
                        F.UPDATED_AT: now_iso,
                    },
                )
                _retire_outbox(txn, lease.uid, lease.stage_id, now_iso)
                txn.update(ledger_ref, dict(patch, **{F.LEDGER_UPDATED_AT: now_iso}))
                _settle_project_in_txn(
                    txn, project_settlement, result.cost_microusd,
                    cost_known=result.cost_known, now_iso=now_iso,
                )
                txn.update(run_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now_iso})
                _audit_event(
                    txn,
                    uid=lease.uid,
                    run_id=lease.run_id,
                    sequence=sequence,
                    event_type="stage_abandoned",
                    occurred_at=now_iso,
                    expires_at=expires_at,
                    stage_id=lease.stage_id,
                    stage_kind=lease.stage_kind,
                    attempt=lease.attempt,
                    lease_token=lease.lease_token,
                    reason_code=blocked,
                    units=released,
                )
                return AdvanceOutcome(
                    False, blocked, state=str(run.get(F.STATE, "")), released_units=released
                )

            # --- the committing path ---
            patch, released = _commit_units(
                ledger,
                lease.stage_id,
                result.actuals,
                cost_microusd=int(result.cost_microusd),
                cost_known=result.cost_known,
            )
            txn.update(ledger_ref, dict(patch, **{F.LEDGER_UPDATED_AT: now_iso}))
            _settle_project_in_txn(
                txn, project_settlement, result.cost_microusd,
                cost_known=result.cost_known, now_iso=now_iso,
            )

            txn.update(
                stage_ref,
                {
                    F.STAGE_STATE: F.STAGE_DONE,
                    F.LEASE_TOKEN_HASH: "",
                    F.STAGE_DEADLINE_AT: "",
                    "outputs": dict(result.stage_outputs),
                    "result_kind": str(result.kind),
                    F.UPDATED_AT: now_iso,
                },
            )
            txn.update(
                job_ref,
                {
                    F.JOB_STATE: F.STAGE_DONE,
                    F.LEASE_TOKEN_HASH: "",
                    F.DISPATCH_DUE_AT: DISPATCH_NEVER,
                    F.UPDATED_AT: now_iso,
                },
            )
            _retire_outbox(txn, lease.uid, lease.stage_id, now_iso)

            run_updates: dict[str, Any] = dict(result.run_updates)
            run_updates.update(
                {
                    F.STATE_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                }
            )
            next_state = result.next_state or str(run.get(F.STATE, ""))
            if result.next_state:
                run_updates[F.STATE] = result.next_state
            clarify_notify_created: list[str] = []
            if result.kind is StageResultKind.CLARIFY:
                question = dict(result.questions[0])
                run_updates[F.STATE] = F.STATE_AWAITING_CLARIFICATION
                run_updates[F.PENDING_QUESTION] = question
                run_updates[F.PENDING_QUESTION_EXPIRES_AT] = (
                    now + timedelta(seconds=CLARIFICATION_TTL_S)
                ).isoformat()
                rounds = int(run.get(F.CLARIFICATION_ROUNDS, 0)) + 1
                run_updates[F.CLARIFICATION_ROUNDS] = rounds
                next_state = F.STATE_AWAITING_CLARIFICATION
                # CLARIFY forbids next_jobs (a parked run holds no paid-work
                # reservation), so without this the park was silent: the copy,
                # action, and _NOTIFIABLE_STATES membership for
                # awaiting_clarification all existed while nothing ever created
                # the notify job. The ordinal carries the round so a second
                # question cannot collide with the first round's job.
                clarify_notify_id = stage_id_for(
                    F.STAGE_NOTIFY_RESULT, lease.run_id, lease.wave, f"clarify{rounds}"
                )
                _create_job_triplet(
                    txn,
                    uid=lease.uid,
                    run_id=lease.run_id,
                    stage_id=clarify_notify_id,
                    stage_kind=F.STAGE_NOTIFY_RESULT,
                    wave=lease.wave,
                    ordinal=f"clarify{rounds}",
                    payload={
                        "terminal_state": F.STATE_AWAITING_CLARIFICATION,
                        "plan_version": lease.admitted_plan_version,
                    },
                    now_iso=now_iso,
                    expires_at=expires_at,
                    correlation_id=lease.correlation_id,
                    causation_id=lease.stage_id,
                )
                clarify_notify_created.append(clarify_notify_id)
            if result.kind is StageResultKind.TERMINAL:
                run_updates[F.FAILURE_CODE] = result.failure_code
                next_state = result.next_state or F.STATE_FAILED
                run_updates[F.STATE] = next_state
            if result.kind is StageResultKind.NOT_IMPLEMENTED:
                # A stub ran. Record it honestly and move nothing: inventing a plausible
                # transition here would let a phase-four regression hide behind a green
                # phase-two inspection.
                run_updates[F.PROCESSING_STAGE] = lease.stage_kind
            elif result.next_jobs:
                run_updates[F.PROCESSING_STAGE] = result.next_jobs[0].stage_kind
            txn.update(run_ref, run_updates)

            # Updates to documents that already exist (a source row the search wave
            # created, a claim an earlier pass wrote). Applied before the creates below
            # so a stage that does both reads a consistent picture.
            for subcollection, docs in result.document_updates.items():
                for doc_id, body in docs.items():
                    txn.update(
                        _sub_ref(lease.uid, lease.run_id, subcollection).document(doc_id),
                        dict(body, **{F.UPDATED_AT: now_iso}),
                    )

            # Run-owned documents the stage produced. expires_at is stamped HERE, by the
            # engine, so no stage can forget retention and orphan a document past TTL.
            for subcollection, docs in result.documents.items():
                for doc_id, body in docs.items():
                    _txn_create(
                        txn,
                        _sub_ref(lease.uid, lease.run_id, subcollection).document(doc_id),
                        dict(body, **{F.EXPIRES_AT: expires_at}),
                    )

            # Fan-out coordinator, created in the SAME transaction as the children, so a
            # crash can never leave children with nothing to join into.
            if result.kind is StageResultKind.FANOUT:
                # The coordinator is keyed on the CHILDREN's wave, not on this stage's.
                # Deriving it from lease.wave + 1 instead would silently disagree with a
                # child that names its own wave, and complete_child would then look for
                # a coordinator that does not exist, so the join could never fire.
                child_wave = (
                    result.next_jobs[0].wave if result.next_jobs else lease.wave + 1
                )
                wave_id = f"w{child_wave}"
                join_job_id = stage_id_for(F.STAGE_READ_JOIN, lease.run_id, child_wave)
                _txn_create(
                    txn,
                    _coord_ref(lease.uid, lease.run_id, wave_id),
                    {
                        F.COORD_EXPECTED: result.expected_children,
                        F.COORD_COMPLETED: 0,
                        F.COORD_JOIN_CLAIMED: False,
                        F.COORD_JOIN_JOB_ID: join_job_id,
                        F.COORD_DEADLINE_AT: (
                            now + timedelta(seconds=lease.budget.per_stage_s * 2)
                        ).isoformat(),
                        F.WAVE: child_wave,
                        F.CREATED_AT: now_iso,
                        F.EXPIRES_AT: expires_at,
                    },
                )

            created: list[str] = list(clarify_notify_created)
            for job in result.next_jobs:
                next_stage_id = stage_id_for(
                    job.stage_kind, lease.run_id, job.wave, job.ordinal
                )
                _create_job_triplet(
                    txn,
                    uid=lease.uid,
                    run_id=lease.run_id,
                    stage_id=next_stage_id,
                    stage_kind=job.stage_kind,
                    wave=job.wave,
                    ordinal=job.ordinal,
                    payload=dict(job.payload, plan_version=lease.admitted_plan_version),
                    now_iso=now_iso,
                    expires_at=expires_at,
                    correlation_id=lease.correlation_id,
                    causation_id=lease.stage_id,
                )
                created.append(next_stage_id)

            _audit_event(
                txn,
                uid=lease.uid,
                run_id=lease.run_id,
                sequence=sequence,
                event_type="stage_advanced",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=lease.stage_id,
                stage_kind=lease.stage_kind,
                attempt=lease.attempt,
                lease_token=lease.lease_token,
                prior_state=str(run.get(F.STATE, "")),
                next_state=next_state,
                reason_code=str(result.kind),
                plan_version=lease.admitted_plan_version,
                units=dict(result.actuals),
                correlation_id=lease.correlation_id,
            )
            return AdvanceOutcome(
                committed=True,
                disposition="advanced",
                state=next_state,
                created_job_ids=tuple(created),
                released_units=released,
            )

        return _execute(transaction)

    return await asyncio.to_thread(_run)


# --- the fan-out join ------------------------------------------------------------


async def complete_child(
    lease: StageLease, result: StageResult, *, wave_id: str = ""
) -> ChildOutcome:
    """Complete one fan-out child and, if it is the last, claim the join. ONE txn.

    Returns the disposition plus the join job id when this child claimed the join. The
    id has to travel back out: it is created inside this transaction and nothing else
    knows it exists, so returning only the disposition left the join durable but
    undelivered until the next sweep.

    This is the highest-risk transaction in the engine, and every guard in it earns its
    place:

      * The completion increment is gated on the STAGE document, not on delivery, so a
        redelivered child returns "duplicate" and does not double-count.
      * The counter read, the join_claimed flip and the join job's create() are in ONE
        serialisable transaction, so two children racing to be last serialise and the
        loser simply sees the flag already set.
      * A cancelled run marks the child abandoned WITHOUT incrementing, so a cancelled
        wave can never satisfy its own join and resurrect itself.
      * The join's outbox row commits with the last child, so a crash before dispatch
        leaves a durable pending row for the sweeper rather than a hung run.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    coord_wave_id = wave_id or f"w{lease.wave}"

    def _run() -> ChildOutcome:
        db = admin_firestore()
        run_ref = _run_ref(lease.uid, lease.run_id)
        stage_ref = _stages_ref(lease.uid, lease.run_id).document(lease.stage_id)
        coord_ref = _coord_ref(lease.uid, lease.run_id, coord_wave_id)
        ledger_ref = _ledger_ref(lease.uid, lease.run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> ChildOutcome:
            stage_snap = stage_ref.get(transaction=txn)
            coord_snap = coord_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            ledger_snap = ledger_ref.get(transaction=txn)
            stage = stage_snap.to_dict() or {}
            coord = coord_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            ledger = ledger_snap.to_dict() or {}
            project_settlement = _read_project_settlement(txn, stage)

            if stage.get(F.STAGE_STATE) == F.STAGE_DONE:
                return ChildOutcome("duplicate")  # the increment guard
            if not _lease_matches(stage, lease):
                return ChildOutcome("duplicate")

            expires_at = str(run.get(F.EXPIRES_AT, ""))
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            blocked = _blocked_reason(run)

            # The join for this wave has already fired, either because a real last child
            # claimed it or because the sweeper collapsed the wave around a child that
            # was never coming back. This child is that late child. Its slot is gone: the
            # join has run, verify may already have merged the sources, and letting it
            # write now would add evidence to a wave that was closed without it and bill
            # for work nothing will read. Recorded as abandoned, with its grant released.
            late = bool(coord.get(F.COORD_JOIN_CLAIMED)) and not blocked
            if blocked or late:
                # Do NOT touch the counter: a cancelled wave must never be able to satisfy
                # its own join and resurrect itself.
                #
                # What IS committed is whatever this child actually spent before it found
                # out it was late. Releasing the whole grant here refunded Firecrawl
                # credits and model tokens that had already been billed, so a cancelled
                # wide wave read as costing nothing while the invoice said otherwise, and
                # the freed units were handed to work that then spent them a second time.
                # Only the unused remainder is released.
                patch, _released = _commit_units(
                    ledger,
                    lease.stage_id,
                    result.actuals,
                    cost_microusd=int(result.cost_microusd),
                    cost_known=result.cost_known,
                )
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_ABANDONED,
                        F.LAST_ERROR_CODE: blocked or "join_already_claimed",
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(ledger_ref, dict(patch, **{F.LEDGER_UPDATED_AT: now_iso}))
                _settle_project_in_txn(
                    txn, project_settlement, result.cost_microusd,
                    cost_known=result.cost_known, now_iso=now_iso,
                )
                txn.update(
                    _jobs_ref(lease.uid).document(lease.stage_id),
                    {
                        F.JOB_STATE: F.STAGE_ABANDONED,
                        F.DISPATCH_DUE_AT: DISPATCH_NEVER,
                        F.UPDATED_AT: now_iso,
                    },
                )
                _retire_outbox(txn, lease.uid, lease.stage_id, now_iso)
                return ChildOutcome("abandoned" if blocked else "late")

            patch, _released = _commit_units(
                ledger,
                lease.stage_id,
                result.actuals,
                cost_microusd=int(result.cost_microusd),
                cost_known=result.cost_known,
            )
            txn.update(ledger_ref, dict(patch, **{F.LEDGER_UPDATED_AT: now_iso}))
            _settle_project_in_txn(
                txn, project_settlement, result.cost_microusd,
                cost_known=result.cost_known, now_iso=now_iso,
            )
            txn.update(
                stage_ref,
                {
                    F.STAGE_STATE: F.STAGE_DONE,
                    F.LEASE_TOKEN_HASH: "",
                    F.STAGE_DEADLINE_AT: "",
                    "outputs": dict(result.stage_outputs),
                    F.UPDATED_AT: now_iso,
                },
            )
            txn.update(
                _jobs_ref(lease.uid).document(lease.stage_id),
                {
                    F.JOB_STATE: F.STAGE_DONE,
                    F.DISPATCH_DUE_AT: DISPATCH_NEVER,
                    F.UPDATED_AT: now_iso,
                },
            )
            _retire_outbox(txn, lease.uid, lease.stage_id, now_iso)

            # The child's own evidence lands on its own source document, which the
            # search wave already created when it discovered the URL. Children never
            # touch a shared claim doc: that is what avoids hot-document contention and
            # lost evidence-array updates across a concurrent wave.
            for subcollection, docs in result.document_updates.items():
                for doc_id, body in docs.items():
                    txn.update(
                        _sub_ref(lease.uid, lease.run_id, subcollection).document(doc_id),
                        dict(body, **{F.UPDATED_AT: now_iso}),
                    )

            for subcollection, docs in result.documents.items():
                for doc_id, body in docs.items():
                    _txn_create(
                        txn,
                        _sub_ref(lease.uid, lease.run_id, subcollection).document(doc_id),
                        dict(body, **{F.EXPIRES_AT: expires_at}),
                    )

            completed = int(coord.get(F.COORD_COMPLETED, 0)) + 1
            expected = int(coord.get(F.COORD_EXPECTED, 0))
            txn.update(coord_ref, {F.COORD_COMPLETED: completed, F.UPDATED_AT: now_iso})
            txn.update(run_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now_iso})

            disposition = "ok"
            created: tuple[str, ...] = ()
            if completed >= expected and not coord.get(F.COORD_JOIN_CLAIMED):
                txn.update(coord_ref, {F.COORD_JOIN_CLAIMED: True})
                join_job_id = str(
                    coord.get(F.COORD_JOIN_JOB_ID)
                    or stage_id_for(F.STAGE_READ_JOIN, lease.run_id, lease.wave)
                )
                _create_job_triplet(
                    txn,
                    uid=lease.uid,
                    run_id=lease.run_id,
                    stage_id=join_job_id,
                    stage_kind=F.STAGE_READ_JOIN,
                    wave=lease.wave,
                    ordinal="0",
                    payload={"plan_version": lease.admitted_plan_version},
                    now_iso=now_iso,
                    expires_at=expires_at,
                    correlation_id=lease.correlation_id,
                    causation_id=lease.stage_id,
                )
                disposition = "join_claimed"
                created = (join_job_id,)

            _audit_event(
                txn,
                uid=lease.uid,
                run_id=lease.run_id,
                sequence=sequence,
                event_type="child_completed",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=lease.stage_id,
                stage_kind=lease.stage_kind,
                attempt=lease.attempt,
                lease_token=lease.lease_token,
                reason_code=disposition,
                units=dict(result.actuals),
                correlation_id=lease.correlation_id,
            )
            return ChildOutcome(disposition, created)

        return _execute(transaction)

    return await asyncio.to_thread(_run)


# --- failure + cancellation ------------------------------------------------------


async def fail_stage(
    lease: StageLease,
    *,
    error_code: str,
    retryable: bool,
    max_attempts: int = STAGE_ATTEMPT_CAP,
    spent_actuals: dict[str, int] | None = None,
    spent_cost_microusd: int = 0,
    spent_cost_known: bool = True,
) -> FailOutcome:
    """Record a stage failure and either schedule a retry or go terminal.

    Terminal here means PARTIAL when the run already holds evidence and FAILED only when
    it holds none. That asymmetry is the product: a sourced partial answer with named
    gaps is useful, and a bare failure is not.

    ``spent_actuals`` and ``spent_cost_microusd`` are what the failing stage PROVED it
    consumed before it failed. They are committed rather than released, so a retry is
    never handed units that have already been spent. Passing nothing means the stage got
    nowhere near a provider, which is true for a refusal at admission and false for
    anything that raised after a call.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    # Full jitter, matching the meetings engine rather than inventing a second backoff.
    delay_s = random.uniform(0, min(3600, 30 * (2 ** max(lease.attempt - 1, 0))))
    due = (now + timedelta(seconds=delay_s)).isoformat()

    def _run() -> FailOutcome:
        db = admin_firestore()
        run_ref = _run_ref(lease.uid, lease.run_id)
        stage_ref = _stages_ref(lease.uid, lease.run_id).document(lease.stage_id)
        job_ref = _jobs_ref(lease.uid).document(lease.stage_id)
        outbox_ref = _outbox_ref(lease.uid).document(lease.stage_id)
        ledger_ref = _ledger_ref(lease.uid, lease.run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> FailOutcome:
            stage_snap = stage_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            ledger_snap = ledger_ref.get(transaction=txn)
            outbox_snap = outbox_ref.get(transaction=txn)
            deletion_active = _deletion_active(
                _read_deletion_receipt(txn, lease.uid, lease.run_id)
            )
            if not stage_snap.exists or not run_snap.exists:
                return FailOutcome("lost_lease")
            stage = stage_snap.to_dict() or {}
            run = run_snap.to_dict() or {}
            ledger = ledger_snap.to_dict() or {}
            project_settlement = _read_project_settlement(txn, stage)
            if not _lease_matches(stage, lease):
                return FailOutcome("lost_lease")

            expires_at = str(run.get(F.EXPIRES_AT, ""))
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            # Release only what this stage can be PROVEN not to have spent.
            #
            # This released the whole grant unconditionally, which is wrong in the exact
            # direction a spend guard must never fail in. A read that bought a Firecrawl
            # page and then died in extraction had its page credit, its bytes and its
            # model tokens handed back to the ledger, and the retry spent them all again
            # against a budget that had no record of the first attempt. Repeat that at the
            # attempt cap and the run bills three times for one page while its ledger reads
            # one. Whatever the stage proved it consumed is committed here; the remainder,
            # which is the only part that was genuinely never used, is released.
            patch, released_units = _commit_units(
                ledger,
                lease.stage_id,
                spent_actuals or {},
                cost_microusd=max(0, int(spent_cost_microusd)),
                cost_known=spent_cost_known,
            )
            txn.update(ledger_ref, dict(patch, **{F.LEDGER_UPDATED_AT: now_iso}))
            _settle_project_in_txn(
                txn, project_settlement, spent_cost_microusd,
                cost_known=spent_cost_known, now_iso=now_iso,
            )

            from .stages.registry import POST_TERMINAL_KINDS, may_run_post_terminal

            rearmed = ""
            rearmed_due = ""
            should_retry = retryable and lease.attempt < max_attempts
            if should_retry:
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_PENDING,
                        F.LEASE_TOKEN_HASH: "",
                        F.STAGE_DEADLINE_AT: "",
                        F.LAST_ERROR_CODE: error_code,
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(
                    job_ref,
                    {
                        F.JOB_STATE: F.STAGE_PENDING,
                        F.NEXT_ATTEMPT_AT: due,
                        F.LAST_ERROR_CODE: error_code,
                        F.UPDATED_AT: now_iso,
                    },
                )
                row = {
                    F.OUTBOX_STATE: F.OUTBOX_RETRY,
                    F.DISPATCH_DUE_AT: due,
                    F.UPDATED_AT: now_iso,
                }
                if outbox_snap.exists:
                    txn.update(outbox_ref, row)
                else:
                    # The row was TTL'd away under a long retry. Recreate it, or the
                    # stage would be durable but permanently undeliverable.
                    _txn_create(
                        txn,
                        outbox_ref,
                        dict(
                            _outbox_row(
                                uid=lease.uid,
                                run_id=lease.run_id,
                                stage_id=lease.stage_id,
                                now_iso=now_iso,
                                expires_at=expires_at,
                                correlation_id=lease.correlation_id,
                            ),
                            **row,
                        ),
                    )
                outcome = "retry_scheduled"
                created: tuple[str, ...] = ()
                # The re-armed stage travels back out so the caller can dispatch it for
                # its jittered due time straight away. Leaving it to the five-minute sweep
                # is a five-minute wait inside a run whose whole wall clock is 240 seconds:
                # every retry the engine scheduled was, in practice, a run that expired.
                rearmed = lease.stage_id
                rearmed_due = due
            elif lease.stage_kind in POST_TERMINAL_KINDS:
                # A DELIVERY stage that ran out of attempts. It must not touch the run.
                #
                # These kinds run AFTER the run is already terminal, so the generic
                # terminal branch below would rewrite a finished result on the strength
                # of a failed toast: a READY brief whose notification could not be
                # delivered three times was being downgraded to PARTIAL, which tells the
                # user their research is incomplete when the only thing that failed was
                # the message about it.
                #
                # It would also mint a notification for the failure of a notification.
                # create_terminal_notify_job builds a deterministic id from this run and
                # wave, which for a notify stage is the id this very stage already owns,
                # so the create() collides, the transaction raises, and the handler
                # answers 500 into another retry.
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_FAILED,
                        F.LEASE_TOKEN_HASH: "",
                        F.LAST_ERROR_CODE: error_code,
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(
                    job_ref,
                    {F.JOB_STATE: F.STAGE_FAILED, F.LAST_ERROR_CODE: error_code,
                     F.DISPATCH_DUE_AT: DISPATCH_NEVER, F.UPDATED_AT: now_iso},
                )
                _retire_outbox(txn, lease.uid, lease.stage_id, now_iso)
                logger.warn(
                    "research.store: delivery stage exhausted, result left intact",
                    {"run_id": lease.run_id, "stage_kind": lease.stage_kind,
                     "error_code": "research_delivery_exhausted"},
                )
                outcome = "delivery_failed"
                created = ()
                # notion_deliver chains notify_result on success, so its
                # exhaustion would otherwise leave the run finished and the
                # user never told. Minting the terminal notify here is safe for
                # any delivery kind EXCEPT notify_result itself (whose own id
                # is the one create_terminal_notify_job would derive - the
                # collision described above). notify_result reads the absent
                # DELIVERY_RESULT receipt and reports the failure honestly.
                if lease.stage_kind != F.STAGE_NOTIFY_RESULT:
                    notifiable = may_run_post_terminal(
                        F.STAGE_NOTIFY_RESULT, run, deletion_active=deletion_active
                    )
                    if notifiable:
                        created = (
                            create_terminal_notify_job(
                                txn,
                                uid=lease.uid,
                                run_id=lease.run_id,
                                terminal_state=str(run.get(F.STATE) or ""),
                                wave=lease.wave,
                                now_iso=now_iso,
                                expires_at=expires_at,
                                correlation_id=lease.correlation_id,
                                causation_id=lease.stage_id,
                                plan_version=lease.admitted_plan_version,
                            ),
                        )
            else:
                has_evidence = int(run.get(F.CLAIM_COUNT, 0)) > 0 or int(
                    run.get(F.SOURCE_COUNT, 0)
                ) > 0
                terminal = F.STATE_PARTIAL if has_evidence else F.STATE_FAILED
                txn.update(
                    stage_ref,
                    {
                        F.STAGE_STATE: F.STAGE_FAILED,
                        F.LEASE_TOKEN_HASH: "",
                        F.LAST_ERROR_CODE: error_code,
                        F.UPDATED_AT: now_iso,
                    },
                )
                txn.update(
                    job_ref,
                    {F.JOB_STATE: F.STAGE_FAILED, F.LAST_ERROR_CODE: error_code,
                     F.DISPATCH_DUE_AT: DISPATCH_NEVER, F.UPDATED_AT: now_iso},
                )
                _retire_outbox(txn, lease.uid, lease.stage_id, now_iso)
                txn.update(
                    run_ref,
                    {
                        F.STATE: terminal,
                        F.FAILURE_CODE: error_code or F.FAIL_ATTEMPT_CAP,
                        F.STATE_REVISION: gcloud_firestore.Increment(1),
                        F.UPDATED_AT: now_iso,
                    },
                )
                # This run is now absorbing and will never reach finalize, so this is the
                # last chance to tell the user anything at all about it. Asked through the
                # same predicate the delivery stage itself is gated on, so a run whose
                # deletion has already begun does not get a job minted for a toast that
                # would then be refused, or worse, land after the data is gone.
                notifiable = may_run_post_terminal(
                    F.STAGE_NOTIFY_RESULT,
                    dict(run, **{F.STATE: terminal}),
                    deletion_active=deletion_active,
                )
                created = (
                    (
                        create_terminal_notify_job(
                            txn,
                            uid=lease.uid,
                            run_id=lease.run_id,
                            terminal_state=terminal,
                            wave=lease.wave,
                            now_iso=now_iso,
                            expires_at=expires_at,
                            correlation_id=lease.correlation_id,
                            causation_id=lease.stage_id,
                            plan_version=lease.admitted_plan_version,
                        ),
                    )
                    if notifiable
                    else ()
                )
                outcome = terminal

            txn.update(run_ref, {F.AUDIT_SEQUENCE: sequence, F.UPDATED_AT: now_iso})
            _audit_event(
                txn,
                uid=lease.uid,
                run_id=lease.run_id,
                sequence=sequence,
                event_type="stage_failed",
                occurred_at=now_iso,
                expires_at=expires_at,
                stage_id=lease.stage_id,
                stage_kind=lease.stage_kind,
                attempt=lease.attempt,
                lease_token=lease.lease_token,
                reason_code=error_code,
                units=released_units,
                correlation_id=lease.correlation_id,
            )
            return FailOutcome(outcome, created, rearmed, rearmed_due)

        return _execute(transaction)

    return await asyncio.to_thread(_run)


async def request_cancel(
    uid: str, run_id: str, *, correlation_id: str = ""
) -> tuple[str, list[str]]:
    """Cancel a run. A WRITE, not an interrupt. Idempotent on a terminal run.

    Returns the resulting state plus the task names worth deleting. In-flight children
    are never killed: each finishes its current bounded call, then its own completion
    transaction sees the cancellation, writes abandoned, does not increment and does not
    join. From the user's point of view the run is cancelled the instant this returns.

    Task deletion is deliberately OUTSIDE the transaction and best-effort. It is what
    actually saves money on a wide fan-out, but it must never be able to fail the
    cancellation itself.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> tuple[str, list[str]]:
        db = admin_firestore()
        run_ref = _run_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> str:
            snap = run_ref.get(transaction=txn)
            if not snap.exists:
                return ""
            run = snap.to_dict() or {}
            state = str(run.get(F.STATE, ""))
            if state in F.TERMINAL_STATES:
                return state  # idempotent: already finished, nothing to cancel
            sequence = int(run.get(F.AUDIT_SEQUENCE, 0)) + 1
            txn.update(
                run_ref,
                {
                    F.CANCEL_REQUESTED_AT: now_iso,
                    F.STATE: F.STATE_CANCELLED,
                    F.FAILURE_CODE: F.FAIL_CANCELLED_BY_USER,
                    F.STATE_REVISION: gcloud_firestore.Increment(1),
                    F.AUDIT_SEQUENCE: sequence,
                    F.UPDATED_AT: now_iso,
                },
            )
            _audit_event(
                txn,
                uid=uid,
                run_id=run_id,
                sequence=sequence,
                event_type="run_cancelled",
                occurred_at=now_iso,
                expires_at=str(run.get(F.EXPIRES_AT, "")),
                prior_state=state,
                next_state=F.STATE_CANCELLED,
                reason_code=F.FAIL_CANCELLED_BY_USER,
                correlation_id=correlation_id,
            )
            return F.STATE_CANCELLED

        state = _execute(transaction)
        # Collected after the fact; a stage doc read here cannot affect correctness.
        task_names: list[str] = []
        if state == F.STATE_CANCELLED:
            try:
                for snap in _stages_ref(uid, run_id).stream():
                    stage = snap.to_dict() or {}
                    if stage.get(F.STAGE_STATE) in F.STAGE_ACTIVE_STATES and stage.get(
                        F.TASK_NAME
                    ):
                        task_names.append(str(stage[F.TASK_NAME]))
            except Exception as exc:  # pragma: no cover - best effort only
                logger.debug(
                    "research.store: cancel task-name sweep failed",
                    {"run_id": run_id, "error": str(exc)},
                )
        return state, task_names

    return await asyncio.to_thread(_run)
