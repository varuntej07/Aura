"""Schedule builder — turns a TopicResearch into the checkpoint due-queue, and
diffs a fresh research pass against the stored checkpoints so the schedule
self-heals (new rounds appear, shifted times update, dropped events expire).

Pure functions (no I/O): the engine reads/writes Firestore around these. A
checkpoint id is DETERMINISTIC (topic_key + event + date + phase) so re-researching
the same event upserts the same row — a kickoff time that moved updates in place
instead of creating a duplicate, and an event that fell off the schedule is left out
of the new set so the engine can expire it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import Checkpoint, ScheduledEvent, TopicResearch
from . import fields as f

# How far before an event a "pre" nudge fires, and how long after its start a
# "post" result check fires when the event carries no explicit end time.
PRE_LEAD = timedelta(hours=2)
POST_LAG = timedelta(hours=2, minutes=30)

# Adaptive pulse cadence (seconds). The heartbeat starts at INITIAL, halves toward
# MIN each time it finds genuinely-new state (a hot topic), and grows by GROWTH
# toward MAX when it finds nothing (a quiet topic) — so cost and relevance both track
# how fast the topic is actually moving, never a fixed poll.
PULSE_INTERVAL_INITIAL_S = 6 * 3600
PULSE_INTERVAL_MIN_S = 1 * 3600
PULSE_INTERVAL_MAX_S = 24 * 3600
_PULSE_TIGHTEN_FACTOR = 0.5
_PULSE_LOOSEN_FACTOR = 1.5


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
    """The one recurring heartbeat for an ongoing topic. Unlike event checkpoints it is
    never terminal: the engine re-arms it (resets to pending with a fresh fire_at) after
    each fire, so a topic with no dated events still gets adaptive updates."""
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


def _event_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower())
    return re.sub(r"-{2,}", "-", slug).strip("-")[:48].strip("-") or "event"


def _checkpoint_id(topic_key: str, event: ScheduledEvent, phase: str) -> str:
    """Stable id so an upsert on re-research updates the SAME row. The event date
    disambiguates two same-label events (e.g. a home + away fixture)."""
    day = event.start_at.date().isoformat()
    return f"{topic_key}__{_event_slug(event.label)}__{day}__{phase}"


def _phase_fire_at(event: ScheduledEvent, phase: str) -> datetime | None:
    if phase == f.CHECKPOINT_PHASE_PRE:
        return event.start_at - PRE_LEAD
    if phase in (f.CHECKPOINT_PHASE_LIVE, f.CHECKPOINT_PHASE_MILESTONE):
        return event.start_at
    if phase == f.CHECKPOINT_PHASE_POST:
        return event.end_at or (event.start_at + POST_LAG)
    return None


def build_checkpoints(
    research: TopicResearch, *, topic_key: str, now: datetime | None = None,
) -> list[Checkpoint]:
    """Materialize pre/live/post checkpoints for each upcoming event. A phase whose
    fire_at is already in the past is skipped (we never enqueue an instantly-due
    checkpoint for a moment that already happened).

    ``topic_key`` is the key of the TOPIC DOCUMENT these checkpoints belong to — used for
    both the checkpoint id and ``Checkpoint.topic_key``, NEVER ``research.topic_key``. The
    two can differ: a topic whose provision-time research timed out is created under a
    request-derived slug, while the later reconcile's research returns its own clean slug.
    Keying checkpoints to the research slug instead of the doc orphans them — they fire,
    ``get_tracked_topic`` returns None, and they silently expire (the 2026-06-16 outage
    where a World Cup tracker's 17 event checkpoints all self-expired). The doc key is
    authoritative once the doc exists."""
    now = now or datetime.now(UTC)
    out: list[Checkpoint] = []
    for event in research.events:
        for phase in (event.phases or []):
            fire_at = _phase_fire_at(event, phase)
            if fire_at is None or fire_at <= now:
                continue
            out.append(Checkpoint(
                id=_checkpoint_id(topic_key, event, phase),
                topic_key=topic_key,
                event_label=event.label,
                phase=phase,
                fire_at=fire_at,
                status=f.CHECKPOINT_STATUS_PENDING,
                created_at=now,
            ))
    return out


def plan_reconcile(
    research: TopicResearch,
    existing: list[Checkpoint],
    *,
    topic_key: str,
    now: datetime | None = None,
) -> tuple[list[Checkpoint], list[str]]:
    """Diff a fresh research pass against stored checkpoints.

    ``topic_key`` is the key of the topic DOCUMENT being reconciled; the fresh checkpoints
    are built under it (not ``research.topic_key``) so they always share a key with the
    doc and the existing checkpoints, and the diff below is meaningful. See
    ``build_checkpoints`` for why the research slug must not be used.

    Returns ``(to_upsert, expire_ids)``:
      to_upsert  — the freshly built checkpoints (new + time-shifted; upsert is
                   idempotent by id, so an unchanged one is a harmless no-op write).
      expire_ids — ids of STILL-PENDING stored checkpoints that are no longer in the
                   schedule (event cancelled/dropped); the engine marks them expired.
                   Already-fired/claimed checkpoints are left alone.
    """
    now = now or datetime.now(UTC)
    fresh = build_checkpoints(research, topic_key=topic_key, now=now)
    fresh_ids = {cp.id for cp in fresh}
    expire_ids = [
        cp.id for cp in existing
        if cp.status == f.CHECKPOINT_STATUS_PENDING
        and cp.id not in fresh_ids
        # The recurring pulse is never event-derived, so it is never in `fresh`; it
        # must NOT be expired here or the heartbeat for an ongoing topic would die on
        # the first reconcile. The engine owns the pulse lifecycle (re-arm / complete).
        and cp.phase != f.CHECKPOINT_PHASE_PULSE
    ]
    return fresh, expire_ids
