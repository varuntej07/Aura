"""OpenAI Whisper fallback for speech-bearing segments Deepgram returns empty.

The desktop's channel contract remains authoritative: channel 0 is the device
owner's microphone and channel 1 is system loopback. Each channel is converted
to a mono WAV and transcribed independently so fallback never invents speaker
attribution.
"""

from __future__ import annotations

import io
from typing import Any

import soundfile  # type: ignore
from openai import AsyncOpenAI

from ...config.settings import settings
from . import deepgram

_MODEL = "whisper-1"


async def transcribe_segment(flac_bytes: bytes) -> deepgram.SegmentTranscript:
    api_key = settings.OPENAI_API_KEY.strip()
    if not api_key:
        raise deepgram.DeepgramError("OPENAI_API_KEY is not configured")

    try:
        with soundfile.SoundFile(io.BytesIO(flac_bytes)) as audio:
            sample_rate = int(audio.samplerate)
            samples = audio.read(dtype="int16", always_2d=True)
        if samples.shape[1] != 2:
            raise deepgram.DeepgramError("OpenAI fallback requires two-channel audio")

        client = AsyncOpenAI(api_key=api_key, timeout=120.0)
        result = deepgram.SegmentTranscript(
            provider="openai",
            model=_MODEL,
            parameters={"response_format": "verbose_json"},
        )
        responses: list[dict[str, Any]] = []
        for channel in (deepgram.MIC_CHANNEL, deepgram.LOOPBACK_CHANNEL):
            wav = io.BytesIO()
            soundfile.write(
                wav,
                samples[:, channel],
                sample_rate,
                format="WAV",
                subtype="PCM_16",
            )
            response = await client.audio.transcriptions.create(
                model=_MODEL,
                file=(f"channel-{channel}.wav", wav.getvalue(), "audio/wav"),
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
            body = response.model_dump()
            responses.append(body)
            channel_words = 0
            for segment in body.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                text = str(segment.get("text") or "").strip()
                if not text:
                    continue
                start = float(segment.get("start", 0.0))
                end = float(segment.get("end", start))
                result.utterances.append(
                    deepgram.Utterance(
                        channel=channel,
                        start_s=start,
                        end_s=end,
                        text=text,
                    )
                )
                channel_words += len(text.split())
            if channel == deepgram.MIC_CHANNEL:
                result.mic_words = channel_words
            else:
                result.loopback_words = channel_words
            language = body.get("language")
            if result.language is None and language:
                result.language = str(language)
        result.raw_response = {"channels": responses}
        return result
    except deepgram.DeepgramError:
        raise
    except Exception as exc:
        raise deepgram.DeepgramError("OpenAI meeting transcription fallback failed") from exc
