"""The Agent protocol + the shared types the Self-Heal Envelope passes between an
agent's steps.

Every proactive capability (curiosity, icebreaker, news, ...) is an Agent: it
implements ``sense -> plan -> act -> verify -> repair -> to_proposal`` and the
envelope (envelope.py) gives it resilience (bounded retry/repair, escalation) and
observability (agent_runs spans) for free. Adding a capability = write one Agent
class + register it; no scheduler surgery.

The step types (``Inputs``, ``Plan.payload``, ``RawResult``) are agent-specific,
so the protocol carries them as ``Any`` and the envelope treats them opaquely. The
two things the envelope DOES read are ``Plan.act_needed`` (should I run act at
all?) and the ``Verdict`` (is the result usable, and if not, how to recover?). A
verdict, never an exception, is what triggers recovery, so an empty-but-successful
fetch and a timeout both route to repair instead of to silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .events import Event

if TYPE_CHECKING:  # avoid importing the proposal contract at module load
    from ..notifications.proposal import NotificationProposal


# ── Risk (drives the §4.6 HITL gate later) ───────────────────────────────────
RISK_LOW = "low"
RISK_MED = "med"
RISK_HIGH = "high"


# ── Verdicts (the recovery trigger) ──────────────────────────────────────────
class VerdictKind(StrEnum):
    OK = "ok"                    # result is usable; ship it
    INFRA = "infra"              # transient infra fault (429 / timeout / 5xx)
    EMPTY = "empty"              # call succeeded but produced no usable data
    LOW_QUALITY = "low_quality"  # data present but not good enough (LLM re-plan)
    POLICY = "policy"            # a no-go; stand down, do not escalate


@dataclass
class Verdict:
    kind: VerdictKind
    reason: str = ""
    # Instructive: what went wrong and what to try next, so an LLM re-plan has the
    # context to adapt rather than guess (Anthropic: "Rate limit exceeded. Retry
    # after 60 seconds.").
    detail: str = ""

    @classmethod
    def ok(cls) -> Verdict:
        return cls(VerdictKind.OK)

    @classmethod
    def infra(cls, reason: str, detail: str = "") -> Verdict:
        return cls(VerdictKind.INFRA, reason, detail)

    @classmethod
    def empty(cls, reason: str, detail: str = "") -> Verdict:
        return cls(VerdictKind.EMPTY, reason, detail)

    @classmethod
    def low_quality(cls, reason: str, detail: str = "") -> Verdict:
        return cls(VerdictKind.LOW_QUALITY, reason, detail)

    @classmethod
    def policy(cls, reason: str, detail: str = "") -> Verdict:
        return cls(VerdictKind.POLICY, reason, detail)

    @property
    def is_ok(self) -> bool:
        return self.kind == VerdictKind.OK


@dataclass
class Plan:
    """What ``plan`` decided. ``act_needed=False`` is a normal, frequent outcome
    (most curiosity ticks send nothing); the envelope short-circuits to a clean
    ``skipped`` run, never a repair. ``payload`` carries the agent's own plan
    object. ``idempotency_key`` is set only when ``act`` has an external side
    effect, in which case the envelope claims it through the shared primitive
    before acting."""

    act_needed: bool
    stand_down_reason: str = ""
    payload: Any = None
    idempotency_key: str | None = None

    @classmethod
    def stand_down(cls, reason: str) -> Plan:
        return cls(act_needed=False, stand_down_reason=reason)

    @classmethod
    def act(cls, payload: Any, *, idempotency_key: str | None = None) -> Plan:
        return cls(act_needed=True, payload=payload, idempotency_key=idempotency_key)


@dataclass
class UserContext:
    """What an agent is woken with. ``event`` is the triggering event once the
    orchestrator drives dispatch (P2); it is ``None`` for a cadence-driven run."""

    user_id: str
    event: Event | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalCase:
    """One registration-time eval scenario for an agent (poka-yoke: a new agent
    ships measured). Kept as data here; the eval harness consumes it."""

    name: str
    description: str = ""


@runtime_checkable
class Agent(Protocol):
    """The contract every proactive capability implements. Methods are async
    because Aura's I/O (Firestore, LLM) is async; the design doc's sync signatures
    are illustrative."""

    name: str                    # namespaced, e.g. "curiosity.thread_followup"
    intent: str                  # the user-need it owns (non-overlap gate key)
    subscribes_to: frozenset[str]  # which event types can wake it
    risk: str                    # RISK_LOW | RISK_MED | RISK_HIGH
    allow_degraded_delivery: bool  # on escalate, may a degraded output still ship?

    async def sense(self, ctx: UserContext) -> Any: ...
    async def plan(self, inputs: Any) -> Plan: ...
    async def act(self, plan: Plan, attempt: int) -> Any: ...
    async def verify(self, raw: Any) -> Verdict: ...
    # Return a NEW Plan to act on (re-plan / broaden / retry), or None to give up.
    async def repair(self, verdict: Verdict, plan: Plan, attempt: int) -> Plan | None: ...
    # Best-effort graceful output when the bounded loop escalates; None = ship nothing.
    async def degraded(self, plan: Plan) -> Any | None: ...
    def to_proposal(self, raw: Any) -> NotificationProposal | None: ...
    def eval_cases(self) -> list[EvalCase]: ...
