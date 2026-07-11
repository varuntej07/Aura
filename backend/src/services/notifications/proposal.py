"""The proposal contract + the pure decision logic the orchestrator runs.

Everything here is side-effect free so it can be unit-tested without Firestore
(``tests/test_notification_orchestrator.py``). The orchestrator (orchestrator.py)
supplies the I/O (queue reads, ledger reads, the FCM send); this module only
decides *what* should happen to a proposal.

Single source of truth for: the source identifiers, the priority ladder, and the
per-source hard freshness windows. A producer that emits a notification names its
source from the ``SOURCE_*`` constants here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the ledger at module load (keeps this pure)
    from ..notification_ledger import NotificationDecision


# ── Source identifiers (the 7 producers) ────────────────────────────────────
SOURCE_REMINDER = "reminder"
SOURCE_TRACKING = "tracking"
SOURCE_CALENDAR = "calendar"
SOURCE_THREAD = "thread"
SOURCE_BRIEFING = "briefing"
SOURCE_ICEBREAKER = "icebreaker"
SOURCE_NEWS = "news"  # the signal-engine content path
SOURCE_REENGAGE = "reengage"  # the dormancy win-back opener (before the 7-day cliff)
SOURCE_CHAT_REPLY = "chat_reply"  # "your Buddy reply is ready" after a backgrounded turn
SOURCE_FOLLOWUP = "followup"  # a revocable pending-intent follow-up ("how did mom's surgery go?")
SOURCE_DEVICE_LINK = "device_link"  # security alert: a new device paired to the account
SOURCE_TRIAL = "trial"  # trial-ending / trial-ended account lifecycle notice
SOURCE_WELCOME = "welcome"  # one-time day-0 welcome, fired once off first device registration
SOURCE_BILLING = "billing"  # entitlement-updated sync push after a payment webhook write

ALL_SOURCES = (
    SOURCE_REMINDER,
    SOURCE_TRACKING,
    SOURCE_CALENDAR,
    SOURCE_THREAD,
    SOURCE_BRIEFING,
    SOURCE_ICEBREAKER,
    SOURCE_NEWS,
    SOURCE_REENGAGE,
    SOURCE_CHAT_REPLY,
    SOURCE_FOLLOWUP,
    SOURCE_DEVICE_LINK,
    SOURCE_TRIAL,
    SOURCE_WELCOME,
    SOURCE_BILLING,
)


class ProposalKind(str, Enum):
    """How a proposal is allowed to travel through the orchestrator.

    COMMITTED = the user asked for it (reminder, tracking subscription, calendar,
    the guaranteed daily briefing). It is never held or arbitrated; it sends inline
    the instant it is submitted, subject only to freshness + dedup.

    PROACTIVE = engine-initiated (thread, icebreaker, news). It is enqueued and the
    drain arbitrates it against everything else pending for that user.
    """

    COMMITTED = "committed"
    PROACTIVE = "proactive"


# ── Priority ladder (higher wins arbitration) ───────────────────────────────
# Set by the product owner: a thing the user explicitly asked Buddy to remind
# them of edges out an auto-tracked topic when both want the same instant, so
# reminder (96) sits one above tracking (95). Icebreaker is second-lowest, news
# (RSS content) is lowest. Arbitration only bites between two PROACTIVE proposals
# in the same window; committed sends bypass it entirely.
PRIORITY: dict[str, int] = {
    SOURCE_REMINDER: 96,
    SOURCE_TRACKING: 95,
    # "A new device just got your account" is a security alert the user must see now.
    # COMMITTED, so it sends inline and never competes; the 95 documents its rank
    # alongside tracking if it ever has to.
    SOURCE_DEVICE_LINK: 95,
    # A reply to a question the user JUST asked Buddy and is waiting on. COMMITTED, so it
    # sends inline and bypasses arbitration anyway; the rank only matters if it ever has
    # to compete. Sits just under the time-exact reminder/tracking, above calendar.
    SOURCE_CHAT_REPLY: 94,
    # The devices-refetch nudge after a payment webhook writes the entitlement doc
    # ("your plan is active"). COMMITTED, so it sends inline and bypasses arbitration;
    # the rank only matters if it ever has to compete. A purchase the user just made
    # sits right under their awaited chat reply, above calendar.
    SOURCE_BILLING: 92,
    SOURCE_CALENDAR: 90,
    # Trial ending/ended is important account info the user must see, akin to the
    # security alert above. COMMITTED, so it sends inline and bypasses arbitration;
    # the rank only matters if it ever has to compete.
    SOURCE_TRIAL: 88,
    # A user about to churn (5-6 days idle, before the 7-day active cliff) is the most
    # valuable proactive moment there is, and it fires at most once per dormancy
    # episode, so it outranks the routine proactive openers when both want a window.
    SOURCE_REENGAGE: 80,
    # A follow-up Buddy promised itself about something personal ("how did mom's
    # surgery go?"). It is implicitly user-wanted and emotionally load-bearing, so it
    # edges out the routine curiosity thread but sits under the about-to-churn win-back.
    SOURCE_FOLLOWUP: 75,
    SOURCE_THREAD: 70,
    SOURCE_BRIEFING: 60,
    SOURCE_ICEBREAKER: 20,
    SOURCE_NEWS: 10,
    # The first thing Buddy ever says to a new account. COMMITTED, so it sends
    # inline and bypasses arbitration; the rank only matters if it ever has to
    # compete. Sits just under chat_reply, above calendar.
    SOURCE_WELCOME: 93,
}
DEFAULT_PRIORITY = 50


# ── Hard freshness windows ──────────────────────────────────────────────────
# A proposal whose underlying event/article is older than its window is DROPPED
# before it can ever fire — the gate the old pipeline never had (a morning match
# could resurface as an evening push because freshness was only a soft score
# weight). ``None`` = not time-bound (personal openers: thread, icebreaker,
# briefing-of-the-day). Tracking varies by topic kind (a live match vs an
# open interest), so its producer overrides ``freshness_max_age`` per proposal;
# the value here is only the fallback when it does not.
FRESHNESS_MAX_AGE: dict[str, timedelta | None] = {
    SOURCE_TRACKING: timedelta(hours=6),
    SOURCE_NEWS: timedelta(hours=18),
    SOURCE_REMINDER: None,    # time-exact; content_timestamp is irrelevant
    SOURCE_CALENDAR: None,    # time-exact
    SOURCE_THREAD: None,
    SOURCE_ICEBREAKER: None,
    SOURCE_BRIEFING: None,
    SOURCE_REENGAGE: None,    # a win-back opener is untimed (personal, not content)
    SOURCE_CHAT_REPLY: None,  # the user's own awaited reply: deliver whenever it is ready
    SOURCE_FOLLOWUP: None,    # a personal promise; its fire time is the intent's, not content age
    SOURCE_DEVICE_LINK: None,  # a security alert never goes stale; it always delivers
    SOURCE_TRIAL: None,       # account lifecycle notice; not content, never stale
    SOURCE_WELCOME: None,     # a scripted greeting; not content, never stale
    SOURCE_BILLING: None,     # a plan-state sync nudge; not content, never stale
}


# ── Dispositions the pipeline can reach ─────────────────────────────────────
class Disposition(str, Enum):
    SEND = "send"    # deliver now
    HOLD = "hold"    # keep in the queue, reconsider next window (proactive only)
    DROP = "drop"    # terminal; never deliver this proposal


@dataclass
class OrchestratorDecision:
    disposition: Disposition
    reason: str
    # On a SEND, whether FCM actually reached a device. Producers that keep a
    # per-user cursor (the tracker advances ``last_sent_summary`` only on a real
    # delivery) read this; it is None for HOLD/DROP.
    delivered: bool | None = None
    # FCM delivery counts, surfaced on a SEND so a committed caller that needs the
    # no-devices vs all-rejected distinction (the calendar handler decides Cloud
    # Tasks retry on it) keeps full fidelity. None on HOLD/DROP.
    tokens_targeted: int | None = None
    success_count: int | None = None
    failure_count: int | None = None


# Reasons (single source of truth so logs + tests agree on the strings).
REASON_STALE = "stale"
REASON_DUPLICATE = "duplicate"
REASON_QUIET_HOURS = "quiet_hours"
REASON_BUDGET = "budget"
REASON_SUPERSEDED = "superseded"
REASON_TAP_GATE = "low_tap_value"
REASON_OFF_PEAK = "off_peak"
REASON_PRESENCE = "user_present"  # surface-aware: in the app now / on a dismiss streak
REASON_ACTIVE_TRACKER = "active_tracker"  # a tracked event (e.g. a live match) fired recently
REASON_OK = "ok"


@dataclass
class NotificationProposal:
    """One candidate notification handed to ``orchestrator.submit``.

    Producers build this and never touch FCM directly. Copy (``title``/``body``)
    is already framed by the producer at submit time; for the proactive lane the
    framed copy rides along in the queue doc so a held proposal is never re-framed.
    """

    user_id: str
    source: str                      # one of SOURCE_*
    kind: ProposalKind
    dedup_key: str                   # stable content identity for cross-agent dedup

    title: str = ""
    body: str = ""
    data: dict[str, str] = field(default_factory=dict)
    collapse_key: str | None = None
    notification_type: str = ""      # client routing key; defaults to ``source``
    data_only: bool = False
    apns_category: str | None = None

    # When the underlying event/article actually happened. Drives the freshness
    # gate. ``None`` for time-exact committed sends (reminder/calendar).
    content_timestamp: datetime | None = None
    # Per-proposal override of the source freshness window (tracking sets this
    # from the topic kind). ``None`` falls back to ``FRESHNESS_MAX_AGE[source]``.
    freshness_max_age: timedelta | None = None
    # Per-proposal override of the source priority. ``None`` falls back to PRIORITY.
    priority: int | None = None

    # Optional learning-substrate metadata persisted to the ledger (signal/news).
    decision: "NotificationDecision | None" = None

    def __post_init__(self) -> None:
        if not self.notification_type:
            self.notification_type = self.source

    @property
    def effective_priority(self) -> int:
        if self.priority is not None:
            return self.priority
        return PRIORITY.get(self.source, DEFAULT_PRIORITY)

    @property
    def effective_max_age(self) -> timedelta | None:
        if self.freshness_max_age is not None:
            return self.freshness_max_age
        return FRESHNESS_MAX_AGE.get(self.source)


# ── Pure decision helpers (unit-tested without any I/O) ─────────────────────
def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_stale(proposal: NotificationProposal, now: datetime) -> bool:
    """True when the proposal's content is older than its freshness window.

    No window or no content_timestamp => never stale (personal openers, time-exact
    sends). This is a HARD gate: a stale proposal is dropped, never sent.
    """
    max_age = proposal.effective_max_age
    if max_age is None or proposal.content_timestamp is None:
        return False
    return (_as_aware(now) - _as_aware(proposal.content_timestamp)) > max_age


def proposal_sort_key(p: NotificationProposal) -> tuple[int, int, float]:
    """Arbitration ordering (use with ``reverse=True``): highest priority first,
    then a proposal that carries a ``content_timestamp`` over one that does not,
    then the more recent timestamp. Shared by ``arbitrate`` and the drain so the
    winner is chosen the same way whether we sort proposals or (id, proposal) pairs.
    """
    ts = p.content_timestamp
    has_ts = 1 if ts is not None else 0
    ts_epoch = _as_aware(ts).timestamp() if ts is not None else 0.0
    return (p.effective_priority, has_ts, ts_epoch)


def arbitrate(
    proposals: list[NotificationProposal],
) -> tuple[NotificationProposal | None, list[NotificationProposal]]:
    """Pick the single winner among competing proactive proposals.

    Highest ``effective_priority`` wins; ties break toward the more recent
    ``content_timestamp`` (a fresher item is the better thing to surface), and a
    proposal that carries a timestamp beats one that does not. Returns
    ``(winner, losers)``; losers are HELD by the caller, never dropped, so a
    low-priority news item is not killed by a thread, it just waits its turn.
    """
    if not proposals:
        return None, []
    ordered = sorted(proposals, key=proposal_sort_key, reverse=True)
    return ordered[0], ordered[1:]


def freshness_decision(
    proposal: NotificationProposal, now: datetime
) -> OrchestratorDecision | None:
    """DROP if stale, else ``None`` (pass)."""
    if is_stale(proposal, now):
        return OrchestratorDecision(Disposition.DROP, REASON_STALE)
    return None


def dedup_decision(
    proposal: NotificationProposal, recent_dedup_keys: set[str]
) -> OrchestratorDecision | None:
    """DROP if this exact content was already sent recently, else ``None``."""
    if proposal.dedup_key and proposal.dedup_key in recent_dedup_keys:
        return OrchestratorDecision(Disposition.DROP, REASON_DUPLICATE)
    return None
