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
from datetime import UTC, datetime
from typing import Any

import httpx

from ...config.settings import settings
from ...lib.logger import logger
from . import transcript

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

# DTOs and channel constants live in transcript.py; imported here unchanged so
# existing `deepgram.SegmentTranscript` / channel-constant imports keep working.
from .transcript import (  # noqa: E402  (re-export)
    LOOPBACK_CHANNEL,
    MIC_CHANNEL,
    SegmentTranscript,
    Utterance,
)


class DeepgramError(transcript.TranscriptionError):
    """A Deepgram request that failed after retries (retryable infra)."""

    provider = "deepgram"
    model = "nova-3"
    parser_version = "deepgram-meeting-v2"


class DeepgramRejectedError(DeepgramError, transcript.TranscriptionRejectedError):
    """Deepgram rejected the request outright (terminal 4xx): resending the
    same bytes can never succeed. The worker treats this as TERMINAL - a
    retry loop here just resends the identical bad audio forever."""


class ProviderOutputError(DeepgramError, transcript.ProviderOutputError):
    pass


class ProviderMalformedError(ProviderOutputError, transcript.ProviderMalformedError):
    pass


class ProviderEmptyError(ProviderOutputError, transcript.ProviderEmptyError):
    # Never raised - see transcript.ProviderEmptyError. Kept, not deleted.
    pass


async def transcribe_segment(
    flac_bytes: bytes,
    *,
    force_english: bool = False,
) -> SegmentTranscript:
    """Transcribe one 2-channel FLAC segment. Retries transient failures
    (429/422/5xx/network) twice with a short backoff, then raises
    DeepgramError. Any other 4xx raises DeepgramRejectedError immediately -
    resending the same bytes cannot fix a rejected request."""
    if not settings.DEEPGRAM_API_KEY:
        raise DeepgramError("DEEPGRAM_API_KEY is not configured")

    headers = {
        "Authorization": f"Token {settings.DEEPGRAM_API_KEY.strip()}",
        "Content-Type": "audio/flac",
    }
    params = dict(_PARAMS)
    if force_english:
        params["language"] = "en"
        params["detect_language"] = "false"

    last_error = ""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.post(
                    _LISTEN_URL,
                    params=params,
                    headers=headers,
                    content=flac_bytes,
                )
            except httpx.HTTPError as exc:
                last_error = f"network: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise ProviderMalformedError("Deepgram returned invalid JSON.") from exc
                    return _parse(
                        body,
                        request_id=response.headers.get("dg-request-id", ""),
                        parameters=params,
                        force_english=force_english,
                    )
                # Deepgram's error contract (developers.deepgram.com/reference/errors):
                # 400/401/402/403/404 can never succeed on a resend; 422 means the
                # upload was interrupted mid-transfer and MAY succeed on retry, so
                # it stays transient alongside 429 and 5xx.
                if response.status_code not in (429, 422) and response.status_code < 500:
                    raise DeepgramRejectedError(
                        f"deepgram rejected request: {response.status_code}"
                    )
                last_error = f"status {response.status_code}"

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(2.0 * attempt)

    raise DeepgramError(f"deepgram failed after {_MAX_ATTEMPTS} attempts: {last_error}")


def _parse(
    body: dict[str, Any],
    *,
    request_id: str = "",
    parameters: dict[str, str] | None = None,
    force_english: bool = False,
) -> SegmentTranscript:
    """Parse provider evidence strictly; malformed data is never silence."""
    if not isinstance(body, dict) or not isinstance(body.get("results"), dict):
        raise ProviderMalformedError("Deepgram response is missing results.")
    results = body["results"]
    utterance_rows = results.get("utterances", [])
    channel_rows = results.get("channels", [])
    if not isinstance(utterance_rows, list) or not isinstance(channel_rows, list):
        raise ProviderMalformedError("Deepgram response has malformed channels.")
    if not channel_rows and not utterance_rows and not str(results.get("transcript") or "").strip():
        raise ProviderMalformedError("Deepgram response has no recognition payload.")
    out = SegmentTranscript(
        parameters=dict(parameters or _PARAMS),
        request_id=request_id,
        responded_at=datetime.now(UTC).isoformat(),
        raw_response=body,
        forced_english=force_english,
    )

    previous_start = -1.0
    for utt in utterance_rows:
        if not isinstance(utt, dict):
            raise ProviderMalformedError("Deepgram utterance is malformed.")
        text = str(utt.get("transcript") or "").strip()
        if not text:
            continue
        try:
            start = float(utt["start"])
            end = float(utt["end"])
            channel = int(utt.get("channel", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderMalformedError("Deepgram utterance timing is malformed.") from exc
        if start < 0 or end < start or start < previous_start:
            raise ProviderMalformedError("Deepgram utterance timing is non-monotonic.")
        previous_start = start
        out.utterances.append(
            Utterance(
                channel=channel,
                start_s=start,
                end_s=end,
                text=text,
            )
        )

    channel_fallbacks: list[Utterance] = []
    confidences: list[float] = []
    for index, channel in enumerate(channel_rows[:2]):
        if not isinstance(channel, dict):
            raise ProviderMalformedError("Deepgram channel is malformed.")
        alternatives = channel.get("alternatives") or []
        if not isinstance(alternatives, list) or not alternatives:
            raise ProviderMalformedError("Deepgram channel has no alternatives.")
        alternative = alternatives[0]
        if not isinstance(alternative, dict):
            raise ProviderMalformedError("Deepgram alternative is malformed.")
        fallback_text = str(alternative.get("transcript") or "").strip()
        word_rows = alternative.get("words") or []
        if not isinstance(word_rows, list):
            raise ProviderMalformedError("Deepgram words are malformed.")
        normalized_words: list[dict[str, Any]] = []
        previous_word_start = -1.0
        for word in word_rows:
            if not isinstance(word, dict):
                raise ProviderMalformedError("Deepgram word is malformed.")
            try:
                start = float(word["start"])
                end = float(word["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderMalformedError("Deepgram word timing is malformed.") from exc
            if start < 0 or end < start or start < previous_word_start:
                raise ProviderMalformedError("Deepgram word timing is non-monotonic.")
            previous_word_start = start
            confidence = word.get("confidence")
            if isinstance(confidence, (int, float)):
                confidences.append(float(confidence))
            normalized_words.append(
                {
                    "word": str(word.get("punctuated_word") or word.get("word") or ""),
                    "start_s": start,
                    "end_s": end,
                    "confidence": float(confidence)
                    if isinstance(confidence, (int, float))
                    else None,
                    "channel": index,
                }
            )
        out.words.extend(normalized_words)
        words = len(normalized_words) or len(fallback_text.split())
        if index == MIC_CHANNEL:
            out.mic_words = words
        else:
            out.loopback_words = words
        if out.language is None:
            detected = channel.get("detected_language")
            if detected:
                out.language = str(detected)
                raw_language_confidence = channel.get("language_confidence")
                if isinstance(raw_language_confidence, (int, float)):
                    out.language_confidence = float(raw_language_confidence)

        # Deepgram can occasionally return a channel transcript without the
        # requested utterance array. Keep the channel provenance rather than
        # degrading a meeting with recognized words to an empty transcript.
        if fallback_text:
            channel_fallbacks.append(
                Utterance(
                    channel=index,
                    start_s=0.0,
                    end_s=0.0,
                    text=fallback_text,
                )
            )

    if not out.utterances:
        if channel_fallbacks:
            out.utterances.extend(channel_fallbacks)
        else:
            # This is not part of the current multichannel Deepgram response,
            # but keeps a future mono or provider fallback transcript usable
            # without inventing speaker attribution.
            fallback_text = str(results.get("transcript") or body.get("transcript") or "").strip()
            if fallback_text:
                out.utterances.append(
                    Utterance(
                        channel=-1,
                        start_s=0.0,
                        end_s=0.0,
                        text=fallback_text,
                        speaker="",
                    )
                )

    if not out.utterances and (out.mic_words or out.loopback_words):
        logger.warn(
            "meetings.deepgram: words without utterances",
            {
                "mic_words": out.mic_words,
                "loopback_words": out.loopback_words,
            },
        )
    out.confidence = sum(confidences) / len(confidences) if confidences else None
    return out
