"""Regression tests for the notification timing/orchestration layer:
quiet hours and the cold-start daypart prior. Pure functions, no I/O.
"""

from __future__ import annotations

from src.services.signal_engine.feature_store import TIME_SLOTS_PER_DAY
from src.services.signal_engine.scoring import (
    QUIET_HOURS_END_HOUR,
    QUIET_HOURS_END_MINUTE,
    QUIET_HOURS_START_HOUR,
    QUIET_HOURS_START_MINUTE,
    TIME_SLOT_SCORE_FLOOR,
    cold_start_daypart_prior,
    is_within_active_hours,
    time_slot_open_score,
)


class TestQuietHours:
    def test_night_hours_are_blocked(self):
        # Quiet window is [23:30, 07:00). Whole night hours must be quiet.
        for hour in [0, 1, 2, 3, 4, 5, 6]:
            assert is_within_active_hours(hour, 0) is False, f"hour {hour} should be quiet"
        # 23:30 onward is quiet; 23:00-23:29 is still active.
        assert is_within_active_hours(23, 30) is False
        assert is_within_active_hours(23, 45) is False
        assert is_within_active_hours(23, 0) is True
        assert is_within_active_hours(23, 29) is True

    def test_day_hours_are_active(self):
        for hour in [7, 8, 9, 12, 15, 18, 21, 23]:
            assert is_within_active_hours(hour, 0) is True, f"hour {hour} should be active"

    def test_boundaries(self):
        # Active window is [end, start): end inclusive, start exclusive.
        assert is_within_active_hours(QUIET_HOURS_END_HOUR, QUIET_HOURS_END_MINUTE) is True
        assert is_within_active_hours(QUIET_HOURS_START_HOUR, QUIET_HOURS_START_MINUTE) is False
        # 06:59 is still quiet; 07:00 resumes.
        assert is_within_active_hours(6, 59) is False


class TestColdStartDaypartPrior:
    def test_peaks_are_boosted_above_one(self):
        for hour in [8, 9, 12, 13, 18, 19, 20, 21]:
            assert cold_start_daypart_prior(hour) > 1.0, f"hour {hour} should peak"

    def test_daytime_plateau_is_neutral(self):
        for hour in [10, 11, 14, 15, 16, 17]:
            assert cold_start_daypart_prior(hour) == 1.0

    def test_night_edges_use_floor(self):
        for hour in [0, 3, 6, 7, 22, 23]:
            assert cold_start_daypart_prior(hour) == TIME_SLOT_SCORE_FLOOR

    def test_peak_prior_lets_a_realistic_match_clear_threshold(self):
        # A cold user with a decent cosine should be able to clear 0.45 at a peak
        # hour. Before the prior, the flat 0.5 made this effectively impossible.
        from src.services.signal_engine.scoring import (
            NOTIFICATION_SCORE_THRESHOLD,
            combine_notification_score,
        )

        score = combine_notification_score(
            cosine=0.5,
            time_slot=cold_start_daypart_prior(19),  # evening peak
            freshness=0.95,
            fatigue=0.0,
            diversity=1.0,
        )
        assert score >= NOTIFICATION_SCORE_THRESHOLD


class TestTimeSlotColdStartFallback:
    def test_empty_rates_fall_back_to_daypart_prior(self):
        # No learned history at all -> daypart prior, not a flat floor.
        assert time_slot_open_score([], user_local_hour=19, user_local_minute=0) == (
            cold_start_daypart_prior(19)
        )

    def test_all_zero_rates_fall_back_to_daypart_prior(self):
        zeros = [0.0] * TIME_SLOTS_PER_DAY
        assert time_slot_open_score(zeros, user_local_hour=12, user_local_minute=0) == (
            cold_start_daypart_prior(12)
        )

    def test_learned_rates_still_use_ratio(self):
        # One hot slot among zeros -> that slot scores above the mean (ratio path),
        # proving the cold-start fallback only applies when there is no signal.
        rates = [0.0] * TIME_SLOTS_PER_DAY
        rates[18] = 1.0  # 09:00 slot (hour*2)
        score = time_slot_open_score(rates, user_local_hour=9, user_local_minute=0)
        assert score > 1.0
