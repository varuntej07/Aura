"""Stage bodies for one research run.

A stage is one bounded unit of work: exactly one Cloud Task, exactly one lease, exactly
one state-advance transaction. The bodies here are deliberately the DUMBEST part of the
engine. They perform I/O and they return a StageResult, and that is all. They write no
Firestore state, they know nothing about Cloud Tasks, leases, outboxes or budgets, and
they never decide what state a run moves to next by writing it themselves.

That constraint is not tidiness. It is the swap test from the architecture document: a
stage body that touches no durability primitive is reusable verbatim as a Temporal
activity, so the engine underneath can be replaced without reopening a single stage.

Phase two ships every stage as a STUB. The registry is complete and the engine can be
driven end to end, but no stub calls Brave, Firecrawl or a model. Phase four replaces
them one at a time; `StageStub.is_stub` is how a caller can tell which are still inert.
"""

from __future__ import annotations

from .base import (
    NextJob,
    StageContext,
    StageResult,
    StageResultKind,
)
from .registry import REGISTRY, StageNotRegisteredError, get_stage, is_stub

__all__ = [
    "REGISTRY",
    "NextJob",
    "StageContext",
    "StageNotRegisteredError",
    "StageResult",
    "StageResultKind",
    "get_stage",
    "is_stub",
]
