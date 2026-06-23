"""
AuraReflection (per-session tier) — pure patch-fold logic + session-gating.

The Firestore transaction wrapper (_apply_patch_txn) is a thin, standard
@fs.transactional shell mirroring icebreaker_store; the merge logic it runs lives in
the pure _fold_patch_into_profile, which these tests exercise directly (no Firestore).
consolidate_session's gates are tested with monkeypatch + asyncio.run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from src.services import aura_reflection as reflection
from src.services.aura_reflection import (
    InterestKindOp,
    InterestPruneOp,
    LifeFactCorrection,
    ReflectionPatch,
    ReflectionStoryline,
    ReflectionTrait,
)
from src.services.life_facts_schema import LIFE_FACTS_FIELD
from src.services.user_aura_schema import apply_interest_signal, shown_traits

NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Patch model coercion
# --------------------------------------------------------------------------

def test_patch_kind_coerces_unknown_to_durable():
    assert ReflectionStoryline(id="x", summary="y", kind="bogus").kind == "durable"
    assert ReflectionStoryline(id="x", summary="y", kind="EVENT_DRIVEN").kind == "event_driven"
    assert InterestKindOp(category="sports", subject="z", kind="garbage").kind == "durable"


def test_normalize_turns_counts_user_and_filters_empty():
    turns = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": ""},      # empty -> dropped
        {"role": "buddy", "text": "hey"},       # buddy -> Buddy
        {"role": "user", "text": "   "},        # whitespace -> dropped
        {"role": "user", "text": "second"},
    ]
    cleaned, user_turns = reflection._normalize_turns(turns)
    assert user_turns == 2
    assert ("Buddy", "hey") in cleaned
    assert all(text.strip() for _, text in cleaned)


# --------------------------------------------------------------------------
# Pure patch fold (the heart of the reflection write)
# --------------------------------------------------------------------------

def test_fold_applies_storyline_summary_and_marks_consolidated():
    profile: dict = {}
    patch = ReflectionPatch(
        session_summary="talked through a career goal",
        storylines=[ReflectionStoryline(
            id="annapurna_sde",
            summary="writing a tensor-parallelism blog to land an SDE role at Annapurna Labs",
            entities=["Annapurna Labs"], categories=["career_jobs"],
            intent="career_goal", kind="goal_instrumental", confidence=0.85,
        )],
        traits=[ReflectionTrait(name="passion-oriented", confidence=0.85)],
    )
    applied = reflection._fold_patch_into_profile(profile, "s1", 4, patch, NOW)
    assert applied is True
    assert profile["storylines"]["annapurna_sde"]["kind"] == "goal_instrumental"
    assert profile["session_summary"] == "talked through a career goal"
    assert profile["reflected_sessions"]["s1"] == 4
    # A single trait inference is stored but NOT yet shown (needs corroboration).
    assert shown_traits(profile, now=NOW) == []


def test_fold_idempotent_at_same_size_but_reflects_on_growth():
    patch = ReflectionPatch(storylines=[ReflectionStoryline(id="x", summary="y")])
    # Already reflected at 4 turns -> the same size is a no-op.
    profile: dict = {"reflected_sessions": {"s1": 4}}
    assert reflection._fold_patch_into_profile(profile, "s1", 4, patch, NOW) is False
    assert "storylines" not in profile
    # Grown to 6 turns -> re-reflects. THIS is the fix for the freeze bug.
    assert reflection._fold_patch_into_profile(profile, "s1", 6, patch, NOW) is True
    assert "x" in profile["storylines"]
    assert profile["reflected_sessions"]["s1"] == 6


def test_fold_backfills_session_frozen_by_legacy_ring():
    # A session frozen by the OLD id-only ring must reflect once under the new logic,
    # then migrate onto reflected_sessions.
    patch = ReflectionPatch(storylines=[ReflectionStoryline(id="x", summary="y")])
    profile: dict = {"consolidated_session_ids": ["s1"]}
    assert reflection._fold_patch_into_profile(profile, "s1", 4, patch, NOW) is True
    assert "x" in profile["storylines"]
    assert profile["reflected_sessions"]["s1"] == 4
    assert "consolidated_session_ids" not in profile  # migrated off


def test_fold_trait_shown_only_after_two_distinct_sessions():
    profile: dict = {}
    patch = ReflectionPatch(traits=[ReflectionTrait(name="passion-oriented", confidence=0.9)])
    reflection._fold_patch_into_profile(profile, "s1", 2, patch, NOW)
    assert shown_traits(profile, now=NOW) == []                  # 1 session -> hidden
    reflection._fold_patch_into_profile(profile, "s2", 2, patch, NOW)
    assert "passion-oriented" in shown_traits(profile, now=NOW)  # 2 distinct -> shown


def test_fold_trait_not_double_counted_when_same_session_regrows():
    profile: dict = {}
    patch = ReflectionPatch(traits=[ReflectionTrait(name="passion-oriented", confidence=0.9)])
    reflection._fold_patch_into_profile(profile, "s1", 2, patch, NOW)
    reflection._fold_patch_into_profile(profile, "s1", 5, patch, NOW)  # same session grew
    # Still ONE distinct session -> re-runs must never corroborate a trait into view.
    assert shown_traits(profile, now=NOW) == []


def test_fold_reclassifies_fifa_interest_to_event_driven():
    interests: dict = {}
    apply_interest_signal(interests, "sports", "FIFA World Cup", NOW)
    profile = {"interests": interests}
    patch = ReflectionPatch(interest_kind_ops=[
        InterestKindOp(category="sports", subject="FIFA World Cup", kind="event_driven"),
    ])
    reflection._fold_patch_into_profile(profile, "s1", 3, patch, NOW)
    assert profile["interests"]["sports"]["subjects"]["FIFA World Cup"]["kind"] == "event_driven"


def test_fold_prunes_misattributed_interest():
    interests: dict = {}
    apply_interest_signal(interests, "fitness_nutrition", "road bike", NOW)
    profile = {"interests": interests}
    patch = ReflectionPatch(interest_prune=[
        InterestPruneOp(category="fitness_nutrition", subject="road bike"),
    ])
    reflection._fold_patch_into_profile(profile, "s1", 3, patch, NOW)
    assert "fitness_nutrition" not in profile["interests"]  # emptied -> dropped


def test_fold_compacts_facts_and_goals_with_antiwipe():
    profile: dict = {
        "explicit_facts": ["watches with family", "enjoys watching with family", "lives in Berlin"],
        "inferred_goals": ["g1", "g1 restated"],
    }
    patch = ReflectionPatch(
        facts_canonical=["watches with family", "lives in Berlin"],
        goals_canonical=["g1"],
    )
    reflection._fold_patch_into_profile(profile, "s1", 4, patch, NOW)
    assert profile["explicit_facts"] == ["watches with family", "lives in Berlin"]
    assert profile["inferred_goals"] == ["g1"]
    # Anti-wipe: an empty canonical never erases the existing list.
    reflection._fold_patch_into_profile(profile, "s1", 9, ReflectionPatch(), NOW)
    assert profile["explicit_facts"] == ["watches with family", "lives in Berlin"]


def test_fold_corrects_wrong_life_fact():
    profile: dict = {LIFE_FACTS_FIELD: {"home_city": {"value": "Osaka"}}}
    patch = ReflectionPatch(life_fact_corrections=[
        LifeFactCorrection(key="home_city", value=None),  # Osaka is a destination, clear it
    ])
    reflection._fold_patch_into_profile(profile, "s1", 4, patch, NOW)
    assert "home_city" not in profile[LIFE_FACTS_FIELD]


# --------------------------------------------------------------------------
# consolidate_session gating (consent + trivial), no Firestore
# --------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self) -> None:
        self.balanced_called = False

    async def balanced(self, *args, **kwargs):
        self.balanced_called = True
        return ReflectionPatch(session_summary="ok")

    async def cheap(self, *args, **kwargs):
        return "summary"


def _patch_common(monkeypatch, fake, consent: bool):
    monkeypatch.setattr(reflection, "get_model_provider", lambda: fake)

    async def _consent(_uid):
        return consent
    monkeypatch.setattr(reflection, "_user_has_granted_aura_consent", _consent)


def test_consolidate_skips_without_consent(monkeypatch):
    fake = _FakeProvider()
    _patch_common(monkeypatch, fake, consent=False)
    asyncio.run(reflection.consolidate_session("u1", "s1", [
        {"role": "user", "text": "i wrote a blog on tensor parallelism"},
        {"role": "user", "text": "how do i get a job at annapurna labs"},
    ]))
    assert fake.balanced_called is False  # GDPR gate: never profiled


def test_consolidate_skips_trivial_session(monkeypatch):
    fake = _FakeProvider()
    _patch_common(monkeypatch, fake, consent=True)
    asyncio.run(reflection.consolidate_session("u1", "s1", [
        {"role": "user", "text": "what's 5km in miles"},  # one user turn -> no arc
    ]))
    assert fake.balanced_called is False


def test_consolidate_runs_model_and_applies_patch(monkeypatch):
    fake = _FakeProvider()
    _patch_common(monkeypatch, fake, consent=True)

    async def _read(_uid):
        return {}
    monkeypatch.setattr(reflection, "_read_profile", _read)

    captured: dict = {}

    def _apply(uid, session_id, turn_count, patch, now):
        captured["session_id"] = session_id
        captured["turn_count"] = turn_count
        captured["patch"] = patch
        return True
    monkeypatch.setattr(reflection, "_apply_patch_txn", _apply)

    asyncio.run(reflection.consolidate_session("u1", "s1", [
        {"role": "user", "text": "i wrote a blog on tensor parallelism"},
        {"role": "assistant", "text": "nice, what's it about?"},
        {"role": "user", "text": "how do i get an SDE role at annapurna labs"},
    ]))
    assert fake.balanced_called is True
    assert captured.get("session_id") == "s1"
    assert isinstance(captured.get("patch"), ReflectionPatch)
