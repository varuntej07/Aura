"""
Coverage for the global-salience score that powers the breaking lane.

The contract that matters: ONLY a story carried across all locale editions can
clear the breaking bar, so a single- or two-edition item can never fire an
everyone-gets-it push (high precision, low recall — see salience.py).
"""

from __future__ import annotations

from src.services.signal_engine.salience import compute_salience
from src.services.signal_engine.scoring import BREAKING_SALIENCE_BAR


def test_all_editions_clears_breaking_bar():
    # 3 editions alone (0.92) already clears the bar, with or without a lead bonus.
    assert compute_salience(edition_count=3, feed_rank=5) >= BREAKING_SALIENCE_BAR
    top = compute_salience(edition_count=3, feed_rank=0, is_world_section=True)
    assert BREAKING_SALIENCE_BAR <= top <= 1.0


def test_two_editions_never_breaking_even_at_best():
    # Best a two-edition story can reach: 0.60 + lead 0.08 + world 0.04 = 0.72.
    best_two = compute_salience(edition_count=2, feed_rank=0, is_world_section=True)
    assert best_two < BREAKING_SALIENCE_BAR


def test_single_edition_is_a_low_personal_nudge_only():
    s = compute_salience(edition_count=1, feed_rank=10)
    assert 0.0 < s < 0.5
    assert s < BREAKING_SALIENCE_BAR


def test_edition_count_floored_and_result_clamped():
    # edition_count below 1 is floored to 1; above 3 is capped; result stays [0, 1].
    low = compute_salience(edition_count=0, feed_rank=0, is_world_section=True)
    high = compute_salience(edition_count=99, feed_rank=0, is_world_section=True)
    assert 0.0 <= low <= 1.0
    assert high <= 1.0
