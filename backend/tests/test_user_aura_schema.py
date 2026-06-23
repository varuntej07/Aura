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
    INTEREST_KIND_DURABLE,
    INTEREST_KIND_EVENT_DRIVEN,
    MAX_SUBJECTS_PER_CATEGORY,
    OTHER_CATEGORY,
    apply_interest_signal,
    apply_storyline,
    apply_trait,
    category_count,
    decayed_weight_by_kind,
    interest_embedding_texts,
    interest_prompt_lines,
    prune_interest,
    ranked_storylines,
    reclassify_interest_kind,
    shown_traits,
    storyline_prompt_lines,
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
    # A capture-written subject carries no kind, so it decays as durable (90d half-life).
    old = NOW - timedelta(days=90)  # one durable half-life
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


# --------------------------------------------------------------------------
# Kind-aware decay (FIFA: event_driven fades fast; legacy/no-kind = durable)
# --------------------------------------------------------------------------

def test_event_driven_decays_faster_than_durable():
    forty_days = (NOW - timedelta(days=40)).isoformat()
    durable = decayed_weight_by_kind(1.0, forty_days, INTEREST_KIND_DURABLE, NOW)
    event = decayed_weight_by_kind(1.0, forty_days, INTEREST_KIND_EVENT_DRIVEN, NOW)
    # After 40 days a durable interest is still substantial; an event-driven one is gone.
    assert durable > 0.5
    assert event < 0.05
    assert durable > event


def test_missing_kind_defaults_to_durable():
    # CRITICAL regression: legacy nodes and every capture-written node carry NO kind.
    # They must decay as durable, NOT event_driven, or existing profiles would suddenly
    # evaporate when this change ships.
    ninety_days = (NOW - timedelta(days=90)).isoformat()
    none_kind = decayed_weight_by_kind(1.0, ninety_days, None, NOW)
    durable = decayed_weight_by_kind(1.0, ninety_days, INTEREST_KIND_DURABLE, NOW)
    unknown = decayed_weight_by_kind(1.0, ninety_days, "bogus_kind", NOW)
    assert none_kind == durable == unknown
    assert 0.45 < none_kind < 0.55  # exactly one durable half-life


# --------------------------------------------------------------------------
# Storylines (the narrative layer)
# --------------------------------------------------------------------------

def test_storyline_insert_and_prompt_line():
    storylines: dict = {}
    apply_storyline(
        storylines, "annapurna_sde",
        summary="writing a tensor-parallelism blog to land an SDE role at Annapurna Labs (AWS)",
        entities=["tensor parallelism", "Annapurna Labs", "AWS"],
        categories=["technology_computing", "career_jobs"],
        intent="career_goal", kind="goal_instrumental", confidence=0.82, now=NOW,
    )
    node = storylines["annapurna_sde"]
    assert node["kind"] == "goal_instrumental"
    assert node["weight"] == 1.0
    assert "Annapurna Labs" in node["entities"]
    lines = storyline_prompt_lines({"storylines": storylines}, now=NOW)
    assert lines and "Annapurna Labs" in lines[0]


def test_storyline_merges_across_sessions_by_id():
    storylines: dict = {}
    apply_storyline(storylines, "annapurna_sde", "blog draft started",
                    [], ["career_jobs"], "career_goal", "goal_instrumental", 0.6, NOW)
    apply_storyline(storylines, "annapurna_sde", "blog published, applying now",
                    [], ["career_jobs"], "career_goal", "goal_instrumental", 0.9, NOW)
    # Same id -> one node, weight accumulates, summary updates to the latest.
    assert len(storylines) == 1
    assert storylines["annapurna_sde"]["weight"] == 2.0
    assert "applying" in storylines["annapurna_sde"]["summary"]


def test_ranked_storylines_orders_fresh_durable_above_stale_event():
    storylines: dict = {}
    three_weeks = NOW - timedelta(days=21)
    apply_storyline(storylines, "wc", "wants World Cup score updates",
                    ["FIFA World Cup"], ["sports"], "event_follow", "event_driven", 0.8, three_weeks)
    apply_storyline(storylines, "ml", "studies ML systems",
                    [], ["technology_computing"], None, "durable", 0.8, NOW)
    ranked = ranked_storylines({"storylines": storylines}, now=NOW)
    # 21 days at a 7-day half-life crushes the event storyline; fresh durable wins.
    assert ranked[0]["summary"] == "studies ML systems"


# --------------------------------------------------------------------------
# Interest reclassification (the FIFA merge-op)
# --------------------------------------------------------------------------

def test_reclassify_interest_kind_marks_subject_event_driven():
    interests: dict = {}
    apply_interest_signal(interests, "sports", "FIFA World Cup", NOW)
    applied = reclassify_interest_kind(interests, "sports", "FIFA World Cup", INTEREST_KIND_EVENT_DRIVEN)
    assert applied is True
    assert interests["sports"]["subjects"]["FIFA World Cup"]["kind"] == "event_driven"


def test_reclassify_interest_kind_case_insensitive_and_missing():
    interests: dict = {}
    apply_interest_signal(interests, "sports", "FIFA World Cup", NOW)
    # Different casing still matches by display.
    assert reclassify_interest_kind(interests, "sports", "fifa world cup", INTEREST_KIND_EVENT_DRIVEN)
    # Absent subject is a safe no-op, never an error.
    assert reclassify_interest_kind(interests, "sports", "Wimbledon", INTEREST_KIND_EVENT_DRIVEN) is False


# --------------------------------------------------------------------------
# Traits (corroboration + confidence gate; the user sees their own Aura)
# --------------------------------------------------------------------------

def test_trait_hidden_until_corroborated():
    traits: dict = {}
    apply_trait(traits, "passion-oriented", confidence=0.9, now=NOW)
    # One piece of evidence -> stored but NOT shown.
    assert shown_traits({"traits": traits}, now=NOW) == []
    apply_trait(traits, "passion-oriented", confidence=0.9, now=NOW)
    # Corroborated (>=2) -> now shown.
    assert "passion-oriented" in shown_traits({"traits": traits}, now=NOW)


def test_trait_below_confidence_never_shown():
    traits: dict = {}
    apply_trait(traits, "perfectionist", confidence=0.3, now=NOW)
    apply_trait(traits, "perfectionist", confidence=0.3, now=NOW)
    # Corroborated by count, but confidence under threshold -> stays hidden.
    assert shown_traits({"traits": traits}, now=NOW) == []


def test_trait_corroboration_counts_distinct_sessions_not_reruns():
    traits: dict = {}
    # Re-reflecting the SAME session (a long session re-consolidated as it grows) must NOT
    # inflate the trait toward the shown gate.
    apply_trait(traits, "goal-oriented", confidence=0.9, now=NOW, session_id="s1")
    apply_trait(traits, "goal-oriented", confidence=0.9, now=NOW, session_id="s1")
    assert shown_traits({"traits": traits}, now=NOW) == []  # one distinct session
    # A genuinely DIFFERENT session corroborates -> shown.
    apply_trait(traits, "goal-oriented", confidence=0.9, now=NOW, session_id="s2")
    assert "goal-oriented" in shown_traits({"traits": traits}, now=NOW)


def test_prune_interest_removes_subject_and_empty_category():
    interests: dict = {}
    apply_interest_signal(interests, "fitness_nutrition", "road bike", NOW)
    apply_interest_signal(interests, "sports", "cricket", NOW)
    # Prune the mis-attributed road bike (a gift for someone else), case-insensitively.
    assert prune_interest(interests, "fitness_nutrition", "Road Bike") is True
    assert "fitness_nutrition" not in interests  # category emptied -> dropped
    assert "sports" in interests             # unrelated category untouched
    assert prune_interest(interests, "sports", "tennis") is False  # absent -> safe no-op
