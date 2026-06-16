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


def build_checkpoints(research: TopicResearch, *, now: datetime | None = None) -> list[Checkpoint]:
    """Materialize pre/live/post checkpoints for each upcoming event. A phase whose
    fire_at is already in the past is skipped (we never enqueue an instantly-due
    checkpoint for a moment that already happened)."""
    now = now or datetime.now(UTC)
    out: list[Checkpoint] = []
    for event in research.events:
        for phase in event.phases:
            fire_at = _phase_fire_at(event, phase)
            if fire_at is None or fire_at <= now:
                continue
            out.append(Checkpoint(
                id=_checkpoint_id(research.topic_key, event, phase),
                topic_key=research.topic_key,
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
    now: datetime | None = None,
) -> tuple[list[Checkpoint], list[str]]:
    """Diff a fresh research pass against stored checkpoints.

    Returns ``(to_upsert, expire_ids)``:
      to_upsert  — the freshly built checkpoints (new + time-shifted; upsert is
                   idempotent by id, so an unchanged one is a harmless no-op write).
      expire_ids — ids of STILL-PENDING stored checkpoints that are no longer in the
                   schedule (event cancelled/dropped); the engine marks them expired.
                   Already-fired/claimed checkpoints are left alone.
    """
    now = now or datetime.now(UTC)
    fresh = build_checkpoints(research, now=now)
    fresh_ids = {cp.id for cp in fresh}
    expire_ids = [
        cp.id for cp in existing
        if cp.status == f.CHECKPOINT_STATUS_PENDING and cp.id not in fresh_ids
    ]
    return fresh, expire_ids
