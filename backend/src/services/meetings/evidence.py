"""Pure Meeting Recording V2 evidence validation and canonicalization.

This module deliberately has no Firestore, GCS, or provider dependencies.  The
HTTP handler, completion transaction, worker, and operational inspection tools
all use the same parsers and policy constants.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from . import fields as F

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

AUDIO_METRIC_FIELDS = (
    "mic_rms_dbfs",
    "system_rms_dbfs",
    "mic_clipping_ratio",
    "system_clipping_ratio",
    "mic_zero_ratio",
    "system_zero_ratio",
    "mic_vad_speech_ms",
    "system_vad_speech_ms",
    "mic_device_id_hash",
    "system_device_id_hash",
)

SEGMENT_IDENTITY_FIELDS = (
    "seq",
    "start_ms",
    "duration_ms",
    "incomplete",
    "content_sha256",
    "byte_length",
    "channel_count",
    "sample_rate_hz",
)


class EvidenceValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FlacStreamInfo:
    sample_rate_hz: int
    channel_count: int
    bits_per_sample: int
    total_samples: int

    @property
    def duration_ms(self) -> int:
        if self.sample_rate_hz <= 0:
            return 0
        return round(self.total_samples * 1000 / self.sample_rate_hz)


def require_identity(value: str, label: str) -> str:
    if not _IDENTITY_RE.fullmatch(value):
        raise EvidenceValidationError("invalid_identity", f"Invalid {label}.")
    return value


def require_sha256(value: str, label: str = "SHA-256") -> str:
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise EvidenceValidationError("invalid_digest", f"Invalid {label}.")
    return normalized


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    # serde_json's default map representation sorts keys.  This matches the
    # desktop's compact `serde_json::to_vec` manifest serialization exactly.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_manifest(
    segments: list[dict[str, Any]],
    *,
    total_duration_ms: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": F.MANIFEST_SCHEMA_VERSION,
        "segments": [
            {field: segment[field] for field in SEGMENT_IDENTITY_FIELDS} for segment in segments
        ],
        "total_duration_ms": total_duration_ms,
        "reason": reason,
    }


def manifest_sha256(
    segments: list[dict[str, Any]],
    *,
    total_duration_ms: int,
    reason: str,
) -> str:
    return sha256_hex(
        canonical_json_bytes(
            canonical_manifest(
                segments,
                total_duration_ms=total_duration_ms,
                reason=reason,
            )
        )
    )


def parse_completion_segment(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceValidationError("invalid_manifest", "Segment must be an object.")
    try:
        segment = {
            "seq": int(raw["seq"]),
            "start_ms": int(raw["start_ms"]),
            "duration_ms": int(raw["duration_ms"]),
            "incomplete": raw["incomplete"],
            "content_sha256": require_sha256(str(raw["content_sha256"])),
            "byte_length": int(raw["byte_length"]),
            "channel_count": int(raw["channel_count"]),
            "sample_rate_hz": int(raw["sample_rate_hz"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceValidationError("invalid_manifest", "Invalid segment identity.") from exc
    if not isinstance(segment["incomplete"], bool):
        raise EvidenceValidationError("invalid_manifest", "incomplete must be boolean.")
    if not 0 <= segment["seq"] < F.MAX_SEGMENTS_PER_MEETING:
        raise EvidenceValidationError("invalid_manifest", "Segment sequence out of range.")
    if not 0 <= segment["start_ms"] <= F.MAX_SEGMENT_START_MS:
        raise EvidenceValidationError("invalid_manifest", "Segment start out of range.")
    if not 0 < segment["duration_ms"] <= F.MAX_SEGMENT_DURATION_MS:
        raise EvidenceValidationError("invalid_manifest", "Segment duration out of range.")
    if not 0 < segment["byte_length"] <= F.MAX_SEGMENT_BYTES:
        raise EvidenceValidationError("invalid_manifest", "Segment byte length out of range.")
    if segment["channel_count"] != 2 or segment["sample_rate_hz"] != 16_000:
        raise EvidenceValidationError(
            "invalid_audio_format",
            "Meeting segments must be two-channel 16 kHz FLAC.",
        )
    metrics = raw.get("audio_metrics")
    if not isinstance(metrics, dict):
        raise EvidenceValidationError("invalid_audio_metrics", "Missing audio_metrics.")
    parsed_metrics: dict[str, Any] = {}
    for name in AUDIO_METRIC_FIELDS:
        if name.endswith("_device_id_hash"):
            parsed_metrics[name] = require_sha256(str(metrics.get(name, "")), name)
            continue
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceValidationError("invalid_audio_metrics", f"Invalid {name}.")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise EvidenceValidationError("invalid_audio_metrics", f"Invalid {name}.")
        parsed_metrics[name] = int(numeric) if name.endswith("_ms") else numeric
    for name in (
        "mic_clipping_ratio",
        "system_clipping_ratio",
        "mic_zero_ratio",
        "system_zero_ratio",
    ):
        if not 0.0 <= parsed_metrics[name] <= 1.0:
            raise EvidenceValidationError("invalid_audio_metrics", f"Invalid {name}.")
    for name in ("mic_vad_speech_ms", "system_vad_speech_ms"):
        if not 0 <= parsed_metrics[name] <= segment["duration_ms"]:
            raise EvidenceValidationError("invalid_audio_metrics", f"Invalid {name}.")
    segment["audio_metrics"] = parsed_metrics
    return segment


def parse_flac_streaminfo(data: bytes) -> FlacStreamInfo:
    """Read the mandatory FLAC STREAMINFO block without decoding audio."""
    if len(data) < 42 or data[:4] != b"fLaC":
        raise EvidenceValidationError("invalid_flac", "Missing FLAC marker.")
    block_type = data[4] & 0x7F
    block_length = int.from_bytes(data[5:8], "big")
    if block_type != 0 or block_length != 34 or len(data) < 8 + block_length:
        raise EvidenceValidationError("invalid_flac", "Missing FLAC STREAMINFO.")
    packed = int.from_bytes(data[18:26], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    bits_per_sample = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & ((1 << 36) - 1)
    if sample_rate <= 0 or total_samples <= 0:
        raise EvidenceValidationError("invalid_flac", "Invalid FLAC duration.")
    return FlacStreamInfo(sample_rate, channels, bits_per_sample, total_samples)


def decode_flac_info(data: bytes) -> FlacStreamInfo:
    """Decode every FLAC frame so truncated/corrupt payloads cannot reach STT."""
    import soundfile  # type: ignore

    try:
        header = parse_flac_streaminfo(data)
        max_frames = round(
            header.sample_rate_hz
            * (F.MAX_SEGMENT_DURATION_MS + duration_tolerance_ms(F.MAX_SEGMENT_DURATION_MS))
            / 1_000
        )
        if header.total_samples > max_frames:
            raise EvidenceValidationError("invalid_flac", "FLAC duration is out of range.")
        with soundfile.SoundFile(io.BytesIO(data)) as audio:
            sample_rate = int(audio.samplerate)
            channels = int(audio.channels)
            expected_frames = int(audio.frames)
            decoded = audio.read(dtype="int16", always_2d=True)
            total_frames = len(decoded)
            if total_frames != expected_frames:
                raise EvidenceValidationError("invalid_flac", "FLAC frame count is truncated.")
            return FlacStreamInfo(
                sample_rate_hz=sample_rate,
                channel_count=channels,
                bits_per_sample=16,
                total_samples=total_frames,
            )
    except EvidenceValidationError:
        raise
    except Exception as exc:
        raise EvidenceValidationError("invalid_flac", "FLAC decode failed.") from exc


def duration_tolerance_ms(expected_ms: int) -> int:
    return max(F.DURATION_TOLERANCE_FLOOR_MS, round(expected_ms * 0.01))
