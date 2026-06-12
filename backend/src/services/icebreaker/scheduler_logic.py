"""Pure scheduling logic for the Icebreaker engine — no I/O, fully unit-testable.

Two deterministic decisions live here, both seeded by the user id so they are
reproducible (re-running a tick yields the same answer, which is what makes the
whole thing safe under retries and concurrent ticks):

  1. The weekly dice roll: which ~3 days of the coming week get an icebreaker.
  2. The daily target hour: which single good local hour on a chosen day fires it,
     so the time also feels human (not always 9am) and exactly one hourly tick per
     chosen day matches.

Because both are pure functions of (user_id, date), two overlapping ticks compute
identical schedules — the store's atomic claim is what guarantees a single send;
this module guarantees they never disagree on WHEN.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

# How many days per week get an icebreaker. 3 of 7 — frequent enough to feel
# present, sparse enough to never feel like a daily nag.
ICEBREAKER_DAYS_PER_WEEK = 3

# The good-local-hour window the daily target hour is drawn from. Kept comfortably
# inside the signal engine's active hours (07:00-23:29) so an icebreaker never
# lands in quiet time even at the window edges.
TARGET_HOUR_EARLIEST = 9
TARGET_HOUR_LATEST = 20


def _stable_seed(*parts: str) -> int:
    """A process-stable integer seed from string parts. ``hash()`` is salted per
    process and therefore unusable for reproducible scheduling; sha256 is stable."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def current_week_start_date(local_now: datetime) -> str:
    """The user-local Sunday (``YYYY-MM-DD``) of the week containing ``local_now``.

    Sunday is the anchor so a fresh roll lands at the start of the week. The most
    recent Sunday on or before today: Python ``weekday()`` is Mon=0..Sun=6, so the
    days to subtract back to Sunday is ``(weekday + 1) % 7``.
    """
    today = local_now.date()
    days_since_sunday = (today.weekday() + 1) % 7
    sunday = today - timedelta(days=days_since_sunday)
    return sunday.isoformat()


def roll_week_dates(
    user_id: str,
    week_start_date: str,
    days_per_week: int = ICEBREAKER_DAYS_PER_WEEK,
) -> list[str]:
    """Deterministically pick ``days_per_week`` distinct user-local dates in the
    week beginning ``week_start_date`` (a Sunday). Same inputs always yield the
    same dates, so a re-roll on an overlapping tick is a no-op, not a conflict.

    Returns ISO date strings sorted ascending.
    """
    try:
        sunday = datetime.fromisoformat(week_start_date).date()
    except ValueError:
        # A malformed stored week start should never crash a tick; treat it as
        # "no days scheduled" so the engine simply sends nothing until the next
        # week rolls cleanly.
        return []

    count = max(0, min(days_per_week, 7))
    rng = random.Random(_stable_seed(user_id, week_start_date))
    offsets = sorted(rng.sample(range(7), count))
    return [(sunday + timedelta(days=off)).isoformat() for off in offsets]


def target_local_hour(
    user_id: str,
    local_date: str,
    earliest: int = TARGET_HOUR_EARLIEST,
    latest: int = TARGET_HOUR_LATEST,
) -> int:
    """The single good local hour an icebreaker should fire on ``local_date``.

    Deterministic per (user, date): the engine runs hourly, so exactly one hourly
    tick per chosen day matches this hour. Varies day to day so the time never
    feels robotic.
    """
    rng = random.Random(_stable_seed(user_id, local_date, "hour"))
    lo, hi = (earliest, latest) if earliest <= latest else (latest, earliest)
    return rng.randint(lo, hi)


def is_scheduled_today(local_date: str, scheduled_dates: list[str]) -> bool:
    """True if today is one of this week's chosen icebreaker days."""
    return local_date in (scheduled_dates or [])
