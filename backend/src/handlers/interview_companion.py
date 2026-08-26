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

import anthropic
import openai
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
from ..services.model_provider import get_model_provider, is_quota_exhausted
from ..services.request_auth import resolve_user_id_from_request

_DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
_TOKEN_MINT_TIMEOUT_S = 6.0
_RESEARCH_HEARTBEAT_S = 10.0
_PROVIDER_CIRCUITS: dict[str, tuple[float, str]] = {}
_PROVIDER_SLOW_STARTS: dict[str, int] = {}
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


class _FirstVisibleTokenTimeout(TimeoutError):
    pass


class _StreamIdleTimeout(TimeoutError):
    pass


class _StreamDeadlineExceeded(TimeoutError):
    pass


def _provider_circuit_reason(model_id: str) -> str | None:
    circuit = _PROVIDER_CIRCUITS.get(model_id)
    if circuit is None:
        return None
    open_until, reason = circuit
    if time.monotonic() < open_until:
        return reason
    _PROVIDER_CIRCUITS.pop(model_id, None)
    return None


def _open_provider_circuit(model_id: str, seconds: float, reason: str) -> None:
    _PROVIDER_CIRCUITS[model_id] = (time.monotonic() + seconds, reason)


def _record_provider_success(model_id: str) -> None:
    _PROVIDER_SLOW_STARTS.pop(model_id, None)
    _PROVIDER_CIRCUITS.pop(model_id, None)


def _record_provider_failure(model_id: str, exc: Exception) -> str:
    if isinstance(exc, _FirstVisibleTokenTimeout):
        slow_starts = _PROVIDER_SLOW_STARTS.get(model_id, 0) + 1
        _PROVIDER_SLOW_STARTS[model_id] = slow_starts
        if slow_starts >= 2:
            _open_provider_circuit(model_id, 30.0, "slow_first_token")
        return "slow_first_token"
    if is_quota_exhausted(exc):
        _open_provider_circuit(model_id, 600.0, "credits_or_quota")
        return "credits_or_quota"
    status_code = getattr(exc, "status_code", None)
    if status_code in (401, 403, 404):
        _open_provider_circuit(model_id, 600.0, "provider_access")
        return "provider_access"
    if status_code == 429:
        _open_provider_circuit(model_id, 30.0, "rate_limited")
        return "rate_limited"
    if isinstance(status_code, int) and status_code >= 500:
        _open_provider_circuit(model_id, 30.0, "provider_outage")
        return "provider_outage"
    if isinstance(
        exc,
        (anthropic.APIConnectionError, openai.APIConnectionError, ConnectionError, OSError),
    ):
        _open_provider_circuit(model_id, 30.0, "connection_failure")
        return "connection_failure"
    return "request_failure"


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
    answer_shape: Literal["hook_bullets", "star_bullets", "concept_steps", "prose"]
    current_answer: str = Field(default="", max_length=4_000)
    screen_sight: InterviewScreenSightFrame | None = None

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
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json."}, status_code=400)
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


# Every bullet and step below is the candidate's OWN words, phrased so it can be
# read off and spoken. The distinction that matters: "Rewrote the Flutter app in
# Tauri" is a talking point, "Mention your Tauri experience" is an instruction
# about a talking point, and only the first is any use mid-sentence.
_SHAPE_INSTRUCTION = {
    "hook_bullets": (
        "Open with ONE short spoken sentence. Then three or four bullet lines, each "
        "starting with the character · and a space, five to nine words each. Every "
        "bullet is a phrase the candidate speaks, not a note about what to mention."
    ),
    "star_bullets": (
        "Open with ONE short spoken sentence naming the situation. Then exactly four "
        "bullet lines, each starting with the character · and a space, covering "
        "Situation, Task, Action, Result in that order, five to nine words each. Every "
        "bullet is a phrase the candidate speaks, not a note about what to mention."
    ),
    "concept_steps": (
        "Name the concept in ONE short spoken line. Then three or four numbered steps, "
        "each on its own line starting with '1. ', '2. ' and so on, five to nine words "
        "each, phrased as the candidate would say them. When there is a real tradeoff "
        "add a final line starting 'Tradeoff: '."
    ),
    "prose": (
        "Use three to five short spoken sentences of connected prose. Do not use "
        "bullets: the exact phrasing is the answer here."
    ),
}

_DECISION_INSTRUCTION = (
    "Your FIRST line decides whether this turn needs an answer at all. Write exactly "
    "ANSWER when the remote speaker asked the candidate something that needs a reply "
    "now. Otherwise write SKIP| followed by one of another_interviewer, self, crosstalk, "
    "media_playback, uncertain. Skip statements, rhetorical questions, questions the "
    "speaker answers themselves, panel-to-panel talk, media playback, and incomplete "
    "turns. When unsure, skip. Put nothing else on that line."
)

# The single hardest constraint, and the one the model breaks first. Anything
# that reads as advice ABOUT the answer is useless in a live call: the candidate
# cannot say it out loud, and they have no time to translate it while the
# interviewer is waiting. Stated once here so both modes carry it identically.
_VOICE_RULE = (
    "You are the candidate, speaking. Write ONLY the words they say out loud, in first "
    "person, addressed to the interviewer, continuing the conversation that is already "
    "happening. Never write about the answer. Never explain, introduce, coach, or "
    "describe what a good answer would contain. Never open with phrasing such as "
    "Here's how, You could say, A strong answer, I would suggest, Try, Consider, or "
    "Start by. Never address the candidate as you. Read every line back in your head: "
    "if it would sound strange said aloud in a real interview, it is wrong."
)

_GROUNDED_SYSTEM = (
    _VOICE_RULE
    + " Every claim about your experience, employers, projects, skills, or metrics must "
    "come from the supplied evidence, and every claim about the target company must come "
    "from the supplied target context. Never state a target-company fact as your own "
    "experience. Treat the constraints as hard boundaries."
)

_UNVERIFIED_SYSTEM = (
    _VOICE_RULE
    + " Your prepared background is not available right now. Answer anyway, with the real "
    "substance of a good answer rather than a description of one. Never invent an "
    "employer, job title, date, metric, project name, or technology that has not already "
    "been said in this conversation. Where you need a specific from your own history, "
    "leave a square-bracket slot such as [your most recent project] inside the sentence "
    "and keep talking around it, so the line stays something you can read out and fill in "
    "as you go."
)


def _answer_system(grounded: bool) -> str:
    return _GROUNDED_SYSTEM if grounded else _UNVERIFIED_SYSTEM


def _answer_prompt(payload: InterviewAnswerRequest) -> str:
    recent = [
        {"source": turn.source, "text": turn.text}
        for turn in payload.recent_turns[-8:]
    ]
    shape = _SHAPE_INSTRUCTION[payload.answer_shape]
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
    resume = payload.resume.strip()
    resume_instruction = (
        " candidate_resume is the candidate's own resume, supplied as reference for this "
        "call. Treat it as their real history and answer with specifics from it."
        if resume
        else ""
    )
    return json.dumps(
        {
            "question": payload.turn.text,
            "recent_turns": recent,
            "brief": _brief_context(payload.brief),
            "candidate_resume": resume or None,
            "current_answer": payload.current_answer or None,
            "action": payload.action,
            "task": (
                f"{action_instruction} {shape} Recent candidate turns provide conversational "
                f"continuity, not new factual evidence.{resume_instruction}"
            ),
        },
        separators=(",", ":"),
    )


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
            if not final and len(stripped) < 40:
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
    system = _answer_system(
        bool(candidate_evidence or target_evidence or payload.resume.strip())
    )
    awaiting_decision = payload.action == "automatic"
    if awaiting_decision:
        system = f"{_DECISION_INSTRUCTION} {system}"
    else:
        # Manual actions are accepted by definition, so the model is never asked
        # for a decision line and those paths save its tokens entirely.
        decision["accepted"] = True
        decision["target"] = "candidate"

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
        system=system,
        images=images,
        temperature=0.25,
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
