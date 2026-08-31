"""GET /insights/voice-profile — LLM-written voice persona for the desktop Insights page.

Turns the user's recent session recaps (users/{uid}/voice_sessions, written by
voice_session_summarizer.py) into a short WisprFlow-style profile: a 2-3 word persona
title plus a 2-3 sentence second-person blurb. Reads only what already exists; the one
thing it writes is its own cache doc at users/{uid}/voice_session_state/voice_profile.

Cost discipline: the cheap-tier model runs at most once per user per week, or earlier
only when at least _REGEN_SESSION_DELTA new sessions have landed since the last
generation. Everything else is served from the cache doc. Generation failures never
surface as errors: the handler falls back to the stale cached profile if one exists,
else returns {"profile": null}, so the client card simply stays hidden.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..lib.logger import logger
from ..services import voice_session_fields as vf
from ..services.firebase import admin_firestore
from ..services.model_provider import get_model_provider
from ..services.request_auth import resolve_user_id_from_request

_SESSIONS_COLLECTION = "voice_sessions"
_STATE_COLLECTION = "voice_session_state"
_PROFILE_DOC = "voice_profile"

_MAX_SUMMARIES = 20
_MIN_SUMMARIES = 3
_REGEN_MAX_AGE = timedelta(days=7)
_REGEN_SESSION_DELTA = 5

_PROFILE_PROMPT = """You write playful, flattering persona cards for a voice assistant's insights page.

            Below are recaps of one user's recent voice conversations, newest first:

            {summaries}

            Write their voice persona:
            - title: a 2-3 word persona name capturing HOW they use voice (e.g. "Scenario Architect", "Rapid Prototyper"). No punctuation.
            - blurb: 2-3 sentences in second person ("You...", "Your conversations...") describing their conversational style and what they use voice for. Specific to these recaps, warm but not sycophantic. Plain sentences, no em dashes, no bullet points.
        """


class VoiceProfile(BaseModel):
    title: str = ""
    blurb: str = ""


def _profile_ref(uid: str):
    return (
        admin_firestore()
        .collection("users").document(uid)
        .collection(_STATE_COLLECTION).document(_PROFILE_DOC)
    )


def _profile_payload(data: dict) -> dict:
    return {
        "title": data.get("title", ""),
        "blurb": data.get("blurb", ""),
        "generated_at": data.get("generated_at", ""),
    }


def _read_recent_summaries(uid: str) -> list[str]:
    query = (
        admin_firestore()
        .collection("users").document(uid)
        .collection(_SESSIONS_COLLECTION)
        .order_by("started_at", direction="DESCENDING")
        .limit(_MAX_SUMMARIES)
    )
    summaries: list[str] = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        if data.get("archived"):
            continue
        text = (data.get(vf.RECAP) or data.get("summary") or "").strip()
        if text:
            summaries.append(text)
    return summaries


async def handle_get_voice_profile(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    cached = await asyncio.to_thread(
        lambda: (_profile_ref(user_id).get().to_dict() or {})
    )
    summaries = await asyncio.to_thread(_read_recent_summaries, user_id)

    if cached.get("title"):
        generated_at = cached.get("generated_at", "")
        fresh_by_age = False
        try:
            generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=UTC)
            fresh_by_age = datetime.now(UTC) - generated < _REGEN_MAX_AGE
        except ValueError:
            pass
        count_at_generation = int(cached.get("session_count_at_generation") or 0)
        few_new_sessions = len(summaries) - count_at_generation < _REGEN_SESSION_DELTA
        if fresh_by_age and few_new_sessions:
            return JSONResponse({"profile": _profile_payload(cached)})

    if len(summaries) < _MIN_SUMMARIES:
        return JSONResponse({"profile": None})

    try:
        prompt = _PROFILE_PROMPT.format(
            summaries="\n".join(f"{i + 1}. {s}" for i, s in enumerate(summaries)),
        )
        result = await get_model_provider().cheap(
            prompt,
            response_model=VoiceProfile,
            temperature=0.6,
        )
        profile = result if isinstance(result, VoiceProfile) else VoiceProfile()
        if not profile.title.strip() or not profile.blurb.strip():
            raise ValueError("model returned an empty profile")
    except Exception as exc:
        logger.warn("voice_profile: generation failed", {"user_id": user_id, "error": str(exc)})
        if cached.get("title"):
            return JSONResponse({"profile": _profile_payload(cached)})
        return JSONResponse({"profile": None})

    doc = {
        "title": profile.title.strip(),
        "blurb": profile.blurb.strip(),
        "generated_at": datetime.now(UTC).isoformat(),
        "session_count_at_generation": len(summaries),
    }
    
    try:
        await asyncio.to_thread(lambda: _profile_ref(user_id).set(doc))
    except Exception as exc:
        logger.warn("voice_profile: cache write failed", {"user_id": user_id, "error": str(exc)})
    return JSONResponse({"profile": _profile_payload(doc)})
