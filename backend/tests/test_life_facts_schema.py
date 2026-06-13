"""Round-trip + safety tests for the life_facts schema (the writer/reader contract).

Per CLAUDE.md data-layer discipline: a write through `apply_life_fact` must read
back through `read_life_facts_for_arming`, and the closed-key + min-dwell rules
must hold so a rename or a too-fresh fact can never leak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services.life_facts_schema import (
    LIFE_FACT_HAS_PET,
    LIFE_FACT_HOME_CITY,
    LIFE_FACTS_FIELD,
    MAX_LIFE_FACT_VALUE_LENGTH,
    MIN_FACT_DWELL,
    apply_life_fact,
    has_life_facts,
    read_life_facts_for_arming,
    remove_life_fact,
)


def test_apply_then_read_round_trip():
    facts: dict = {}
    # Learned two days ago so it is past the min-dwell window.
    learned_at = datetime.now(UTC) - timedelta(days=2)
    apply_life_fact(facts, LIFE_FACT_HAS_PET, "dog named Bruno", learned_at)

    profile = {LIFE_FACTS_FIELD: facts}
    assert has_life_facts(profile)
    armed = read_life_facts_for_arming(profile)
    assert armed[LIFE_FACT_HAS_PET] == "dog named Bruno"


def test_off_taxonomy_key_is_dropped_not_coerced():
    facts: dict = {}
    apply_life_fact(facts, "favourite_colour", "teal", datetime.now(UTC))
    assert facts == {}  # unknown key never stored


def test_oversized_value_is_dropped():
    facts: dict = {}
    apply_life_fact(facts, LIFE_FACT_HOME_CITY, "x" * (MAX_LIFE_FACT_VALUE_LENGTH + 1), datetime.now(UTC))
    assert facts == {}


def test_fresh_fact_is_withheld_until_min_dwell():
    facts: dict = {}
    just_now = datetime.now(UTC)
    apply_life_fact(facts, LIFE_FACT_HAS_PET, "cat named Mochi", just_now)

    profile = {LIFE_FACTS_FIELD: facts}
    # Too fresh — never surface a fact moments after learning it (anti-creepiness).
    assert read_life_facts_for_arming(profile, now=just_now) == {}
    # Past the dwell window it becomes available.
    later = just_now + MIN_FACT_DWELL + timedelta(minutes=1)
    assert read_life_facts_for_arming(profile, now=later)[LIFE_FACT_HAS_PET] == "cat named Mochi"


def test_remove_clears_a_denied_fact():
    facts: dict = {}
    apply_life_fact(facts, LIFE_FACT_HAS_PET, "dog named Bruno", datetime.now(UTC))
    # User later denies it ("I don't have a dog") -> the fact is cleared so no
    # opener ever asks about a pet they never had.
    remove_life_fact(facts, LIFE_FACT_HAS_PET)
    assert LIFE_FACT_HAS_PET not in facts


def test_remove_is_a_noop_for_unknown_or_unstored_key():
    facts: dict = {}
    remove_life_fact(facts, LIFE_FACT_HAS_PET)          # never stored
    remove_life_fact(facts, "favourite_colour")         # off-taxonomy
    assert facts == {}


def test_newest_value_wins_but_first_seen_preserved():
    facts: dict = {}
    first = datetime.now(UTC) - timedelta(days=5)
    apply_life_fact(facts, LIFE_FACT_HOME_CITY, "Hyderabad", first)
    second = first + timedelta(days=1)
    apply_life_fact(facts, LIFE_FACT_HOME_CITY, "Bengaluru", second)

    node = facts[LIFE_FACT_HOME_CITY]
    assert node["value"] == "Bengaluru"
    assert node["first_seen"] == first.isoformat()
