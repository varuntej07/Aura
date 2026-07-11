"""agent_runs — the observability span for one Self-Heal Envelope run.

Every envelope run writes ONE doc to ``users/{uid}/agent_runs/{run_id}`` recording
the agent, the terminal status, and every attempt + recovery taken. The system is
non-deterministic and a bug may be an event-ordering or recovery-loop issue, so a
durable, per-user, queryable trace is non-negotiable (a log line alone is not
enough). Best-effort: a tracing write never raises into the run it is tracing.

It captures the envelope's own control flow (sense/plan/act/verify/repair).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ...lib.logger import logger
from ..firebase import admin_firestore

AGENT_RUNS_SUBCOLLECTION = "agent_runs"

# A run trace is for debugging recent behavior; native Firestore TTL on
# ``expires_at`` reaps it so the collection cannot grow unbounded.
AGENT_RUN_TTL = timedelta(days=7)

# ── Field-name contract ──────────────────────────────────────────────────────
FIELD_RUN_ID = "run_id"
FIELD_AGENT = "agent"
FIELD_INTENT = "intent"
FIELD_STATUS = "status"
FIELD_REASON = "reason"
FIELD_ATTEMPTS = "attempts"
FIELD_REPAIRS = "repairs"
FIELD_REPLANS = "replans"
FIELD_STEPS = "steps"
FIELD_DELIVERED = "delivered"
FIELD_EVENT_TYPE = "event_type"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"


@dataclass
class AgentRunTrace:
    """Accumulates the steps of one envelope run, then is flushed to Firestore."""

    run_id: str
    agent: str
    intent: str
    user_id: str
    event_type: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add_step(self, step: str, **detail: Any) -> None:
        """Record one control-flow step (sense/plan/act/verify/repair/escalate) and
        log it loudly so nothing is silent."""
        entry = {"step": step, **detail}
        self.steps.append(entry)
        logger.info("envelope.step", {
            "agent": self.agent,
            "user_id": self.user_id,
            **entry,
        })


async def record_run(
    trace: AgentRunTrace,
    *,
    status: str,
    reason: str,
    attempts: int,
    repairs: int,
    replans: int,
    delivered: bool | None,
    now: datetime | None = None,
) -> None:
    """Persist the run trace. Best-effort — never raises into the envelope."""
    when = now or datetime.now(UTC)
    doc = {
        FIELD_RUN_ID: trace.run_id,
        FIELD_AGENT: trace.agent,
        FIELD_INTENT: trace.intent,
        FIELD_STATUS: status,
        FIELD_REASON: reason,
        FIELD_ATTEMPTS: attempts,
        FIELD_REPAIRS: repairs,
        FIELD_REPLANS: replans,
        FIELD_STEPS: trace.steps,
        FIELD_DELIVERED: delivered,
        FIELD_EVENT_TYPE: trace.event_type,
        FIELD_CREATED_AT: when,
        FIELD_EXPIRES_AT: when + AGENT_RUN_TTL,
    }

    def _write() -> None:
        (
            admin_firestore()
            .collection("users")
            .document(trace.user_id)
            .collection(AGENT_RUNS_SUBCOLLECTION)
            .document(trace.run_id)
            .set(doc)
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("agent_runs: record_run failed", {
            "user_id": trace.user_id, "agent": trace.agent, "error": str(exc),
        })
