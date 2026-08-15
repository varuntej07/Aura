"""Buddy reading a reminder aloud, rendered ahead of time so it survives 3 AM.

The `buddy` alarm tone plays the normal bed clip and then, twenty seconds in,
Buddy says the reminder in the user's own chosen voice. That line cannot be
synthesized when the alarm rings: the alarm fires from a local OS schedule into
a process with no Flutter engine, and the phone may be in airplane mode. So it
is rendered here, cached on the device at ARM time, and played from disk.

The device never decides when to re-render. It is handed a :func:`clip_tag`,
the stable SHA-1 prefix of ``message|resolved voice``, and uses it as part of the
cache filename. A tag it already has on disk is a cache hit, and nothing is
fetched. That indirection is what keeps this from costing a Cartesia render on
every reconcile, and reconcile runs on every app resume.

The line is the reminder verbatim behind a time preamble. No LLM writes it: it
is the user's own words, which is both cheaper and more honest than a model
paraphrasing what someone asked to be woken for.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime

import httpx

from ..agent.voice.voice_catalog import resolve_voice
from ..config.settings import settings
from ..lib.logger import logger

# Matches generate_voice_previews.py, which is the working reference for this
# REST contract. `speed` in particular differs from the LiveKit plugin kwarg.
_API_URL = "https://api.cartesia.ai/tts/bytes"
_API_VERSION = "2025-04-16"
_MODEL = "sonic-3.5"

# Slightly under natural pace. This is heard by someone who is barely awake, and
# the same value the voice previews are rendered at, so the voice a user picked
# in settings is the voice they recognise at 6 AM.
_SPEED = 0.92

# Long enough for any reminder worth speaking, short enough that a pathological
# message cannot turn one alarm into a minute of billed audio.
MAX_SPOKEN_CHARS = 200

_TIMEOUT_SECONDS = 20.0


def wake_line(message: str, local_time_label: str) -> str:
    """What Buddy says. The user's own words, behind the time.

    The time comes first because it is the one thing a person surfacing from
    sleep actually needs, and it is what makes the sentence land as a wake-up
    rather than as a notification read aloud.
    """
    body = str(message or "").strip()
    label = str(local_time_label or "").strip()
    if not body:
        body = "Time to get up."
    line = f"It's {label}. {body}" if label else body
    return line[:MAX_SPOKEN_CHARS]


def clip_tag(message: str, voice_slug: str) -> str:
    """A short opaque handle for "the audio these inputs produce".

    The contract deliberately keys on message and resolved voice only, so
    repeated app resumes and a snooze do not turn one reminder into repeated
    Cartesia renders.
    """
    digest = hashlib.sha1(f"{str(message or '')[:200]}|{voice_slug}".encode())
    return digest.hexdigest()[:12]


def cartesia_payload(line: str, cartesia_voice_id: str) -> dict[str, object]:
    """The exact provider request body, exposed for throwaway inspection."""
    return {
        "model_id": _MODEL,
        "transcript": line,
        "voice": {"mode": "id", "id": cartesia_voice_id},
        "speed": _SPEED,
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
    }


async def render(message: str, local_time_label: str, voice_slug: str, user_tier: str) -> bytes | None:
    """Synthesize the wake line as MP3 bytes, or None if it cannot be rendered.

    None is a normal outcome, not an exception: no API key configured, Cartesia
    down, a timeout. Every caller degrades to the bed tone looping uninterrupted,
    which still wakes the user. An alarm must never fail because a nicety did.
    """
    api_key = (settings.CARTESIA_API_KEY or "").strip()
    if not api_key:
        logger.warn("alarm_voice: CARTESIA_API_KEY unset; no spoken wake-up", {})
        return None

    voice, fallback_reason = resolve_voice(voice_slug, user_tier)
    line = wake_line(message, local_time_label)

    payload = cartesia_payload(line, voice.cartesia_voice_id)

    def _post() -> bytes | None:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, http2=False) as client:
            resp = client.post(
                _API_URL,
                headers={
                    "Cartesia-Version": _API_VERSION,
                    "X-API-Key": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code >= 400:
            # Log the body. A 4xx here means the request shape is wrong, and that
            # is the only signal that would ever fix it.
            logger.warn("alarm_voice: Cartesia rejected the render", {
                "status": resp.status_code,
                "body": resp.text[:300],
                "voice": voice.slug,
            })
            return None
        return resp.content

    try:
        audio = await asyncio.to_thread(_post)
    except Exception as exc:
        logger.warn("alarm_voice: render failed; alarm falls back to the tone", {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "voice": voice.slug,
        })
        return None

    if not audio:
        return None

    logger.info("alarm_voice: wake line rendered", {
        "voice": voice.slug,
        "voice_fallback_reason": fallback_reason,
        "chars": len(line),
        "bytes": len(audio),
    })
    return audio


def spoken_time_label(local_time_iso: str) -> str:
    """The wall clock as Buddy would say it: "6:30". Blank if unparseable.

    Built from the stored wall clock rather than the UTC instant, because that
    is the hour the user asked for and the hour they will see on the screen.
    """
    raw = str(local_time_iso or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return f"{(parsed.hour % 12) or 12}:{parsed.minute:02d}"


async def resolved_voice_slug(user_id: str) -> str:
    """The voice Buddy would actually speak in for this user, after entitlement.

    Feeds :func:`clip_tag`, so a lapsed subscriber's cached clip invalidates the
    moment their paid voice stops being the one they would hear. Never raises:
    any failure resolves to the default voice, and the cost of being wrong is one
    unnecessary re-render, not a silent alarm.
    """
    from .entitlement import get_user_effective_tier
    from .firebase import admin_firestore

    def _read_voice_id() -> str:
        doc = admin_firestore().collection("users").document(user_id).get()
        settings_map = (doc.to_dict() or {}).get("settings") or {}
        return str(settings_map.get("tts_voice_id") or "").strip()

    try:
        voice_id = await asyncio.to_thread(_read_voice_id)
        tier = await get_user_effective_tier(user_id)
    except Exception as exc:
        logger.warn("alarm_voice: voice resolve failed; assuming the default", {
            "user_id": user_id,
            "error": str(exc),
        })
        voice_id, tier = "", "free"

    voice, _ = resolve_voice(voice_id, tier)
    return voice.slug
