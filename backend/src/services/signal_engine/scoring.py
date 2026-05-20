"""
Pure scoring functions. No I/O, no Firestore, no logging side effects.

Every callable here is referentially transparent: given the same inputs,
returns the same output. This keeps the scoring loop testable without
mocks and lets the recommender re-use the exact same math.

Constants tunable per docs/signal_engine.md.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .feature_store import TIME_SLOTS_PER_DAY

# Score below this means do not send. Tune via experimentation, not env.
NOTIFICATION_SCORE_THRESHOLD = 0.45

# Hard daily ceiling. Counted in feature_store state.sends_today.
DAILY_HARD_CAP = 3

# Freshness half-life. After this many hours, freshness multiplier is 0.5.
FRESHNESS_HALF_LIFE_HOURS = 24.0

# If the last send was within this many hours, fatigue jumps by RECENCY_FATIGUE_KICK.
RECENCY_FATIGUE_WINDOW_HOURS = 2.0
RECENCY_FATIGUE_KICK = 0.4

# Same-category diversity penalty. 0.6 means a same-category second send is
# multiplied by 0.6 vs a fresh category.
SAME_CATEGORY_DIVERSITY_PENALTY = 0.6

# Time-slot score clamps. Slot rate of 0 still gets a baseline 0.5 multiplier;
# a strong slot can reach 1.5x.
TIME_SLOT_SCORE_FLOOR = 0.5
TIME_SLOT_SCORE_CEILING = 1.5


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Standard cosine. Returns 0 if either vector is zero or wrong length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def time_slot_open_score(
    time_slot_open_rates: list[float],
    *,
    user_local_hour: int,
    user_local_minute: int,
) -> float:
    """Clamp slot_rate / mean_rate to [floor, ceiling].

    A user with completely flat rates gets 1.0 everywhere. A user with a
    strong morning preference gets >1 at 09:00 and <1 at midnight.
    """
    if not time_slot_open_rates or len(time_slot_open_rates) != TIME_SLOTS_PER_DAY:
        return 1.0
    slot = (user_local_hour * 2 + (1 if user_local_minute >= 30 else 0)) % TIME_SLOTS_PER_DAY
    slot_rate = time_slot_open_rates[slot]
    mean_rate = sum(time_slot_open_rates) / TIME_SLOTS_PER_DAY
    if mean_rate <= 0:
        return TIME_SLOT_SCORE_FLOOR
    ratio = slot_rate / mean_rate
    return max(TIME_SLOT_SCORE_FLOOR, min(TIME_SLOT_SCORE_CEILING, ratio))


def freshness_decay(freshness_ts: datetime, now: datetime | None = None) -> float:
    """0.5 ** (age_hours / half_life). Floored at a small positive value."""
    current = now or datetime.now(timezone.utc)
    if freshness_ts.tzinfo is None:
        freshness_ts = freshness_ts.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (current - freshness_ts).total_seconds())
    age_hours = age_seconds / 3600.0
    return max(0.01, 0.5 ** (age_hours / FRESHNESS_HALF_LIFE_HOURS))


def fatigue_penalty(
    sends_today: int,
    last_notification_at: datetime | None,
    now: datetime | None = None,
) -> float:
    """0 = no fatigue. 1 = fully fatigued. Combine into score as (1 - penalty)."""
    base = min(1.0, sends_today / float(DAILY_HARD_CAP))
    if last_notification_at is None:
        return base
    current = now or datetime.now(timezone.utc)
    if last_notification_at.tzinfo is None:
        last_notification_at = last_notification_at.replace(tzinfo=timezone.utc)
    hours_since = (current - last_notification_at).total_seconds() / 3600.0
    if hours_since < RECENCY_FATIGUE_WINDOW_HOURS:
        base += RECENCY_FATIGUE_KICK
    return min(1.0, base)


def diversity_penalty(category: str, recent_categories: list[str]) -> float:
    """Returns a multiplier in [SAME_CATEGORY_DIVERSITY_PENALTY, 1.0]."""
    if not recent_categories:
        return 1.0
    most_recent = recent_categories[0]
    if category and category == most_recent:
        return SAME_CATEGORY_DIVERSITY_PENALTY
    return 1.0


def combine_notification_score(
    *,
    cosine: float,
    time_slot: float,
    freshness: float,
    fatigue: float,
    diversity: float,
) -> float:
    """Multiplicative combine. Each component pulls the final score down.

    Capped at [0, 2] to keep the threshold meaningful when time_slot peaks at 1.5.
    """
    raw = cosine * time_slot * freshness * (1.0 - fatigue) * diversity
    return max(0.0, min(2.0, raw))


def is_sendable(
    score: float,
    sends_today: int,
    sends_today_date: str,
    user_local_date: str,
) -> tuple[bool, str | None]:
    """Final gate. Returns (allowed, reason_when_blocked)."""
    if sends_today_date == user_local_date and sends_today >= DAILY_HARD_CAP:
        return False, "daily_hard_cap"
    if score < NOTIFICATION_SCORE_THRESHOLD:
        return False, "below_threshold"
    return True, None
