"""Pure data model and state machine for the durable LLM batch queue.

No I/O lives here: every function is a pure decision over values, so the whole
state machine can be exercised with a throwaway ``python -c`` against the real
objects instead of a live Firestore or a live provider.

The queue exists because the Gemini Batch API is asynchronous by design — a job
targets a 24h turnaround and is EXPIRED by the provider after 48h pending-or-
running. Nothing can await that inside a request, so work is persisted, polled on
the existing scheduler tick, and dispatched to a registered handler when it lands.

Two consequences shape everything below:

- ``EXPIRED`` is a NORMAL terminal state, not an anomaly. It is documented
  behaviour at 48h, so it gets a first-class state and a re-enqueue-once policy
  rather than an exception path.
- A finished job can be PARTIALLY successful (``batchStats.failedRequestCount``),
  and an individual result line may be an error object rather than a response. So
  item state is tracked per item, never inferred from the job.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

# Provider-side job states. Kept as plain strings (not an enum) because they
# arrive as `batch_job.state.name` and an unrecognised future value must degrade
# rather than raise.
#
# The public docs page lists six states; the installed google-genai SDK's
# `types.JobState` enum actually defines TWELVE. The extras are mapped explicitly
# below because two of them matter a great deal:
#
#   JOB_STATE_PARTIALLY_SUCCEEDED — a finished job with some failed keys. If this
#       fell through to "still running" the job would be polled until local
#       expiry and its SUCCESSFUL results would never be dispatched. It must map
#       to `succeeded` so results are fetched; per-key failures are then handled
#       from the result lines, which is where partial failure belongs anyway.
#   JOB_STATE_QUEUED — accepted but not started. Distinct from RUNNING only for
#       observability, mapped to `pending`.
PROVIDER_PENDING = "JOB_STATE_PENDING"
PROVIDER_QUEUED = "JOB_STATE_QUEUED"
PROVIDER_RUNNING = "JOB_STATE_RUNNING"
PROVIDER_SUCCEEDED = "JOB_STATE_SUCCEEDED"
PROVIDER_PARTIALLY_SUCCEEDED = "JOB_STATE_PARTIALLY_SUCCEEDED"
PROVIDER_FAILED = "JOB_STATE_FAILED"
PROVIDER_CANCELLED = "JOB_STATE_CANCELLED"
PROVIDER_CANCELLING = "JOB_STATE_CANCELLING"
PROVIDER_EXPIRED = "JOB_STATE_EXPIRED"
PROVIDER_PAUSED = "JOB_STATE_PAUSED"
PROVIDER_UPDATING = "JOB_STATE_UPDATING"
PROVIDER_UNSPECIFIED = "JOB_STATE_UNSPECIFIED"

# Our own job states. `dispatched` is ours alone: it marks that a SUCCEEDED job's
# results have been handed to the registered handler, so a re-poll cannot deliver
# the same results twice.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_EXPIRED = "expired"
JOB_DISPATCHED = "dispatched"

_PROVIDER_TO_JOB: dict[str, str] = {
    PROVIDER_PENDING: JOB_PENDING,
    PROVIDER_QUEUED: JOB_PENDING,
    PROVIDER_RUNNING: JOB_RUNNING,
    PROVIDER_UPDATING: JOB_RUNNING,
    PROVIDER_PAUSED: JOB_RUNNING,
    PROVIDER_CANCELLING: JOB_RUNNING,
    PROVIDER_UNSPECIFIED: JOB_RUNNING,
    PROVIDER_SUCCEEDED: JOB_SUCCEEDED,
    # Finished with some failed keys: fetch the results, then resolve per key.
    PROVIDER_PARTIALLY_SUCCEEDED: JOB_SUCCEEDED,
    PROVIDER_FAILED: JOB_FAILED,
    PROVIDER_CANCELLED: JOB_CANCELLED,
    PROVIDER_EXPIRED: JOB_EXPIRED,
}

# Job states after which the provider will never change its mind.
TERMINAL_JOB_STATES = frozenset(
    {JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED, JOB_EXPIRED, JOB_DISPATCHED}
)
# States where polling still has something to learn.
OPEN_JOB_STATES = frozenset({JOB_PENDING, JOB_RUNNING, JOB_SUCCEEDED})

# Item states.
ITEM_PENDING = "pending"        # queued, not yet in a provider job
ITEM_SUBMITTED = "submitted"    # inside a provider job, awaiting results
ITEM_FAILED = "failed"          # provider returned an error for this key
ITEM_DISPATCHED = "dispatched"  # handler ran successfully; terminal
ITEM_ABANDONED = "abandoned"    # retries exhausted; the caller must fall back

TERMINAL_ITEM_STATES = frozenset({ITEM_DISPATCHED, ITEM_ABANDONED})

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

# The provider expires a job after 48h pending-or-running. We give ourselves a
# margin and declare a job expired locally a little later, so a provider that
# stops answering can never leave a job open forever.
PROVIDER_EXPIRY_HOURS = 48
LOCAL_EXPIRY_MARGIN_HOURS = 6

# An item that lands in a FAILED/EXPIRED job is re-enqueued this many times
# before being abandoned. One retry: a second failure is almost never transient,
# and the caller always has a fallback (the curated catalog), so burning more
# quota to avoid a graceful degradation is the wrong trade.
MAX_ITEM_ATTEMPTS = 2

# Polling backoff. Jobs live for hours, so a fixed per-minute poll is ~1440
# wasted reads per job per day. Doubling from 1 minute to a 15-minute ceiling
# costs ~100 polls across a full 24h job.
POLL_BASE_SECONDS = 60
POLL_MAX_SECONDS = 900

# Submission trigger: send a job once enough work has piled up, or once the
# oldest queued item has waited long enough that batching more is not worth the
# added delay.
SUBMIT_MIN_ITEMS = 12
SUBMIT_MAX_WAIT_MINUTES = 20

# The provider caps concurrent jobs at 100. Stay well under it so an unrelated
# subsystem using this same queue can always get a job through.
MAX_CONCURRENT_JOBS = 40



# ---------------------------------------------------------------------------
# Item keys
# ---------------------------------------------------------------------------

# `{job_kind}:{uid_hash8}:{artifact_id}:{seq}` — the audit spine. It is the one
# string that appears in the provider job, the Firestore item doc, our logs and
# the generated artifact's audit block, so any one of them can be traced to the
# others in both directions.
#
# It is an IDENTIFIER, not a routing table: dispatch reads `job_kind` and
# `owner_uid` off the item document, which is authoritative. An inverse parser was
# built for a routing strategy that was not adopted and has been removed rather
# than left to imply the key is parsed somewhere.
#
# The uid is HASHED, never embedded: keys are sent to the provider and appear in
# logs, and a raw Firebase uid in either is an avoidable identifier leak.
_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")


def uid_hash(user_id: str) -> str:
    """Stable 8-hex-char digest of a uid, for keys and display names."""
    return hashlib.sha256((user_id or "").encode("utf-8")).hexdigest()[:8]


def build_item_key(*, job_kind: str, user_id: str, artifact_id: str, seq: int) -> str:
    """Compose an item key. Raises on segments that would make it unparseable."""
    for name, value in (("job_kind", job_kind), ("artifact_id", artifact_id)):
        if not value or not _KEY_SEGMENT.match(value):
            raise ValueError(f"llm_batch: {name} must match [A-Za-z0-9_.-]+, got {value!r}")
    if seq < 0:
        raise ValueError(f"llm_batch: seq must be non-negative, got {seq}")
    return f"{job_kind}:{uid_hash(user_id)}:{artifact_id}:{seq}"




# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class BatchItem:
    """One request queued for batch execution."""

    key: str
    job_kind: str
    owner_uid: str
    request: dict[str, Any]
    # A provider job targets exactly one model, so the queue is packed by model.
    # job_kind deliberately does NOT partition jobs: dispatch is per item via the
    # key, so one job can carry several kinds and pack better for it.
    model: str = ""
    # Free-form pointer home (e.g. the mix doc's week_start) so a handler can
    # find the artifact without re-deriving it from the key.
    context_ref: str = ""
    state: str = ITEM_PENDING
    attempt: int = 1
    error: str = ""
    job_id: str = ""
    created_at: datetime | None = None
    dispatched_at: datetime | None = None


@dataclass
class BatchJob:
    """One submitted provider job and our bookkeeping around it."""

    job_id: str
    job_kind: str
    model: str
    provider_job_name: str = ""
    state: str = JOB_PENDING
    item_count: int = 0
    failed_request_count: int = 0
    poll_attempts: int = 0
    display_name: str = ""
    submitted_at: datetime | None = None
    last_polled_at: datetime | None = None
    terminal_at: datetime | None = None
    error: str = ""
    result_file_name: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------


def normalize_provider_state(raw: Any) -> str:
    """Map a provider state name onto our job state.

    An unknown value maps to ``running`` rather than raising: the provider may
    add states, and treating an unrecognised one as "still working" keeps the job
    under the local-expiry guard below instead of silently dropping it.
    """
    name = getattr(raw, "name", None) or str(raw or "")
    return _PROVIDER_TO_JOB.get(name.strip().upper(), JOB_RUNNING)


def is_terminal_job(state: str) -> bool:
    return state in TERMINAL_JOB_STATES


def next_poll_delay_seconds(poll_attempts: int) -> int:
    """Exponential backoff with jitter, capped.

    Jitter matters here specifically: jobs cut in the same tick share a
    ``submitted_at``, so a deterministic schedule keeps a whole cohort's polls in
    lockstep for the job's entire life and every ``batches.get`` fires in the same
    second. Every other backoff in this repo jitters
    (``model_provider.py:1300``, ``:1372``, ``claude_client.py:204``); this one
    had no reason to be the exception.
    """
    if poll_attempts < 0:
        poll_attempts = 0
    # 1 << n overflows nothing here because the cap bites by attempt 4.
    delay = min(POLL_BASE_SECONDS * (1 << min(poll_attempts, 8)), POLL_MAX_SECONDS)
    # +/-20%, never below the base interval.
    jittered = delay * random.uniform(0.8, 1.2)
    return max(POLL_BASE_SECONDS, int(jittered))


def should_poll(job: BatchJob, *, now: datetime) -> bool:
    """Is this job due for a status check?

    A job already in a terminal state is never polled again, which is what makes
    ``dispatched`` worth having as a distinct state.
    """
    if job.state in TERMINAL_JOB_STATES and job.state != JOB_SUCCEEDED:
        return False
    if job.state == JOB_SUCCEEDED:
        # Succeeded-but-not-dispatched: results are waiting, fetch immediately.
        return True
    reference = job.last_polled_at or job.submitted_at
    if reference is None:
        return True
    return now - reference >= timedelta(seconds=next_poll_delay_seconds(job.poll_attempts))


def is_locally_expired(job: BatchJob, *, now: datetime) -> bool:
    """True when a job has outlived the provider's own 48h expiry plus margin.

    Defensive: the provider is expected to move the job to EXPIRED itself. This
    catches the case where polling has been failing and nobody has been able to
    read that transition, so the job would otherwise sit open forever.
    """
    if job.submitted_at is None:
        return False
    limit = timedelta(hours=PROVIDER_EXPIRY_HOURS + LOCAL_EXPIRY_MARGIN_HOURS)
    return now - job.submitted_at > limit


def should_submit(
    *, pending_count: int, oldest_pending_at: datetime | None, open_job_count: int, now: datetime
) -> bool:
    """Is it time to cut a new provider job from the pending queue?

    Batching across users is the whole point: generation is already delivered a
    visit late, so waiting to accumulate items costs nothing the user perceives
    and buys a better-packed job.
    """
    if pending_count <= 0:
        return False
    if open_job_count >= MAX_CONCURRENT_JOBS:
        return False
    if pending_count >= SUBMIT_MIN_ITEMS:
        return True
    if oldest_pending_at is None:
        return False
    return now - oldest_pending_at >= timedelta(minutes=SUBMIT_MAX_WAIT_MINUTES)


def item_outcome_for_terminal_job(item_state: str, job_state: str, attempt: int) -> str:
    """What becomes of an item when its job reaches a terminal state.

    Items that already succeeded keep their result — a job can be partially
    successful, and throwing away good work because a sibling key failed would be
    both wasteful and wrong.
    """
    if item_state in TERMINAL_ITEM_STATES:
        return item_state
    if job_state == JOB_SUCCEEDED:
        # Job succeeded but this key produced nothing usable: an error line.
        return ITEM_FAILED if attempt >= MAX_ITEM_ATTEMPTS else ITEM_PENDING
    if job_state in (JOB_FAILED, JOB_EXPIRED, JOB_CANCELLED):
        return ITEM_ABANDONED if attempt >= MAX_ITEM_ATTEMPTS else ITEM_PENDING
    return item_state


def utcnow() -> datetime:
    return datetime.now(UTC)
