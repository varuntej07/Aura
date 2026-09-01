"""Shared FLAC validation: STREAMINFO parse, full-decode truncation check, and
the max(floor, 1%) duration-tolerance rule.

Promoted from services/meetings/evidence.py so dictation and meetings enforce
ONE strictness level for "the audio must match its declared duration" (the
dictation path previously accepted truncated FLACs the meetings path
rejected). Policy stays per-surface and arrives as parameters: channel/rate
requirements, the maximum-duration ceiling, and the tolerance floor (meetings
uses a 2s floor over 5-minute segments; dictation 250ms over <=2-minute
clips). ``services/meetings/evidence.py`` wraps these and converts the error
type, so its callers are unchanged.
"""

from __future__ import annotations

import io
from dataclasses import dataclass


class AudioValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FlacStreamInfo:
    sample_rate_hz: int
    channel_count: int
    bits_per_sample: int
    total_samples: int
    subtype: str = ""

    @property
    def duration_ms(self) -> int:
        if self.sample_rate_hz <= 0:
            return 0
        return round(self.total_samples * 1000 / self.sample_rate_hz)


def duration_tolerance_ms(expected_ms: int, *, floor_ms: int) -> int:
    """The architecture's max(floor, 1 percent) duration tolerance."""
    return max(floor_ms, round(expected_ms * 0.01))


def parse_flac_streaminfo(data: bytes) -> FlacStreamInfo:
    """Read the mandatory FLAC STREAMINFO block without decoding audio."""
    if len(data) < 42 or data[:4] != b"fLaC":
        raise AudioValidationError("invalid_flac", "Missing FLAC marker.")
    block_type = data[4] & 0x7F
    block_length = int.from_bytes(data[5:8], "big")
    if block_type != 0 or block_length != 34 or len(data) < 8 + block_length:
        raise AudioValidationError("invalid_flac", "Missing FLAC STREAMINFO.")
    packed = int.from_bytes(data[18:26], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    bits_per_sample = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & ((1 << 36) - 1)
    if sample_rate <= 0 or total_samples <= 0:
        raise AudioValidationError("invalid_flac", "Invalid FLAC duration.")
    return FlacStreamInfo(sample_rate, channels, bits_per_sample, total_samples)


def decode_flac_info(data: bytes, *, max_duration_ms: int | None = None) -> FlacStreamInfo:
    """Decode every FLAC frame so truncated/corrupt payloads cannot pass.

    ``max_duration_ms`` (when given) rejects a declared duration beyond the
    surface's ceiling BEFORE paying for the full decode.
    """
    import soundfile  # type: ignore

    try:
        header = parse_flac_streaminfo(data)
        if max_duration_ms is not None:
            max_frames = round(header.sample_rate_hz * max_duration_ms / 1_000)
            if header.total_samples > max_frames:
                raise AudioValidationError("invalid_flac", "FLAC duration is out of range.")
        with soundfile.SoundFile(io.BytesIO(data)) as audio:
            sample_rate = int(audio.samplerate)
            channels = int(audio.channels)
            expected_frames = int(audio.frames)
            decoded = audio.read(dtype="int16", always_2d=True)
            total_frames = len(decoded)
            if total_frames != expected_frames:
                raise AudioValidationError("invalid_flac", "FLAC frame count is truncated.")
            return FlacStreamInfo(
                sample_rate_hz=sample_rate,
                channel_count=channels,
                bits_per_sample=16,
                total_samples=total_frames,
                subtype=str(audio.subtype or ""),
            )
    except AudioValidationError:
        raise
    except Exception as exc:
        raise AudioValidationError("invalid_flac", "FLAC decode failed.") from exc
