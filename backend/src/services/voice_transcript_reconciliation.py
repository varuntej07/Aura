"""Idempotent worker repair for canonical voice messages.

The Flutter client and worker share deterministic ids ``{conversation_id}__v{index}``.
The worker never appends a second id. It first verifies every already-present message's
role and normalized text; any conflict fails closed. Only absent ids are then upserted.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from ..lib.logger import logger
from .firebase import admin_firestore

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    expected: int = 0
    matched: int = 0
    inserted: int = 0
    conflicts: int = 0
    status: str = "skipped"

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


def deterministic_voice_message_id(conversation_id: str, index: int) -> str:
    return f"{conversation_id}__v{index}"


def normalize_transcript_text(value: object) -> str:
    return _WHITESPACE.sub(" ", str(value or "").strip()).casefold()


def _canonical_turns(turns: list[dict]) -> list[dict]:
    return [
        turn
        for turn in turns
        if turn.get("role") in {"user", "assistant"} and str(turn.get("text") or "").strip()
    ]


async def reconcile_voice_transcript(
    *,
    user_id: str,
    conversation_id: str,
    voice_run_id: str,
    turns: list[dict],
) -> ReconciliationResult:
    expected_turns = _canonical_turns(turns)
    if not user_id or not conversation_id or not voice_run_id or not expected_turns:
        return ReconciliationResult(expected=len(expected_turns))

    def _reconcile() -> ReconciliationResult:
        db = admin_firestore()
        chat_ref = (
            db.collection("users").document(user_id)
            .collection("chat_sessions").document(conversation_id)
        )
        messages = chat_ref.collection("messages")
        parent_missing = not chat_ref.get().exists
        missing: list[tuple[object, dict, int]] = []
        matched = 0
        conflicts = 0

        for index, turn in enumerate(expected_turns):
            message_id = deterministic_voice_message_id(conversation_id, index)
            ref = messages.document(message_id)
            snap = ref.get()
            if not snap.exists:
                missing.append((ref, turn, index))
                continue
            data = snap.to_dict() or {}
            if (
                data.get("role") != turn.get("role")
                or normalize_transcript_text(data.get("text"))
                != normalize_transcript_text(turn.get("text"))
            ):
                conflicts += 1
            else:
                matched += 1

        if conflicts:
            return ReconciliationResult(
                expected=len(expected_turns), matched=matched,
                conflicts=conflicts, status="conflict",
            )

        if not missing and not parent_missing:
            return ReconciliationResult(
                expected=len(expected_turns), matched=matched, status="parity",
            )

        batch = db.batch()
        if parent_missing:
            first_timestamp = expected_turns[0].get("timestamp") or datetime.now(UTC).isoformat()
            last_timestamp = expected_turns[-1].get("timestamp") or first_timestamp
            batch.set(chat_ref, {
                "started_at": first_timestamp,
                "updated_at": last_timestamp,
                "last_message_at": last_timestamp,
                "last_message_preview": str(expected_turns[-1].get("text") or "")[:160],
                "message_count": len(expected_turns),
            }, merge=True)
        for ref, turn, original_index in missing:
            timestamp = turn.get("timestamp") or datetime.now(UTC).isoformat()
            batch.set(ref, {
                "session_id": conversation_id,
                "role": turn["role"],
                "text": str(turn["text"]),
                "channel": "voice",
                "created_at": timestamp,
                "sequence": original_index + 1,
                "status": "sent",
                "voice_run_id": voice_run_id,
            }, merge=True)
        batch.commit()
        return ReconciliationResult(
            expected=len(expected_turns), matched=matched,
            inserted=len(missing), status="repaired",
        )

    try:
        result = await asyncio.to_thread(_reconcile)
    except Exception as exc:
        logger.warn("VoiceTranscript: reconciliation failed", {
            "user_id": user_id, "conversation_id": conversation_id,
            "voice_run_id": voice_run_id, "error": str(exc),
        })
        return ReconciliationResult(expected=len(expected_turns), status="failed")

    log_payload = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "voice_run_id": voice_run_id,
        **result.as_dict(),
    }
    if result.conflicts:
        logger.error("voice_transcript_parity_mismatch", log_payload)
    else:
        logger.info("VoiceTranscript: reconciliation complete", log_payload)
    return result
