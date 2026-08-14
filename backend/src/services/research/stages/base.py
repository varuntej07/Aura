"""What a stage receives and what it returns. Pure data, zero imports beyond the models.

Frozen dataclasses rather than pydantic models on purpose. These never cross an API or
a model boundary and never get validated against untrusted input: they are in-process
handles passed between the engine and a stage body inside one request. The pydantic
models in ``models.py`` are for the wire and for LLM structured output; these are not.
That is the same split ``meetings/store.py`` uses for ``JobLease``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..budget import RunBudget


class StageResultKind(StrEnum):
    """What the engine should do with a finished stage body.

    The kind decides the SHAPE of the advance transaction, not the destination state.
    A stage names its own next state; the kind tells the engine whether that transition
    also has to create a fan-out coordinator, park for an answer, or go absorbing.
    """

    # Ordinary forward progress: set next_state, create next_jobs.
    DONE = "done"
    # Park the run awaiting a clarification answer. Creates NO paid-work job, which is
    # the whole point: a parked run holds no reservation and has debited no credit.
    CLARIFY = "clarify"
    # Create a coord doc plus one child job per ordinal, then let the join reassemble.
    FANOUT = "fanout"
    # Absorbing. Search, read, verify and synthesize all refuse a run in this state.
    TERMINAL = "terminal"
    # A phase-two stub ran. The engine records the stage as done and stops the run
    # rather than inventing a transition a real stage has not been written to make yet.
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class NextJob:
    """One job the advance transaction must create, deterministically.

    ``ordinal`` is the source_id for a fan-out child and "0" for everything else, which
    is what makes the resulting stage_id unique per child without a counter.
    """

    stage_kind: str
    wave: int = 0
    ordinal: str = "0"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageContext:
    """Everything a stage body may read. Notably NOT a Firestore handle.

    A stage cannot reach the database through this, by construction. If a stage needs a
    persisted value it has to arrive here, which keeps the read set of every stage
    visible in one place instead of scattered through provider code.
    """

    uid: str
    run_id: str
    stage_id: str
    stage_kind: str
    wave: int
    ordinal: str
    attempt: int
    # Pinned at admission. A stage refusing a different version is what stops a task
    # delayed behind a clarification round from running against a newer interpretation.
    admitted_plan_version: int
    budget: RunBudget
    # Units this stage was actually granted, which may be fewer than it asked for.
    grant: dict[str, int] = field(default_factory=dict)
    # True when the grant came back short. The stage runs smaller and reports the
    # shortfall as a gap rather than failing.
    degraded: bool = False
    request_text: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    # Every clarification the user has answered on this run, oldest first. Read from the
    # RUN DOCUMENT by the engine, not threaded through the job payload: the run doc is
    # the single owner of this list, and a payload copy would be a second version of it
    # that can disagree after a retry or a superseded question.
    #
    # classify_plan derives its round counter from the length of this list, so an empty
    # one does not merely lose the answer, it resets the two-round clarification cap and
    # lets the same question be asked indefinitely.
    clarification_answers: tuple[dict[str, Any], ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    # Re-read between external calls so a 240s stage cannot burn its whole extract
    # quota after the user has already cancelled. Injected by the engine, not fetched.
    is_cancelled: Any = None
    # What this stage has spent SO FAR, updated as the spend happens rather than reported
    # once at the end.
    #
    # The dataclass is frozen and these two are mutable containers on purpose. A stage
    # that raises never returns a StageResult, so everything it consumed before raising
    # was invisible: the engine released the whole grant and the retry spent it all over
    # again, against a ledger with no record of the first attempt. A Firecrawl page bought
    # by an attempt that then died in extraction is bought. This is where it stays visible.
    spent: dict[str, int] = field(default_factory=dict)
    spent_cost: dict[str, Any] = field(
        default_factory=lambda: {"microusd": 0, "known": True}
    )

    def record_spend(
        self,
        actuals: dict[str, int],
        cost_microusd: int = 0,
        *,
        cost_known: bool = True,
    ) -> None:
        """Add to the running total of what this stage has irreversibly consumed."""
        for unit, value in (actuals or {}).items():
            self.spent[unit] = int(self.spent.get(unit, 0)) + int(value)
        self.spent_cost["microusd"] = int(self.spent_cost.get("microusd", 0)) + int(
            cost_microusd
        )
        if not cost_known:
            self.spent_cost["known"] = False

    @property
    def spent_cost_microusd(self) -> int:
        return int(self.spent_cost.get("microusd", 0))

    @property
    def spent_cost_known(self) -> bool:
        return bool(self.spent_cost.get("known", True))


@dataclass(frozen=True)
class StageResult:
    """What a stage body hands back for exactly one advance transaction to commit."""

    kind: StageResultKind
    # The run state this stage moves to. Empty leaves the state untouched, which is
    # what a fan-out child does: children never move the run, only the join does.
    next_state: str = ""
    next_jobs: tuple[NextJob, ...] = ()
    # Merged onto the run doc. The engine owns state, revision, audit sequence and
    # timestamps; a stage must not put those here.
    run_updates: dict[str, Any] = field(default_factory=dict)
    # Merged onto this stage's own doc, for the audit trail.
    stage_outputs: dict[str, Any] = field(default_factory=dict)
    # Documents to create under the run, as {subcollection: {doc_id: fields}}. The
    # engine stamps expires_at on every one, so a stage cannot forget retention.
    documents: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Documents to UPDATE, same shape. Separate from ``documents`` because the create
    # there is a deliberate collision guard: a replayed advance must not silently
    # overwrite a live document. A fan-out child needs the other half of that, since its
    # source document was already created by the search wave that discovered the URL,
    # and merging the two would quietly disable the guard everywhere else.
    document_updates: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # unit -> units ACTUALLY consumed. Drives reserved -> used; the remainder of the
    # grant is released in the same transaction.
    actuals: dict[str, int] = field(default_factory=dict)
    cost_microusd: int = 0
    # False means the numeric cost is only the known floor. Settlement must retain the
    # reservation estimate instead of treating that floor as the complete spend.
    cost_known: bool = True
    # CLARIFY only. One compact question, which may group up to three related fields.
    questions: tuple[dict[str, Any], ...] = ()
    # FANOUT only: how many children the join must wait for.
    expected_children: int = 0
    # TERMINAL only. A stable FAIL_* code, never a provider exception string.
    failure_code: str = ""

    def __post_init__(self) -> None:
        # Cheap structural guards. These catch a stage that returns a shape its kind
        # cannot mean, which would otherwise surface as a confusing half-transition.
        if self.kind is StageResultKind.FANOUT and self.expected_children <= 0:
            raise ValueError("FANOUT result must expect at least one child")
        if self.kind is StageResultKind.CLARIFY and not self.questions:
            raise ValueError("CLARIFY result must carry at least one question")
        if self.kind is StageResultKind.CLARIFY and self.next_jobs:
            raise ValueError("CLARIFY result must create no paid-work job")
