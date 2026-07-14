"""Moments — the sparse per-fixture notification schedule that replaced the poll grid.

A fixture gets AT MOST three scheduled moments (founder decision 2026-07-10):

  pre      start - lead (default 30 min): "Spain vs Belgium kicks off in 30 minutes".
           Fetchless — the fixture doc is the fact source.
  kickoff  at start: "it's underway". Also fetchless.
  result   at expected end: the one moment that fetches the web, extracts structured
           facts, and pushes ONLY on a fact transition (see fact_gate). Not yet
           determinable -> bounded re-arm (+RESULT_RECHECK_DELAY, max
           MAX_RESULT_CHECKS), the only polling in the engine, confined to the
           narrow uncertainty window after the expected end.

Moment doc ids are deterministic per (topic_key, fixture_id, moment) with NO
timestamp component, so a rescheduled fixture UPDATES its moments in place — forking
a duplicate series is structurally impossible, not just discouraged.

This module also hosts the fetch-query construction, the notify window, and the
adaptive pulse (relocated here when the poll-grid schedule builder was deleted).
Pure functions only (no I/O).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import fields as f
from .models import Checkpoint, Fixture

# ── Moment timing ────────────────────────────────────────────────────────────
PRE_LEAD_DEFAULT = timedelta(minutes=30)
PRE_LEAD_MIN = timedelta(minutes=15)
PRE_LEAD_MAX = timedelta(hours=2)

# A pre/kickoff push is only useful NEAR its moment. Firing later than these (a
# backlogged queue, a re-armed doc) abstains instead — the exact bug where "kicks off
# soon!" was delivered 40 minutes after kickoff.
PRE_USEFUL_UNTIL_AFTER_START = timedelta(minutes=10)
KICKOFF_USEFUL_FOR = timedelta(minutes=15)

# The result moment's bounded re-arm loop: the ONLY polling in the engine.
RESULT_RECHECK_DELAY = timedelta(minutes=12)
MAX_RESULT_CHECKS = 5
# Give a result check landing before the fixture even started nothing to do but wait.
RESULT_NOT_BEFORE_MARGIN = timedelta(minutes=1)

_MOMENT_PHASES = (
    f.CHECKPOINT_PHASE_PRE,
    f.CHECKPOINT_PHASE_KICKOFF,
    f.CHECKPOINT_PHASE_RESULT,
)


def moment_id(topic_key: str, fixture_id: str, moment: str) -> str:
    """Deterministic moment doc id. No timestamp: a moved kickoff updates the SAME
    doc's fire_at instead of minting a sibling."""
    return f"{topic_key}__{fixture_id}__{moment}"


def clamped_pre_lead(lead_minutes: int) -> timedelta:
    """Research may widen/narrow the heads-up; clamped so it can neither fire a week
    early nor 30 seconds before kickoff."""
    if lead_minutes <= 0:
        return PRE_LEAD_DEFAULT
    return max(PRE_LEAD_MIN, min(PRE_LEAD_MAX, timedelta(minutes=lead_minutes)))


def build_moments(fixture: Fixture, *, now: datetime | None = None) -> list[Checkpoint]:
    """The moment checkpoints for one fixture. Past moments are not enqueued (a
    tracker provisioned mid-match gets no retroactive "kicks off soon"), with one
    exception: a result whose expected end already passed but whose outcome is still
    unknown checks shortly after now, so a mid-match provision still gets the final.

    A finished or cancelled fixture gets no moments at all."""
    now = now or datetime.now(UTC)
    if fixture.status in (f.FIXTURE_STATUS_FINISHED, f.FIXTURE_STATUS_CANCELLED):
        return []

    fires: list[tuple[str, datetime]] = []
    pre_at = fixture.start_at - clamped_pre_lead(fixture.lead_minutes)
    if pre_at > now:
        fires.append((f.CHECKPOINT_PHASE_PRE, pre_at))
    if fixture.start_at > now:
        fires.append((f.CHECKPOINT_PHASE_KICKOFF, fixture.start_at))

    expected_end = fixture.expected_end_at or fixture.start_at
    result_at = max(expected_end, now + RESULT_NOT_BEFORE_MARGIN)
    fires.append((f.CHECKPOINT_PHASE_RESULT, result_at))

    return [
        Checkpoint(
            id=moment_id(fixture.topic_key, fixture.id, phase),
            topic_key=fixture.topic_key,
            event_label=fixture.label,
            phase=phase,
            fire_at=fire_at,
            status=f.CHECKPOINT_STATUS_PENDING,
            wake_override=fixture.wake_override,
            fixture_id=fixture.id,
            created_at=now,
        )
        for phase, fire_at in fires
    ]


def is_moment_phase(phase: str) -> bool:
    return phase in _MOMENT_PHASES


def is_legacy_poll_phase(phase: str, fixture_id: str) -> bool:
    """A pending checkpoint from the pre-fixture poll-grid era: an old phase value,
    or a non-pulse doc with no fixture binding. The fire path expires these on sight
    so the cutover deploy is safe before the migration script sweeps them."""
    if phase in (f.CHECKPOINT_PHASE_LIVE, f.CHECKPOINT_PHASE_POST, f.CHECKPOINT_PHASE_MILESTONE):
        return True
    return phase != f.CHECKPOINT_PHASE_PULSE and not fixture_id


# ── Adaptive pulse (relocated from schedule_builder) ─────────────────────────
# The heartbeat starts at INITIAL, halves toward MIN each time it finds a genuinely
# new development, and grows by GROWTH toward MAX when it finds nothing — cost and
# relevance both track how fast the topic actually moves.
PULSE_INTERVAL_INITIAL_S = 6 * 3600
PULSE_INTERVAL_MIN_S = 1 * 3600
PULSE_INTERVAL_MAX_S = 24 * 3600
_PULSE_TIGHTEN_FACTOR = 0.5
_PULSE_LOOSEN_FACTOR = 1.5

# How many development keys the topic remembers for the pulse's novelty gate.
MAX_RECENT_DEVELOPMENT_KEYS = 20


def next_pulse_interval(current_seconds: int, *, found_new: bool) -> int:
    """Pure adaptive step for the pulse cadence. ``found_new`` tightens toward MIN;
    otherwise it loosens toward MAX. A zero/missing current value starts from INITIAL
    so a topic written before this field existed still gets a sane first cadence."""
    base = current_seconds if current_seconds > 0 else PULSE_INTERVAL_INITIAL_S
    if found_new:
        return max(PULSE_INTERVAL_MIN_S, int(base * _PULSE_TIGHTEN_FACTOR))
    return min(PULSE_INTERVAL_MAX_S, int(base * _PULSE_LOOSEN_FACTOR))


def pulse_checkpoint_id(topic_key: str) -> str:
    """Stable id for a topic's single recurring heartbeat checkpoint."""
    return f"{topic_key}__pulse"


def build_pulse_checkpoint(topic_key: str, *, fire_at: datetime, now: datetime | None = None) -> Checkpoint:
    """The one recurring heartbeat for a topic's developments between fixtures.
    Unlike moment checkpoints it is never terminal: the engine re-arms it (resets to
    pending with a fresh fire_at) after each fire."""
    now = now or datetime.now(UTC)
    return Checkpoint(
        id=pulse_checkpoint_id(topic_key),
        topic_key=topic_key,
        event_label="",
        phase=f.CHECKPOINT_PHASE_PULSE,
        fire_at=fire_at,
        status=f.CHECKPOINT_STATUS_PENDING,
        created_at=now,
    )


# ── Fetch query construction (relocated from schedule_builder) ───────────────
# Leading "please keep me posted on …" wrappers and trailing "… and let me know /
# until it ends" clauses that turn a subject into a verbose request sentence. Stripped
# so a topic whose provision-time research timed out (and was stored with the user's
# whole sentence as its query) still searches the SUBJECT, not the meta-request.
_REQUEST_LEAD = re.compile(
    r"^\s*(?:please\s+|hey\s+|can you\s+|could you\s+|i(?:'?d| would) like (?:you )?to\s+)*"
    r"(?:keep me (?:posted|updated|informed)|let me know|tell me|alert me|notify me|"
    r"update me|ping me|remind me|keep (?:me )?(?:an eye|track|tabs)|track|follow|"
    r"watch|monitor|stay (?:on top|updated) (?:of|on))"
    r"(?:\s+(?:me|on|about|of|with|when|if|to|for|the|any|all))*\b[\s:,\-]*",
    re.IGNORECASE,
)
_REQUEST_TAIL = re.compile(
    r"[\s,\-]+(?:and\s+)?(?:please\s+)?"
    r"(?:keep me (?:posted|updated|informed)|let me know|notify me|ping me|"
    r"so i (?:can|know|don'?t)|until (?:the\s+)?\S[\w\s]*?(?:ends?|is over|finish\w*|"
    r"conclud\w*|wrap\w* up))\b.*$",
    re.IGNORECASE,
)


def clean_topic_descriptor(text: str) -> str:
    """Reduce a raw "keep me posted on X" request to the searchable subject.

    A topic whose provision-time research timed out is stored with the user's whole
    sentence as its query/title; searching that verbatim returns a meta-jumble the
    composer can find nothing concrete in. Stripping the leading request verb and
    trailing request clause leaves the subject to drive the search. Never reduces to
    empty: if the heuristics consume everything, the original is kept."""
    original = (text or "").strip()
    if not original:
        return original
    s = _REQUEST_LEAD.sub("", original)
    s = _REQUEST_TAIL.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,-:;")
    return s or original


def build_fetch_query(*, event_label: str, research_query: str, title: str) -> str:
    """The web-search query for one moment's live fetch.

    A fixture moment searches its OWN beat (``event_label``, e.g. "Spain vs
    Belgium") anchored by a short clean topic descriptor, so a broad topic with
    several same-day fixtures fetches the firing fixture's own result. A PULSE has no
    specific beat, so it searches the topic's own clean query."""
    topic_query = clean_topic_descriptor(research_query or title)
    label = (event_label or "").strip()
    if not label:
        return topic_query
    return f"{label} {topic_query}".strip()


# ── Notify window / local quiet hours (relocated from schedule_builder) ──────
def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def within_notify_window(now: datetime, *, tz_name: str, start_hour: int, end_hour: int) -> bool:
    """True when ``now`` (UTC) falls inside the topic's LOCAL notify window. ``start==end``
    means a 24h window (always on); ``start>end`` is a window that wraps past midnight
    (e.g. 22->6). Fail-open: an unparseable timezone is treated as UTC and a bad/equal
    pair as 24h, so a config slip never SILENTLY suppresses a tracker push."""
    local_hour = now.astimezone(_zone(tz_name)).hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= local_hour < end_hour
    return local_hour >= start_hour or local_hour < end_hour


def next_window_open(now: datetime, *, tz_name: str, start_hour: int, end_hour: int) -> datetime:
    """The next UTC instant the notify window opens (``start_hour`` local). Returns ``now``
    unchanged when already inside the window."""
    if within_notify_window(now, tz_name=tz_name, start_hour=start_hour, end_hour=end_hour):
        return now
    tz = _zone(tz_name)
    local = now.astimezone(tz)
    candidate = local.replace(hour=start_hour % 24, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
