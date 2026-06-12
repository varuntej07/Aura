"""
Pure scoring functions. No I/O, no Firestore, no logging side effects.

Every callable here is referentially transparent: given the same inputs,
returns the same output. This keeps the scoring loop testable without
mocks and lets the recommender re-use the exact same math.

Constants tunable per docs/signal_engine.md.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from .feature_store import TIME_SLOTS_PER_DAY

# Score below this means do not send. Tune via experimentation, not env.
NOTIFICATION_SCORE_THRESHOLD = 0.45

# Hard daily ceiling for the signal engine alone (personal + breaking news),
# counted in feature_store state.sends_today. Sits at the unified proactive budget
# (4) so the engine on its own can never exceed the cross-decider ceiling.
DAILY_NOTIFICATION_HARD_CAP = 4

# Global-salience constants for the breaking lane (see scoring_loop Lane B and
# services/signal_engine/salience.py).
#   BREAKING_SALIENCE_BAR — a candidate at/above this can bypass the personal
#     interest gate and reach EVERY user. 0.85 is only reachable by a story carried
#     across all locale editions (salience.compute_salience), i.e. genuinely
#     worldwide news — so single/two-edition items can never fire an everyone push.
#   MAX_BREAKING_SENDS_PER_DAY — hard cap so a busy news day can't spam breaking.
#   SALIENCE_NUDGE_WEIGHT — on the PERSONAL lane, a mild multiplier so a more
#     globally-important story ranks slightly higher among already-relevant picks.
BREAKING_SALIENCE_BAR = 0.85
MAX_BREAKING_SENDS_PER_DAY = 1
SALIENCE_NUDGE_WEIGHT = 0.1


def apply_salience_nudge(base_score: float, salience: float) -> float:
    """Personal-lane nudge: base * (1 + WEIGHT * salience), clamped to [0, 2].

    Never lowers a score (salience >= 0) and never gates — it only lets a more
    globally-important story edge ahead among candidates that already cleared the
    threshold on personal relevance."""
    nudged = base_score * (1.0 + SALIENCE_NUDGE_WEIGHT * max(0.0, min(1.0, salience)))
    return max(0.0, min(2.0, nudged))

# Freshness half-life. After this many hours, freshness multiplier is 0.5.
FRESHNESS_HALF_LIFE_HOURS = 24.0

# If the last send was within this many hours, fatigue jumps by RECENCY_FATIGUE_KICK.
RECENCY_FATIGUE_WINDOW_HOURS = 2.0
RECENCY_FATIGUE_KICK = 0.4

# Same-category diversity penalty. 0.6 means a same-category second send is
# multiplied by 0.6 vs a fresh category.
SAME_NOTIFICATION_CATEGORY_DIVERSITY_PENALTY = 0.6

# Time-slot score clamps. Slot rate of 0 still gets a baseline 0.5 multiplier;
# a strong slot can reach 1.5x.
TIME_SLOT_SCORE_FLOOR = 0.5
TIME_SLOT_SCORE_CEILING = 1.5

# Local quiet hours. Notifications are never delivered outside the active window,
# regardless of score. Expressed with minute precision so a half-hour edge like
# 23:30 is exact. The active window is [end, start) in local time.
QUIET_HOURS_START_HOUR = 23
QUIET_HOURS_START_MINUTE = 30  # 11:30pm — stop sending
QUIET_HOURS_END_HOUR = 7
QUIET_HOURS_END_MINUTE = 0     # 7:00am — resume sending


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


def is_within_active_hours(user_local_hour: int, user_local_minute: int = 0) -> bool:
    """False during local quiet hours — a hard gate, independent of score.

    The active window is [end, start) in local time with minute precision, so a
    half-hour edge (23:30) is honored exactly. With END=07:00 and START=23:30 the
    window is active 07:00-23:29 and quiet 23:30-06:59.
    """
    now_minutes = user_local_hour * 60 + user_local_minute
    start_minutes = QUIET_HOURS_START_HOUR * 60 + QUIET_HOURS_START_MINUTE
    end_minutes = QUIET_HOURS_END_HOUR * 60 + QUIET_HOURS_END_MINUTE
    if end_minutes <= start_minutes:
        return end_minutes <= now_minutes < start_minutes
    # Window wraps midnight: active outside [start, end).
    return now_minutes >= end_minutes or now_minutes < start_minutes


def cold_start_daypart_prior(user_local_hour: int) -> float:
    """Time-slot multiplier for a user with no learned open history yet.

    Until real per-slot rates exist, prefer well-known engagement windows so the
    first notifications land at sane times instead of a flat 0.5 that makes the
    send threshold effectively unreachable. Night edges keep the floor; the quiet
    hours gate hard-blocks them anyway.
    """
    if user_local_hour in (8, 9) or user_local_hour in (12, 13) or 18 <= user_local_hour <= 21:
        return 1.2  # morning, lunch, and evening peaks
    if 10 <= user_local_hour <= 17:
        return 1.0  # daytime plateau
    return TIME_SLOT_SCORE_FLOOR  # early morning / late night edges


def time_slot_open_score(
    time_slot_open_rates: list[float],
    *,
    user_local_hour: int,
    user_local_minute: int,
) -> float:
    """Clamp slot_rate / mean_rate to [floor, ceiling].

    A user with a strong morning preference gets >1 at 09:00 and <1 at midnight.
    A user with no learned history yet falls back to a time-of-day prior
    (see cold_start_daypart_prior) instead of a flat floor.
    """
    if not time_slot_open_rates or len(time_slot_open_rates) != TIME_SLOTS_PER_DAY:
        return cold_start_daypart_prior(user_local_hour)
    slot = (user_local_hour * 2 + (1 if user_local_minute >= 30 else 0)) % TIME_SLOTS_PER_DAY
    slot_rate = time_slot_open_rates[slot]
    mean_rate = sum(time_slot_open_rates) / TIME_SLOTS_PER_DAY
    if mean_rate <= 0:
        return cold_start_daypart_prior(user_local_hour)
    ratio = slot_rate / mean_rate
    return max(TIME_SLOT_SCORE_FLOOR, min(TIME_SLOT_SCORE_CEILING, ratio))


def freshness_decay(freshness_ts: datetime, now: datetime | None = None) -> float:
    """0.5 ** (age_hours / half_life). Floored at a small positive value."""
    current = now or datetime.now(UTC)
    if freshness_ts.tzinfo is None:
        freshness_ts = freshness_ts.replace(tzinfo=UTC)
    age_seconds = max(0.0, (current - freshness_ts).total_seconds())
    age_hours = age_seconds / 3600.0
    return max(0.01, 0.5 ** (age_hours / FRESHNESS_HALF_LIFE_HOURS))


def fatigue_penalty(
    sends_today: int,
    last_notification_at: datetime | None,
    now: datetime | None = None,
) -> float:
    """0 = no fatigue. 1 = fully fatigued. Combine into score as (1 - penalty)."""
    base = min(1.0, sends_today / float(DAILY_NOTIFICATION_HARD_CAP))
    if last_notification_at is None:
        return base
    current = now or datetime.now(UTC)
    if last_notification_at.tzinfo is None:
        last_notification_at = last_notification_at.replace(tzinfo=UTC)
    hours_since = (current - last_notification_at).total_seconds() / 3600.0
    if hours_since < RECENCY_FATIGUE_WINDOW_HOURS:
        base += RECENCY_FATIGUE_KICK
    return min(1.0, base)


def diversity_penalty(category: str, recent_categories: list[str]) -> float:
    """Returns a multiplier in [SAME_NOTIFICATION_CATEGORY_DIVERSITY_PENALTY, 1.0]."""
    if not recent_categories:
        return 1.0
    most_recent = recent_categories[0]
    if category and category == most_recent:
        return SAME_NOTIFICATION_CATEGORY_DIVERSITY_PENALTY
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
    if sends_today_date == user_local_date and sends_today >= DAILY_NOTIFICATION_HARD_CAP:
        return False, "daily_notification_hard_cap"
    if score < NOTIFICATION_SCORE_THRESHOLD:
        return False, "below_threshold"
    return True, None
