"""Post-meeting synthesis for fenced V2 jobs.

V2 pipeline: claim the durable job lease -> verify and decode each immutable
segment -> persist provider attempts -> merge provider-derived turns -> apply
``meeting-quality-v1`` -> create immutable revision artifacts -> fenced publish.
Transient failures retry only the failed segment. Successful or failed
processing never deletes source audio. Claim owns monthly billing.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import Counter
from typing import Any, cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ...prompts import MEETING_NOTE_SYSTEM_PROMPT
from ..entitlement import get_user_effective_tier
from ..model_provider import get_model_provider
from . import deepgram, evidence, gcs_audio, notifications, openai_stt, quality, store, transcript
from . import fields as F

# One segment is 5 minutes; 3 in flight keeps a 4-hour meeting under ~10
# minutes of wall clock without hammering Deepgram's rate limits.
# Currently UNREFERENCED: segments are transcribed serially today (see
# _transcribe_segments); kept as the sizing target for when long-meeting
# support makes concurrent transcription worth its complexity.
_TRANSCRIBE_CONCURRENCY = 3

# Transcript budget for the LLM prompt. A 4-hour meeting can exceed this;
# keep the head (agenda, framing) and the tail (decisions, wrap-up) and mark
# the elision so the model never treats the gap as silence.
_TRANSCRIPT_HEAD_CHARS = 90_000
_TRANSCRIPT_TAIL_CHARS = 30_000

# Lives in prompts.py (every prompt in one home); aliased for local use.
_SYSTEM_PROMPT = MEETING_NOTE_SYSTEM_PROMPT


class MeetingNote(BaseModel):
    summary: str = Field(description="2-4 sentences on what the meeting covered and concluded.")
    decisions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# Below this, a keyword matches so much ordinary text that exclusion becomes
# noise ("hr" is inside "three"); such keywords are skipped loudly, never
# matched broadly.
_EXCLUDE_KEYWORD_MIN_CHARS = 3


def _title_matches_exclude_keywords(title: str, keywords: list[str]) -> bool:
    """Word-boundary match of the user's own exclude list against the title.

    The previous unanchored substring match let a short keyword silently
    exclude unrelated meetings, and the user's only feedback was
    FAIL_EXCLUDED_SENSITIVE on a meeting they expected notes for. This is
    user-authored configuration, not an inference about what the user meant.
    Neither the title nor the keyword is ever logged.
    """
    lowered = title.lower()
    matched = False
    for keyword in keywords:
        if len(keyword) < _EXCLUDE_KEYWORD_MIN_CHARS:
            logger.warn(
                "meetings.synthesis: exclude keyword too short, skipped",
                {"keyword_length": len(keyword)},
            )
            continue
        if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", lowered):
            matched = True
    return matched


class SynthesisLeaseBusyError(RuntimeError):
    """A synthesis delivery arrived while another worker still owns the lease.

    Cloud Tasks must retry this delivery instead of treating the meeting as
    settled. The active worker may still finish successfully; if it died, the
    existing lease timeout makes the next delivery reclaimable.
    """


async def run_synthesis(uid: str, meeting_id: str, *, job_id: str) -> str:
    """Run the fenced V2 synthesis job delivered by Cloud Tasks."""
    if not job_id:
        raise ValueError("Meeting synthesis requires job_id.")
    return await _run_v2_synthesis(uid, meeting_id, job_id)
def _normalized_transcript(result: deepgram.SegmentTranscript) -> dict[str, Any]:
    return {
        "utterances": [
            {
                "channel": utterance.channel,
                "start_s": utterance.start_s,
                "end_s": utterance.end_s,
                "text": utterance.text,
                "speaker": utterance.speaker,
            }
            for utterance in result.utterances
        ],
        "mic_words": result.mic_words,
        "loopback_words": result.loopback_words,
        "language": result.language,
        "language_confidence": result.language_confidence,
        "confidence": result.confidence,
        "words": result.words,
    }


def _attempt_payload(
    result: deepgram.SegmentTranscript,
    *,
    source_audio_digest: str,
    channel_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
        "provider": result.provider,
        "model": result.model,
        "parameters": result.parameters,
        "request_id": result.request_id,
        "responded_at": result.responded_at,
        "detected_language": result.language,
        "language_confidence": result.language_confidence,
        "confidence": result.confidence,
        "channel_count": channel_count,
        "source_audio_digest": source_audio_digest,
        "parser_version": result.parser_version,
        "forced_english": result.forced_english,
        "raw_provider_response": result.raw_response,
        "normalized": _normalized_transcript(result),
        "normalized_errors": [],
    }


def _artifact_dict(value: gcs_audio.ImmutableObject, **extra: Any) -> dict[str, Any]:
    return {
        "path": value.path,
        "generation": value.generation,
        "sha256": value.sha256,
        "size": value.size,
        "schema_version": extra.pop("schema_version", ""),
        **extra,
    }


def _speaker_label(channel: int, override: str | None) -> str:
    """Channel-derived display speaker, unless the provider supplied one.

    ``override`` substitutes ONLY when None: an intentional empty-string
    speaker (the mono fallback in deepgram._parse) must stay empty rather
    than being re-guessed from the channel. This helper replaced two subtly
    different inline copies (one used ``or``, which swallowed "").
    """
    if override is not None:
        return override
    if channel == transcript.MIC_CHANNEL:
        return "You"
    if channel == transcript.LOOPBACK_CHANNEL:
        return "Others"
    return ""


def _vtt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{millis:03}"


async def _persist_attempt(
    lease: store.JobLease,
    *,
    seq: int,
    segment: dict[str, Any],
    result: deepgram.SegmentTranscript,
    attempt_id: str,
) -> dict[str, Any]:
    payload = _attempt_payload(
        result,
        source_audio_digest=str(segment["content_sha256"]),
        channel_count=int(segment["channel_count"]),
    )
    path = gcs_audio.transcript_attempt_path(
        lease.user_id,
        lease.meeting_id,
        attempt_id,
        seq,
    )
    artifact = await gcs_audio.create_json_artifact(
        path,
        payload,
        metadata={
            "schema_version": F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
            "meeting_id": lease.meeting_id,
            "capture_run_id": lease.capture_run_id,
            "capture_fence": str(lease.capture_fence),
            "job_id": lease.job_id,
            "job_attempt": str(lease.job_attempt),
            "seq": str(seq),
            "source_audio_digest": str(segment["content_sha256"]),
            "provider": result.provider,
            "model": result.model,
        },
    )
    pointer = _artifact_dict(
        artifact,
        schema_version=F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
        seq=seq,
        provider=result.provider,
        model=result.model,
        request_id=result.request_id,
    )
    if not await store.record_segment_attempt(
        lease,
        seq=seq,
        attempt_artifact=pointer,
        outcome="succeeded",
    ):
        raise SynthesisLeaseBusyError("Worker lease was lost before progress commit.")
    return pointer


async def _persist_provider_error(
    lease: store.JobLease,
    *,
    seq: int,
    segment: dict[str, Any],
    error: Exception,
) -> None:
    code = getattr(error, "code", F.FAIL_TRANSCRIPTION_UNAVAILABLE)
    # The failure evidence names the leg that actually failed: openai_stt
    # raises errors carrying its own provider/model/parser identity, so an
    # OpenAI-fallback failure is never attributed to Deepgram.
    provider = getattr(error, "provider", "deepgram")
    model = getattr(error, "model", "nova-3")
    parser_version = getattr(error, "parser_version", "deepgram-meeting-v2")
    retryable = not isinstance(error, transcript.TranscriptionRejectedError)
    attempt_id = f"{lease.job_id[:12]}-{lease.job_attempt}-{seq}-error-{uuid.uuid4().hex[:8]}"
    path = gcs_audio.transcript_attempt_path(
        lease.user_id,
        lease.meeting_id,
        attempt_id,
        seq,
    )
    payload = {
        "schema_version": F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
        "provider": provider,
        "model": model,
        "parameters": {},
        "request_id": "",
        "responded_at": "",
        "detected_language": None,
        "confidence": None,
        "word_timings": [],
        "channel_count": segment["channel_count"],
        "source_audio_digest": segment["content_sha256"],
        "parser_version": parser_version,
        "raw_provider_response": None,
        "normalized": None,
        "normalized_errors": [{"code": code, "retryable": retryable}],
    }
    artifact = await gcs_audio.create_json_artifact(
        path,
        payload,
        metadata={
            "schema_version": F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
            "meeting_id": lease.meeting_id,
            "capture_run_id": lease.capture_run_id,
            "capture_fence": str(lease.capture_fence),
            "job_id": lease.job_id,
            "job_attempt": str(lease.job_attempt),
            "seq": str(seq),
            "source_audio_digest": str(segment["content_sha256"]),
            "provider": provider,
            "model": model,
        },
    )
    pointer = _artifact_dict(
        artifact,
        schema_version=F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
        seq=seq,
        error_code=code,
    )
    if not await store.record_segment_attempt(
        lease,
        seq=seq,
        attempt_artifact=pointer,
        outcome="failed",
        error_code=code,
    ):
        raise SynthesisLeaseBusyError("Worker lease was lost before failure commit.")


async def _load_prior_attempt(pointer: dict[str, Any]) -> deepgram.SegmentTranscript:
    raw = await gcs_audio.download_exact(
        str(pointer["path"]),
        str(pointer["generation"]),
        transcript=True,
    )
    if evidence.sha256_hex(raw) != pointer.get("sha256"):
        raise store.MeetingIntegrityError(
            F.FAIL_MANIFEST_INTEGRITY,
            "Provider attempt artifact digest mismatch.",
        )
    payload = json.loads(raw)
    normalized = payload["normalized"]
    # Restore the provider identity too: a resumed segment used to rehydrate
    # with the dataclass defaults, so every prior OpenAI-fallback attempt
    # looked like Deepgram on the next job attempt.
    return deepgram.SegmentTranscript(
        utterances=[
            deepgram.Utterance(
                channel=int(row["channel"]),
                start_s=float(row["start_s"]),
                end_s=float(row["end_s"]),
                text=str(row["text"]),
                speaker=row.get("speaker"),
            )
            for row in normalized["utterances"]
        ],
        mic_words=int(normalized["mic_words"]),
        loopback_words=int(normalized["loopback_words"]),
        language=normalized.get("language"),
        language_confidence=normalized.get("language_confidence"),
        confidence=normalized.get("confidence"),
        words=list(normalized.get("words") or []),
        provider=str(payload.get("provider") or "deepgram"),
        model=str(payload.get("model") or "nova-3"),
        parser_version=str(payload.get("parser_version") or "deepgram-meeting-v2"),
        forced_english=bool(payload.get("forced_english", False)),
    )


async def _transcribe_segments(
    lease: store.JobLease,
    segments: list[dict[str, Any]],
    prior_attempts: dict[str, Any],
) -> tuple[list[transcript.SegmentTranscript], list[dict[str, Any]], bool]:
    """Verify, transcribe (Deepgram primary, OpenAI fallback), and persist an
    immutable attempt for every segment, resuming past segments that already
    succeeded on a prior job attempt.

    Returns ``(results, attempt_pointers, forced_english_attempted)``. Raises
    exactly what the orchestrator's error-to-failure-code map expects:
    ``MeetingIntegrityError``, ``TranscriptionError`` (and subclasses), and
    ``SynthesisLeaseBusyError`` on a lost lease/heartbeat.
    """
    results: list[transcript.SegmentTranscript] = []
    attempt_pointers: list[dict[str, Any]] = []
    forced_english_attempted = False
    for segment in segments:
        seq = int(segment["seq"])
        prior = prior_attempts.get(str(seq)) or {}
        pointer = prior.get("artifact") if prior.get("outcome") == "succeeded" else None
        if isinstance(pointer, dict) and pointer.get("path"):
            result = await _load_prior_attempt(pointer)
            attempt_pointers.append(pointer)
            results.append(result)
            continue

        receipt = segment["upload_receipt"]
        audio = await gcs_audio.download_exact(
            str(receipt["object"]),
            str(receipt["generation"]),
        )
        if evidence.sha256_hex(audio) != segment["content_sha256"] or len(audio) != int(
            segment["byte_length"]
        ):
            raise store.MeetingIntegrityError(
                F.FAIL_MANIFEST_INTEGRITY,
                f"Source audio identity mismatch for segment {seq}.",
            )
        stream = await asyncio.to_thread(evidence.decode_flac_info, audio)
        if (
            stream.channel_count != 2
            or stream.sample_rate_hz != 16_000
            or abs(stream.duration_ms - int(segment["duration_ms"]))
            > evidence.duration_tolerance_ms(int(segment["duration_ms"]))
        ):
            raise store.MeetingIntegrityError(
                F.FAIL_MANIFEST_INTEGRITY,
                f"FLAC validation failed for segment {seq}.",
            )
        try:
            try:
                result = await deepgram.transcribe_segment(audio)
            except deepgram.DeepgramError as exc:
                logger.warn(
                    "meetings.synthesis: primary STT failed, using OpenAI fallback",
                    {
                        "meeting_id": lease.meeting_id,
                        "capture_run_id": lease.capture_run_id,
                        "seq": seq,
                        "error_type": type(exc).__name__,
                    },
                )
                try:
                    result = await openai_stt.transcribe_segment(audio)
                except transcript.TranscriptionError as fallback_exc:
                    # A Deepgram rejection stays terminal even when the
                    # fallback also failed: redelivering the job re-sends
                    # the same rejected bytes to the same rejection.
                    if isinstance(exc, transcript.TranscriptionRejectedError):
                        raise exc from fallback_exc
                    raise
            vad_ms = int(segment["audio_metrics"]["mic_vad_speech_ms"]) + int(
                segment["audio_metrics"]["system_vad_speech_ms"]
            )
            if not result.utterances and vad_ms >= quality.EMPTY_WITH_SPEECH_MS:
                # Worth a second opinion: the two providers disagree often
                # enough on hard audio to be worth one extra call.
                if result.provider == "openai":
                    result = await deepgram.transcribe_segment(audio)
            if not result.utterances and vad_ms >= quality.EMPTY_WITH_SPEECH_MS:
                # Both providers succeeded and both found no speech. That is
                # a legitimate result, not a fault: the VAD is a bare RMS
                # energy threshold, so music, a hold tone, a video or room
                # noise all register as "speech" here while containing none.
                #
                # This used to raise and fail the WHOLE meeting. A single
                # such segment discarded every other segment's transcript -
                # one 60 minute meeting lost 7 successfully transcribed
                # segments to 30 seconds of non-speech system audio.
                # Publishability is meeting-wide and already belongs to
                # meeting-quality-v1, which scores this exact condition
                # (`empty_with_speech`) across the whole transcript.
                logger.warn(
                    "meetings.synthesis: no speech recognized in energetic segment",
                    {
                        "meeting_id": lease.meeting_id,
                        "capture_run_id": lease.capture_run_id,
                        "seq": seq,
                        "vad_speech_ms": vad_ms,
                        "provider": result.provider,
                        "error_code": "segment_empty_with_energy",
                    },
                )
            first_attempt_id = (
                f"{lease.job_id[:12]}-{lease.job_attempt}-{seq}-{uuid.uuid4().hex[:8]}"
            )
            first_pointer = await _persist_attempt(
                lease,
                seq=seq,
                segment=segment,
                result=result,
                attempt_id=first_attempt_id,
            )
            attempt_pointers.append(first_pointer)
            language_requires_retry = (
                result.provider == "deepgram"
                and bool(result.language)
                and (
                    not str(result.language).lower().startswith("en")
                    or (
                        result.language_confidence is not None
                        and result.language_confidence < 0.70
                    )
                )
            )
            if language_requires_retry:
                forced_english_attempted = True
                result = await deepgram.transcribe_segment(audio, force_english=True)
                forced_attempt_id = (
                    f"{lease.job_id[:12]}-{lease.job_attempt}-{seq}-en-{uuid.uuid4().hex[:8]}"
                )
                forced_pointer = await _persist_attempt(
                    lease,
                    seq=seq,
                    segment=segment,
                    result=result,
                    attempt_id=forced_attempt_id,
                )
                attempt_pointers.append(forced_pointer)
        except transcript.TranscriptionError as exc:
            await _persist_provider_error(
                lease,
                seq=seq,
                segment=segment,
                error=exc,
            )
            raise
        results.append(result)
        if not await store.heartbeat_job(lease):
            raise SynthesisLeaseBusyError("Worker lease was lost during transcription.")
    return results, attempt_pointers, forced_english_attempted


def _merge_turns(
    segments: list[dict[str, Any]],
    results: list[transcript.SegmentTranscript],
) -> tuple[
    list[tuple[float, float, int, str | None, str]],
    list[dict[str, Any]],
    list[dict[str, str]],
    str | None,
    str,
]:
    """Merge per-segment transcripts into absolute-time utterances, speaker
    turns, the dominant language, and the flat transcript text."""
    utterances: list[tuple[float, float, int, str | None, str]] = []
    transcript_rows: list[dict[str, Any]] = []
    languages: Counter[str] = Counter()
    for segment, result in zip(segments, results, strict=True):
        normalized = _normalized_transcript(result)
        transcript_rows.append(normalized)
        if result.language:
            languages[result.language] += 1
        for utterance in result.utterances:
            utterances.append(
                (
                    int(segment["start_ms"]) / 1000 + utterance.start_s,
                    int(segment["start_ms"]) / 1000 + utterance.end_s,
                    utterance.channel,
                    utterance.speaker,
                    utterance.text,
                )
            )
    utterances.sort(key=lambda row: row[0])
    turns: list[dict[str, str]] = []
    for _, _, channel, override, text in utterances:
        speaker = _speaker_label(channel, override)
        if turns and turns[-1][F.TRANSCRIPT_SPEAKER] == speaker:
            turns[-1][F.TRANSCRIPT_TEXT] += f" {text}"
        else:
            turns.append(
                {
                    F.TRANSCRIPT_SPEAKER: speaker,
                    F.TRANSCRIPT_TEXT: text,
                }
            )
    language = languages.most_common(1)[0][0] if languages else None
    transcript_text = "\n".join(
        f"{turn[F.TRANSCRIPT_SPEAKER]}: {turn[F.TRANSCRIPT_TEXT]}"
        if turn[F.TRANSCRIPT_SPEAKER]
        else turn[F.TRANSCRIPT_TEXT]
        for turn in turns
    )
    return utterances, transcript_rows, turns, language, transcript_text


async def _render_artifacts(
    lease: store.JobLease,
    *,
    revision: int,
    title: str,
    language: str | None,
    turns: list[dict[str, str]],
    transcript_rows: list[dict[str, Any]],
    utterances: list[tuple[float, float, int, str | None, str]],
    attempt_pointers: list[dict[str, Any]],
    transcript_text: str,
    quality_report: dict[str, Any],
) -> dict[str, Any]:
    """Write the four immutable revision artifacts and return the Firestore
    pointer projection (plus the provider_attempts row)."""
    revision_id = f"r{revision + 1}-{lease.manifest_sha256[:12]}"
    base_metadata = {
        "meeting_id": lease.meeting_id,
        "capture_run_id": lease.capture_run_id,
        "capture_fence": str(lease.capture_fence),
        "manifest_sha256": lease.manifest_sha256,
        "revision": str(revision + 1),
        "quality_policy_version": F.QUALITY_POLICY_V1,
    }
    canonical = {
        "schema_version": F.TRANSCRIPT_SCHEMA_VERSION,
        "meeting_id": lease.meeting_id,
        "capture_run_id": lease.capture_run_id,
        "capture_fence": lease.capture_fence,
        "manifest_sha256": lease.manifest_sha256,
        "revision": revision + 1,
        "language": language,
        "turns": turns,
        "segments": transcript_rows,
        "provider_attempts": attempt_pointers,
    }
    webvtt_lines = ["WEBVTT", ""]
    for index, (start_s, end_s, channel, override, text) in enumerate(utterances):
        speaker = _speaker_label(channel, override)
        webvtt_lines.extend(
            [
                str(index + 1),
                f"{_vtt_timestamp(start_s)} --> {_vtt_timestamp(max(end_s, start_s + 0.001))}",
                f"{speaker}: {text}" if speaker else text,
                "",
            ]
        )
    note_input = {
        "schema_version": "meeting-note-input-v1",
        "meeting_id": lease.meeting_id,
        "revision": revision + 1,
        "title": title,
        "language": language,
        "transcript": transcript_text,
        "quality_decision": quality_report["decision"],
    }
    # One table (F.REVISION_ARTIFACTS) drives both the writes and the
    # Firestore pointer projection, so adding an artifact - or changing a
    # schema version - is one edit that cannot leave the two out of sync.
    revision_payloads: dict[str, Any] = {
        "canonical": canonical,
        "webvtt": "\n".join(webvtt_lines),
        "quality_report": quality_report,
        "note_input": note_input,
    }
    artifact_objects: dict[str, Any] = {}
    for name, (filename, content_type, schema_version) in F.REVISION_ARTIFACTS.items():
        artifact_path = gcs_audio.transcript_revision_path(
            lease.user_id,
            lease.meeting_id,
            revision_id,
            filename,
        )
        artifact_metadata = {**base_metadata, "schema_version": schema_version}
        if content_type is None:
            artifact_objects[name] = await gcs_audio.create_json_artifact(
                artifact_path,
                revision_payloads[name],
                metadata=artifact_metadata,
            )
        else:
            artifact_objects[name] = await gcs_audio.create_text_artifact(
                artifact_path,
                revision_payloads[name],
                content_type=content_type,
                metadata=artifact_metadata,
            )
    artifacts = {
        name: _artifact_dict(
            value,
            schema_version=F.REVISION_ARTIFACTS[name][2],
            quality_policy_version=F.QUALITY_POLICY_V1,
            revision=revision + 1,
        )
        for name, value in artifact_objects.items()
    }
    artifacts["provider_attempts"] = {
        "items": attempt_pointers,
        "schema_version": F.PROVIDER_ATTEMPT_SCHEMA_VERSION,
        "quality_policy_version": F.QUALITY_POLICY_V1,
        "revision": revision + 1,
    }
    return artifacts


async def _run_v2_synthesis(uid: str, meeting_id: str, job_id: str) -> str:
    lease = await store.claim_job(uid, job_id)
    if lease is None:
        job = await store.get_job(uid, job_id)
        if job and job.get("state") == F.JOB_COMPLETE:
            return F.STATUS_READY
        raise SynthesisLeaseBusyError(f"Meeting job {job_id} is not claimable.")
    if lease.meeting_id != meeting_id:
        raise store.MeetingIntegrityError("job_identity_mismatch", "Job meeting mismatch.")

    try:
        meeting, segments = await store.get_job_context(lease)
        keywords = await store.get_exclude_keywords(uid)
        title = str(meeting.get(F.TITLE, ""))
        if _title_matches_exclude_keywords(title, keywords):
            await store.fail_job(
                lease,
                error_code=F.FAIL_EXCLUDED_SENSITIVE,
                retryable=False,
            )
            await notifications.notify_settled(uid, meeting_id)
            return F.STATUS_NEEDS_ATTENTION

        job = await store.get_job(uid, job_id) or {}
        prior_attempts = job.get("segment_attempts") or {}
        results, attempt_pointers, forced_english_attempted = await _transcribe_segments(
            lease,
            segments,
            prior_attempts,
        )

        utterances, transcript_rows, turns, language, transcript_text = _merge_turns(
            segments,
            results,
        )
        quality_report = quality.evaluate(
            segments=segments,
            transcripts=transcript_rows,
            total_duration_ms=int(meeting[F.TOTAL_DURATION_MS]),
            turn_count=len(turns),
            forced_english_attempted=forced_english_attempted,
        )
        if quality_report["warning_codes"]:
            logger.warn(
                "meetings.v2: quality warnings retained in partial note",
                {
                    "user_id": uid,
                    "meeting_id": meeting_id,
                    "capture_run_id": lease.capture_run_id,
                    "capture_fence": lease.capture_fence,
                    "job_id": lease.job_id,
                    "attempt": lease.job_attempt,
                    "warning_codes": quality_report["warning_codes"],
                },
            )
        revision = int(meeting.get(F.ARTIFACT_REVISION, 0))
        artifacts = await _render_artifacts(
            lease,
            revision=revision,
            title=title,
            language=language,
            turns=turns,
            transcript_rows=transcript_rows,
            utterances=utterances,
            attempt_pointers=attempt_pointers,
            transcript_text=transcript_text,
            quality_report=quality_report,
        )
        note: dict[str, Any] | None = None
        if quality_report["decision"] == "quality_passed":
            has_gaps = any(segment.get("incomplete") is True for segment in segments)
            await store.set_stage(uid, meeting_id, F.STAGE_BUILDING_INSIGHTS)
            note = await _synthesize_note(
                title=title,
                transcript=transcript_text,
                language=language,
                one_sided=(
                    quality_report["recognition"]["mic_words"] == 0
                    or quality_report["recognition"]["system_words"] == 0
                ),
                has_gaps=has_gaps,
            )
            note[F.NOTE_TRANSCRIPT] = turns
        effective_tier = await get_user_effective_tier(uid)
        published = await store.publish_v2_result(
            lease,
            expected_revision=revision,
            artifacts=artifacts,
            quality_report=quality_report,
            note=note,
            effective_tier=effective_tier,
        )
        if not published:
            raise SynthesisLeaseBusyError("Worker lease was lost before publication.")
        await notifications.notify_settled(uid, meeting_id)
        return (
            F.STATUS_READY
            if quality_report["decision"] in ("quality_passed", "verified_silence")
            else F.STATUS_NEEDS_ATTENTION
        )
    except SynthesisLeaseBusyError:
        raise
    except transcript.TranscriptionRejectedError as exc:
        # Terminal by provider contract: a non-429/422 4xx means the same
        # bytes can never succeed, so re-driving the job only re-pays for the
        # same rejection (the class docstring said this; nothing enforced it).
        committed = await store.fail_job(
            lease,
            error_code=F.FAIL_AUDIO_REJECTED,
            retryable=False,
        )
        if not committed:
            raise SynthesisLeaseBusyError("Worker lease was lost during failure commit.") from exc
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION
    except transcript.ProviderOutputError as exc:
        committed = await store.fail_job(
            lease,
            error_code=getattr(exc, "code", F.FAIL_PROVIDER_MALFORMED),
            retryable=True,
        )
        if not committed:
            raise SynthesisLeaseBusyError("Worker lease was lost during failure commit.") from exc
        if lease.job_attempt < F.MAX_SYNTHESIS_ATTEMPTS:
            raise
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION
    except transcript.TranscriptionError as exc:
        committed = await store.fail_job(
            lease,
            error_code=F.FAIL_TRANSCRIPTION_UNAVAILABLE,
            retryable=True,
        )
        if not committed:
            raise SynthesisLeaseBusyError("Worker lease was lost during failure commit.") from exc
        if lease.job_attempt < F.MAX_SYNTHESIS_ATTEMPTS:
            raise
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION
    except (store.MeetingIntegrityError, evidence.EvidenceValidationError) as exc:
        await store.fail_job(
            lease,
            error_code=getattr(exc, "code", F.FAIL_MANIFEST_INTEGRITY),
            retryable=False,
        )
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION
    except Exception as exc:
        # Anything unclassified - most realistically note generation exhausting
        # every LLM tier - must still release the 30 minute lease. Without this
        # the meeting stayed "synthesizing" forever: each Cloud Tasks retry found
        # the lease still held, and the queue eventually stopped retrying with
        # nothing recording that it had given up.
        logger.error(
            "meetings.synthesis: unclassified v2 failure",
            {
                "meeting_id": meeting_id,
                "job_id": lease.job_id,
                "job_attempt": lease.job_attempt,
                "error_code": "synthesis_unclassified_failure",
                "error": str(exc),
            },
        )
        committed = await store.fail_job(
            lease,
            error_code=F.FAIL_INSIGHT_GENERATION_FAILED,
            retryable=True,
        )
        if not committed:
            raise SynthesisLeaseBusyError("Worker lease was lost during failure commit.") from exc
        if lease.job_attempt < F.MAX_SYNTHESIS_ATTEMPTS:
            raise
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION


async def _synthesize_note(
    *,
    title: str,
    transcript: str,
    language: str | None,
    one_sided: bool,
    has_gaps: bool,
) -> dict[str, Any]:
    """One LLM pass over the merged transcript. An empty transcript short-
    circuits to a stock note - there is nothing for a model to add to
    silence, and a hallucinated summary of it is strictly worse."""
    if not transcript.strip():
        return {
            "summary": "No speech was captured for this meeting.",
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "language": language or "",
            "one_sided": one_sided,
            "partial": has_gaps,
        }

    if len(transcript) > _TRANSCRIPT_HEAD_CHARS + _TRANSCRIPT_TAIL_CHARS:
        transcript = (
            transcript[:_TRANSCRIPT_HEAD_CHARS]
            + "\n[... middle of transcript elided for length ...]\n"
            + transcript[-_TRANSCRIPT_TAIL_CHARS:]
        )

    caveats: list[str] = []
    if one_sided:
        caveats.append(
            "The transcript is one-sided: effectively only one side of the "
            "conversation was captured."
        )
    if has_gaps:
        caveats.append(
            "Some segments may contain silent gaps from an audio device "
            "change; treat the transcript as possibly partial."
        )
    if language and not language.startswith("en"):
        caveats.append(f"The meeting language was detected as '{language}'.")
    caveat_block = ("\n".join(caveats) + "\n\n") if caveats else ""

    prompt = f"Meeting title: {title or '(untitled)'}\n\n{caveat_block}Transcript:\n{transcript}"
    note = cast(
        MeetingNote,
        await get_model_provider().balanced(
            prompt,
            system=_SYSTEM_PROMPT,
            response_model=MeetingNote,
            temperature=0.3,
        ),
    )
    return {
        **note.model_dump(),
        "language": language or "",
        "one_sided": one_sided,
        "partial": has_gaps,
    }
