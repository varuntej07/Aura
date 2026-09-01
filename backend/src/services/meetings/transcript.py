"""Provider-neutral transcript DTOs and error hierarchy for meeting STT.

Both provider legs (``deepgram``, ``openai_stt``) import from here, so the
"transcription failed" vocabulary is no longer owned by one provider and a
third leg needs no absurd cross-import. ``deepgram.py`` keeps back-compat
subclasses/aliases (``DeepgramError`` et al.) for existing catch sites.

The desktop's channel contract is authoritative for every provider: channel 0
is the device owner's microphone and channel 1 is system loopback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MIC_CHANNEL = 0
LOOPBACK_CHANNEL = 1


@dataclass
class Utterance:
    channel: int
    start_s: float
    end_s: float
    text: str
    speaker: str | None = None


@dataclass
class SegmentTranscript:
    """One segment's normalized transcript plus provider evidence.

    The provider identity defaults describe the primary leg (Deepgram); every
    other provider must override them at construction, the way
    ``openai_stt.transcribe_segment`` does.
    """

    utterances: list[Utterance] = field(default_factory=list)
    mic_words: int = 0
    loopback_words: int = 0
    language: str | None = None
    language_confidence: float | None = None
    confidence: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "deepgram"
    model: str = "nova-3"
    parameters: dict[str, str] = field(default_factory=dict)
    request_id: str = ""
    responded_at: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    parser_version: str = "deepgram-meeting-v2"
    forced_english: bool = False


class TranscriptionError(Exception):
    """A transcription request that failed after retries. The worker treats
    this as an infrastructure failure (retryable), not a bad meeting.

    ``provider``/``model``/``parser_version`` identify the attempted leg so
    persisted failure evidence never misattributes one provider's failure to
    another; providers override them on subclasses or instances."""

    provider = ""
    model = ""
    parser_version = ""


class TranscriptionRejectedError(TranscriptionError):
    """The provider rejected the request outright (terminal 4xx): resending
    the same bytes can never succeed. TERMINAL - a retry loop here just
    resends the identical bad audio forever."""

    code = "audio_rejected"  # F.FAIL_AUDIO_REJECTED


class ProviderOutputError(TranscriptionError):
    code = "provider_output_invalid"


class ProviderMalformedError(ProviderOutputError):
    code = "provider_output_malformed"


class ProviderEmptyError(ProviderOutputError):
    # Currently never raised: the empty-with-speech case was deliberately
    # downgraded to a warn in synthesis (a bare-RMS VAD counts music and hold
    # tones as speech, and meeting-quality-v1 already scores the condition
    # meeting-wide). Kept for a future strict mode rather than deleted.
    code = "provider_output_empty"
