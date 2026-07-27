from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

GetBetterImageKey = Literal[
    "momentum",
    "focus",
    "calm",
    "learning",
    "wellbeing",
    "relationships",
    "career",
    "creativity",
    "money",
    "routines",
    "confidence",
    "adventure",
]

GetBetterCardType = Literal["hero", "wide", "square", "prompt", "challenge"]

GetBetterEventType = Literal[
    "opened",
    "saved",
    "unsaved",
    "completed",
    "uncompleted",
    "shared",
    "related_opened",
    "buddy_chat_started",
]


class GetBetterIdea(BaseModel):
    """One canonical, shareable story.

    The legacy fields remain required because already-installed clients render
    them. New clients use the narrative fields and related story identifiers.
    """

    id: str = Field(min_length=3, max_length=72, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=3, max_length=72)
    category: str = Field(min_length=2, max_length=28)
    summary: str = Field(min_length=12, max_length=220)
    why_it_fits: str = Field(min_length=12, max_length=280)
    steps: list[str] = Field(min_length=3, max_length=4)
    chat_prompt: str = Field(min_length=6, max_length=160)
    image_key: GetBetterImageKey
    personalized: bool = False
    minutes: int = Field(ge=1, le=90)
    story_version: int = Field(default=1, ge=1)
    narrative: str = Field(min_length=120, max_length=1_800)
    what_it_means: str = Field(min_length=24, max_length=420)
    try_this: str = Field(min_length=12, max_length=280)
    related_story_ids: list[str] = Field(default_factory=list, max_length=5)
    card_type: GetBetterCardType = "square"
    display_order: int = Field(ge=0, le=10_000)
    featured: bool = False
    status: Literal["published", "retired"] = "published"

    @field_validator(
        "id",
        "title",
        "category",
        "summary",
        "why_it_fits",
        "chat_prompt",
        "what_it_means",
        "try_this",
    )
    @classmethod
    def normalize_single_line_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("narrative")
    @classmethod
    def normalize_narrative(cls, value: str) -> str:
        return "\n\n".join(" ".join(paragraph.split()) for paragraph in value.split("\n\n"))

    @field_validator("steps")
    @classmethod
    def normalize_steps(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(step.split())[:160] for step in value if step.strip()]
        if len(cleaned) < 3:
            raise ValueError("Get Better stories need at least three concrete steps")
        return cleaned[:4]

    @field_validator("related_story_ids")
    @classmethod
    def normalize_related_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(story_id.strip() for story_id in value if story_id.strip()))
        return normalized[:5]


class GetBetterCatalog(BaseModel):
    catalog_version: str = Field(min_length=3, max_length=64)
    published_at: datetime
    headline: str = Field(min_length=4, max_length=80)
    intro: str = Field(min_length=20, max_length=260)
    stories: list[GetBetterIdea] = Field(min_length=20, max_length=300)

    @field_validator("headline", "intro")
    @classmethod
    def normalize_copy(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def validate_catalog_graph(self) -> GetBetterCatalog:
        story_ids = [story.id for story in self.stories]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("Get Better story ids must be unique")

        published_ids = {story.id for story in self.stories if story.status == "published"}
        featured = [
            story
            for story in self.stories
            if story.status == "published" and story.featured
        ]
        if len(featured) != 1:
            raise ValueError("Get Better catalog needs exactly one published featured story")

        for story in self.stories:
            if story.id in story.related_story_ids:
                raise ValueError(f"Story {story.id} cannot relate to itself")
            missing = set(story.related_story_ids) - published_ids
            if missing:
                raise ValueError(
                    f"Story {story.id} references unpublished or missing stories: {sorted(missing)}"
                )
        return self

    @property
    def published_stories(self) -> list[GetBetterIdea]:
        return sorted(
            (story for story in self.stories if story.status == "published"),
            key=lambda story: (story.display_order, story.id),
        )


class GetBetterFeed(BaseModel):
    headline: str
    intro: str
    banner: GetBetterIdea
    ideas: list[GetBetterIdea] = Field(min_length=1, max_length=299)
    next_cursor: int = Field(default=0, ge=0)
    generated_at: str
    catalog_version: str


class GetBetterActivityEvent(BaseModel):
    event_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    event_type: GetBetterEventType
    story_id: str = Field(min_length=3, max_length=72, pattern=r"^[a-z0-9_]+$")
    story_version: int = Field(ge=1)
    occurred_at: datetime


class GetBetterActivityBatch(BaseModel):
    batch_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    events: list[GetBetterActivityEvent] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_unique_event_ids(self) -> GetBetterActivityBatch:
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Get Better activity event ids must be unique within a batch")
        return self
