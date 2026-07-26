from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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


class GetBetterIdea(BaseModel):
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

    @field_validator(
        "id",
        "title",
        "category",
        "summary",
        "why_it_fits",
        "chat_prompt",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("steps")
    @classmethod
    def normalize_steps(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(step.split())[:160] for step in value if step.strip()]
        if len(cleaned) < 3:
            raise ValueError("Get Better ideas need at least three concrete steps")
        return cleaned[:4]


class GetBetterFeedDraft(BaseModel):
    headline: str = Field(min_length=4, max_length=80)
    intro: str = Field(min_length=20, max_length=260)
    banner: GetBetterIdea
    ideas: list[GetBetterIdea] = Field(min_length=6, max_length=8)

    @field_validator("headline", "intro")
    @classmethod
    def normalize_copy(cls, value: str) -> str:
        return " ".join(value.split())


class GetBetterFeed(GetBetterFeedDraft):
    next_cursor: int = Field(ge=1)
    generated_at: str
