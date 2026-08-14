"""Post-meeting synthesis for compatibility and fenced V2 jobs.

V2 pipeline: claim the durable job lease -> verify and decode each immutable
segment -> persist provider attempts -> merge provider-derived turns -> apply
``meeting-quality-v1`` -> create immutable revision artifacts -> fenced publish.
Transient failures retry only the failed segment. Successful or failed
processing never deletes source audio.

The V1 branch remains for tasks already in flight during rollout. Both branches
leave the monthly counter untouched because claim owns billing.
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
from ..entitlement import get_user_effective_tier
from ..model_provider import get_model_provider
from . import deepgram, evidence, gcs_audio, notifications, openai_stt, quality, store
from . import fields as F

# One segment is 5 minutes; 3 in flight keeps a 4-hour meeting under ~10
# minutes of wall clock without hammering Deepgram's rate limits.
_TRANSCRIBE_CONCURRENCY = 3

# Others' share of total words below this ratio marks the transcript
# one-sided (phone dial-in, listen-only webinar, loopback silence).
_ONE_SIDED_RATIO = 0.01

# Transcript budget for the LLM prompt. A 4-hour meeting can exceed this;
# keep the head (agenda, framing) and the tail (decisions, wrap-up) and mark
# the elision so the model never treats the gap as silence.
_TRANSCRIPT_HEAD_CHARS = 90_000
_TRANSCRIPT_TAIL_CHARS = 30_000

_SYSTEM_PROMPT = (
    "You turn a raw meeting transcript into a short, faithful note. "
    "The transcript labels the device owner's speech as 'You' and everyone "
    "else as 'Others'. Only state things the transcript supports. If the "
    "meeting had no decisions, action items, or open questions, return empty "
    "lists for those fields; never invent content to fill a field. If the "
    "transcript is marked one-sided, say so in the summary rather than "
    "guessing at the missing half."
)


class MeetingNote(BaseModel):
    summary: str = Field(description="2-4 sentences on what the meeting covered and concluded.")
    decisions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class SynthesisLeaseBusyError(RuntimeError):
    """A synthesis delivery arrived while another worker still owns the lease.

    Cloud Tasks must retry this delivery instead of treating the meeting as
    settled. The active worker may still finish successfully; if it died, the
    existing lease timeout makes the next delivery reclaimable.
    """


async def run_synthesis(uid: str, meeting_id: str, *, job_id: str = "") -> str:
    """Synthesize one completed meeting. Returns the terminal status
    ("ready" | "excluded" | "failed", or the already-settled status of a
    re-run). Raises on retryable infrastructure failures."""
    if job_id:
        return await _run_v2_synthesis(uid, meeting_id, job_id)

    # Compatibility worker for durable V1 jobs created before the V2 rollout.
    # Lease, not a plain compare-and-set: a Cloud Tasks duplicate delivered
    # while a fresh run is mid-flight is refused (status_now "synthesizing"
    # answers 200 and the queue stops); a crashed run's lease expires and the
    # redelivery re-claims it.
    claimed, status_now = await store.claim_synthesis(uid, meeting_id)
    if not claimed:
        if status_now == F.STATUS_SYNTHESIZING:
            raise SynthesisLeaseBusyError(f"Meeting {meeting_id} synthesis lease is still active.")
        logger.info(
            "meetings.synthesis: skipped, not claimable",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
                "status": status_now,
            },
        )
        await notifications.notify_settled(uid, meeting_id)
        return status_now or F.STATUS_FAILED

    meeting = await store.get_meeting(uid, meeting_id)
    if meeting is None:
        return F.STATUS_FAILED

    # Sensitive-meeting exclusion: checked before a single byte reaches STT.
    title = str(meeting.get(F.TITLE, ""))
    keywords = await store.get_exclude_keywords(uid)
    title_lower = title.lower()
    if any(keyword in title_lower for keyword in keywords):
        await store.transition_status(
            uid,
            meeting_id,
            from_statuses=(F.STATUS_SYNTHESIZING,),
            to_status=F.STATUS_EXCLUDED,
            extra=store.failure_meta(code=F.FAIL_EXCLUDED_SENSITIVE, retryable=False),
        )
        await notifications.notify_settled(uid, meeting_id)
        logger.info(
            "meetings.synthesis: excluded by keyword",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
            },
        )
        return F.STATUS_EXCLUDED

    cap_minutes = int(meeting.get(F.CAP_MINUTES, F.FREE_SYNTHESIS_CAP_MINUTES))
    try:
        transcript, transcript_turns, language, one_sided, has_gaps = await _transcribe_meeting(
            uid,
            meeting_id,
            meeting,
            cap_ms=cap_minutes * 60_000,
        )
    except deepgram.DeepgramRejectedError as exc:
        # Deepgram will reject these exact bytes forever - terminal, stop the
        # retry loop and drop the audio.
        logger.warn(
            "meetings.synthesis: audio rejected by deepgram",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
                "error": str(exc),
            },
        )
        await store.mark_failed(
            uid,
            meeting_id,
            from_statuses=(F.STATUS_SYNTHESIZING,),
            code=F.FAIL_AUDIO_REJECTED,
            retryable=False,
        )
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_FAILED

    try:
        await store.set_stage(uid, meeting_id, F.STAGE_BUILDING_INSIGHTS)
        note = await _synthesize_note(
            title=title,
            transcript=transcript,
            language=language,
            one_sided=one_sided,
            has_gaps=has_gaps,
        )
        # Speaker attribution comes only from the capture channels and
        # Deepgram output. The insight model never rewrites the transcript.
        note[F.NOTE_TRANSCRIPT] = transcript_turns
    except Exception as exc:
        # The provider already walked its complete fallback chain. Without a
        # separately approved short-lived transcript artifact there is no
        # privacy-safe retry input, so settle visibly while retaining audio
        # for explicit deletion or lifecycle enforcement.
        logger.warn(
            "meetings.synthesis: note generation failed",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
                "error": str(exc),
            },
        )
        await store.mark_failed(
            uid,
            meeting_id,
            from_statuses=(F.STATUS_SYNTHESIZING,),
            code=F.FAIL_INSIGHT_GENERATION_FAILED,
            retryable=False,
        )
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_FAILED

    effective_tier = await get_user_effective_tier(uid)
    await store.save_note(uid, meeting_id, note, effective_tier=effective_tier)
    await notifications.notify_settled(uid, meeting_id)
    return F.STATUS_READY


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
        "schema_version": "meeting-provider-attempt-v2",
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
            "schema_version": "meeting-provider-attempt-v2",
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
        schema_version="meeting-provider-attempt-v2",
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
    attempt_id = f"{lease.job_id[:12]}-{lease.job_attempt}-{seq}-error-{uuid.uuid4().hex[:8]}"
    path = gcs_audio.transcript_attempt_path(
        lease.user_id,
        lease.meeting_id,
        attempt_id,
        seq,
    )
    payload = {
        "schema_version": "meeting-provider-attempt-v2",
        "provider": "deepgram",
        "model": "nova-3",
        "parameters": {},
        "request_id": "",
        "responded_at": "",
        "detected_language": None,
        "confidence": None,
        "word_timings": [],
        "channel_count": segment["channel_count"],
        "source_audio_digest": segment["content_sha256"],
        "parser_version": "deepgram-meeting-v2",
        "raw_provider_response": None,
        "normalized": None,
        "normalized_errors": [{"code": code, "retryable": True}],
    }
    artifact = await gcs_audio.create_json_artifact(
        path,
        payload,
        metadata={
            "schema_version": "meeting-provider-attempt-v2",
            "meeting_id": lease.meeting_id,
            "capture_run_id": lease.capture_run_id,
            "capture_fence": str(lease.capture_fence),
            "job_id": lease.job_id,
            "job_attempt": str(lease.job_attempt),
            "seq": str(seq),
            "source_audio_digest": str(segment["content_sha256"]),
            "provider": "deepgram",
            "model": "nova-3",
        },
    )
    pointer = _artifact_dict(
        artifact,
        schema_version="meeting-provider-attempt-v2",
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
    )


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
        if any(keyword in title.lower() for keyword in keywords):
            await store.fail_job(
                lease,
                error_code=F.FAIL_EXCLUDED_SENSITIVE,
                retryable=False,
            )
            await notifications.notify_settled(uid, meeting_id)
            return F.STATUS_NEEDS_ATTENTION

        job = await store.get_job(uid, job_id) or {}
        prior_attempts = job.get("segment_attempts") or {}
        results: list[deepgram.SegmentTranscript] = []
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
                            "meeting_id": meeting_id,
                            "capture_run_id": lease.capture_run_id,
                            "seq": seq,
                            "error_type": type(exc).__name__,
                        },
                    )
                    result = await openai_stt.transcribe_segment(audio)
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
                            "meeting_id": meeting_id,
                            "capture_run_id": lease.capture_run_id,
                            "seq": seq,
                            "vad_speech_ms": vad_ms,
                            "provider": result.provider,
                            "error_code": "segment_empty_with_energy",
                        },
                    )
                first_attempt_id = f"{job_id[:12]}-{lease.job_attempt}-{seq}-{uuid.uuid4().hex[:8]}"
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
                        f"{job_id[:12]}-{lease.job_attempt}-{seq}-en-{uuid.uuid4().hex[:8]}"
                    )
                    forced_pointer = await _persist_attempt(
                        lease,
                        seq=seq,
                        segment=segment,
                        result=result,
                        attempt_id=forced_attempt_id,
                    )
                    attempt_pointers.append(forced_pointer)
            except (deepgram.DeepgramError, deepgram.ProviderOutputError) as exc:
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
            speaker = override
            if speaker is None:
                speaker = (
                    "You"
                    if channel == deepgram.MIC_CHANNEL
                    else ("Others" if channel == deepgram.LOOPBACK_CHANNEL else "")
                )
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
        quality_report = quality.evaluate(
            segments=segments,
            transcripts=transcript_rows,
            total_duration_ms=int(meeting[F.TOTAL_DURATION_MS]),
            turn_count=len(turns),
            forced_english_attempted=forced_english_attempted,
        )
        revision = int(meeting.get(F.ARTIFACT_REVISION, 0))
        revision_id = f"r{revision + 1}-{lease.manifest_sha256[:12]}"
        base_metadata = {
            "meeting_id": meeting_id,
            "capture_run_id": lease.capture_run_id,
            "capture_fence": str(lease.capture_fence),
            "manifest_sha256": lease.manifest_sha256,
            "revision": str(revision + 1),
            "quality_policy_version": F.QUALITY_POLICY_V1,
        }
        canonical = {
            "schema_version": F.TRANSCRIPT_SCHEMA_VERSION,
            "meeting_id": meeting_id,
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
            speaker = override or (
                "You"
                if channel == deepgram.MIC_CHANNEL
                else "Others"
                if channel == deepgram.LOOPBACK_CHANNEL
                else ""
            )
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
            "meeting_id": meeting_id,
            "revision": revision + 1,
            "title": title,
            "language": language,
            "transcript": transcript_text,
            "quality_decision": quality_report["decision"],
        }
        artifact_objects = {
            "canonical": await gcs_audio.create_json_artifact(
                gcs_audio.transcript_revision_path(
                    uid,
                    meeting_id,
                    revision_id,
                    "canonical.json",
                ),
                canonical,
                metadata={**base_metadata, "schema_version": F.TRANSCRIPT_SCHEMA_VERSION},
            ),
            "webvtt": await gcs_audio.create_text_artifact(
                gcs_audio.transcript_revision_path(
                    uid,
                    meeting_id,
                    revision_id,
                    "transcript.vtt",
                ),
                "\n".join(webvtt_lines),
                content_type="text/vtt",
                metadata={**base_metadata, "schema_version": "webvtt"},
            ),
            "quality_report": await gcs_audio.create_json_artifact(
                gcs_audio.transcript_revision_path(
                    uid,
                    meeting_id,
                    revision_id,
                    "quality-report.json",
                ),
                quality_report,
                metadata={**base_metadata, "schema_version": "meeting-quality-report-v1"},
            ),
            "note_input": await gcs_audio.create_json_artifact(
                gcs_audio.transcript_revision_path(
                    uid,
                    meeting_id,
                    revision_id,
                    "note-input.json",
                ),
                note_input,
                metadata={**base_metadata, "schema_version": "meeting-note-input-v1"},
            ),
        }
        artifacts = {
            name: _artifact_dict(
                value,
                schema_version=(
                    F.TRANSCRIPT_SCHEMA_VERSION
                    if name == "canonical"
                    else "meeting-quality-report-v1"
                    if name == "quality_report"
                    else "meeting-note-input-v1"
                    if name == "note_input"
                    else "webvtt"
                ),
                quality_policy_version=F.QUALITY_POLICY_V1,
                revision=revision + 1,
            )
            for name, value in artifact_objects.items()
        }
        artifacts["provider_attempts"] = {
            "items": attempt_pointers,
            "schema_version": "meeting-provider-attempt-v2",
            "quality_policy_version": F.QUALITY_POLICY_V1,
            "revision": revision + 1,
        }
        note: dict[str, Any] | None = None
        if quality_report["decision"] == "quality_passed":
            await store.set_stage(uid, meeting_id, F.STAGE_BUILDING_INSIGHTS)
            note = await _synthesize_note(
                title=title,
                transcript=transcript_text,
                language=language,
                one_sided=(
                    quality_report["recognition"]["mic_words"] == 0
                    or quality_report["recognition"]["system_words"] == 0
                ),
                has_gaps=False,
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
    except deepgram.ProviderOutputError as exc:
        committed = await store.fail_job(
            lease,
            error_code=getattr(exc, "code", F.FAIL_PROVIDER_MALFORMED),
            retryable=True,
        )
        if not committed:
            raise SynthesisLeaseBusyError("Worker lease was lost during failure commit.") from exc
        if lease.job_attempt < 3:
            raise
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION
    except deepgram.DeepgramError as exc:
        committed = await store.fail_job(
            lease,
            error_code=F.FAIL_TRANSCRIPTION_UNAVAILABLE,
            retryable=True,
        )
        if not committed:
            raise SynthesisLeaseBusyError("Worker lease was lost during failure commit.") from exc
        if lease.job_attempt < 3:
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
        if lease.job_attempt < 3:
            raise
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_NEEDS_ATTENTION


async def _transcribe_meeting(
    uid: str,
    meeting_id: str,
    meeting: dict[str, Any],
    *,
    cap_ms: int,
) -> tuple[str, list[dict[str, str]], str | None, bool, bool]:
    """Download and transcribe every in-cap segment, then merge utterances
    into one time-ordered labeled transcript. Returns (transcript,
    transcript_turns, language, one_sided, has_gaps). Raises on GCS/Deepgram
    infrastructure failures (retryable) and DeepgramRejectedError (terminal,
    handled by the caller).

    The cap is enforced by CUMULATIVE claimed duration in seq order plus a
    hard segment-count ceiling, never by trusting start_ms alone - offsets
    and durations are client-supplied, and the upload route's range checks
    only bound them, they don't make them honest."""
    meta_by_seq = {int(seg.get("seq", -1)): seg for seg in meeting.get(F.SEGMENTS, [])}
    max_in_cap_segments = cap_ms // (5 * 60_000) + 2

    paths = await gcs_audio.list_segment_paths(uid, meeting_id)
    in_cap: list[tuple[int, int, str, bool]] = []  # (seq, start_ms, path, incomplete)
    cumulative_ms = 0
    dropped = 0
    for path in paths:  # sorted by name = seq order
        match = re.search(r"/(\d{4})\.flac$", path)
        if not match:
            continue
        seq = int(match.group(1))
        meta = meta_by_seq.get(seq, {})
        start_ms = int(meta.get("start_ms", seq * 5 * 60_000))
        duration_ms = int(meta.get("duration_ms", 5 * 60_000))
        if cumulative_ms >= cap_ms or start_ms >= cap_ms or len(in_cap) >= max_in_cap_segments:
            dropped += 1
            continue
        cumulative_ms += max(duration_ms, 1)
        in_cap.append((seq, start_ms, path, meta.get("incomplete") is True))
    if dropped:
        logger.info(
            "meetings.synthesis: segments past cap dropped",
            {
                "user_id": uid,
                "meeting_id": meeting_id,
                "dropped": dropped,
                "cap_ms": cap_ms,
            },
        )
    has_gaps = any(incomplete for _, _, _, incomplete in in_cap)

    semaphore = asyncio.Semaphore(_TRANSCRIBE_CONCURRENCY)

    async def _one(seq: int, start_ms: int, path: str, _incomplete: bool):
        async with semaphore:
            data = await gcs_audio.download_segment(path)
            result = await deepgram.transcribe_segment(data)
            return start_ms, result

    results = await asyncio.gather(*(_one(*item) for item in in_cap))

    utterances: list[tuple[float, int, str | None, str]] = []
    mic_words = 0
    loopback_words = 0
    languages: Counter[str] = Counter()
    for start_ms, segment in results:
        mic_words += segment.mic_words
        loopback_words += segment.loopback_words
        if segment.language:
            languages[segment.language] += 1
        for utt in segment.utterances:
            utterances.append(
                (
                    start_ms / 1000.0 + utt.start_s,
                    utt.channel,
                    utt.speaker,
                    utt.text,
                )
            )

    utterances.sort(key=lambda item: item[0])
    turns: list[dict[str, str]] = []
    for _, channel, speaker_override, text in utterances:
        speaker = speaker_override
        if speaker is None:
            if channel == deepgram.MIC_CHANNEL:
                speaker = "You"
            elif channel == deepgram.LOOPBACK_CHANNEL:
                speaker = "Others"
            else:
                speaker = ""
        if turns and turns[-1][F.TRANSCRIPT_SPEAKER] == speaker:
            turns[-1][F.TRANSCRIPT_TEXT] = f"{turns[-1][F.TRANSCRIPT_TEXT]} {text}"
        else:
            turns.append(
                {
                    F.TRANSCRIPT_SPEAKER: speaker,
                    F.TRANSCRIPT_TEXT: text,
                }
            )

    lines = [
        f"{turn[F.TRANSCRIPT_SPEAKER]}: {turn[F.TRANSCRIPT_TEXT]}"
        if turn[F.TRANSCRIPT_SPEAKER]
        else turn[F.TRANSCRIPT_TEXT]
        for turn in turns
    ]

    total_words = mic_words + loopback_words
    one_sided = total_words > 0 and (
        min(mic_words, loopback_words) / total_words < _ONE_SIDED_RATIO
    )
    language = languages.most_common(1)[0][0] if languages else None
    return "\n".join(lines), turns, language, one_sided, has_gaps


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
