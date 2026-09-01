"""Shared InterviewBrief contracts and preparation helpers.

This module deliberately has no LiveKit, request, capture, or persistence
dependencies. Interview Companion and Mock Interview may share these models and
pure preparation rules while retaining separate runtimes and lifecycle policy.
"""

from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..prompts import INTERVIEW_BRIEF_BUILD_TASK

VerificationState = Literal["verified", "unverified"]
SourceKind = Literal[
    "company",
    "role",
    "resume",
    "job_description",
    "candidate_fact",
    "star_story",
    "metric",
    "gap",
    "do_not_claim",
    "company_research",
    "likely_interviewer_question",
]
ClaimScope = Literal["target", "candidate", "constraint", "practice"]
AnswerLength = Literal["brief", "balanced", "detailed"]


class BriefSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=64)
    kind: SourceKind
    label: str = Field(min_length=1, max_length=120)
    verification_state: VerificationState
    urls: list[str] = Field(default_factory=list, max_length=8)
    as_of: str = Field(default="", max_length=80)


class BriefBuildSource(BriefSource):
    text: str = Field(min_length=1, max_length=12_000)


class BriefClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2_000)
    source_ids: list[str] = Field(min_length=1, max_length=8)
    verification_state: VerificationState
    scope: ClaimScope


class StarStory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    situation: BriefClaim
    task: BriefClaim
    action: BriefClaim
    result: BriefClaim


class InterviewBriefSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[3] = 3
    brief_id: str = Field(min_length=1, max_length=64)
    company: BriefClaim | None = None
    role: BriefClaim | None = None
    sources: list[BriefSource] = Field(default_factory=list, max_length=64)
    target_facts: list[BriefClaim] = Field(default_factory=list, max_length=24)
    candidate_facts: list[BriefClaim] = Field(default_factory=list, max_length=12)
    projects: list[BriefClaim] = Field(default_factory=list, max_length=10)
    star_stories: list[StarStory] = Field(default_factory=list, max_length=6)
    metrics: list[BriefClaim] = Field(default_factory=list, max_length=10)
    jd_requirements: list[BriefClaim] = Field(default_factory=list, max_length=10)
    gaps: list[BriefClaim] = Field(default_factory=list, max_length=10)
    do_not_claim: list[BriefClaim] = Field(default_factory=list, max_length=10)
    answer_length: AnswerLength = "balanced"
    likely_interviewer_questions: list[BriefClaim] = Field(default_factory=list, max_length=12)

    def claims(self) -> list[BriefClaim]:
        claims = [*self.target_facts, *self.candidate_facts, *self.projects, *self.metrics]
        claims.extend(self.jd_requirements)
        claims.extend(self.gaps)
        claims.extend(self.do_not_claim)
        claims.extend(self.likely_interviewer_questions)
        if self.company:
            claims.append(self.company)
        if self.role:
            claims.append(self.role)
        for story in self.star_stories:
            claims.extend([story.situation, story.task, story.action, story.result])
        return claims

    @model_validator(mode="after")
    def validate_provenance(self) -> InterviewBriefSlice:
        known = {source.source_id for source in self.sources}
        if len(known) != len(self.sources):
            raise ValueError("brief source IDs must be unique")
        claim_ids: set[str] = set()
        for claim in self.claims():
            if claim.claim_id in claim_ids:
                raise ValueError("brief claim IDs must be unique")
            claim_ids.add(claim.claim_id)
            if any(source_id not in known for source_id in claim.source_ids):
                raise ValueError("brief claim references an unknown source")
        return self


class InterviewBriefBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[3] = 3
    sources: list[BriefBuildSource] = Field(min_length=1, max_length=64)
    answer_length: AnswerLength = "balanced"

    @model_validator(mode="after")
    def validate_sources(self) -> InterviewBriefBuildRequest:
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("source IDs must be unique")
        if sum(len(source.text) for source in self.sources) > 40_000:
            raise ValueError("preparation context is too large")
        return self


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=2_000)
    source_ids: list[str] = Field(min_length=1, max_length=8)


class DraftStarStory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    situation: DraftClaim
    task: DraftClaim
    action: DraftClaim
    result: DraftClaim


class InterviewBriefDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_facts: list[DraftClaim] = Field(default_factory=list, max_length=20)
    projects: list[DraftClaim] = Field(default_factory=list, max_length=16)
    star_stories: list[DraftStarStory] = Field(default_factory=list, max_length=10)
    metrics: list[DraftClaim] = Field(default_factory=list, max_length=16)
    jd_requirements: list[DraftClaim] = Field(default_factory=list, max_length=20)
    likely_interviewer_questions: list[DraftClaim] = Field(default_factory=list, max_length=12)


class InterviewBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[3] = 3
    brief_id: str
    company: BriefClaim | None = None
    role: BriefClaim | None = None
    sources: list[BriefBuildSource]
    target_facts: list[BriefClaim]
    candidate_facts: list[BriefClaim]
    projects: list[BriefClaim]
    star_stories: list[StarStory]
    metrics: list[BriefClaim]
    jd_requirements: list[BriefClaim]
    gaps: list[BriefClaim]
    do_not_claim: list[BriefClaim]
    answer_length: AnswerLength
    likely_interviewer_questions: list[BriefClaim]
    reviewed_at_ms: int | None = None


def interview_brief_prompt(payload: InterviewBriefBuildRequest) -> str:
    # The task text lives in prompts.py (every prompt in one home; prompts.py
    # is itself dependency-free, so this module stays contracts-plus-glue).
    return json.dumps(
        {
            "sources": [source.model_dump() for source in payload.sources],
            "task": INTERVIEW_BRIEF_BUILD_TASK,
        },
        separators=(",", ":"),
    )


def assemble_interview_brief(
    payload: InterviewBriefBuildRequest,
    draft: InterviewBriefDraft,
) -> InterviewBrief:
    source_by_id = {source.source_id: source for source in payload.sources}
    next_claim = 1

    def normalize(
        value: DraftClaim,
        *,
        scope: ClaimScope,
        allowed_kinds: set[SourceKind],
    ) -> BriefClaim | None:
        nonlocal next_claim
        source_ids = list(dict.fromkeys(value.source_ids))
        if not source_ids or any(source_id not in source_by_id for source_id in source_ids):
            return None
        if any(source_by_id[source_id].kind not in allowed_kinds for source_id in source_ids):
            return None
        state: VerificationState = (
            "verified"
            if all(
                source_by_id[source_id].verification_state == "verified"
                for source_id in source_ids
            )
            else "unverified"
        )
        claim = BriefClaim(
            claim_id=f"claim-{next_claim}",
            text=value.text.strip(),
            source_ids=source_ids,
            verification_state=state,
            scope=scope,
        )
        next_claim += 1
        return claim

    def exact_sources(kind: SourceKind, scope: ClaimScope) -> list[BriefClaim]:
        claims: list[BriefClaim] = []
        for source in payload.sources:
            if source.kind != kind:
                continue
            claim = normalize(
                DraftClaim(text=source.text, source_ids=[source.source_id]),
                scope=scope,
                allowed_kinds={kind},
            )
            if claim:
                claims.append(claim)
        return claims

    def normalized(
        values: list[DraftClaim],
        *,
        scope: ClaimScope,
        allowed_kinds: set[SourceKind],
    ) -> list[BriefClaim]:
        claims: list[BriefClaim] = []
        seen: set[str] = set()
        for value in values:
            claim = normalize(value, scope=scope, allowed_kinds=allowed_kinds)
            if not claim:
                continue
            key = claim.text.casefold()
            if key in seen:
                continue
            seen.add(key)
            claims.append(claim)
        return claims

    def deduplicated(claims: list[BriefClaim]) -> list[BriefClaim]:
        seen: set[str] = set()
        result: list[BriefClaim] = []
        for claim in claims:
            key = claim.text.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(claim)
        return result

    candidate_source_kinds: set[SourceKind] = {
        "resume", "candidate_fact", "star_story", "metric"
    }
    target_source_kinds: set[SourceKind] = {
        "company", "role", "job_description", "company_research",
        "likely_interviewer_question",
    }
    company_claims = exact_sources("company", "target")
    role_claims = exact_sources("role", "target")
    target_facts = exact_sources("company_research", "target")
    candidate_facts = exact_sources("candidate_fact", "candidate")
    candidate_facts.extend(
        normalized(
            draft.candidate_facts,
            scope="candidate",
            allowed_kinds=candidate_source_kinds,
        )
    )
    candidate_facts = deduplicated(candidate_facts)
    metrics = exact_sources("metric", "candidate")
    metrics.extend(
        normalized(
            draft.metrics,
            scope="candidate",
            allowed_kinds=candidate_source_kinds,
        )
    )
    metrics = deduplicated(metrics)
    likely_questions = exact_sources("likely_interviewer_question", "practice")
    likely_questions.extend(normalized(
        draft.likely_interviewer_questions,
        scope="practice",
        allowed_kinds=target_source_kinds,
    ))
    likely_questions = deduplicated(likely_questions)

    stories: list[StarStory] = []
    for index, story in enumerate(draft.star_stories, start=1):
        situation = normalize(
            story.situation, scope="candidate", allowed_kinds=candidate_source_kinds
        )
        task = normalize(story.task, scope="candidate", allowed_kinds=candidate_source_kinds)
        action = normalize(
            story.action, scope="candidate", allowed_kinds=candidate_source_kinds
        )
        result = normalize(
            story.result, scope="candidate", allowed_kinds=candidate_source_kinds
        )
        if situation and task and action and result:
            stories.append(
                StarStory(
                    story_id=f"story-{index}",
                    title=story.title.strip(),
                    situation=situation,
                    task=task,
                    action=action,
                    result=result,
                )
            )

    return InterviewBrief(
        brief_id=uuid.uuid4().hex,
        company=company_claims[0] if company_claims else None,
        role=role_claims[0] if role_claims else None,
        sources=payload.sources,
        target_facts=target_facts[:24],
        candidate_facts=candidate_facts[:24],
        projects=normalized(
            draft.projects,
            scope="candidate",
            allowed_kinds=candidate_source_kinds,
        )[:16],
        star_stories=stories[:10],
        metrics=metrics[:20],
        jd_requirements=normalized(
            draft.jd_requirements,
            scope="target",
            allowed_kinds={"job_description"},
        )[:20],
        gaps=exact_sources("gap", "constraint")[:16],
        do_not_claim=exact_sources("do_not_claim", "constraint")[:16],
        answer_length=payload.answer_length,
        likely_interviewer_questions=likely_questions[:12],
    )


def prepare_mock_interview_brief(
    *,
    company: str,
    role: str,
    experience: str,
    job_description: str,
) -> InterviewBrief:
    values = (
        ("mock-company", "company", "Company", company, "verified"),
        ("mock-role", "role", "Role", role, "verified"),
        (
            "mock-background",
            "candidate_fact",
            "Candidate background",
            experience,
            "unverified",
        ),
        (
            "mock-job-description",
            "job_description",
            "Job description",
            job_description,
            "unverified",
        ),
    )
    limits = {
        "company": 300,
        "role": 300,
        "candidate_fact": 12_000,
        "job_description": 12_000,
    }
    sources = [
        BriefBuildSource(
            source_id=source_id,
            kind=kind,
            label=label,
            text=text.strip()[: limits[kind]],
            verification_state=verification_state,
        )
        for source_id, kind, label, text, verification_state in values
        if text.strip()
    ]
    return assemble_interview_brief(
        InterviewBriefBuildRequest(sources=sources),
        InterviewBriefDraft(),
    )
