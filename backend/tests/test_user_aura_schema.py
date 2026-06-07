"""
UserAura interest schema — writer->reader round-trip, decay, coercion, caps,
and legacy fallback.

These guard the field-name contract (CLAUDE.md data-layer discipline): the
writer (`apply_interest_signal` / `_merge_profile`) and every reader accessor
exercise the SAME nested shape here, so a rename or shape change on either side
breaks CI instead of silently flattening the interest signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services.user_aura_extractor import InterestSignal, MessageInsight, _merge_profile
from src.services.user_aura_schema import (
    INTEREST_CATEGORIES,
    MAX_SUBJECTS_PER_CATEGORY,
    OTHER_CATEGORY,
    apply_interest_signal,
    category_count,
    interest_embedding_texts,
    interest_prompt_lines,
    top_interest_subjects,
)

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Writer primitive + round-trip
# --------------------------------------------------------------------------

def test_apply_signal_builds_nested_category_and_subject():
    interests: dict = {}
    apply_interest_signal(interests, "politics_governance", "KCR", NOW)

    node = interests["politics_governance"]
    assert node["weight"] == 1.0
    assert node["last_seen"] == NOW.isoformat()
    subject = node["subjects"]["KCR"]
    assert subject["display"] == "KCR"
    assert subject["weight"] == 1.0


def test_round_trip_prompt_line_shows_category_label_and_subject():
    interests: dict = {}
    apply_interest_signal(interests, "politics_governance", "KCR", NOW)
    apply_interest_signal(interests, "politics_governance", "Telangana DGP", NOW)
    profile = {"interests": interests}

    lines = interest_prompt_lines(profile, now=NOW)
    assert lines == ["politics & governance: KCR, Telangana DGP"]


def test_subject_is_optional_category_still_counts():
    interests: dict = {}
    apply_interest_signal(interests, "personal_finance", None, NOW)
    node = interests["personal_finance"]
    assert node["weight"] == 1.0
    assert node["subjects"] == {}


def test_repeated_signal_accumulates_weight():
    interests: dict = {}
    apply_interest_signal(interests, "automotive", "XUV 3XO", NOW)
    apply_interest_signal(interests, "automotive", "XUV 3XO", NOW)
    assert interests["automotive"]["subjects"]["XUV 3XO"]["weight"] == 2.0


def test_unknown_category_coerced_to_other():
    interests: dict = {}
    apply_interest_signal(interests, "made_up_category", "thing", NOW)
    assert OTHER_CATEGORY in interests
    assert "made_up_category" not in interests


# --------------------------------------------------------------------------
# Recency decay
# --------------------------------------------------------------------------

def test_decay_ranks_recent_category_above_stale_one():
    long_ago = NOW - timedelta(days=120)  # 4 half-lives -> ~1/16 weight
    interests: dict = {}
    # Stale category hit 5 times long ago.
    for _ in range(5):
        apply_interest_signal(interests, "sports", "cricket", long_ago)
    # Fresh category hit twice just now.
    for _ in range(2):
        apply_interest_signal(interests, "automotive", "XUV 3XO", NOW)

    lines = interest_prompt_lines({"interests": interests}, now=NOW, k_categories=2)
    # Fresh automotive should outrank decayed sports despite fewer raw hits.
    assert lines[0].startswith("automotive")


def test_decay_then_increment_keeps_weight_recency_aware():
    interests: dict = {}
    old = NOW - timedelta(days=30)  # one half-life
    apply_interest_signal(interests, "health_medical", "vaccine", old)
    apply_interest_signal(interests, "health_medical", "vaccine", NOW)
    # First hit decays to ~0.5 over one half-life, then +1 -> ~1.5.
    weight = interests["health_medical"]["subjects"]["vaccine"]["weight"]
    assert 1.4 < weight < 1.6


# --------------------------------------------------------------------------
# Subject cap
# --------------------------------------------------------------------------

def test_subjects_capped_per_category_evicting_lowest_weight():
    interests: dict = {}
    # Add one heavily-weighted subject, then flood past the cap with singletons.
    for _ in range(10):
        apply_interest_signal(interests, "general_knowledge", "anchor", NOW)
    for i in range(MAX_SUBJECTS_PER_CATEGORY + 5):
        apply_interest_signal(interests, "general_knowledge", f"filler_{i}", NOW)

    subjects = interests["general_knowledge"]["subjects"]
    assert len(subjects) == MAX_SUBJECTS_PER_CATEGORY
    # The high-weight anchor must survive eviction.
    assert "anchor" in subjects


# --------------------------------------------------------------------------
# Legacy fallback (transition safety)
# --------------------------------------------------------------------------

def test_prompt_lines_fall_back_to_legacy_map():
    profile = {"deep_interest_frequencies": {"Indian politics": 9, "cricket": 3}}
    lines = interest_prompt_lines(profile, now=NOW)
    assert "Indian politics" in lines


def test_embedding_texts_prefer_subjects_then_fall_back_to_legacy():
    # New structure present -> uses subjects, never raw slugs.
    interests: dict = {}
    apply_interest_signal(interests, "politics_governance", "KCR", NOW)
    texts = interest_embedding_texts({"interests": interests}, now=NOW)
    assert "KCR" in texts
    assert "politics_governance" not in texts  # never emit raw slugs

    # No new structure -> legacy keys.
    legacy_texts = interest_embedding_texts(
        {"deep_interest_frequencies": {"GPU programming": 4}}, now=NOW
    )
    assert legacy_texts == ["GPU programming"]


def test_top_subjects_supplemented_by_legacy_when_sparse():
    interests: dict = {}
    apply_interest_signal(interests, "automotive", "XUV 3XO", NOW)
    profile = {
        "interests": interests,
        "deep_interest_frequencies": {"old interest": 5},
    }
    subjects = top_interest_subjects(profile, now=NOW, k=3)
    assert "XUV 3XO" in subjects
    assert "old interest" in subjects  # legacy fills the remaining slots


# --------------------------------------------------------------------------
# Full merge path + legacy sunset
# --------------------------------------------------------------------------

def _insight(interests: list[tuple[str, str | None]]) -> MessageInsight:
    return MessageInsight(
        primary_intent="information_lookup",
        secondary_intent=None,
        interests=[InterestSignal(category=c, subject=s) for c, s in interests],
        domain="unclear",
        tone="terse",
        emotional_state=None,
        urgency="none",
        response_depth_preference=None,
        question_type="what_is",
        explicit_facts=[],
        inferred_goal_hints=[],
        used_prev_query_context=False,
        extraction_skipped=False,
        turn_score=0,
        signal_type="none",
        directive_hint=None,
    )


def test_merge_drops_dead_maps_but_keeps_deep_interest_for_old_clients():
    # Start with the old dead maps + the legacy field the shipped app still reads.
    existing = {
        "surface_topic_frequencies": {"foo": 1},
        "named_entities_seen": {"bar": 1},
        "deep_interest_frequencies": {"baz": 1},
    }
    # Max 3 interests per message, so 5 categories accrue across two messages.
    after_first = _merge_profile(existing, _insight([
        ("politics_governance", "KCR"),
        ("regional_local_affairs", "Telangana"),
        ("health_medical", "vaccine"),
    ]), "who is KCR")
    updated = _merge_profile(after_first, _insight([
        ("automotive", "XUV 3XO"),
        ("sports", "cricket"),
    ]), "xuv price")

    assert category_count(updated) >= 5
    # The maps nothing reads are reclaimed once mature...
    assert "surface_topic_frequencies" not in updated
    assert "named_entities_seen" not in updated
    # ...but deep_interest_frequencies stays so the old app's profile screen and
    # the reader fallback keep working until the client update rolls out.
    assert "deep_interest_frequencies" in updated


def test_merge_keeps_legacy_fallback_while_structure_sparse():
    existing = {"deep_interest_frequencies": {"Indian politics": 9}}
    insight = _insight([("politics_governance", "KCR")])
    updated = _merge_profile(existing, insight, "who is KCR")
    # Only one category so far -> legacy map retained for reader fallback.
    assert "deep_interest_frequencies" in updated
    assert "KCR" in updated["interests"]["politics_governance"]["subjects"]


def test_insight_signal_subject_defaults_when_omitted():
    # A model response that omits the subject key must not fail validation.
    sig = InterestSignal(category="science_nature")
    assert sig.subject is None


def test_insight_validator_coerces_unknown_category():
    sig = InterestSignal(category="Politics & Governance", subject="KCR")
    # Normalised form maps onto the real slug.
    assert sig.category == "politics_governance"
    bad = InterestSignal(category="totally_made_up", subject="x")
    assert bad.category == OTHER_CATEGORY
    assert bad.category in INTEREST_CATEGORIES


def test_insight_accepts_null_classification_fields():
    # Zero-signal/ack messages make Gemini return null for the required-enum
    # classification fields (primary_intent, domain, tone, urgency). The model must
    # accept them instead of rejecting the whole extraction (an ERROR on every ack),
    # and _merge_profile must not record a null into any frequency map.
    insight = MessageInsight(
        primary_intent=None,
        secondary_intent=None,
        interests=[],
        domain=None,
        tone=None,
        emotional_state=None,
        urgency=None,
        response_depth_preference=None,
        question_type=None,
        explicit_facts=[],
        inferred_goal_hints=[],
        used_prev_query_context=False,
        extraction_skipped=False,
        turn_score=0,
        signal_type="none",
        directive_hint=None,
    )
    assert insight.primary_intent is None
    assert insight.domain is None

    # Merge must not crash, and no map may contain a null/None key.
    updated = _merge_profile({}, insight, "ok")
    for map_key in (
        "intent_distribution",
        "domain_frequencies",
        "tone_signals",
        "urgency_distribution",
    ):
        keys = updated.get(map_key, {})
        assert "null" not in keys and "None" not in keys, map_key
