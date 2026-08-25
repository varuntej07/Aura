"""Authenticated Interview Companion STT credential and answer stream.

Raw transcripts are request-scoped. Nothing in this module persists or logs
speech, preparation context, gate explanations, or generated answers.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import AsyncGenerator
from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..config.settings import settings
from ..lib.logger import logger
from ..services.company_research import CompanyResearchRequest, research_company
from ..services.interview_preparation import (
    BriefClaim,
    InterviewBriefBuildRequest,
    InterviewBriefDraft,
    InterviewBriefSlice,
    assemble_interview_brief,
    interview_brief_prompt,
)
from ..services.model_provider import get_model_provider
from ..services.request_auth import resolve_user_id_from_request

_DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
_TOKEN_MINT_TIMEOUT_S = 6.0
_GATE_CONFIDENCE = 0.78
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


class TranscriptTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1)
    turn_id: str = Field(min_length=1, max_length=128)
    source: Literal["candidate", "remote"]
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4_000)
    is_final: bool
    remote_speaker_id: str | None = Field(default=None, max_length=128)
    speaker_overlap: bool = False
    final_word_at_ms: int | None = Field(default=None, ge=0)


class InterviewContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: str = Field(default="", max_length=300)
    role: str = Field(default="", max_length=300)
    resume: str = Field(default="", max_length=12_000)
    job_description: str = Field(default="", max_length=12_000)


class InterviewScreenSightFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mime_type: Literal["image/jpeg"]
    data: str = Field(min_length=1, max_length=4_000_000)
    width_px: int = Field(ge=1, le=8_192)
    height_px: int = Field(ge=1, le=8_192)
    captured_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_image(self) -> InterviewScreenSightFrame:
        try:
            decoded = base64.b64decode(self.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("screen sight image is not valid base64") from exc
        if len(decoded) > 3_000_000:
            raise ValueError("screen sight image is too large")
        if not decoded.startswith(b"\xff\xd8\xff") or not decoded.endswith(b"\xff\xd9"):
            raise ValueError("screen sight image is not a JPEG")
        return self


AnswerAction = Literal[
    "automatic",
    "suggest",
    "shorter",
    "another_example",
    "more_technical",
    "screen_sight",
]


class InterviewAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    contract_version: Literal[1, 2, 3] = 1
    turn: TranscriptTurn
    recent_turns: list[TranscriptTurn] = Field(default_factory=list, max_length=12)
    context: InterviewContext = Field(default_factory=InterviewContext)
    brief: InterviewBriefSlice | None = None
    action: AnswerAction = "automatic"
    current_answer: str = Field(default="", max_length=4_000)
    screen_sight: InterviewScreenSightFrame | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> InterviewAnswerRequest:
        if self.contract_version == 1 and (
            self.brief is not None
            or self.action != "automatic"
            or self.current_answer
            or self.screen_sight is not None
        ):
            raise ValueError("phase 3 fields require contract version 2")
        if self.contract_version < 3 and self.screen_sight is not None:
            raise ValueError("screen sight requires contract version 3")
        if self.action == "screen_sight" and self.screen_sight is None:
            raise ValueError("screen sight action requires an image")
        if self.action != "screen_sight" and self.screen_sight is not None:
            raise ValueError("screen sight image requires the screen sight action")
        if self.action in ("shorter", "another_example", "more_technical"):
            if not self.current_answer.strip():
                raise ValueError("answer transformation requires a current answer")
        return self


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: Literal[
        "candidate",
        "another_interviewer",
        "self",
        "crosstalk",
        "media_playback",
        "uncertain",
    ]
    intent: Literal["question", "request", "statement", "rhetorical", "incomplete"]
    requires_response: bool
    confidence: float = Field(ge=0.0, le=1.0)
    normalized_question: str = Field(default="", max_length=2_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    likely_follow_up: bool = False


class GroundedSentence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=1_000)
    kind: Literal["candidate_fact", "target_fact", "general"]
    source_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_grounding(self) -> GroundedSentence:
        if self.kind in ("candidate_fact", "target_fact") and not self.source_ids:
            raise ValueError("factual sentences require evidence")
        if self.kind == "general" and self.source_ids:
            raise ValueError("general sentences cannot claim evidence")
        return self


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sentences: list[GroundedSentence] = Field(min_length=1, max_length=10)


class InterviewReflectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    session_id: str = Field(min_length=1, max_length=128)
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int = Field(ge=0)
    turns: list[TranscriptTurn] = Field(min_length=1, max_length=120)
    brief: InterviewBriefSlice | None = None

    @model_validator(mode="after")
    def validate_session(self) -> InterviewReflectionRequest:
        if self.ended_at_ms < self.started_at_ms:
            raise ValueError("reflection timing is invalid")
        if self.ended_at_ms - self.started_at_ms > (2 * 60 * 60 * 1_000) + 60_000:
            raise ValueError("reflection session is too long")
        if any(not turn.is_final or turn.session_id != self.session_id for turn in self.turns):
            raise ValueError("reflection turns must be final and from one session")
        if sum(len(turn.text) for turn in self.turns) > 40_000:
            raise ValueError("reflection transcript is too large")
        return self


class InterviewReflection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=1, max_length=2_000)
    strengths: list[str] = Field(default_factory=list, max_length=6)
    improvements: list[str] = Field(default_factory=list, max_length=6)
    follow_up_actions: list[str] = Field(default_factory=list, max_length=6)


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _terminal() -> str:
    return "data: [DONE]\n\n"


async def _mint_deepgram_stt_token() -> tuple[str, int] | None:
    if not settings.DEEPGRAM_DICTATION_API_KEY:
        return None
    import httpx

    try:
        async with httpx.AsyncClient(timeout=_TOKEN_MINT_TIMEOUT_S) as client:
            grant = await client.post(
                _DEEPGRAM_GRANT_URL,
                headers={
                    "Authorization": f"Token {settings.DEEPGRAM_DICTATION_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json={"ttl_seconds": settings.DEEPGRAM_STT_TOKEN_TTL_S},
            )
    except Exception as exc:
        logger.warn(
            "interview_companion: deepgram token mint failed",
            {"error_type": type(exc).__name__},
        )
        return None
    if grant.status_code != 200:
        logger.warn(
            "interview_companion: deepgram token mint rejected",
            {"status": grant.status_code},
        )
        return None
    try:
        payload = grant.json()
        access_token = payload["access_token"]
        expires_in = int(float(payload.get("expires_in") or settings.DEEPGRAM_STT_TOKEN_TTL_S))
    except Exception as exc:
        logger.warn(
            "interview_companion: deepgram token response unusable",
            {"error_type": type(exc).__name__},
        )
        return None
    if not isinstance(access_token, str) or not access_token or expires_in <= 0:
        return None
    return access_token, expires_in


async def _mint_openai_stt_token() -> tuple[str, int] | None:
    if not settings.OPENAI_API_KEY:
        return None
    from .realtime import _get_openai_client

    session_config = {
        "type": "transcription",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "gpt-live-transcribe", "delay": "low"},
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": 600,
                },
            }
        },
    }
    try:
        secret = await asyncio.wait_for(
            _get_openai_client().realtime.client_secrets.create(
                expires_after={
                    "anchor": "created_at",
                    "seconds": settings.OPENAI_REALTIME_SECRET_TTL_S,
                },
                session=session_config,
            ),
            timeout=_TOKEN_MINT_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn(
            "interview_companion: openai token mint failed",
            {"error_type": type(exc).__name__},
        )
        return None
    expires_in = max(1, int(secret.expires_at - time.time()))
    if not isinstance(secret.value, str) or not secret.value:
        return None
    return secret.value, expires_in


async def handle_mint_stt_token(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    deepgram, openai = await asyncio.gather(
        _mint_deepgram_stt_token(),
        _mint_openai_stt_token(),
    )
    if deepgram is None and openai is None:
        return JSONResponse({"error": "Interview transcription is unavailable."}, status_code=503)
    deepgram_token, deepgram_ttl = deepgram or ("", 30)
    openai_token, openai_ttl = openai or ("", 30)
    response = JSONResponse(
        {
            "accessToken": deepgram_token,
            "deepgramAccessToken": deepgram_token,
            "openaiAccessToken": openai_token,
            "expiresInSeconds": min(deepgram_ttl, openai_ttl),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def handle_build_brief(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)
    try:
        payload = InterviewBriefBuildRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse({"error": "Invalid interview preparation."}, status_code=422)

    try:
        draft = await asyncio.wait_for(
            get_model_provider().balanced(
                interview_brief_prompt(payload),
                system=(
                    "You build factual interview briefs from user-supplied sources. Return only "
                    "the requested structured brief. Source IDs are provenance, never prose. "
                    "Never infer an employer, project, skill, result, or metric beyond a source."
                ),
                response_model=InterviewBriefDraft,
                temperature=0.1,
                max_output_tokens=2_400,
            ),
            timeout=25.0,
        )
        if not isinstance(draft, InterviewBriefDraft):
            raise TypeError("brief builder returned the wrong response type")
        brief = assemble_interview_brief(payload, draft)
    except Exception as exc:
        logger.warn(
            "interview_companion: brief generation failed",
            {"error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "Interview preparation failed."}, status_code=503)

    response = JSONResponse(brief.model_dump())
    response.headers["Cache-Control"] = "no-store"
    return response


async def handle_company_research(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)
    try:
        payload = CompanyResearchRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse({"error": "Invalid company research request."}, status_code=422)

    try:
        result = await asyncio.wait_for(
            research_company(payload, uid=uid),
            timeout=95.0,
        )
    except Exception as exc:
        logger.warn(
            "interview_companion: company research failed",
            {"error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "Company research failed."}, status_code=503)

    response = JSONResponse(result.model_dump())
    response.headers["Cache-Control"] = "no-store"
    return response


def _gate_prompt(payload: InterviewAnswerRequest) -> str:
    recent = [
        {
            "source": turn.source,
            "text": turn.text,
            "remote_speaker_id": turn.remote_speaker_id,
            "speaker_overlap": turn.speaker_overlap,
        }
        for turn in payload.recent_turns[-8:]
    ]
    prompt = {
        "remote_turn": {
            "text": payload.turn.text,
            "remote_speaker_id": payload.turn.remote_speaker_id,
            "speaker_overlap": payload.turn.speaker_overlap,
        },
        "recent_turns": recent,
        "task": (
            "Decide whether this completed remote video-call turn is addressed to the "
            "candidate and requires an answer now. Fail closed for uncertainty, "
            "crosstalk, panel-to-panel speech, rhetorical questions, self-answered "
            "questions, media playback, statements, and incomplete turns. Speaker labels "
            "are supporting context only and never establish who a question targets."
        ),
    }
    if payload.contract_version >= 2:
        prompt["available_evidence"] = _brief_context(payload.brief)
        prompt["task"] = (
                "Decide whether this completed remote video-call turn is addressed to the "
                "candidate and requires an answer now. Fail closed for uncertainty, "
                "crosstalk, panel-to-panel speech, rhetorical questions, self-answered "
                "questions, media playback, statements, and incomplete turns. Speaker labels "
                "are supporting context only and never establish who a question targets. "
                "Evidence IDs must be source IDs from available_evidence."
        )
    return json.dumps(prompt, separators=(",", ":"))


def _brief_context(brief: InterviewBriefSlice | None) -> dict | None:
    if not brief:
        return None

    def verified_claims(claims: list[BriefClaim]) -> list[dict]:
        return [
            claim.model_dump()
            for claim in claims
            if claim.verification_state == "verified"
        ]

    stories = []
    for story in brief.star_stories:
        story_claims = [story.situation, story.task, story.action, story.result]
        if all(claim.verification_state == "verified" for claim in story_claims):
            stories.append(story.model_dump())
    return {
        "target_context": {
            "company": brief.company.model_dump() if brief.company else None,
            "role": brief.role.model_dump() if brief.role else None,
            "company_facts": [claim.model_dump() for claim in brief.target_facts],
            "job_requirements": [claim.model_dump() for claim in brief.jd_requirements],
        },
        "candidate_evidence": {
            "facts": verified_claims(brief.candidate_facts),
            "projects": verified_claims(brief.projects),
            "star_stories": stories,
            "metrics": verified_claims(brief.metrics),
        },
        "constraints": {
            "gaps": [claim.model_dump() for claim in brief.gaps],
            "do_not_claim": [claim.model_dump() for claim in brief.do_not_claim],
        },
        "answer_length": brief.answer_length,
    }


def _allowed_evidence_ids(brief: InterviewBriefSlice | None) -> set[str]:
    if not brief:
        return set()
    claims = [*brief.target_facts, *brief.jd_requirements]
    claims.extend(brief.candidate_facts)
    claims.extend(brief.projects)
    claims.extend(brief.metrics)
    if brief.company:
        claims.append(brief.company)
    if brief.role:
        claims.append(brief.role)
    for story in brief.star_stories:
        story_claims = [story.situation, story.task, story.action, story.result]
        if all(claim.verification_state == "verified" for claim in story_claims):
            claims.extend(story_claims)
    return {
        source_id
        for claim in claims
        if claim.scope == "target" or claim.verification_state == "verified"
        for source_id in claim.source_ids
    }


def _evidence_ids_by_scope(brief: InterviewBriefSlice | None) -> tuple[set[str], set[str]]:
    if not brief:
        return set(), set()
    candidate_claims = [*brief.candidate_facts, *brief.projects, *brief.metrics]
    for story in brief.star_stories:
        story_claims = [story.situation, story.task, story.action, story.result]
        if all(claim.verification_state == "verified" for claim in story_claims):
            candidate_claims.extend(story_claims)
    target_claims = [*brief.target_facts, *brief.jd_requirements]
    if brief.company:
        target_claims.append(brief.company)
    if brief.role:
        target_claims.append(brief.role)
    candidate_ids = {
        source_id
        for claim in candidate_claims
        if claim.verification_state == "verified"
        for source_id in claim.source_ids
    }
    target_ids = {
        source_id
        for claim in target_claims
        for source_id in claim.source_ids
    }
    return candidate_ids, target_ids


def _answer_prompt(payload: InterviewAnswerRequest, decision: GateDecision) -> str:
    recent = [
        {"source": turn.source, "text": turn.text}
        for turn in payload.recent_turns[-8:]
    ]
    if payload.contract_version == 1:
        return json.dumps(
            {
                "question": decision.normalized_question or payload.turn.text,
                "recent_turns": recent,
                "context": payload.context.model_dump(),
                "task": (
                    "Draft a concise answer the candidate can say aloud. Use two to four short "
                    "sentences. Use only facts in the supplied context or recent candidate turns. "
                    "When facts are missing, give a truthful approach instead of inventing "
                    "experience."
                ),
            },
            separators=(",", ":"),
        )
    length = payload.brief.answer_length if payload.brief else "balanced"
    length_instruction = {
        "brief": "Use one or two short sentences.",
        "balanced": "Use two to four short sentences.",
        "detailed": "Use four to six concise sentences.",
    }[length]
    action_instruction = {
        "automatic": "Draft the answer now.",
        "suggest": "Draft an answer because the candidate explicitly requested a suggestion.",
        "shorter": "Rewrite the current answer more briefly without adding facts.",
        "another_example": "Answer with a different verified STAR story when one is available.",
        "more_technical": "Make the answer more technically specific using only verified evidence.",
        "screen_sight": (
            "Use the one screenshot the candidate explicitly attached as visual context for "
            "this answer only. Do not imply the screenshot proves candidate experience."
        ),
    }[payload.action]
    task = f"{action_instruction} {length_instruction}"
    return json.dumps(
        {
            "question": decision.normalized_question or payload.turn.text,
            "recent_turns": recent,
            "legacy_context": (
                payload.context.model_dump() if payload.contract_version == 1 else None
            ),
            "brief": _brief_context(payload.brief),
            "current_answer": payload.current_answer or None,
            "action": payload.action,
            "task": (
                f"{task} Draft text the candidate can say aloud. Candidate experience may come "
                "only from verified candidate_evidence. Target-company facts may come only from "
                "target_context and must never be phrased as candidate experience. Recent "
                "candidate turns provide conversational continuity, not new factual evidence. "
                "Treat constraints as hard boundaries. When candidate evidence is missing, give "
                "a truthful approach instead of inventing experience. Each factual sentence must "
                "identify whether it is a candidate_fact or target_fact and list its source IDs."
            ),
        },
        separators=(",", ":"),
    )


async def _answer_stream(payload: InterviewAnswerRequest) -> AsyncGenerator[str, None]:
    provider = get_model_provider()
    identity = {
        "session_id": payload.turn.session_id,
        "epoch": payload.turn.epoch,
        "turn_id": payload.turn.turn_id,
    }
    gate_started = time.perf_counter()
    if payload.action == "automatic" and payload.turn.speaker_overlap:
        yield _frame(
            "decision",
            {
                "type": "decision",
                **identity,
                "target": "crosstalk",
                "intent": "incomplete",
                "requires_response": False,
                "confidence": 1.0,
                "normalized_question": "",
                "evidence_ids": [],
                "likely_follow_up": False,
                "accepted": False,
                "gate_ms": 0,
            },
        )
        yield _frame(
            "answer_done",
            {
                "type": "answer_done",
                **identity,
                "generated": False,
                "evidence_ids": [],
                "evidence": [],
                "answer_ms": 0,
            },
        )
        yield _terminal()
        return
    try:
        decision = await asyncio.wait_for(
            provider.cheap(
                _gate_prompt(payload),
                system=(
                    "You are a strict interview turn classifier. Return only the requested "
                    "structured decision. Physical source is already verified as remote; do "
                    "not infer that remote automatically means addressed to the candidate."
                ),
                response_model=GateDecision,
                temperature=0.0,
                max_output_tokens=350,
            ),
            timeout=8.0,
        )
        if not isinstance(decision, GateDecision):
            raise TypeError("gate returned the wrong response type")
    except Exception as exc:
        logger.warn(
            "interview_companion: gate failed",
            {"error_type": type(exc).__name__},
        )
        yield _frame(
            "error",
            {
                "type": "error",
                **identity,
                "code": "gate_failed",
                "message": "Question check failed.",
            },
        )
        yield _terminal()
        return

    automatic_accepted = (
        decision.target == "candidate"
        and decision.intent in ("question", "request")
        and decision.requires_response
        and decision.confidence >= _GATE_CONFIDENCE
    )
    allowed_evidence = _allowed_evidence_ids(payload.brief)
    decision.evidence_ids = [
        evidence_id
        for evidence_id in decision.evidence_ids
        if evidence_id in allowed_evidence
    ]
    accepted = automatic_accepted if payload.action == "automatic" else True
    yield _frame(
        "decision",
        {
            "type": "decision",
            **identity,
            **decision.model_dump(),
            "accepted": accepted,
            "gate_ms": round((time.perf_counter() - gate_started) * 1_000),
        },
    )
    if not accepted:
        yield _frame(
            "answer_done",
            {
                "type": "answer_done",
                **identity,
                "generated": False,
                "evidence_ids": [],
                "evidence": [],
                "answer_ms": 0,
            },
        )
        yield _terminal()
        return

    answer_started = time.perf_counter()
    try:
        if payload.contract_version == 1:
            legacy_answer = await asyncio.wait_for(
                provider.balanced(
                    _answer_prompt(payload, decision),
                    system=(
                        "You write truthful, speakable interview answers for the candidate. "
                        "Never claim experience, metrics, employers, projects, or skills that are "
                        "not present in the supplied context. Return answer text only."
                    ),
                    temperature=0.35,
                    max_output_tokens=450,
                ),
                timeout=15.0,
            )
            if not isinstance(legacy_answer, str) or not legacy_answer.strip():
                raise ValueError("answer was empty")
            answer = legacy_answer.strip()
            evidence_ids: list[str] = []
        else:
            grounded = await asyncio.wait_for(
                provider.balanced(
                    _answer_prompt(payload, decision),
                    system=(
                        "You write truthful, speakable interview answers. Return only the "
                        "requested structured sentences. Never claim experience, metrics, "
                        "employers, projects, or skills without verified candidate evidence. A "
                        "company or role statement must use target evidence and cannot describe "
                        "candidate experience. A generic approach must use kind general with no "
                        "source ID."
                    ),
                    response_model=GroundedAnswer,
                    images=(
                        [{"media_type": "image/jpeg", "data": payload.screen_sight.data}]
                        if payload.screen_sight
                        else None
                    ),
                    temperature=0.25,
                    max_output_tokens=650,
                ),
                timeout=15.0,
            )
            if not isinstance(grounded, GroundedAnswer):
                raise TypeError("answer returned the wrong response type")
            candidate_evidence, target_evidence = _evidence_ids_by_scope(payload.brief)
            evidence_ids = []
            for sentence in grounded.sentences:
                permitted = (
                    candidate_evidence
                    if sentence.kind == "candidate_fact"
                    else target_evidence
                    if sentence.kind == "target_fact"
                    else set()
                )
                if any(source_id not in permitted for source_id in sentence.source_ids):
                    raise ValueError("answer referenced evidence from the wrong scope")
                evidence_ids.extend(sentence.source_ids)
            answer = " ".join(sentence.text.strip() for sentence in grounded.sentences).strip()
            if not answer:
                raise ValueError("answer was empty")
            evidence_ids = list(dict.fromkeys(evidence_ids))
    except Exception as exc:
        logger.warn(
            "interview_companion: answer generation failed",
            {"error_type": type(exc).__name__},
        )
        yield _frame(
            "error",
            {
                "type": "error",
                **identity,
                "code": "answer_failed",
                "message": "Answer generation failed.",
            },
        )
        yield _terminal()
        return

    for offset in range(0, len(answer), 64):
        yield _frame(
            "answer_delta",
            {
                "type": "answer_delta",
                **identity,
                "delta": answer[offset : offset + 64],
            },
        )
    yield _frame(
        "answer_done",
        {
            "type": "answer_done",
            **identity,
            "generated": True,
            "evidence_ids": evidence_ids,
            "evidence": [
                {"source_id": source_id, "verification_state": "verified"}
                for source_id in evidence_ids
            ],
            "answer_ms": round((time.perf_counter() - answer_started) * 1_000),
        },
    )
    yield _terminal()


async def handle_answer_stream(request: Request) -> JSONResponse | StreamingResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)
    try:
        payload = InterviewAnswerRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse({"error": "Invalid interview turn."}, status_code=422)

    # This is a hard trust boundary. Candidate audio updates context locally,
    # but it can never trigger automatic generation even if a client is buggy.
    if payload.turn.source != "remote" or not payload.turn.is_final:
        return JSONResponse(
            {"error": "Only final remote turns may request an answer."},
            status_code=422,
        )
    if payload.turn.end_ms < payload.turn.start_ms:
        return JSONResponse({"error": "Invalid interview turn timing."}, status_code=422)

    return StreamingResponse(
        _answer_stream(payload),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _reflection_prompt(payload: InterviewReflectionRequest) -> str:
    return json.dumps(
        {
            "role_context": _brief_context(payload.brief),
            "turns": [
                {"source": turn.source, "text": turn.text}
                for turn in payload.turns
            ],
            "task": (
                "Reflect only on interview behavior visible in these turns. Summarize the "
                "conversation, identify concrete strengths, identify specific improvements, "
                "and suggest practical follow-up actions. Distinguish observed evidence from "
                "uncertainty. Do not infer personality, health, protected traits, or facts not "
                "present in the transcript and reviewed role context."
            ),
        },
        separators=(",", ":"),
    )


async def handle_reflection(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)
    try:
        payload = InterviewReflectionRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse({"error": "Invalid interview reflection."}, status_code=422)

    try:
        reflection = await asyncio.wait_for(
            get_model_provider().balanced(
                _reflection_prompt(payload),
                system=(
                    "You provide concise, evidence-bound post-interview coaching. Return only "
                    "the requested structured reflection. Never invent what happened or persist "
                    "the transcript."
                ),
                response_model=InterviewReflection,
                temperature=0.2,
                max_output_tokens=1_200,
            ),
            timeout=20.0,
        )
        if not isinstance(reflection, InterviewReflection):
            raise TypeError("reflection returned the wrong response type")
    except Exception as exc:
        logger.warn(
            "interview_companion: reflection failed",
            {"error_type": type(exc).__name__},
        )
        return JSONResponse({"error": "Interview reflection failed."}, status_code=503)

    response = JSONResponse(reflection.model_dump())
    response.headers["Cache-Control"] = "no-store"
    return response
