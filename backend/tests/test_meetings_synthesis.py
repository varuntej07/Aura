"""The synthesis worker's branch behavior.

Cloud Tasks delivers at-least-once, so the run must be idempotent (a settled
meeting no-ops), the sensitive-exclude check must run before any byte reaches
STT, the synthesis cap must drop out-of-cap segments, the one-sided heuristic
must survive into the saved note, and raw audio must be deleted on every
terminal path (ready, excluded, failed) but never on a retryable one.

Collaborators (store, gcs_audio, deepgram, entitlement, model provider) are
monkeypatched at the synthesis module's own import sites; the store itself is
covered by test_meetings_claim.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.services.meetings import deepgram as dg
from src.services.meetings import fields as F
from src.services.meetings import synthesis

UID = "user-1"
MID = "meeting-1"


class _Env:
    """One synthesis run's world: an in-memory meeting doc plus call records."""

    def __init__(self, monkeypatch, *, status=F.STATUS_UPLOADED, title="Weekly sync",
                 cap_minutes=60, segments=None, exclude=None, tier="free"):
        self.meeting: dict[str, Any] = {
            "meeting_id": MID,
            F.TITLE: title,
            F.STATUS: status,
            F.CAP_MINUTES: cap_minutes,
            F.SEGMENTS: segments or [],
        }
        self.audio_deleted = False
        self.saved_note: dict | None = None
        self.transcribed_paths: list[str] = []
        self.gcs_paths = [
            f"meetings/{UID}/{MID}/{int(seg['seq']):04d}.flac"
            for seg in (segments or [])
        ]
        self.deepgram_result = dg.SegmentTranscript(
            utterances=[
                dg.Utterance(channel=0, start_s=1.0, end_s=2.0, text="Hello there."),
                dg.Utterance(channel=1, start_s=3.0, end_s=4.0, text="Hi, let's start."),
            ],
            mic_words=2, loopback_words=3, language="en",
        )

        async def fake_transition(uid, meeting_id, *, from_statuses, to_status, extra=None):
            current = self.meeting[F.STATUS]
            if current not in from_statuses:
                return False, current
            self.meeting[F.STATUS] = to_status
            return True, to_status

        async def fake_claim_synthesis(uid, meeting_id):
            current = self.meeting[F.STATUS]
            if current not in (F.STATUS_UPLOADED, F.STATUS_SYNTHESIZING):
                return False, current
            self.meeting[F.STATUS] = F.STATUS_SYNTHESIZING
            return True, F.STATUS_SYNTHESIZING

        async def fake_get_meeting(uid, meeting_id):
            return dict(self.meeting)

        async def fake_exclude(uid):
            return exclude or []

        async def fake_save_note(uid, meeting_id, note, *, effective_tier):
            self.saved_note = note
            self.meeting[F.STATUS] = F.STATUS_READY

        async def fake_list_paths(uid, meeting_id):
            return list(self.gcs_paths)

        async def fake_download(path):
            return b"flac-bytes"

        async def fake_delete(uid, meeting_id):
            self.audio_deleted = True
            return len(self.gcs_paths)

        async def fake_transcribe(data):
            self.transcribed_paths.append("called")
            return self.deepgram_result

        async def fake_tier(uid):
            return tier

        monkeypatch.setattr(synthesis.store, "transition_status", fake_transition)
        monkeypatch.setattr(synthesis.store, "claim_synthesis", fake_claim_synthesis)
        monkeypatch.setattr(synthesis.store, "get_meeting", fake_get_meeting)
        monkeypatch.setattr(synthesis.store, "get_exclude_keywords", fake_exclude)
        monkeypatch.setattr(synthesis.store, "save_note", fake_save_note)
        monkeypatch.setattr(synthesis.gcs_audio, "list_segment_paths", fake_list_paths)
        monkeypatch.setattr(synthesis.gcs_audio, "download_segment", fake_download)
        monkeypatch.setattr(synthesis.gcs_audio, "delete_meeting_audio", fake_delete)
        monkeypatch.setattr(synthesis.deepgram, "transcribe_segment", fake_transcribe)
        monkeypatch.setattr(synthesis, "get_user_effective_tier", fake_tier)
        self._set_llm(monkeypatch, ok=True)
        self._monkeypatch = monkeypatch

    def _set_llm(self, monkeypatch, *, ok: bool):
        env = self

        class _Provider:
            async def balanced(self, prompt, *, system=None, response_model=None,
                               temperature=0.5):
                if not ok:
                    raise RuntimeError("all models exhausted")
                env.llm_prompt = prompt
                return response_model(
                    summary="Discussed the launch.",
                    decisions=["Ship Friday"],
                    action_items=[],
                    open_questions=[],
                )

        monkeypatch.setattr(synthesis, "get_model_provider", lambda: _Provider())

    def fail_llm(self):
        self._set_llm(self._monkeypatch, ok=False)


def _seg(seq: int, start_min: int, incomplete: bool = False) -> dict:
    return {
        "seq": seq,
        "start_ms": start_min * 60_000,
        "duration_ms": 300_000,
        "incomplete": incomplete,
    }


def test_settled_meeting_no_ops(monkeypatch):
    env = _Env(monkeypatch, status=F.STATUS_READY, segments=[_seg(0, 0)])
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    assert not env.transcribed_paths     # STT never ran
    assert not env.audio_deleted         # nothing touched


def test_exclude_keyword_deletes_audio_before_stt(monkeypatch):
    env = _Env(monkeypatch, title="HR performance review",
               segments=[_seg(0, 0)], exclude=["performance review"])
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_EXCLUDED
    assert env.audio_deleted
    assert not env.transcribed_paths
    assert env.meeting[F.STATUS] == F.STATUS_EXCLUDED


def test_segments_past_cap_are_dropped(monkeypatch):
    env = _Env(monkeypatch, cap_minutes=60,
               segments=[_seg(0, 0), _seg(11, 55), _seg(12, 60), _seg(13, 65)])
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    assert len(env.transcribed_paths) == 2   # 0 and 55 min are in-cap; 60/65 dropped


def test_one_sided_flag_survives_into_the_note(monkeypatch):
    env = _Env(monkeypatch, segments=[_seg(0, 0)])
    env.deepgram_result = dg.SegmentTranscript(
        utterances=[dg.Utterance(channel=0, start_s=0.0, end_s=1.0, text="Just me talking.")],
        mic_words=300, loopback_words=0, language="en",
    )
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    assert env.saved_note is not None
    assert env.saved_note["one_sided"] is True


def test_empty_transcript_short_circuits_without_llm(monkeypatch):
    env = _Env(monkeypatch, segments=[_seg(0, 0)])
    env.deepgram_result = dg.SegmentTranscript()
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    assert env.saved_note is not None
    assert env.saved_note["action_items"] == []
    assert "No speech" in env.saved_note["summary"]


def test_llm_failure_is_terminal_fails_and_deletes_audio(monkeypatch):
    env = _Env(monkeypatch, segments=[_seg(0, 0)])
    env.fail_llm()
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_FAILED
    assert env.audio_deleted
    assert env.meeting[F.STATUS] == F.STATUS_FAILED


def test_deepgram_infra_failure_propagates_and_keeps_audio(monkeypatch):
    env = _Env(monkeypatch, segments=[_seg(0, 0)])

    async def boom(data):
        raise dg.DeepgramError("deepgram down")

    monkeypatch.setattr(synthesis.deepgram, "transcribe_segment", boom)
    with pytest.raises(dg.DeepgramError):
        asyncio.run(synthesis.run_synthesis(UID, MID))
    assert not env.audio_deleted   # retryable: audio must survive for the retry


def test_deepgram_rejection_is_terminal_not_a_retry_storm(monkeypatch):
    """A non-429 4xx means these bytes will never transcribe; the run must
    settle to failed (200 to Cloud Tasks) and drop the audio (review #13)."""
    env = _Env(monkeypatch, segments=[_seg(0, 0)])

    async def rejected(data):
        raise dg.DeepgramRejectedError("400 corrupt flac")

    monkeypatch.setattr(synthesis.deepgram, "transcribe_segment", rejected)
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_FAILED
    assert env.audio_deleted
    assert env.meeting[F.STATUS] == F.STATUS_FAILED


def test_exclude_read_failure_is_retryable_never_fails_open(monkeypatch):
    """An exclude-list read outage must NOT default to 'not excluded' - that
    would ship a possibly-excluded meeting to third-party STT (review #5)."""
    env = _Env(monkeypatch, segments=[_seg(0, 0)])

    async def unavailable(uid):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(synthesis.store, "get_exclude_keywords", unavailable)
    with pytest.raises(RuntimeError):
        asyncio.run(synthesis.run_synthesis(UID, MID))
    assert not env.audio_deleted
    assert not env.transcribed_paths


def test_incomplete_segment_flags_the_note_partial(monkeypatch):
    env = _Env(monkeypatch, segments=[_seg(0, 0, incomplete=True)])
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    assert env.saved_note is not None
    assert env.saved_note["partial"] is True


def test_forged_offsets_cannot_stretch_the_cap(monkeypatch):
    """All segments claiming start_ms=0 still stop at the cumulative-duration
    cap plus the count ceiling (review #1)."""
    env = _Env(monkeypatch, cap_minutes=60,
               segments=[_seg(i, 0) for i in range(40)])
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    # 60 min cap / 5 min claimed per segment = 12 by cumulative duration,
    # under the count ceiling of 14 - never all 40.
    assert len(env.transcribed_paths) == 12


def test_ready_path_deletes_audio(monkeypatch):
    env = _Env(monkeypatch, segments=[_seg(0, 0)])
    status = asyncio.run(synthesis.run_synthesis(UID, MID))
    assert status == F.STATUS_READY
    assert env.audio_deleted
    assert env.saved_note is not None
    assert env.saved_note["summary"]
