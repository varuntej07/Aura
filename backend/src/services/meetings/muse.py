"""Meta Muse batch STT; source channels and request-local speakers stay separate.

Contract: meta-models/meta-model-cookbook/06_muse_voice/01_voice_api_fundamentals.
No audio is written to disk. Missing/invalid timing evidence fails to the next
provider rather than fabricating timestamps or treating malformed output as silence.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
from datetime import UTC, datetime
from typing import Any

import httpx
import soundfile

from ...config.settings import settings
from . import transcript

_URL = "https://api.meta.ai/v1/asr/transcribe"
_MODEL = "muse-voice-transcribe-1.0"
_PARSER = "muse-meeting-v1"
_MAX_BYTES = 32 * 1024 * 1024
_MAX_ATTEMPTS = 2
_CHANNEL_DEADLINE_S = 180


class MuseError(transcript.TranscriptionError):
    provider = "meta"
    model = _MODEL
    parser_version = _PARSER


class MuseOutputError(MuseError, transcript.ProviderMalformedError):
    pass


def _milliseconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MuseOutputError("Muse timing is missing or nonnumeric")
    if not math.isfinite(value) or value < 0:
        raise MuseOutputError("Muse timing is invalid")
    return float(value)


def _encode_channels(audio: bytes) -> tuple[list[bytes], float]:
    try:
        with soundfile.SoundFile(io.BytesIO(audio)) as source:
            if source.channels != 2 or source.samplerate != 16_000:
                raise MuseError("Muse requires verified stereo 16 kHz source audio")
            duration_ms = source.frames * 1000 / source.samplerate
            if not 0 < duration_ms <= 600_000:
                raise MuseError("Source exceeds Muse batch duration limits")
            samples = source.read(dtype="int16", always_2d=True)
        channels = []
        for channel in (transcript.MIC_CHANNEL, transcript.LOOPBACK_CHANNEL):
            wav = io.BytesIO()
            soundfile.write(wav, samples[:, channel], 16_000, format="WAV", subtype="PCM_16")
            content = wav.getvalue()
            if len(content) > _MAX_BYTES:
                raise MuseError("Source exceeds Muse batch size limit")
            channels.append(content)
        return channels, duration_ms
    except (RuntimeError, ValueError, OSError) as exc:
        raise MuseError("Unable to encode Muse source audio") from exc


async def _request(client: httpx.AsyncClient, audio: bytes) -> dict[str, Any]:
    request = json.dumps({"model": _MODEL, "mode": "DIARIZATION", "audioEncoding": "WAV"})
    for attempt in range(_MAX_ATTEMPTS):
        delay = 1.0 + random.uniform(0, 0.5)
        try:
            response = await client.post(
                _URL,
                files={
                    "request": (None, request, "application/json"),
                    "audio": ("channel.wav", audio, "audio/wav"),
                },
            )
        except httpx.HTTPError:
            pass
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise MuseOutputError("Muse returned invalid JSON") from exc
                if not isinstance(body, dict):
                    raise MuseOutputError("Muse returned a non-object response")
                return body
            # Do not propagate response bodies: they may contain audio-derived text.
            if response.status_code != 429 and response.status_code < 500:
                raise MuseError(f"Muse rejected request: HTTP {response.status_code}")
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    # A date-form Retry-After is not permission to retry early.
                    raise MuseError("Muse requested deferred retry") from None
                if not math.isfinite(wait) or wait > 10:
                    raise MuseError("Muse requested deferred retry")
                delay = max(delay, wait)
        if attempt + 1 < _MAX_ATTEMPTS:
            await asyncio.sleep(delay)
    raise MuseError("Muse transient request failures exhausted")


def _parse_channel(
    body: dict[str, Any], *, channel: int, duration_ms: float, segment_seq: int,
) -> list[transcript.Utterance]:
    if body.get("error") or not isinstance(body.get("transcript"), str):
        raise MuseOutputError("Muse response lacks a valid transcript")
    reported = _milliseconds(body.get("audioDurationMs"))
    if abs(reported - duration_ms) > max(2000, duration_ms * 0.01):
        raise MuseOutputError("Muse response duration does not match source")
    turns = body.get("turns")
    if not isinstance(turns, list):
        raise MuseOutputError("Muse diarization response lacks turns")
    result = []
    previous = -1.0
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("transcript"), str):
            raise MuseOutputError("Muse returned an invalid turn")
        text = turn["transcript"].strip()
        if not text:
            continue
        start = _milliseconds(turn.get("startMs"))
        end = _milliseconds(turn.get("endMs"))
        if start < previous or end <= start or end > duration_ms + 100:
            raise MuseOutputError("Muse turn timing is invalid or incomplete")
        previous = start
        speaker = turn.get("speaker")
        if not isinstance(speaker, str) or len(speaker) != 1 or not "A" <= speaker <= "Z":
            raise MuseOutputError("Muse diarization lacks a valid speaker label")
        result.append(transcript.Utterance(
            channel=channel, start_s=start / 1000, end_s=end / 1000, text=text,
            speaker=None if channel == transcript.MIC_CHANNEL else
            f"Speaker {speaker} (segment {segment_seq + 1})",
        ))
    # A partial turns array must never silently discard the rest of the text.
    if " ".join(body["transcript"].split()) != " ".join(
        " ".join(turn.text for turn in result).split()
    ):
        raise MuseOutputError("Muse transcript and turns disagree")
    return result


async def transcribe_segment(audio: bytes, *, segment_seq: int) -> transcript.SegmentTranscript:
    if not settings.MODEL_API_KEY.strip():
        raise MuseError("MODEL_API_KEY is not configured")
    channels, duration_ms = await asyncio.to_thread(_encode_channels, audio)
    result = transcript.SegmentTranscript(
        provider="meta", model=_MODEL, parser_version=_PARSER,
        parameters={"mode": "DIARIZATION", "audioEncoding": "WAV", "channels": "separate"},
    )
    responses = []
    # Sequential channels bound concurrency and preserve completed channel work
    # across the request-local retries. Durable recovery stays segment-scoped.
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {settings.MODEL_API_KEY.strip()}"},
        timeout=httpx.Timeout(120, connect=10), follow_redirects=False,
    ) as client:
        for channel, wav in enumerate(channels):
            try:
                async with asyncio.timeout(_CHANNEL_DEADLINE_S):
                    body = await _request(client, wav)
            except TimeoutError as exc:
                raise MuseError("Muse channel deadline exceeded") from exc
            turns = _parse_channel(
                body, channel=channel, duration_ms=duration_ms, segment_seq=segment_seq,
            )
            result.utterances.extend(turns)
            count = sum(len(turn.text.split()) for turn in turns)
            if channel == transcript.MIC_CHANNEL:
                result.mic_words = count
            else:
                result.loopback_words = count
            responses.append(body)
    result.raw_response = {"channels": responses}
    result.request_id = ",".join(str(row.get("sessionId", "")) for row in responses)
    result.responded_at = datetime.now(UTC).isoformat()
    return result
