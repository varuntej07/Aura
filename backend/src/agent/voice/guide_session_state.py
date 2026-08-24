"""Session-owned conversation state for Guide Mode handoffs.

Guide task progress already has a durable Firestore model in ``guide_models.py``.
This module owns the separate, shorter-lived question of who controls the live
LiveKit conversation. Agent instances are deliberately absent: they can be
replaced during a handoff, while ``AgentSession.userdata`` survives it.

Entry and exit are two-phase operations:

- A native Guide arm reserves a ``GuideStartClaim`` without changing ownership.
- ``GuideSupervisorAgent.on_enter`` will commit that claim once LiveKit has
  actually activated the supervisor.
- A return request moves to ``RETURN_PENDING`` but Guide keeps ownership until
  ``BuddyAgent.on_enter`` commits ``IDLE``.

The Guide supervisor and Buddy consume this contract directly. No agent-local
persona flag is allowed to substitute for this session-owned authority.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from ...lib.logger import logger

GUIDE_START_CLAIM_TTL_S = 30.0


class GuidePhase(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    ACTIVE = "active"
    RETURN_PENDING = "return_pending"
    TERMINATED = "terminated"


_ALLOWED_TRANSITIONS: dict[GuidePhase, frozenset[GuidePhase]] = {
    GuidePhase.IDLE: frozenset({GuidePhase.PLANNING}),
    GuidePhase.PLANNING: frozenset(
        {
            GuidePhase.ACTIVE,
            GuidePhase.RETURN_PENDING,
            GuidePhase.TERMINATED,
        }
    ),
    GuidePhase.ACTIVE: frozenset(
        {
            GuidePhase.PLANNING,
            GuidePhase.RETURN_PENDING,
            GuidePhase.TERMINATED,
        }
    ),
    GuidePhase.TERMINATED: frozenset({GuidePhase.RETURN_PENDING}),
    # Idempotent so an interrupted handback can be requested again.
    GuidePhase.RETURN_PENDING: frozenset(
        {
            GuidePhase.IDLE,
            GuidePhase.RETURN_PENDING,
            GuidePhase.TERMINATED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class GuideStartClaim:
    """A native arm accepted by the worker but not yet activated by LiveKit."""

    guide_session_id: str
    generation: int
    protocol_version: int
    ownership_epoch: int
    claimed_at: float
    resume_task_id: str = ""

    def is_expired(self, now: float) -> bool:
        return (now - self.claimed_at) >= GUIDE_START_CLAIM_TTL_S


@dataclass(slots=True)
class GuideSessionState:
    """Guide conversation ownership shared by every agent in one session."""

    phase: GuidePhase = GuidePhase.IDLE
    guide_session_id: str = ""
    generation: int = -1
    protocol_version: int = 2
    ownership_epoch: int = 0
    pending_start: GuideStartClaim | None = None
    task_id: str = ""
    resume_task_id: str = ""

    @property
    def active(self) -> bool:
        """Whether Guide still owns the floor, including a pending handback."""
        return self.phase is not GuidePhase.IDLE

    def is_current(self, guide_session_id: str, ownership_epoch: int) -> bool:
        """Whether an async result still belongs to the active Guide ownership."""
        return (
            bool(guide_session_id)
            and guide_session_id == self.guide_session_id
            and ownership_epoch == self.ownership_epoch
        )

    def claim_start(
        self,
        *,
        guide_session_id: str,
        generation: int,
        protocol_version: int,
        resume_task_id: str | None = None,
    ) -> GuideStartClaim | None:
        """Reserve an arm without claiming that the supervisor is active yet."""
        if self.phase is not GuidePhase.IDLE or generation <= self.generation:
            return None

        now = time.monotonic()
        outstanding = self.pending_start
        if outstanding is not None and not outstanding.is_expired(now):
            logger.info(
                "Guide: duplicate start refused",
                {
                    "guide_session_id": outstanding.guide_session_id,
                    "generation": outstanding.generation,
                    "ownership_epoch": outstanding.ownership_epoch,
                    "age_s": round(now - outstanding.claimed_at, 3),
                },
            )
            return None
        if outstanding is not None:
            logger.warn(
                "Guide: start claim expired without activation",
                {
                    "guide_session_id": outstanding.guide_session_id,
                    "generation": outstanding.generation,
                    "ownership_epoch": outstanding.ownership_epoch,
                    "age_s": round(now - outstanding.claimed_at, 3),
                },
            )

        self.ownership_epoch += 1
        self.guide_session_id = guide_session_id
        self.task_id = ""
        self.resume_task_id = resume_task_id or ""
        claim = GuideStartClaim(
            guide_session_id=guide_session_id,
            generation=generation,
            protocol_version=protocol_version,
            ownership_epoch=self.ownership_epoch,
            claimed_at=now,
            resume_task_id=self.resume_task_id,
        )
        self.pending_start = claim
        return claim

    def commit_entry(self, claim: GuideStartClaim) -> bool:
        """Commit entry only after the Guide supervisor has become active."""
        outstanding = self.pending_start
        now = time.monotonic()
        if outstanding is None or outstanding != claim or claim.is_expired(now):
            logger.warn(
                "Guide: entry commit rejected",
                {
                    "claim_guide_session_id": claim.guide_session_id,
                    "claim_generation": claim.generation,
                    "claim_epoch": claim.ownership_epoch,
                    "current_epoch": self.ownership_epoch,
                    "phase": str(self.phase),
                    "expired": claim.is_expired(now),
                },
            )
            if outstanding == claim:
                self.pending_start = None
                self.guide_session_id = ""
                self.resume_task_id = ""
            return False
        if not self.is_current(claim.guide_session_id, claim.ownership_epoch):
            return False
        # Generation is committed with ownership, not when the arm is merely
        # reserved. If LiveKit never activates the supervisor, the desktop may
        # safely retry that same generation instead of being locked out by a
        # transition that never happened.
        self.generation = claim.generation
        self.protocol_version = claim.protocol_version
        if not self.transition(GuidePhase.PLANNING, "supervisor_entered"):
            return False
        self.pending_start = None
        return True

    def cancel_start(self, claim: GuideStartClaim, reason: str) -> bool:
        """Release a reservation that never became a LiveKit handoff."""
        if self.phase is not GuidePhase.IDLE or self.pending_start != claim:
            return False
        logger.warn(
            "Guide: start claim cancelled",
            {
                "guide_session_id": claim.guide_session_id,
                "generation": claim.generation,
                "ownership_epoch": claim.ownership_epoch,
                "reason": reason,
            },
        )
        self.pending_start = None
        self.guide_session_id = ""
        self.task_id = ""
        self.resume_task_id = ""
        # Invalidate any supervisor activation still in flight for this claim.
        self.ownership_epoch += 1
        return True

    def note_active(self) -> bool:
        """Record that initial planning completed and normal guidance owns the floor."""
        return self.transition(GuidePhase.ACTIVE, "planning_completed")

    def begin_planning(self) -> bool:
        """Give one bounded planning task the floor without changing ownership."""
        return self.transition(GuidePhase.PLANNING, "planning_started")

    def finish_planning(self) -> bool:
        """Return a completed planning task to the active Guide supervisor."""
        return self.transition(GuidePhase.ACTIVE, "planning_completed")

    def adopt_task(self, task_id: str) -> bool:
        """Attach the durable Guide task selected for this live ownership epoch."""
        normalized = task_id.strip()
        if not normalized or self.phase not in {GuidePhase.PLANNING, GuidePhase.ACTIVE}:
            return False
        self.task_id = normalized
        return True

    def request_return(self, reason: str) -> bool:
        """Request Buddy without claiming the handoff has already completed."""
        return self.transition(GuidePhase.RETURN_PENDING, reason)

    def terminate(self, reason: str) -> bool:
        """Record an abnormal Guide end while Guide still owns the conversation."""
        return self.transition(GuidePhase.TERMINATED, reason)

    def commit_idle(self, ownership_epoch: int) -> bool:
        """Commit Buddy ownership only once the same BuddyAgent has entered."""
        if ownership_epoch != self.ownership_epoch:
            logger.warn(
                "Guide: idle commit rejected, epoch moved",
                {
                    "resume_epoch": ownership_epoch,
                    "current_epoch": self.ownership_epoch,
                    "phase": str(self.phase),
                },
            )
            return False
        if not self.transition(GuidePhase.IDLE, "buddy_entered"):
            return False
        self.ownership_epoch += 1
        self.pending_start = None
        self.guide_session_id = ""
        self.task_id = ""
        self.resume_task_id = ""
        return True

    def transition(self, to: GuidePhase, reason: str) -> bool:
        """Apply one legal ownership transition and reject every other move."""
        if to not in _ALLOWED_TRANSITIONS.get(self.phase, frozenset()):
            logger.warn(
                "Guide: illegal transition refused",
                {
                    "from": str(self.phase),
                    "to": str(to),
                    "reason": reason,
                    "guide_session_id": self.guide_session_id,
                    "generation": self.generation,
                    "ownership_epoch": self.ownership_epoch,
                },
            )
            return False
        previous = self.phase
        self.phase = to
        logger.info(
            "Guide: phase",
            {
                "from": str(previous),
                "to": str(to),
                "reason": reason,
                "guide_session_id": self.guide_session_id,
                "generation": self.generation,
                "ownership_epoch": self.ownership_epoch,
            },
        )
        return True
