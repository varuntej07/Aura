"""Tests for the icebreaker headline interest-filter (the pure hook gate).

A bare global headline from the shared content pool is newsletter spam, not a
friend hook — these cover that only headlines mentioning one of the user's
interest subjects survive, matched as a whole-word set.
"""

from __future__ import annotations

from src.services.icebreaker.context_bundle import _headlines_matching_interests


def test_keeps_headline_mentioning_an_interest_subject():
    titles = [
        "Liverpool beat Arsenal in a late thriller",
        "New tax slabs announced in the budget",
    ]
    out = _headlines_matching_interests(titles, ["Liverpool", "Python"])
    assert out == ["Liverpool beat Arsenal in a late thriller"]


def test_no_interests_means_no_headlines():
    titles = ["Anything at all", "Another story"]
    assert _headlines_matching_interests(titles, []) == []


def test_unrelated_headlines_are_dropped():
    titles = ["Stock markets dip on inflation data"]
    assert _headlines_matching_interests(titles, ["cricket", "Taylor Swift"]) == []


def test_multiword_subject_requires_all_words_present():
    titles = ["Messi scores twice", "Lionel Messi signs new deal"]
    # The subject is two words; only the headline containing BOTH matches, so a
    # stray "Messi" in an unrelated story does not trigger a false hook.
    out = _headlines_matching_interests(titles, ["Lionel Messi"])
    assert out == ["Lionel Messi signs new deal"]


def test_match_is_case_insensitive_and_whole_word():
    titles = ["RAIN delays the test match", "Brainstorming session notes"]
    # "rain" must match as a whole word, not inside "Brainstorming".
    out = _headlines_matching_interests(titles, ["rain"])
    assert out == ["RAIN delays the test match"]
