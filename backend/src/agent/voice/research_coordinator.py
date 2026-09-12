"""Who owns "we already asked, and they already answered".

The failure this exists to stop, from a live session: Buddy proposed creating a
Notion database, the user said yes, and the same question came back word for
word three more times while no research ever started. Nothing was broken in the
resolve; the problem was that the ONLY record of the confirmation was a boolean
argument the model had to remember to pass back. When it called a sibling tool
instead, or omitted the flag, the tool resolved from scratch and asked again.

So the state lives here, in code, not in the model's arguments:

  * one open question per job, with the count of times it was asked
  * the name that was actually offered out loud, so a "yes" can never create a
    database under a name the user never heard
  * the line last spoken, so nothing is ever repeated verbatim
  * a terminal guard, so a late answer cannot reopen a finished job

State names follow the A2A task vocabulary (working, input_required,
auth_required, completed, failed, canceled) because this IS that state machine:
a long-running task that sometimes has to stop and ask the user something.

Everything in this module is pure. The driver (buddy_agent) performs the I/O and
feeds the outcomes back in, which is what lets the whole conversation from that
session be replayed through ``reduce`` with no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

STATE_WORKING = "working"
STATE_INPUT_REQUIRED = "input_required"
STATE_AUTH_REQUIRED = "auth_required"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

TERMINAL_STATES = (STATE_COMPLETED, STATE_FAILED, STATE_CANCELED)

# Ask twice, never a third time. The third ask is where a live session went from
# "annoying" to unusable: by then the user has answered, and asking again reads
# as not listening. After this many asks the coordinator acts on the most recent
# explicit choice instead, or leaves the results in the app and says so once.
MAX_ASKS = 2

# Question kinds, mirroring notion_backend.decide_destination's outcomes.
KIND_ASK = "ask"  # several candidate databases, pick one
KIND_PROPOSE_CREATE = "propose_create"  # no match, offer to create a named one
KIND_UNNAMED = "unnamed"  # nothing named and nothing to choose from
KIND_REAUTH = "reauth"  # Notion connection needs reconnecting first

CHOICE_EXISTING = "existing"
CHOICE_CREATE = "create"
CHOICE_DECLINE = "decline"
CHOICE_LATER = "later"
CHOICE_CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class PendingDestination:
    """The one open question about where results go."""

    kind: str
    question: str
    candidates: tuple[tuple[str, str], ...] = ()
    # The name Buddy OFFERED out loud. A create confirmation uses this, never
    # the name the model re-sends on the confirm turn, so a "yes" cannot create
    # a database under a name the user never heard.
    proposed_name: str = ""
    asked_count: int = 0


@dataclass(frozen=True, slots=True)
class DestinationChoice:
    """What the user decided, typed, from the intake task's own tools."""

    kind: str
    database_id: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class ResearchJob:
    """One durable run and everything this session knows about it."""

    request: str
    run_id: str = ""
    state: str = STATE_WORKING
    # Where it is going, once that is settled. Empty means unbound: the run is
    # working and the results land in the app unless a destination is chosen.
    database_name: str = ""
    bound: bool = False
    pending: PendingDestination | None = None
    # Every line this job has already spoken, so a re-ask is never a repeat.
    spoken: tuple[str, ...] = field(default_factory=tuple)
    declined: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def awaiting_answer(self) -> bool:
        return self.pending is not None and not self.is_terminal


def start(request: str) -> ResearchJob:
    """A job in ``working`` before anything is known about its destination."""
    return ResearchJob(request=" ".join((request or "").split()))


def run_started(job: ResearchJob, run_id: str) -> ResearchJob:
    """The durable run exists. Nothing may claim research is running before this."""
    if job.is_terminal or not run_id:
        return job
    return replace(job, run_id=run_id)


def ask(
    job: ResearchJob,
    *,
    kind: str,
    question: str,
    candidates: tuple[tuple[str, str], ...] = (),
    proposed_name: str = "",
) -> tuple[ResearchJob, str | None]:
    """Open (or re-open) the destination question. Returns the line to speak.

    A None line means stop asking: the question has had its turns, and the
    caller must act on what it already has rather than ask again.
    """
    if job.is_terminal:
        return job, None
    previous = job.pending
    same_question = (
        previous is not None
        and previous.kind == kind
        and previous.proposed_name == proposed_name
    )
    asked = (previous.asked_count if same_question and previous else 0) + 1
    if asked > MAX_ASKS:
        return job, None
    line = _vary(question, kind=kind, proposed_name=proposed_name, attempt=asked)
    if line in job.spoken:
        # Every wording for this question has already been said. Saying any of
        # them again is the exact loop this module exists to prevent.
        return job, None
    pending = PendingDestination(
        kind=kind,
        question=question,
        candidates=tuple(candidates),
        proposed_name=proposed_name,
        asked_count=asked,
    )
    return (
        replace(
            job,
            state=STATE_AUTH_REQUIRED if kind == KIND_REAUTH else STATE_INPUT_REQUIRED,
            pending=pending,
            spoken=job.spoken + (line,),
        ),
        line,
    )


def choose(job: ResearchJob, choice: DestinationChoice) -> ResearchJob:
    """Apply the user's typed decision. The question closes either way."""
    if job.is_terminal:
        return job
    if choice.kind == CHOICE_CANCEL:
        return replace(job, state=STATE_CANCELED, pending=None)
    if choice.kind == CHOICE_DECLINE:
        # Declining a destination does NOT cancel the research. The run keeps
        # working and the brief lands in the app.
        return replace(job, state=STATE_WORKING, pending=None, declined=True)
    if choice.kind == CHOICE_LATER:
        # The question stays open, but it has been asked enough. Keeping the
        # pending record is what lets a later "put it in my CRM" bind without
        # re-resolving anything.
        return replace(job, state=STATE_WORKING)
    return replace(job, state=STATE_WORKING, pending=None)


def create_name_for(job: ResearchJob, choice: DestinationChoice) -> str:
    """The name a create confirmation may use: the one offered, then theirs.

    Order matters. The name Buddy said out loud wins, because that is the one
    the user agreed to; a name the user supplies themselves in the same breath
    is the only thing that may override it.
    """
    if choice.kind != CHOICE_CREATE:
        return ""
    spoken_name = " ".join((choice.name or "").split())
    if spoken_name:
        return spoken_name[:200]
    pending = job.pending
    return (pending.proposed_name if pending else "")[:200]


def bound(job: ResearchJob, database_name: str, *, pending_delivery: bool) -> ResearchJob:
    """A destination is settled. ``pending_delivery`` means the run is still working."""
    if job.is_terminal:
        return job
    return replace(
        job,
        database_name=database_name,
        bound=True,
        pending=None,
        state=STATE_WORKING if pending_delivery else job.state,
    )


def finished(job: ResearchJob, state: str) -> ResearchJob:
    """The backend run reached a result. Terminal here is absorbing."""
    if job.is_terminal:
        return job
    if state in ("ready", "partial"):
        return replace(job, state=STATE_COMPLETED, pending=None)
    if state == "cancelled":
        return replace(job, state=STATE_CANCELED, pending=None)
    if state == "failed":
        return replace(job, state=STATE_FAILED, pending=None)
    return job


def may_claim_running(job: ResearchJob | None) -> bool:
    """Whether "it's running" is TRUE. No run id, no claim."""
    return bool(job and job.run_id and not job.is_terminal)


def _vary(question: str, *, kind: str, proposed_name: str, attempt: int) -> str:
    """The same question, worded from state rather than repeated or randomized.

    The first ask is the resolver's own wording. A second ask means the first
    answer did not land (a mangled transcript, a sibling tool called instead),
    so it is shorter and names what is already known, which is also the shape
    that is easiest to answer with one word.
    """
    if attempt <= 1:
        return question
    if kind == KIND_PROPOSE_CREATE and proposed_name:
        return f"Just to confirm: make a new Notion database called {proposed_name}?"
    if kind == KIND_ASK:
        return "Which of those two should it go in?"
    if kind == KIND_REAUTH:
        return "Reconnect Notion from the dashboard and I'll put it there."
    return "What should I call the Notion database for this?"
