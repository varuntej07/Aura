"""Schema for a per-user weekly Get Better issue.

Why a separate envelope instead of reusing ``GetBetterCatalog``: that model
enforces three whole-catalog invariants a per-user issue cannot satisfy —
exactly one ``featured`` story across the *shared* catalog, a minimum of 20
stories, and ``related_story_ids`` that resolve inside the same catalog
(``models.py:110-133``). Relaxing them there would weaken the guarantees the
reviewed catalog depends on, so the mix gets its own envelope and
``GetBetterCatalog`` is left untouched.

Two deliberate differences from the catalog:

- **Longer narratives.** ``GetBetterIdea.narrative`` caps at 1,800 chars (~300
  words), which cannot read like published prose. ``GetBetterMixStory`` raises
  only that one bound. Verified safe on already-installed clients: the detail
  sheet is a ``DraggableScrollableSheet`` wrapping a ``ListView``
  (``get_better_screen.dart:901,936``) with no ``maxLines`` on the narrative, and
  Dart parses it as a plain ``String``.
- **Repair, don't raise.** The catalog *raises* on an unresolvable
  ``related_story_id`` (``models.py:125-132``), which is right for a human-
  reviewed artifact published once. For a generated issue that same strictness
  would cost the user their whole week over a dangling cross-reference, so the
  mix drops bad references instead.

The mix carries BOTH the generated stories and the curated library in one list,
so the client needs no change: it is just a catalog with a per-user version.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .models import GetBetterCardType, GetBetterIdea, GetBetterImageKey

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

# A real short chapter rather than a card blurb.
#
# MEASURED, not guessed. Three live gemini-3.8-flash generations from a
# "roughly 1,200 words" prompt returned 8,836 / 9,280 / 9,942 chars
# (1,561-1,777 words) — the model overshoots a word target by 30-45%. A 9,000
# ceiling would therefore have dropped two of three finished stories on
# validation, and the issue would have fallen back to curated with no obvious
# cause. 12,000 leaves headroom above the observed maximum; the prompt asks for
# ~1,000 words so the expected landing zone is well inside it.
MIX_NARRATIVE_MAX_CHARS = 12_000

# What the prose prompt should ask for, given the measured overshoot. Six pieces
# at the resulting length is ~8,000 words a week: a magazine issue, roughly 35
# minutes of reading.
PROSE_TARGET_WORDS = 1_000

# Measured sufficient: all three probe budgets finished with STOP, including
# 4,000. Thinking tokens (~1,200, and they bill as output) count against this
# ceiling, so the headroom above visible prose is deliberate, not slack.
PROSE_MAX_OUTPUT_TOKENS = 5_000
# Generated pieces per weekly issue. Six long stories is ~7,000 words a week —
# a magazine issue. Sixteen would be a third of a novel nobody reads, at a cost
# nobody should pay.
TARGET_GENERATED_STORIES = 6
# Below this many surviving generated stories the issue is not worth serving and
# the caller falls back to the curated catalog.
MIN_GENERATED_STORIES = 4

# The category values the curated catalog actually uses. `GetBetterIdea.category`
# is an unconstrained 2-28 char string, but the client renders it raw into a
# fixed-width pill (`get_better_screen.dart:698`), so generation is held to the
# set that is known to fit and known to look intentional.
GetBetterCategory = Literal[
    "Adventure",
    "Career",
    "Clarity",
    "Confidence",
    "Creativity",
    "Focus",
    "Learning",
    "Momentum",
    "Money",
    "Relationships",
    "Routines",
    "Wellbeing",
]

MIX_STATUS_PENDING = "pending"
MIX_STATUS_GENERATING = "generating"
MIX_STATUS_READY = "ready"
MIX_STATUS_FAILED = "failed"
MIX_STATUS_SKIPPED = "skipped"

MixStatus = Literal["pending", "generating", "ready", "failed", "skipped"]


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------


class GetBetterMixStory(GetBetterIdea):
    """A story inside a personal issue.

    Identical to ``GetBetterIdea`` except for a longer narrative bound, so a
    curated story validates here unchanged (the bound is only ever loosened) and
    the serialized shape the client receives is byte-for-byte the same.
    """

    narrative: str = Field(min_length=120, max_length=MIX_NARRATIVE_MAX_CHARS)


# ---------------------------------------------------------------------------
# Generation-stage models
# ---------------------------------------------------------------------------


class MixPlanEntry(BaseModel):
    """One planned story, before its prose exists."""

    slug: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=3, max_length=72)
    category: GetBetterCategory
    image_key: GetBetterImageKey
    card_type: GetBetterCardType = "square"
    minutes: int = Field(ge=1, le=90)
    # The editorial angle handed to the prose stage. Not shown to the user.
    angle: str = Field(min_length=12, max_length=400)


class MixPlan(BaseModel):
    """Output of the planning stage: the shape of this week's issue."""

    headline: str = Field(min_length=4, max_length=80)
    intro: str = Field(min_length=20, max_length=260)
    entries: list[MixPlanEntry] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_slugs(self) -> MixPlan:
        slugs = [entry.slug for entry in self.entries]
        if len(set(slugs)) != len(slugs):
            raise ValueError("MixPlan entries must have unique slugs")
        return self


class StoryMetadata(BaseModel):
    """Output of the metadata stage: the schema fields derived from free prose.

    Deliberately excludes everything the assembler owns — ``id``,
    ``display_order``, ``featured``, ``personalized``, ``status`` and
    ``related_story_ids`` are decided in code, never by the model, so a model
    that misunderstands the instruction cannot produce two featured stories or a
    colliding id.
    """

    summary: str = Field(min_length=12, max_length=220)
    why_it_fits: str = Field(min_length=12, max_length=280)
    what_it_means: str = Field(min_length=24, max_length=420)
    try_this: str = Field(min_length=12, max_length=280)
    steps: list[str] = Field(min_length=3, max_length=4)
    chat_prompt: str = Field(min_length=6, max_length=160)


class MixAudit(BaseModel):
    """Provenance for one generated issue.

    Every generated story has to be explainable after the fact: which model,
    which prompt revision, which facets, which vetted exemplars, what it cost.
    ``prompt_version`` exists so a quality regression can be bisected against a
    prompt edit rather than guessed at.
    """

    batch_job_ids: list[str] = Field(default_factory=list)
    model: str = ""
    prompt_version: str = ""
    exemplar_ids: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_microusd: int = 0
    generated_at: datetime | None = None
    stage_timings_ms: dict[str, int] = Field(default_factory=dict)
    dropped: dict[str, int] = Field(default_factory=dict)
    """Stories discarded, keyed by reason (validation / named_person /
    sensitive / truncation). A silently shrinking issue is the failure mode this
    field exists to make visible."""


class GetBetterMix(BaseModel):
    """One user's weekly issue: generated stories plus the curated library."""

    schema_version: int = 1
    catalog_version: str = Field(min_length=3, max_length=64)
    week_start: str = Field(min_length=10, max_length=10)
    status: MixStatus = MIX_STATUS_READY
    headline: str = Field(min_length=4, max_length=80)
    intro: str = Field(min_length=20, max_length=260)
    stories: list[GetBetterMixStory] = Field(min_length=1, max_length=80)
    # Slugs and hashes ONLY. The mix must not become a second copy of the private
    # Aura profile living under a different retention policy.
    facet_digest: list[str] = Field(default_factory=list, max_length=40)
    audit: MixAudit = Field(default_factory=MixAudit)
    skip_reason: str | None = None

    @model_validator(mode="after")
    def enforce_mix_invariants(self) -> GetBetterMix:
        ids = [story.id for story in self.stories]
        if len(set(ids)) != len(ids):
            raise ValueError("GetBetterMix stories must have unique ids")

        featured = [story for story in self.stories if story.featured]
        if len(featured) != 1:
            raise ValueError(
                f"GetBetterMix needs exactly one featured story, found {len(featured)}"
            )

        # Repair rather than raise: drop self-references and any id that does not
        # resolve inside this issue. The catalog raises here because it is
        # human-reviewed and published once; a generated issue must not lose a
        # whole week to a dangling cross-reference.
        known = set(ids)
        for story in self.stories:
            resolved = [
                related
                for related in story.related_story_ids
                if related in known and related != story.id
            ]
            if resolved != story.related_story_ids:
                story.related_story_ids = resolved
        return self

    def generated_stories(self) -> list[GetBetterMixStory]:
        return [story for story in self.stories if story.personalized]

    def to_firestore(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
