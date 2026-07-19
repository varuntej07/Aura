"""Post-meeting synthesis - the Cloud Tasks worker body.

Pipeline: status compare-and-set -> sensitive-exclude check -> per-segment
Deepgram transcription (bounded concurrency) -> merged "You"/"Others"
transcript -> LLM insights -> persist insights plus provider-derived turns with
tier-conditional TTL -> delete raw audio immediately.

Failure taxonomy, which decides what Cloud Tasks sees AND how the desktop can
recover:
  - Retryable infrastructure (Deepgram after its own retries, GCS reads,
    Firestore writes): raised to the handler, which answers 5xx; the task
    retries and audio stays in GCS for the next attempt.
  - Terminal (every configured insight model failed, Deepgram rejects the
    audio forever, or a doc cannot proceed): status flips to failed with a safe
    failure code and retryable=false, audio is deleted, and the handler answers
    200. Retaining transcript or audio for a later insight-only retry requires
    a separate privacy-reviewed short-lived artifact and is not implicit here.
Either way the monthly counter is untouched here - it was charged at claim.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any, cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..entitlement import get_user_effective_tier
from ..model_provider import get_model_provider
from . import deepgram, gcs_audio, notifications, store
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


async def run_synthesis(uid: str, meeting_id: str) -> str:
    """Synthesize one completed meeting. Returns the terminal status
    ("ready" | "excluded" | "failed", or the already-settled status of a
    re-run). Raises on retryable infrastructure failures."""
    # Lease, not a plain compare-and-set: a Cloud Tasks duplicate delivered
    # while a fresh run is mid-flight is refused (status_now "synthesizing"
    # answers 200 and the queue stops); a crashed run's lease expires and the
    # redelivery re-claims it.
    claimed, status_now = await store.claim_synthesis(uid, meeting_id)
    if not claimed:
        if status_now == F.STATUS_SYNTHESIZING:
            raise SynthesisLeaseBusyError(
                f"Meeting {meeting_id} synthesis lease is still active."
            )
        logger.info("meetings.synthesis: skipped, not claimable", {
            "user_id": uid, "meeting_id": meeting_id, "status": status_now,
        })
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
            uid, meeting_id,
            from_statuses=(F.STATUS_SYNTHESIZING,),
            to_status=F.STATUS_EXCLUDED,
            extra=store.failure_meta(code=F.FAIL_EXCLUDED_SENSITIVE, retryable=False),
        )
        await gcs_audio.delete_meeting_audio(uid, meeting_id)
        await notifications.notify_settled(uid, meeting_id)
        logger.info("meetings.synthesis: excluded by keyword", {
            "user_id": uid, "meeting_id": meeting_id,
        })
        return F.STATUS_EXCLUDED

    cap_minutes = int(meeting.get(F.CAP_MINUTES, F.FREE_SYNTHESIS_CAP_MINUTES))
    try:
        transcript, transcript_turns, language, one_sided, has_gaps = await _transcribe_meeting(
            uid, meeting_id, meeting, cap_ms=cap_minutes * 60_000,
        )
    except deepgram.DeepgramRejectedError as exc:
        # Deepgram will reject these exact bytes forever - terminal, stop the
        # retry loop and drop the audio.
        logger.warn("meetings.synthesis: audio rejected by deepgram", {
            "user_id": uid, "meeting_id": meeting_id, "error": str(exc),
        })
        await store.mark_failed(
            uid, meeting_id,
            from_statuses=(F.STATUS_SYNTHESIZING,),
            code=F.FAIL_AUDIO_REJECTED, retryable=False,
        )
        await gcs_audio.delete_meeting_audio(uid, meeting_id)
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_FAILED

    try:
        await store.set_stage(uid, meeting_id, F.STAGE_BUILDING_INSIGHTS)
        note = await _synthesize_note(
            title=title, transcript=transcript, language=language,
            one_sided=one_sided, has_gaps=has_gaps,
        )
        # Speaker attribution comes only from the capture channels and
        # Deepgram output. The insight model never rewrites the transcript.
        note[F.NOTE_TRANSCRIPT] = transcript_turns
    except Exception as exc:
        # The provider already walked its complete fallback chain. Without a
        # separately approved short-lived transcript artifact there is no
        # privacy-safe retry input, so settle visibly and delete raw audio.
        logger.warn("meetings.synthesis: note generation failed", {
            "user_id": uid, "meeting_id": meeting_id, "error": str(exc),
        })
        await store.mark_failed(
            uid, meeting_id,
            from_statuses=(F.STATUS_SYNTHESIZING,),
            code=F.FAIL_INSIGHT_GENERATION_FAILED, retryable=False,
        )
        await gcs_audio.delete_meeting_audio(uid, meeting_id)
        await notifications.notify_settled(uid, meeting_id)
        return F.STATUS_FAILED

    effective_tier = await get_user_effective_tier(uid)
    await store.save_note(uid, meeting_id, note, effective_tier=effective_tier)
    await gcs_audio.delete_meeting_audio(uid, meeting_id)
    await notifications.notify_settled(uid, meeting_id)
    return F.STATUS_READY


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
    meta_by_seq = {
        int(seg.get("seq", -1)): seg for seg in meeting.get(F.SEGMENTS, [])
    }
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
        if (
            cumulative_ms >= cap_ms
            or start_ms >= cap_ms
            or len(in_cap) >= max_in_cap_segments
        ):
            dropped += 1
            continue
        cumulative_ms += max(duration_ms, 1)
        in_cap.append((seq, start_ms, path, meta.get("incomplete") is True))
    if dropped:
        logger.info("meetings.synthesis: segments past cap dropped", {
            "user_id": uid, "meeting_id": meeting_id, "dropped": dropped,
            "cap_ms": cap_ms,
        })
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
            utterances.append((
                start_ms / 1000.0 + utt.start_s,
                utt.channel,
                utt.speaker,
                utt.text,
            ))

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
            turns[-1][F.TRANSCRIPT_TEXT] = (
                f"{turns[-1][F.TRANSCRIPT_TEXT]} {text}"
            )
        else:
            turns.append({
                F.TRANSCRIPT_SPEAKER: speaker,
                F.TRANSCRIPT_TEXT: text,
            })

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
            "decisions": [], "action_items": [], "open_questions": [],
            "language": language or "", "one_sided": one_sided,
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

    prompt = (
        f"Meeting title: {title or '(untitled)'}\n\n"
        f"{caveat_block}"
        f"Transcript:\n{transcript}"
    )
    note = cast(MeetingNote, await get_model_provider().balanced(
        prompt,
        system=_SYSTEM_PROMPT,
        response_model=MeetingNote,
        temperature=0.3,
    ))
    return {
        **note.model_dump(),
        "language": language or "",
        "one_sided": one_sided,
        "partial": has_gaps,
    }
