"""Deterministic meeting-quality-v1 policy.

The policy consumes only verified segment evidence and provider-derived word
timings. It never asks an LLM whether a transcript is plausible.
"""

from __future__ import annotations

from typing import Any

from . import fields as F
from .evidence import duration_tolerance_ms

EMPTY_WITH_SPEECH_MS = 15_000
ONE_SIDED_SPEECH_MS = 30_000
ONE_SIDED_MIN_WORDS = 10
MEETING_SPEECH_MS = 60_000
MEETING_MIN_WORDS = 50
TIMING_GATE_SPEECH_MS = 5 * 60_000
MIN_TIMING_COVERAGE = 0.20
LONG_MEETING_MS = 45 * 60_000
MAX_LONG_MEETING_TURNS = 2
SILENCE_MAX_VAD_MS = 2_000
SILENCE_MAX_RMS_DBFS = -55.0
SILENCE_MIN_ZERO_RATIO = 0.98
MAX_CLIPPING_RATIO = 0.20
MAX_UNACCOUNTED_GAP_MS = 2_000


def policy_values() -> dict[str, Any]:
    return {
        "policy_version": F.QUALITY_POLICY_V1,
        "empty_with_speech_ms": EMPTY_WITH_SPEECH_MS,
        "one_sided_speech_ms": ONE_SIDED_SPEECH_MS,
        "one_sided_min_words": ONE_SIDED_MIN_WORDS,
        "meeting_speech_ms": MEETING_SPEECH_MS,
        "meeting_min_words": MEETING_MIN_WORDS,
        "timing_gate_speech_ms": TIMING_GATE_SPEECH_MS,
        "min_timing_coverage": MIN_TIMING_COVERAGE,
        "long_meeting_ms": LONG_MEETING_MS,
        "max_long_meeting_turns": MAX_LONG_MEETING_TURNS,
        "silence_max_vad_ms": SILENCE_MAX_VAD_MS,
        "silence_max_rms_dbfs": SILENCE_MAX_RMS_DBFS,
        "silence_min_zero_ratio": SILENCE_MIN_ZERO_RATIO,
        "max_clipping_ratio": MAX_CLIPPING_RATIO,
        "max_unaccounted_gap_ms": MAX_UNACCOUNTED_GAP_MS,
    }


def _timing_coverage_ms(words: list[dict[str, Any]]) -> int:
    intervals = sorted(
        (
            max(0, round(float(word.get("start_s", 0)) * 1000)),
            max(0, round(float(word.get("end_s", 0)) * 1000)),
        )
        for word in words
        if float(word.get("end_s", 0)) >= float(word.get("start_s", 0))
    )
    covered = 0
    end = 0
    for start, stop in intervals:
        if stop <= start:
            continue
        if start >= end:
            covered += stop - start
        elif stop > end:
            covered += stop - end
        end = max(end, stop)
    return covered


def evaluate(
    *,
    segments: list[dict[str, Any]],
    transcripts: list[dict[str, Any]],
    total_duration_ms: int,
    turn_count: int,
    forced_english_attempted: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    mic_vad = sum(int(row["audio_metrics"]["mic_vad_speech_ms"]) for row in segments)
    system_vad = sum(int(row["audio_metrics"]["system_vad_speech_ms"]) for row in segments)
    vad_total = mic_vad + system_vad
    mic_words = sum(int(row.get("mic_words", 0)) for row in transcripts)
    system_words = sum(int(row.get("loopback_words", 0)) for row in transcripts)
    words = mic_words + system_words
    word_rows = [
        word
        for transcript in transcripts
        for word in transcript.get("words", [])
        if isinstance(word, dict)
    ]
    timing_coverage_ms = _timing_coverage_ms(word_rows)

    expected_seq = list(range(len(segments)))
    if [int(row.get("seq", -1)) for row in segments] != expected_seq:
        failures.append("capture_sequence_integrity")
    if any(row.get("integrity_status") != "verified" for row in segments):
        failures.append("capture_receipt_integrity")
    if any(
        int(row.get("channel_count", 0)) != 2 or int(row.get("sample_rate_hz", 0)) != 16_000
        for row in segments
    ):
        failures.append("flac_format")
    decoded_total = sum(int(row.get("decoded_duration_ms", 0)) for row in segments)
    if abs(decoded_total - total_duration_ms) > duration_tolerance_ms(total_duration_ms):
        failures.append("flac_duration")
    if any(row.get("incomplete") is True for row in segments):
        failures.append("unaccounted_gap")
    if any(
        float(row["audio_metrics"]["mic_clipping_ratio"]) > MAX_CLIPPING_RATIO
        or float(row["audio_metrics"]["system_clipping_ratio"]) > MAX_CLIPPING_RATIO
        for row in segments
    ):
        failures.append("excessive_clipping")
    if words == 0 and vad_total >= EMPTY_WITH_SPEECH_MS:
        failures.append("empty_with_speech")
    if mic_vad >= ONE_SIDED_SPEECH_MS and mic_words < ONE_SIDED_MIN_WORDS:
        failures.append("mic_one_sided_recognition")
    if system_vad >= ONE_SIDED_SPEECH_MS and system_words < ONE_SIDED_MIN_WORDS:
        failures.append("system_one_sided_recognition")
    if vad_total >= MEETING_SPEECH_MS and words < MEETING_MIN_WORDS:
        failures.append("minimum_word_count")
    timing_ratio = timing_coverage_ms / max(vad_total, 1)
    if vad_total >= TIMING_GATE_SPEECH_MS and timing_ratio < MIN_TIMING_COVERAGE:
        failures.append("timing_coverage")
    if total_duration_ms >= LONG_MEETING_MS and turn_count <= MAX_LONG_MEETING_TURNS:
        failures.append("long_meeting_implausibly_short")

    metrics_support_silence = all(
        float(row["audio_metrics"]["mic_rms_dbfs"]) <= SILENCE_MAX_RMS_DBFS
        and float(row["audio_metrics"]["system_rms_dbfs"]) <= SILENCE_MAX_RMS_DBFS
        and float(row["audio_metrics"]["mic_zero_ratio"]) >= SILENCE_MIN_ZERO_RATIO
        and float(row["audio_metrics"]["system_zero_ratio"]) >= SILENCE_MIN_ZERO_RATIO
        for row in segments
    )
    vad_supports_silence = vad_total <= SILENCE_MAX_VAD_MS
    if words == 0 and metrics_support_silence and vad_supports_silence and not failures:
        decision = "verified_silence"
    elif failures:
        decision = "needs_attention"
    else:
        decision = "quality_passed"
    return {
        "schema_version": "meeting-quality-report-v1",
        "policy_version": F.QUALITY_POLICY_V1,
        "mode": "enforced",
        "decision": decision,
        "failure_codes": failures,
        "capture": {
            "segment_count": len(segments),
            "total_duration_ms": total_duration_ms,
            "decoded_duration_ms": decoded_total,
            "mic_vad_speech_ms": mic_vad,
            "system_vad_speech_ms": system_vad,
        },
        "recognition": {
            "mic_words": mic_words,
            "system_words": system_words,
            "total_words": words,
            "turn_count": turn_count,
            "timing_coverage_ms": timing_coverage_ms,
            "timing_coverage_ratio": timing_ratio,
            "forced_english_attempted": forced_english_attempted,
        },
        "audio_metrics_support_silence": metrics_support_silence,
        "vad_supports_silence": vad_supports_silence,
        "policy": policy_values(),
    }
