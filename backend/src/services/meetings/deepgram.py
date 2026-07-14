"""Deepgram prerecorded STT for meeting segments.

Multichannel is the load-bearing choice: the desktop encodes channel 0 = the
user's mic and channel 1 = system loopback (everyone else), so per-channel
transcription gives perfect "You" vs "Others" attribution with no diarization
pass at all. Utterances carry their channel, and the synthesis step merges
them across segments by absolute time.

Uses the same DEEPGRAM_API_KEY the voice worker already mounts (deploy.sh
sets it from Secret Manager); no new secret. httpx is already a runtime dep
(services/billing.py).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...config.settings import settings
from ...lib.logger import logger

_LISTEN_URL = "https://api.deepgram.com/v1/listen"
_PARAMS = {
    "model": "nova-3",
    "multichannel": "true",
    "smart_format": "true",
    "punctuate": "true",
    "utterances": "true",
    "detect_language": "true",
}
_TIMEOUT_S = 120.0
_MAX_ATTEMPTS = 3

MIC_CHANNEL = 0
LOOPBACK_CHANNEL = 1


@dataclass
class Utterance:
    channel: int
    start_s: float
    end_s: float
    text: str


@dataclass
class SegmentTranscript:
    utterances: list[Utterance] = field(default_factory=list)
    mic_words: int = 0
    loopback_words: int = 0
    language: str | None = None


class DeepgramError(Exception):
    """A transcription request that failed after retries. The worker treats
    this as an infrastructure failure (retryable), not a bad meeting."""


class DeepgramRejectedError(DeepgramError):
    """Deepgram rejected the request outright (non-429 4xx): resending the
    same bytes can never succeed. The worker treats this as TERMINAL - a
    retry loop here just resends the identical bad audio forever."""


async def transcribe_segment(flac_bytes: bytes) -> SegmentTranscript:
    """Transcribe one 2-channel FLAC segment. Retries transient failures
    (429/5xx/network) twice with a short backoff, then raises DeepgramError.
    A 4xx other than 429 raises immediately - resending the same bytes cannot
    fix a rejected request."""
    if not settings.DEEPGRAM_API_KEY:
        raise DeepgramError("DEEPGRAM_API_KEY is not configured")

    headers = {
        "Authorization": f"Token {settings.DEEPGRAM_API_KEY.strip()}",
        "Content-Type": "audio/flac",
    }

    last_error = ""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.post(
                    _LISTEN_URL, params=_PARAMS, headers=headers, content=flac_bytes,
                )
            except httpx.HTTPError as exc:
                last_error = f"network: {exc}"
            else:
                if response.status_code == 200:
                    return _parse(response.json())
                if response.status_code not in (429,) and response.status_code < 500:
                    raise DeepgramRejectedError(
                        f"deepgram rejected request: {response.status_code} "
                        f"{response.text[:200]}"
                    )
                last_error = f"status {response.status_code}"

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(2.0 * attempt)

    raise DeepgramError(f"deepgram failed after {_MAX_ATTEMPTS} attempts: {last_error}")


def _parse(body: dict[str, Any]) -> SegmentTranscript:
    """Defensive parse of the prerecorded response. Anything malformed
    degrades to an empty transcript for the segment rather than raising -
    one silent segment must not fail the whole meeting."""
    results = body.get("results") or {}
    out = SegmentTranscript()

    for utt in results.get("utterances") or []:
        text = str(utt.get("transcript") or "").strip()
        if not text:
            continue
        out.utterances.append(Utterance(
            channel=int(utt.get("channel") or 0),
            start_s=float(utt.get("start") or 0.0),
            end_s=float(utt.get("end") or 0.0),
            text=text,
        ))

    channels = results.get("channels") or []
    for index, channel in enumerate(channels[:2]):
        alternatives = channel.get("alternatives") or [{}]
        words = len(alternatives[0].get("words") or [])
        if index == MIC_CHANNEL:
            out.mic_words = words
        else:
            out.loopback_words = words
        if out.language is None:
            detected = channel.get("detected_language")
            if detected:
                out.language = str(detected)

    if not out.utterances and (out.mic_words or out.loopback_words):
        logger.warn("meetings.deepgram: words without utterances", {
            "mic_words": out.mic_words, "loopback_words": out.loopback_words,
        })
    return out
