"""Reserve, commit, release. The per-run and per-project spend guard.

The whole design is one sentence: units are RESERVED before a provider is called and
reconciled to actual afterwards, in the same transaction that commits the stage. That
ordering is the deliberate trade. Committing after the call risks charging for work
never done and losing evidence; reserving before it risks over-counting when a stage
crashes mid-flight. Over-counting is the correct direction for a cost guard, so the
reservation stays held until the sweeper explicitly declares the stage dead.

**This module fails CLOSED, and that is a deliberate divergence from
``reactive/cost_cap.py``.** That module returns True when its meter read raises, which
is right for a free reactive loop where a Firestore blip should not silence a user's
notifications. Research is metered: every admitted unit becomes a Brave query, a
Firecrawl credit or model tokens. An outage that removed the only spend boundary would
be strictly worse than a refused run, so every read failure here raises
``MeterUnavailableError`` and the caller must abandon the stage WITHOUT a provider call.

A refusal is also never a hard no. ``reserve`` grants what is available and reports the
shortfall, so a run that runs out of budget degrades into a smaller, sourced, partial
brief with named gaps instead of dying. Exhaustion is a product outcome here, not an
error path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as gcloud_firestore

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from . import fields as F
from .budget import RunBudget
from .store import (
    _day_key,
    _ledger_ref,
    _project_budget_ref,
    _project_receipt_ref,
    project_receipt_id,
)


class MeterUnavailableError(RuntimeError):
    """The budget meter could not be read or written.

    The caller MUST treat this as "do not call the provider" and let the stage retry.
    It is never converted into a zero grant, because a zero grant looks like ordinary
    exhaustion and would route the run to a partial brief as though the budget were
    genuinely spent.
    """

    def __init__(self, message: str = "research budget meter unavailable") -> None:
        super().__init__(message)
        self.code = F.FAIL_METER_UNAVAILABLE


@dataclass(frozen=True)
class Grant:
    """Units this stage may actually spend. Possibly fewer than it asked for."""

    stage_id: str
    units: dict[str, int] = field(default_factory=dict)
    degraded: bool = False
    # True when this exact stage_id already held a reservation, i.e. a redelivery.
    replayed: bool = False

    def get(self, unit: str) -> int:
        return int(self.units.get(unit, 0))

    @property
    def empty(self) -> bool:
        return not any(v > 0 for v in self.units.values())


def _remaining(ledger: dict[str, Any], budget: RunBudget, unit: str) -> int:
    """max - used - reserved, floored at zero."""
    ceiling = int(getattr(budget, F.UNIT_BUDGET_ATTR[unit]))
    used = int((ledger.get(F.LEDGER_USED) or {}).get(unit, 0))
    reserved = int((ledger.get(F.LEDGER_RESERVED) or {}).get(unit, 0))
    return max(0, ceiling - used - reserved)


class RunCostCapReached(RuntimeError):
    """The run's own micro-USD ceiling would be breached by this stage.

    Distinct from ``MeterUnavailableError``: the meter was readable and the answer was no.
    The caller degrades to a partial brief rather than retrying, because retrying cannot
    make the run cheaper.
    """

    def __init__(self, message: str = "research run cost ceiling reached") -> None:
        super().__init__(message)
        self.code = F.FAIL_COST_CAP_REACHED


def _run_spendable(run: dict[str, Any]) -> bool:
    """Whether a reservation transaction may make provider work reachable."""
    return bool(run) and not (
        run.get(F.CANCEL_REQUESTED_AT)
        or run.get(F.HIDDEN_AT)
        or run.get(F.DELETION_STATE)
        or run.get(F.STATE) in F.TERMINAL_STATES
    )


async def reserve(
    uid: str,
    run_id: str,
    stage_id: str,
    *,
    budget: RunBudget,
    request: dict[str, int],
    cost_microusd: int = 0,
) -> Grant:
    """Reserve units for one stage. Idempotent per stage_id. Partial, never a refusal.

    A redelivered task hitting an existing reservation gets that reservation back
    verbatim rather than a second one. Without that, every Cloud Tasks retry would
    silently double the run's reserved footprint and starve it into a partial brief.

    ``cost_microusd`` is this stage's worst-case dollar estimate, gated in the SAME
    transaction as the units against the run's own ceiling:
    ``actual + reserved + requested <= budget.cost_microusd_max``. It has to be the same
    transaction, because two stages checking a ceiling they each then write to would both
    pass a check neither of them still satisfies. Units are a partial grant; dollars are
    not, because half a dollar ceiling is not a smaller run, it is an unbounded one.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _run() -> Grant:
        db = admin_firestore()
        run_ref = (
            db.collection(F.PARENT_COLLECTION)
            .document(uid)
            .collection(F.SUBCOLLECTION)
            .document(run_id)
        )
        ledger_ref = _ledger_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> Grant:
            run_snap = run_ref.get(transaction=txn)
            snap = ledger_ref.get(transaction=txn)
            run = (run_snap.to_dict() or {}) if run_snap.exists else {}
            if not _run_spendable(run):
                raise MeterUnavailableError("run is not spendable")
            if not snap.exists:
                # No ledger means the run was never created properly. Refusing to
                # invent one keeps an unmetered stage from ever reaching a provider.
                raise MeterUnavailableError("run ledger missing")
            ledger = snap.to_dict() or {}

            reservations = dict(ledger.get(F.LEDGER_RESERVATIONS) or {})
            if stage_id in reservations:
                existing = {k: int(v) for k, v in (reservations[stage_id] or {}).items()}
                return Grant(
                    stage_id=stage_id,
                    units=existing,
                    degraded=any(
                        existing.get(u, 0) < int(n) for u, n in request.items() if int(n) > 0
                    ),
                    replayed=True,
                )

            wanted_cost = max(0, int(cost_microusd))
            cost_actual = int(ledger.get(F.LEDGER_COST_MICROUSD, 0))
            cost_reserved = int(ledger.get(F.LEDGER_RESERVED_MICROUSD, 0))
            if wanted_cost and (
                cost_actual + cost_reserved + wanted_cost > int(budget.cost_microusd_max)
            ):
                raise RunCostCapReached(
                    f"run cost ceiling reached: {cost_actual}+{cost_reserved}+{wanted_cost} "
                    f"> {budget.cost_microusd_max}"
                )

            reserved = dict(ledger.get(F.LEDGER_RESERVED) or {})
            granted: dict[str, int] = {}
            degraded = False
            for unit, wanted in request.items():
                wanted_n = int(wanted)
                if wanted_n <= 0 or unit not in F.UNIT_BUDGET_ATTR:
                    continue
                available = _remaining(ledger, budget, unit)
                give = min(wanted_n, available)
                granted[unit] = give
                if give < wanted_n:
                    degraded = True
                reserved[unit] = int(reserved.get(unit, 0)) + give

            reservations[stage_id] = granted
            cost_reservations = dict(ledger.get(F.LEDGER_COST_RESERVATIONS) or {})
            cost_reservations[stage_id] = wanted_cost
            txn.update(
                ledger_ref,
                {
                    F.LEDGER_RESERVED: reserved,
                    F.LEDGER_RESERVATIONS: reservations,
                    F.LEDGER_RESERVED_MICROUSD: cost_reserved + wanted_cost,
                    F.LEDGER_COST_RESERVATIONS: cost_reservations,
                    F.LEDGER_UPDATED_AT: now_iso,
                },
            )
            return Grant(stage_id=stage_id, units=granted, degraded=degraded)

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except (MeterUnavailableError, RunCostCapReached):
        raise
    except Exception as exc:
        logger.error(
            "research.ledger: reserve failed, failing closed",
            {"run_id": run_id, "stage_id": stage_id, "error": str(exc),
             "error_code": F.FAIL_METER_UNAVAILABLE},
        )
        raise MeterUnavailableError(str(exc)) from exc


def release_patch(
    ledger: dict[str, Any], stage_id: str
) -> tuple[dict[str, Any], dict[str, int]]:
    """The ledger patch that frees one stage's reservation. Pure dict math, no I/O.

    Same shape and same reason as ``store._commit_units``: keeping it pure lets a caller
    apply it inside a transaction it is already taking, instead of following that
    transaction with a second one.

    That difference is load bearing for the sweeper. Releasing AFTER the transaction that
    re-arms the stage leaves a window where the stage is already dispatchable: a
    redelivery can claim it and reserve fresh units in that gap, and the sweeper's late
    release then frees the NEW owner's live grant, not the dead one's.
    """
    reservations = dict(ledger.get(F.LEDGER_RESERVATIONS) or {})
    cost_reservations = dict(ledger.get(F.LEDGER_COST_RESERVATIONS) or {})
    granted = {k: int(v) for k, v in (reservations.pop(stage_id, {}) or {}).items()}
    held_cost = int(cost_reservations.pop(stage_id, 0) or 0)
    if not granted and not held_cost:
        return {}, {}
    reserved = dict(ledger.get(F.LEDGER_RESERVED) or {})
    for unit, n in granted.items():
        reserved[unit] = max(0, int(reserved.get(unit, 0)) - int(n))
    # The dollar reservation is released with the units, and floored at zero for the same
    # reason every other decrement here is: a counter that can go negative silently
    # manufactures headroom, which is the one direction a spend guard must never fail in.
    reserved_cost = max(0, int(ledger.get(F.LEDGER_RESERVED_MICROUSD, 0)) - held_cost)
    return (
        {
            F.LEDGER_RESERVED: reserved,
            F.LEDGER_RESERVATIONS: reservations,
            F.LEDGER_RESERVED_MICROUSD: reserved_cost,
            F.LEDGER_COST_RESERVATIONS: cost_reservations,
        },
        granted,
    )


async def release(uid: str, run_id: str, stage_id: str) -> dict[str, int]:
    """Free a dead stage's reservation. SWEEPER ONLY.

    Deliberately not called on the normal path: a live stage's reservation is released
    by ``store.advance`` as part of the same transaction that commits its actuals. This
    exists for the stage that never came back, and calling it early would hand a crashed
    stage's units to a second attempt that may still be running.
    """
    now_iso = datetime.now(UTC).isoformat()

    def _run() -> dict[str, int]:
        db = admin_firestore()
        ledger_ref = _ledger_ref(uid, run_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> dict[str, int]:
            snap = ledger_ref.get(transaction=txn)
            if not snap.exists:
                return {}
            ledger = snap.to_dict() or {}
            # Same pure dict math the sweeper applies inside its own transaction, so the
            # unit and dollar reservations can never be freed by two different rules.
            patch, granted = release_patch(ledger, stage_id)
            if not patch:
                return {}
            txn.update(ledger_ref, dict(patch, **{F.LEDGER_UPDATED_AT: now_iso}))
            return granted

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.error(
            "research.ledger: release failed",
            {"run_id": run_id, "stage_id": stage_id, "error": str(exc)},
        )
        return {}


async def snapshot(uid: str, run_id: str) -> dict[str, Any]:
    """Read the ledger. Raises rather than returning empty, so callers fail closed."""

    def _run() -> dict[str, Any]:
        snap = _ledger_ref(uid, run_id).get()
        if not snap.exists:
            raise MeterUnavailableError("run ledger missing")
        return snap.to_dict() or {}

    try:
        return await asyncio.to_thread(_run)
    except MeterUnavailableError:
        raise
    except Exception as exc:
        raise MeterUnavailableError(str(exc)) from exc


# --- project-day spend -----------------------------------------------------------
#
# A durable RECEIPT per stage attempt, not a pair of aggregate counters.
#
# Two counters cannot be settled correctly, and both failures were live. Settlement
# recomputed the day from the clock, so a stage that reserved at 23:59 and finished at
# 00:01 credited one day and debited another, driving the second day's reserved figure
# negative and handing back headroom that was never held there. And a bare counter has no
# record of WHICH stage still owes a settlement, so every exit that was not a clean
# success - timeout, provider exception, empty grant, unreadable meter, cancellation,
# stale-lease recovery, terminal failure - leaked its reservation until the day rolled.
#
# A receipt carries its own day and its own amount, so a settlement can only ever close
# the exact reservation it opened, and a crash leaves enough on disk for the sweeper to
# close it later.


@dataclass(frozen=True)
class ProjectReceipt:
    """A reservation this stage attempt holds against the project's daily wallet."""

    day: str
    receipt_id: str
    estimate_microusd: int

    @property
    def held(self) -> bool:
        return bool(self.day and self.receipt_id and self.estimate_microusd > 0)


# Returned when nothing was reserved (a zero estimate, or a cap of zero). Settling it is
# a no-op, so callers never have to branch on whether a reservation happened.
NO_PROJECT_RECEIPT = ProjectReceipt(day="", receipt_id="", estimate_microusd=0)


async def reserve_project_spend(
    microusd: int,
    *,
    uid: str = "",
    run_id: str = "",
    stage_id: str = "",
    attempt: int = 0,
    deadline_at: str = "",
) -> ProjectReceipt | None:
    """Reserve gross provider spend against the project's daily wallet.

    ``None`` means the project cap is reached and the run must degrade to a partial
    brief. A read or write failure raises rather than returning a receipt: the
    project-day cap is the last line between a bug and an unbounded bill.

    Idempotent on ``(stage_id, attempt)``. A redelivery of the same attempt gets its own
    receipt back rather than reserving a second time; a genuine retry is a new attempt and
    reserves again, which is correct because it will spend again.

    Setting ``PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD`` to 0 stops all admission. That
    is the intended full-stop control and is a budget value, not a feature flag.
    """
    cap = int(settings.PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD)
    if cap <= 0:
        return None
    if microusd <= 0:
        return NO_PROJECT_RECEIPT
    now = datetime.now(UTC)
    day = _day_key(now)
    now_iso = now.isoformat()
    receipt_id = project_receipt_id(stage_id or run_id, attempt)
    # Firestore TTL policies only act on timestamp-typed fields. Other research expiry
    # strings are also read as ordering fences, but this receipt expiry is TTL-only, so
    # persist the aware datetime itself rather than an ISO string TTL would ignore.
    expires_at = now + timedelta(days=F.PROJECT_RECEIPT_TTL_DAYS)

    def _run() -> ProjectReceipt | None:
        db = admin_firestore()
        ref = _project_budget_ref(day)
        receipt_ref = _project_receipt_ref(day, receipt_id)
        run_ref = (
            db.collection(F.PARENT_COLLECTION)
            .document(uid)
            .collection(F.SUBCOLLECTION)
            .document(run_id)
        )
        stage_ref = (
            run_ref
            .collection(F.STAGES_SUBCOLLECTION)
            .document(stage_id)
        )
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> ProjectReceipt | None:
            snap = ref.get(transaction=txn)
            receipt_snap = receipt_ref.get(transaction=txn)
            run_snap = run_ref.get(transaction=txn)
            run = (run_snap.to_dict() or {}) if run_snap.exists else {}
            if not _run_spendable(run):
                raise MeterUnavailableError("run is not spendable")
            if receipt_snap.exists:
                existing = receipt_snap.to_dict() or {}
                if existing.get(F.RECEIPT_STATE) == F.RECEIPT_RESERVED:
                    txn.set(stage_ref, {
                        F.STAGE_PROJECT_RECEIPT_DAY: day,
                        F.STAGE_PROJECT_RECEIPT_ID: receipt_id,
                    }, merge=True)
                    # A redelivery of this exact attempt. Hand back the reservation it
                    # already holds instead of opening a second one against the same work.
                    return ProjectReceipt(
                        day=str(existing.get(F.RECEIPT_DAY) or day),
                        receipt_id=receipt_id,
                        estimate_microusd=int(
                            existing.get(F.RECEIPT_ESTIMATE_MICROUSD, 0)
                        ),
                    )
                # Already settled or released. This attempt is done; reserving again would
                # double-count work the wallet has already accounted for.
                return NO_PROJECT_RECEIPT

            data = (snap.to_dict() or {}) if snap.exists else {}
            reserved = int(data.get(F.PROJECT_RESERVED_MICROUSD, 0))
            # Committed spend counts against the cap too. Gating on `reserved` alone
            # made this bound CONCURRENT spend rather than DAILY spend: settlement
            # decrements `reserved` as it moves money into `actual_microusd`, so every
            # stage that finished handed its headroom straight back and a sequence of
            # runs could exceed the daily cap without limit. The whole point of a
            # day-keyed document is that what was already spent today still counts.
            actual = int(data.get(F.PROJECT_ACTUAL_MICROUSD, 0))
            if reserved + actual + microusd > cap:
                return None

            payload = {
                F.PROJECT_RESERVED_MICROUSD: reserved + microusd,
                F.UPDATED_AT: now_iso,
            }
            if snap.exists:
                txn.update(ref, payload)
            else:
                txn.set(
                    ref,
                    dict(payload, **{F.RECEIPT_DAY: day, F.PROJECT_ACTUAL_MICROUSD: 0}),
                )
            txn.set(
                receipt_ref,
                {
                    F.RECEIPT_DAY: day,
                    F.RECEIPT_STAGE_ID: stage_id,
                    F.RECEIPT_ATTEMPT: int(attempt),
                    F.RECEIPT_RUN_ID: run_id,
                    F.RECEIPT_USER_ID: uid,
                    F.RECEIPT_ESTIMATE_MICROUSD: int(microusd),
                    F.RECEIPT_STATE: F.RECEIPT_RESERVED,
                    # What the sweeper uses to decide this receipt was abandoned. Without
                    # a deadline on the receipt itself, a crashed stage's dollars would be
                    # held until the day document expired.
                    F.RECEIPT_DEADLINE_AT: deadline_at,
                    F.CREATED_AT: now_iso,
                    F.EXPIRES_AT: expires_at,
                },
            )
            txn.set(stage_ref, {
                F.STAGE_PROJECT_RECEIPT_DAY: day,
                F.STAGE_PROJECT_RECEIPT_ID: receipt_id,
            }, merge=True)
            return ProjectReceipt(
                day=day, receipt_id=receipt_id, estimate_microusd=int(microusd)
            )

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.error(
            "research.ledger: project reservation failed, failing closed",
            {"run_id": run_id, "stage_id": stage_id, "error": str(exc),
             "error_code": F.FAIL_METER_UNAVAILABLE},
        )
        raise MeterUnavailableError(str(exc)) from exc


async def settle_project_receipt(
    receipt: ProjectReceipt | None,
    actual_microusd: int | None,
    *,
    released: bool = False,
) -> bool:
    """Close one reservation against the EXACT day and amount it was opened with.

    ``actual_microusd=None`` means the real cost could not be determined. The estimate is
    then RETAINED as actual rather than released: an unpriced attempt is an attempt whose
    cost we could not see, not a free one, and reopening its headroom would let an outage
    in the pricing path quietly raise the daily ceiling.

    ``released=True`` is for the narrow case where the stage provably spent nothing at all
    (refused before any provider call). Only then is the whole estimate handed back.

    Best effort on purpose: the reservation already bounded the spend, so a failure here
    costs reporting accuracy, never money, and the sweeper closes what is left behind.
    Returns True when the receipt was closed by this call.
    """
    if receipt is None or not receipt.held:
        return False
    day = receipt.day
    estimate = max(0, int(receipt.estimate_microusd))
    if released:
        settled_actual = 0
    elif actual_microusd is None:
        # Conservative retention. See the docstring: unknown is not zero.
        settled_actual = estimate
    else:
        settled_actual = max(0, int(actual_microusd))
    now_iso = datetime.now(UTC).isoformat()

    def _run() -> bool:
        db = admin_firestore()
        ref = _project_budget_ref(day)
        receipt_ref = _project_receipt_ref(day, receipt.receipt_id)
        transaction = db.transaction()

        @gcloud_firestore.transactional
        def _execute(txn: Any) -> bool:
            receipt_snap = receipt_ref.get(transaction=txn)
            if not receipt_snap.exists:
                return False
            current = receipt_snap.to_dict() or {}
            if current.get(F.RECEIPT_STATE) != F.RECEIPT_RESERVED:
                # Already closed, by an earlier settlement or by the sweeper. Settling
                # twice would decrement `reserved` for a hold that is no longer there.
                return False
            snap = ref.get(transaction=txn)
            data = (snap.to_dict() or {}) if snap.exists else {}
            held = int(current.get(F.RECEIPT_ESTIMATE_MICROUSD, estimate))
            reserved = int(data.get(F.PROJECT_RESERVED_MICROUSD, 0))
            actual = int(data.get(F.PROJECT_ACTUAL_MICROUSD, 0))
            txn.set(
                ref,
                {
                    # Floored at zero. A reserved figure that can go negative silently
                    # manufactures daily headroom, which is exactly the failure the
                    # recomputed-day settlement used to cause.
                    F.PROJECT_RESERVED_MICROUSD: max(0, reserved - held),
                    F.PROJECT_ACTUAL_MICROUSD: actual + settled_actual,
                    F.RECEIPT_DAY: day,
                    F.UPDATED_AT: now_iso,
                },
                merge=True,
            )
            txn.update(
                receipt_ref,
                {
                    F.RECEIPT_STATE: (
                        F.RECEIPT_RELEASED if released else F.RECEIPT_SETTLED
                    ),
                    F.RECEIPT_ACTUAL_MICROUSD: settled_actual,
                    F.RECEIPT_COST_KNOWN: actual_microusd is not None and not released,
                    F.UPDATED_AT: now_iso,
                },
            )
            return True

        return _execute(transaction)

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warn(
            "research.ledger: project settlement failed, left for the sweeper",
            {"day": day, "receipt_id": receipt.receipt_id, "error": str(exc)},
        )
        return False


async def sweep_project_receipts(*, limit: int = 50) -> int:
    """Settle receipts whose stage died holding them. Crash recovery for the wallet.

    Settled at the FULL estimate, never released: a stage that vanished past its own
    deadline may well have spent everything it reserved, and assuming otherwise would hand
    the project back headroom for money that is gone. Over-counting is the correct
    direction for a spend guard, and the same direction the unit ledger already takes.
    """
    now_iso = datetime.now(UTC).isoformat()

    def _query() -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        query = (
            admin_firestore()
            .collection_group(F.PROJECT_RECEIPTS_SUBCOLLECTION)
            .where(F.RECEIPT_STATE, "==", F.RECEIPT_RESERVED)
            .where(F.RECEIPT_DEADLINE_AT, "<=", now_iso)
            .order_by(F.RECEIPT_DEADLINE_AT)
            .limit(limit)
        )
        for snap in query.stream():
            data = snap.to_dict() or {}
            rows.append((
                str(data.get(F.RECEIPT_DAY, "")),
                snap.id,
                int(data.get(F.RECEIPT_ESTIMATE_MICROUSD, 0)),
            ))
        return rows

    try:
        rows = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.error(
            "research.ledger: receipt sweep query failed",
            {"error": str(exc), "error_code": "research_receipt_sweep_failed"},
        )
        return 0

    closed = 0
    for day, receipt_id, estimate in rows:
        if not day:
            continue
        settled = await settle_project_receipt(
            ProjectReceipt(day=day, receipt_id=receipt_id, estimate_microusd=estimate),
            None,
        )
        if settled:
            closed += 1
    if closed:
        logger.warn(
            "research.ledger: orphaned project receipts settled at their estimate",
            {"count": closed, "metric": "research_receipt_sweep"},
        )
    return closed
