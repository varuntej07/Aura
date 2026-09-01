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
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..config.settings import settings
from ..lib.logger import logger
from ..services.company_research import (
    CompanyResearchRequest,
    CompanyResearchResult,
    ResearchProgress,
    research_company,
    research_company_streaming,
)
from ..services.interview_preparation import (
    BriefClaim,
    InterviewBriefBuildRequest,
    InterviewBriefDraft,
    InterviewBriefSlice,
    assemble_interview_brief,
    interview_brief_prompt,
)
from ..prompts import (
    INTERVIEW_ACTION_OVERRIDE_RULE,
    INTERVIEW_DECISION_INSTRUCTION,
    INTERVIEW_GROUNDED_SYSTEM,
    INTERVIEW_REASONING_FLOW_RULE,
    INTERVIEW_SCREEN_NOTE_INSTRUCTION,
    INTERVIEW_SHAPE_INSTRUCTION,
    INTERVIEW_SPOKEN_RULE,
    INTERVIEW_UNVERIFIED_SYSTEM,
    INTERVIEW_VOICE_RULE,
)
from ..services import provider_health
from ..services.model_provider import get_model_provider, provider_for_model
from ..services.stt import deepgram_grant
from .request_guards import SSE_HEADERS as _SSE_HEADERS
from .request_guards import no_store_json, require_json, require_user

_TOKEN_MINT_TIMEOUT_S = 6.0
_RESEARCH_HEARTBEAT_S = 10.0
# How many leading characters may arrive before a bare "SKIP..." opening is
# classified without waiting for its newline. Sized to the longest legal skip
# line ("SKIP|another_interviewer" is 24 chars) plus streaming slack; anything
# longer is answer prose that merely starts with the word. The ANSWER/SKIP
# first line is a wire contract with the prompt (_DECISION_INSTRUCTION) and is
# documented in ECOSYSTEM.md section 5a-2; parsing fails open to answer text.
_DECISION_MAX_PREFIX_CHARS = 40


class _FirstVisibleTokenTimeout(TimeoutError):
    pass


class _StreamIdleTimeout(TimeoutError):
    pass


class _StreamDeadlineExceeded(TimeoutError):
    pass


# Circuit state and the provider-failure classifier live in
# services/provider_health.py (per-process by design; see its docstring).
# These thin wrappers keep the fallback loop's control flow byte-for-byte.
def _provider_circuit_reason(model_id: str) -> str | None:
    return provider_health.open_reason(model_id)


def _record_provider_success(model_id: str) -> None:
    provider_health.record_success(model_id)


def _record_provider_failure(model_id: str, exc: Exception) -> str:
    return provider_health.record_failure(
        model_id,
        exc,
        slow_start=isinstance(exc, _FirstVisibleTokenTimeout),
    )


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

    turn: TranscriptTurn
    recent_turns: list[TranscriptTurn] = Field(default_factory=list, max_length=12)
    brief: InterviewBriefSlice | None = None
    # The candidate's own resume, sent by the desktop only when the ranked brief
    # slice carries no candidate evidence. Reference material for this call, not
    # verified evidence: it never enters _brief_context or the evidence-id gate.
    resume: str = Field(default="", max_length=12_000)
    action: AnswerAction = "automatic"
    # Resolved on the desktop from the round picked in preflight.
    answer_shape: Literal[
        "script_conversational", "script_star", "script_technical", "script_structured"
    ]
    current_answer: str = Field(default="", max_length=4_000)
    screen_sight: InterviewScreenSightFrame | None = None
    # Short captions of screens the candidate showed Aura earlier in this round.
    # Carried so a later spoken turn ("so how would you fix that?") still knows
    # what "that" was, without re-uploading the image or persisting it anywhere.
    screen_notes: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_request(self) -> InterviewAnswerRequest:
        if self.action == "screen_sight" and self.screen_sight is None:
            raise ValueError("screen sight action requires an image")
        if self.action != "screen_sight" and self.screen_sight is not None:
            raise ValueError("screen sight image requires the screen sight action")
        if self.action in ("shorter", "another_example", "more_technical"):
            if not self.current_answer.strip():
                raise ValueError("answer transformation requires a current answer")
        return self


class ReflectionExchange(BaseModel):
    """One suggestion Aura put on screen, paired with the question it answered.

    Sent so the coach can tell a point the candidate never had from one they
    were handed and did not use. It is NOT the thing being graded.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)
    answer: str = Field(min_length=1, max_length=4_000)


class InterviewReflectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    session_id: str = Field(min_length=1, max_length=128)
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int = Field(ge=0)
    turns: list[TranscriptTurn] = Field(min_length=1, max_length=120)
    # Defaulted, so a desktop build that predates this field still validates.
    # The reverse is not true: this model forbids extras, so a desktop that
    # SENDS exchanges must not ship before this handler is deployed.
    exchanges: list[ReflectionExchange] = Field(default_factory=list, max_length=60)
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
    return await deepgram_grant.mint_grant(
        ttl_seconds=settings.DEEPGRAM_STT_TOKEN_TTL_S,
        caller="interview_companion",
    )


async def _mint_openai_stt_token() -> tuple[str, int] | None:
    if not settings.OPENAI_API_KEY:
        return None
    from ..services.openai_client import get_async_openai as _get_openai_client

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
    uid = require_user(request)
    deepgram, openai = await asyncio.gather(
        _mint_deepgram_stt_token(),
        _mint_openai_stt_token(),
    )
    if deepgram is None and openai is None:
        return JSONResponse({"error": "Interview transcription is unavailable."}, status_code=503)
    # A partial outage must be visible as one: a failed leg's token fields are
    # OMITTED, never sent as "", and its hardcoded TTL never drags
    # expiresInSeconds down. Absent field beats blank credential - a released
    # client reading a missing accessToken fails its Deepgram leg loudly
    # instead of handshaking with an empty string from a 200.
    payload: dict[str, Any] = {
        "providers": {"deepgram": deepgram is not None, "openai": openai is not None},
    }
    ttls: list[int] = []
    if deepgram is not None:
        deepgram_token, deepgram_ttl = deepgram
        payload["accessToken"] = deepgram_token
        payload["deepgramAccessToken"] = deepgram_token
        ttls.append(deepgram_ttl)
    if openai is not None:
        openai_token, openai_ttl = openai
        payload["openaiAccessToken"] = openai_token
        ttls.append(openai_ttl)
    payload["expiresInSeconds"] = min(ttls)
    return no_store_json(payload)


async def handle_build_brief(request: Request) -> JSONResponse:
    uid = require_user(request)
    require_json(request)
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
                max_output_tokens=8_000,
            ),
            timeout=90.0,
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

    return no_store_json(brief.model_dump())


async def handle_company_research(request: Request) -> JSONResponse:
    uid = require_user(request)
    require_json(request)
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

    return no_store_json(result.model_dump())


async def _company_research_stream(
    payload: CompanyResearchRequest, uid: str
) -> AsyncGenerator[str, None]:
    """Relay real hosted-search progress as SSE, with a heartbeat across gaps.

    Reasoning between searches routinely runs past twenty seconds, and an idle
    proxy drops a silent connection long before the dossier is ready, so the
    queue read is bounded and a comment frame fills the quiet.
    """
    queue: asyncio.Queue[
        ResearchProgress | CompanyResearchResult | BaseException | None
    ] = asyncio.Queue()

    async def consume() -> None:
        try:
            async for item in research_company_streaming(payload, uid=uid):
                await queue.put(item)
        except BaseException as exc:  # noqa: BLE001 - relayed to the client below
            await queue.put(exc)
        finally:
            await queue.put(None)

    worker = asyncio.create_task(consume())
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=_RESEARCH_HEARTBEAT_S
                )
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                logger.warn(
                    "interview_companion: company research stream failed",
                    {"error_type": type(item).__name__},
                )
                yield _frame(
                    "error",
                    {
                        "type": "error",
                        "code": "research_failed",
                        "message": "Company research failed.",
                    },
                )
                break
            if isinstance(item, ResearchProgress):
                yield _frame(
                    "research_progress",
                    {"type": "research_progress", **item.model_dump()},
                )
                continue
            yield _frame(
                "research_done",
                {"type": "research_done", "result": item.model_dump()},
            )
        yield _terminal()
    finally:
        # A disconnected client closes this generator; drop the provider call
        # with it rather than leaving it to finish into nothing.
        worker.cancel()


async def handle_company_research_stream(
    request: Request,
) -> JSONResponse | StreamingResponse:
    uid = require_user(request)
    require_json(request)
    try:
        payload = CompanyResearchRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse({"error": "Invalid company research request."}, status_code=422)

    return StreamingResponse(
        _company_research_stream(payload, uid),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


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


# The prompt constants live in prompts.py (every prompt in one home), aliased
# to their historical names here. Byte-stability was hash-verified at move
# time: _answer_cache_prefix depends on these strings staying byte-identical
# or the Anthropic prompt-cache prefix churns at the write premium.
_SPOKEN_RULE = INTERVIEW_SPOKEN_RULE
_ACTION_OVERRIDE_RULE = INTERVIEW_ACTION_OVERRIDE_RULE
_REASONING_FLOW_RULE = INTERVIEW_REASONING_FLOW_RULE
_SHAPE_INSTRUCTION = INTERVIEW_SHAPE_INSTRUCTION
_SCREEN_NOTE_INSTRUCTION = INTERVIEW_SCREEN_NOTE_INSTRUCTION
_DECISION_INSTRUCTION = INTERVIEW_DECISION_INSTRUCTION
_VOICE_RULE = INTERVIEW_VOICE_RULE
_GROUNDED_SYSTEM = INTERVIEW_GROUNDED_SYSTEM
_UNVERIFIED_SYSTEM = INTERVIEW_UNVERIFIED_SYSTEM


def _answer_system(grounded: bool) -> str:
    return _GROUNDED_SYSTEM if grounded else _UNVERIFIED_SYSTEM


def _answer_temperature(action: AnswerAction) -> float:
    """Re-rolls need variety; transformations need obedience.

    `another_example` and `suggest` re-ask the SAME question with the same brief
    and the same cached prefix, so at 0.25 they reproduce the previous answer
    almost verbatim - which is exactly what "Another example gives me the same
    thing" was. `shorter` and `more_technical` are transformations of a supplied
    answer and stay low, because there the job is to follow the instruction
    precisely rather than to wander.
    """
    return 0.7 if action in ("another_example", "suggest") else 0.25


def _answer_cache_prefix(payload: InterviewAnswerRequest) -> str:
    """The stable-per-session half of the prompt: how to speak, the brief, and the
    resume. Sent as a cache_control'd system block so turns after the first read
    it at cache-read rates instead of re-prefilling it every turn.

    It must be byte-identical across a session's automatic turns for the cache to
    hit, so it carries only what is frozen at Start. The desktop already sends a
    question-independent brief slice (stableInterviewBriefSlice), which is what
    makes this stable; nothing here may depend on the current question.
    """
    resume = payload.resume.strip()
    return json.dumps(
        {
            "answer_style": (
                f"{_SPOKEN_RULE} {_SHAPE_INSTRUCTION[payload.answer_shape]} "
                f"{_REASONING_FLOW_RULE} {_ACTION_OVERRIDE_RULE}"
            ),
            "brief": _brief_context(payload.brief),
            "candidate_resume": resume or None,
        },
        separators=(",", ":"),
    )


def _answer_prompt(payload: InterviewAnswerRequest) -> str:
    """The volatile half of the prompt: only what changes per turn. The brief,
    resume, and answer style live in the cached prefix (_answer_cache_prefix)."""
    recent = [
        {"source": turn.source, "text": turn.text}
        for turn in payload.recent_turns[-8:]
    ]
    # Every refinement instruction has to work with NO reviewed brief, because
    # that is the common case in a live round. The previous wording gated
    # another_example on "a verified STAR story" and more_technical on "verified
    # evidence"; with neither present the model correctly did nothing and the
    # candidate saw the same answer come back.
    action_instruction = {
        "automatic": "Draft the answer now.",
        "suggest": "Draft an answer because the candidate explicitly requested a suggestion.",
        "shorter": (
            "Cut the current answer to roughly half its length. Keep the single "
            "strongest point and drop the rest. Add no new facts. It must be "
            "visibly shorter than current_answer."
        ),
        "another_example": (
            "Answer the same question from a different angle. Prefer a different "
            "verified STAR story; if none is available, use a different part of the "
            "resume, or a different framing of the same point. Never repeat the "
            "angle current_answer already took."
        ),
        "more_technical": (
            "Go one level deeper on mechanism: name the specific technique, data "
            "structure, protocol, or failure mode. Claims about the candidate's own "
            "history still need supplied evidence, but technical depth about the "
            "domain itself is not a personal claim and needs none."
        ),
        "screen_sight": (
            "A screenshot of the candidate's screen is attached. Treat what is on "
            "that screen as the subject of this answer: if it shows a problem, a "
            "diagram, code, or a document, answer that. Use the spoken turn only as "
            "context for what is being asked about it. Do not imply the screenshot "
            "proves candidate experience."
        ),
    }[payload.action]
    resume_note = (
        " candidate_resume in the reference material is the candidate's own resume; treat it "
        "as their real history and answer with specifics from it."
        if payload.resume.strip()
        else ""
    )
    return json.dumps(
        {
            "question": payload.turn.text,
            "recent_turns": recent,
            "current_answer": payload.current_answer or None,
            "action": payload.action,
            # Volatile half on purpose: putting these in _answer_cache_prefix
            # would break the 1h cache every time a screen was shown.
            "screen_context": payload.screen_notes or None,
            "task": (
                f"{action_instruction} Recent candidate turns provide conversational "
                f"continuity, not new factual evidence.{resume_note}"
            ),
        },
        separators=(",", ":"),
    )


def _read_screen_note(buffer: str, *, final: bool = False) -> tuple[str, str] | None:
    """Split a leading ``SCREEN|<caption>`` line off an answer stream.

    Returns ``(caption, rest)``, or None while more input could still settle it.
    Fail-open like :func:`_read_decision`: a model that ignores the instruction
    and simply starts answering keeps its whole answer, and the caption is just
    absent. A missing caption must never eat the answer.
    """
    stripped = buffer.lstrip()
    if not stripped.upper().startswith("SCREEN|"):
        # Only wait while the prefix is still a possible match; a real answer
        # must not be held hostage to a marker that is never coming.
        if not final and len(stripped) < len("SCREEN|") and "SCREEN|".startswith(stripped.upper()):
            return None
        return ("", buffer)
    rest = stripped[len("SCREEN|"):]
    newline = rest.find("\n")
    if newline < 0:
        if not final:
            return None
        return (rest.strip()[:120], "")
    return (rest[:newline].strip()[:120], rest[newline + 1:])


def _read_decision(buffer: str, *, final: bool = False) -> tuple[str, str, str] | None:
    """Classify the opening of an answer stream, or None while it is still
    ambiguous and more input could settle it.

    Deliberately biased toward "answer". Anything not recognisably one of the two
    keywords comes back as answer text, whitespace and all, so a model that just
    starts answering loses nothing.
    """
    stripped = buffer.lstrip()
    upper = stripped.upper()
    if upper.startswith("SKIP"):
        newline = stripped.find("\n")
        if newline < 0:
            if not final and len(stripped) < _DECISION_MAX_PREFIX_CHARS:
                return None
            line = stripped
        else:
            line = stripped[:newline]
        _, _, target = line.partition("|")
        return ("skip", target.strip().lower() or "uncertain", "")
    if upper.startswith("ANSWER"):
        rest = stripped[len("ANSWER") :]
        newline = rest.find("\n")
        if newline >= 0:
            return ("answer", "candidate", rest[newline + 1 :])
        if rest.strip():
            # "ANSWERING the question..." is a word, not the marker. Keep it all.
            return ("answer", "candidate", buffer)
        return ("answer", "candidate", "") if final else None
    if not final:
        for keyword in ("ANSWER", "SKIP"):
            if keyword.startswith(upper) and len(upper) < len(keyword):
                return None
    return ("answer", "candidate", buffer)


async def _answer_deltas(
    payload: InterviewAnswerRequest,
    decision: dict,
    model_id: str,
) -> AsyncGenerator[str, None]:
    """Stream one answer, treating everything the model writes as answer text
    unless it opened with an explicit skip.

    Fail-open on purpose. An earlier version required a decision line AND a
    trailing evidence line, and raised when either was missing or misplaced. On a
    live interview that turned ordinary formatting drift into one of two
    failures: a lost provider leg with a slow fallback behind it, or an answer
    silently truncated at the sentinel. Metadata is now dropped when it is absent
    or malformed. The text never is.
    """
    candidate_evidence, target_evidence = _evidence_ids_by_scope(payload.brief)
    # Keyed on the resolved evidence, NOT on `payload.brief is None`: the desktop
    # ranks the brief against the question, so a well-prepared user asking
    # something off-axis arrives with an empty slice and must still get a real
    # answer rather than the grounded path with nothing to ground against.
    # A resume counts as grounding. Without this an answer with a resume and no
    # brief would run the unverified path, which tells the model its background
    # is unavailable and to leave [bracket] slots - the exact opposite of what
    # the supplied resume is for.
    # Split by VOLATILITY, not by topic. Everything frozen at Start - the voice
    # rule, the grounding mode, the answer style, the brief, the resume - rides
    # the cached block. Only the per-action first-line instructions vary, and
    # they sit after the cache breakpoint where they invalidate nothing. Before
    # this split every manual pill click re-prefilled the whole brief and resume
    # at the write premium, because the decision instruction was in front of the
    # breakpoint and changed with the action.
    stable_system = _answer_system(
        bool(candidate_evidence or target_evidence or payload.resume.strip())
    )
    awaiting_decision = payload.action == "automatic"
    volatile_system: list[str] = []
    if awaiting_decision:
        volatile_system.append(_DECISION_INSTRUCTION)
    else:
        # Manual actions are accepted by definition, so the model is never asked
        # for a decision line and those paths save its tokens entirely.
        decision["accepted"] = True
        decision["target"] = "candidate"
    awaiting_screen_note = payload.action == "screen_sight"
    if awaiting_screen_note:
        volatile_system.append(_SCREEN_NOTE_INSTRUCTION)

    images = (
        [{"media_type": "image/jpeg", "data": payload.screen_sight.data}]
        if payload.screen_sight
        else None
    )
    buffer = ""
    body_started = False

    def settle(verdict: tuple[str, str, str]) -> str | None:
        kind, target, rest = verdict
        if kind == "skip":
            decision["accepted"] = False
            decision["target"] = target
            return None
        decision["accepted"] = True
        decision["target"] = "candidate"
        return rest

    async for chunk in get_model_provider().stream_text(
        _answer_prompt(payload),
        model_id=model_id,
        system=" ".join(volatile_system) or None,
        cache_prefix=f"{stable_system}\n\n{_answer_cache_prefix(payload)}",
        images=images,
        temperature=_answer_temperature(payload.action),
        max_output_tokens=650,
        caller="interview_companion_answer",
    ):
        buffer += chunk.replace("\r", "")
        if awaiting_decision:
            verdict = _read_decision(buffer)
            if verdict is None:
                continue
            awaiting_decision = False
            rest = settle(verdict)
            if rest is None:
                return
            buffer = rest
        if not body_started:
            buffer = buffer.lstrip("\n")
            if not buffer:
                continue
            body_started = True
        if buffer:
            yield buffer
            buffer = ""

    if awaiting_screen_note:
        awaiting_screen_note = False
        caption, buffer = _read_screen_note(buffer, final=True) or ("", buffer)
        if caption:
            decision["screen_note"] = caption
    if awaiting_decision:
        # final=True always resolves, but the signature cannot express that, and
        # the fallback is the same fail-open choice made everywhere else here.
        verdict = _read_decision(buffer, final=True) or ("answer", "candidate", buffer)
        rest = settle(verdict)
        if rest is None:
            return
        buffer = rest
    if buffer:
        tail = buffer if body_started else buffer.lstrip("\n")
        if tail:
            body_started = True
            yield tail
    if decision.get("accepted") and not body_started:
        raise ValueError("streamed answer was empty")


async def _timed_answer_deltas(
    payload: InterviewAnswerRequest,
    decision: dict,
    model_id: str,
) -> AsyncGenerator[str, None]:
    stream = _answer_deltas(payload, decision, model_id)
    started = time.monotonic()
    first_deadline = started + settings.INTERVIEW_ANSWER_FIRST_TOKEN_TIMEOUT_S
    first_delta = True
    try:
        while True:
            now = time.monotonic()
            remaining = settings.INTERVIEW_ANSWER_MAX_STREAM_S - (now - started)
            if remaining <= 0:
                raise _StreamDeadlineExceeded("answer stream exceeded its total deadline")
            timeout = min(
                first_deadline - now
                if first_delta
                else settings.INTERVIEW_ANSWER_STREAM_IDLE_TIMEOUT_S,
                remaining,
            )
            if timeout <= 0:
                raise _FirstVisibleTokenTimeout(
                    "provider did not produce visible answer text in time"
                )
            try:
                delta = await asyncio.wait_for(anext(stream), timeout=timeout)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                if first_delta:
                    raise _FirstVisibleTokenTimeout(
                        "provider did not produce visible answer text in time"
                    ) from exc
                if remaining <= timeout:
                    raise _StreamDeadlineExceeded(
                        "answer stream exceeded its total deadline"
                    ) from exc
                raise _StreamIdleTimeout("answer stream stopped producing text") from exc
            if first_delta and not delta.strip():
                continue
            first_delta = False
            yield delta
    finally:
        await stream.aclose()


async def _answer_stream(payload: InterviewAnswerRequest) -> AsyncGenerator[str, None]:
    """One streamed model call carries both the decision and the answer.

    There used to be a separate awaited classifier call in front of this, which
    cost 400-900ms during which nothing reached the screen. The decision is now
    the first line the answer model writes, so a turn worth answering starts
    streaming its answer immediately and a turn worth skipping costs only the
    prefill plus four tokens.
    """
    identity = {
        "session_id": payload.turn.session_id,
        "epoch": payload.turn.epoch,
        "turn_id": payload.turn.turn_id,
    }

    def skip_frames(target: str, gate_ms: int) -> list[str]:
        return [
            _frame(
                "decision",
                {
                    "type": "decision",
                    **identity,
                    "target": target,
                    "accepted": False,
                    "gate_ms": gate_ms,
                },
            ),
            _frame(
                "answer_done",
                {
                    "type": "answer_done",
                    **identity,
                    "generated": False,
                    "answer_ms": 0,
                },
            ),
            _terminal(),
        ]

    # Overlapping speech is decided here rather than paid for at the provider:
    # the turn is known to be unusable before any prompt is built.
    if payload.action == "automatic" and payload.turn.speaker_overlap:
        for frame in skip_frames("crosstalk", 0):
            yield frame
        return

    answer_started = time.perf_counter()
    answer_streamed = False
    answer_model = ""
    answer_ttft_ms: int | None = None
    answer = ""
    decision: dict = {}
    try:
        models = tuple(dict.fromkeys((
            settings.INTERVIEW_ANSWER_PRIMARY_MODEL,
            settings.INTERVIEW_ANSWER_FALLBACK_MODEL,
        )))
        if payload.screen_sight is not None:
            # Groq-hosted models have no vision; a screen frame rides the
            # remaining (vision-capable) chain exactly as it did before Groq
            # became the text primary.
            vision_models = tuple(
                model_id for model_id in models
                if provider_for_model(model_id) != "groq"
            )
            models = vision_models or (settings.INTERVIEW_ANSWER_FALLBACK_MODEL,)
        last_exception: Exception | None = None
        for model_id in models:
            circuit_reason = _provider_circuit_reason(model_id)
            if circuit_reason is not None:
                logger.warn(
                    "interview_companion: skipping unhealthy answer provider",
                    {"model": model_id, "reason": circuit_reason},
                )
                continue
            leg_started = time.perf_counter()
            leg_decision: dict = {}
            answer_parts: list[str] = []
            try:
                async for delta in _timed_answer_deltas(
                    payload,
                    leg_decision,
                    model_id,
                ):
                    if not answer_streamed:
                        answer_streamed = True
                        answer_model = model_id
                        answer_ttft_ms = round((time.perf_counter() - leg_started) * 1_000)
                        _record_provider_success(model_id)
                        logger.info(
                            "interview_companion: answer stream visible",
                            {"model": model_id, "ttft_ms": answer_ttft_ms},
                        )
                        # The decision is known the moment text starts, because
                        # text only starts after the model committed to ANSWER.
                        yield _frame(
                            "decision",
                            {
                                "type": "decision",
                                **identity,
                                "target": "candidate",
                                "accepted": True,
                                "gate_ms": answer_ttft_ms,
                                "model": model_id,
                            },
                        )
                        # Emitted alongside the decision, not after the answer:
                        # the caption is known the moment the marker line is
                        # parsed, which is before the first body delta.
                        screen_note = leg_decision.get("screen_note")
                        if screen_note:
                            yield _frame(
                                "screen_note",
                                {
                                    "type": "screen_note",
                                    **identity,
                                    "note": screen_note,
                                },
                            )
                    answer_parts.append(delta)
                    yield _frame(
                        "answer_delta",
                        {
                            "type": "answer_delta",
                            **identity,
                            "delta": delta,
                        },
                    )
                decision = leg_decision
                # A skip is a completed call, not a failed one, so it must not
                # fall through to the next provider.
                if decision.get("accepted") is False:
                    _record_provider_success(model_id)
                    break
                answer = "".join(answer_parts).strip()
                if not answer:
                    raise ValueError("answer was empty")
                break
            except Exception as stream_exc:
                if answer_streamed:
                    raise
                reason = _record_provider_failure(model_id, stream_exc)
                logger.warn(
                    "interview_companion: answer provider failed before visible text",
                    {
                        "model": model_id,
                        "reason": reason,
                        "error_type": type(stream_exc).__name__,
                        "elapsed_ms": round((time.perf_counter() - leg_started) * 1_000),
                    },
                )
                last_exception = stream_exc
        else:
            raise last_exception or RuntimeError("no healthy answer provider was available")
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

    if decision.get("accepted") is False:
        for frame in skip_frames(
            decision.get("target") or "uncertain",
            round((time.perf_counter() - answer_started) * 1_000),
        ):
            yield frame
        return

    yield _frame(
        "answer_done",
        {
            "type": "answer_done",
            **identity,
            "generated": True,
            "answer_ms": round((time.perf_counter() - answer_started) * 1_000),
            "answer_model": answer_model,
            "answer_ttft_ms": answer_ttft_ms,
        },
    )
    yield _terminal()


async def handle_answer_stream(request: Request) -> JSONResponse | StreamingResponse:
    uid = require_user(request)
    require_json(request)
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
            "suggestions_offered": [
                {"question": item.question, "suggested": item.answer}
                for item in payload.exchanges
            ] or None,
            "task": (
                "Grade ONLY what the candidate actually said: the turns whose source is "
                "candidate. suggestions_offered is what Aura put on the candidate's screen "
                "during the round; it is context, never the thing being assessed, and a "
                "strong suggestion the candidate never used is not a strength. "
                "The candidate turns are live speech-to-text, so treat fragments, "
                "restarts, and missing words as transcription artifacts unless the same "
                "gap shows up across several turns; do not report transcription noise as a "
                "communication weakness. Summarize the conversation, identify concrete "
                "strengths, identify specific improvements, and suggest practical follow-up "
                "actions. The standard for a strong technical answer here is that it moves "
                "on its own from what the problem is, to what they would do, to what that "
                "costs, to why it is still the right call, as flowing speech and never as "
                "labelled sections. Distinguish observed evidence from uncertainty. Do not "
                "infer personality, health, protected traits, or facts not present in the "
                "transcript and reviewed role context."
            ),
        },
        separators=(",", ":"),
    )


async def handle_reflection(request: Request) -> JSONResponse:
    uid = require_user(request)
    require_json(request)
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

    return no_store_json(reflection.model_dump())
