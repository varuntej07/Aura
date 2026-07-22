"""Canonical voice-history reads and scoped deletion contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from google.cloud.firestore_v1.base_query import FieldFilter

from ..lib.logger import logger
from . import voice_session_fields as vf
from .firebase import admin_firestore
from .voice_transcript_reconciliation import deterministic_voice_message_id

_DELETE_BATCH_LIMIT = 450


@dataclass(frozen=True, slots=True)
class DeleteResult:
    ok: bool
    messages: int = 0
    voice_runs: int = 0
    conversation_deleted: bool = False


def _canonical_turn_count(raw_turns: list[dict]) -> int:
    return sum(
        1 for turn in raw_turns
        if turn.get("role") in {"user", "assistant"} and str(turn.get("text") or "").strip()
    )


def _belongs_to_voice_run(
    *,
    doc_id: str,
    data: dict,
    conversation_id: str,
    voice_run_id: str,
    raw_turns: list[dict],
) -> bool:
    if data.get(vf.VOICE_RUN_ID) == voice_run_id:
        return True
    expected = {
        deterministic_voice_message_id(conversation_id, index)
        for index in range(_canonical_turn_count(raw_turns))
    }
    return doc_id in expected


def _json_time(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value or "")


def _archive_owns(
    data: dict,
    *,
    voice_run_id: str = "",
    conversation_id: str = "",
) -> bool:
    """Return whether a synthesized archive contains data from the deletion target."""
    return bool(
        (voice_run_id and voice_run_id in data.get("voice_run_ids", []))
        or (
            conversation_id
            and conversation_id in data.get("conversation_ids", [])
        )
    )


async def load_voice_messages(
    user_id: str,
    *,
    conversation_id: str,
    voice_run_id: str,
    raw_turns: list[dict],
) -> list[dict]:
    if not conversation_id:
        return []

    def _read() -> list[dict]:
        messages = (
            admin_firestore().collection("users").document(user_id)
            .collection("chat_sessions").document(conversation_id)
            .collection("messages").order_by("sequence")
        )
        out: list[dict] = []
        for snap in messages.stream():
            data = snap.to_dict() or {}
            if not _belongs_to_voice_run(
                doc_id=snap.id,
                data=data,
                conversation_id=conversation_id,
                voice_run_id=voice_run_id,
                raw_turns=raw_turns,
            ):
                continue
            out.append({
                "message_id": snap.id,
                "role": data.get("role", ""),
                "text": data.get("text", ""),
                "timestamp": _json_time(data.get("created_at")),
                "sequence": data.get("sequence", 0),
                vf.VOICE_RUN_ID: data.get(vf.VOICE_RUN_ID, voice_run_id),
            })
        return out

    return await asyncio.to_thread(_read)


def _delete_refs(db, refs: list[object]) -> None:
    for start in range(0, len(refs), _DELETE_BATCH_LIMIT):
        batch = db.batch()
        for ref in refs[start : start + _DELETE_BATCH_LIMIT]:
            batch.delete(ref)
        batch.commit()


async def delete_voice_run_data(user_id: str, voice_run_id: str) -> DeleteResult:
    """Delete one run's messages and metadata, preserving later text in the thread."""
    def _delete() -> DeleteResult:
        db = admin_firestore()
        user_ref = db.collection("users").document(user_id)
        session_ref = user_ref.collection("voice_sessions").document(voice_run_id)
        snap = session_ref.get()
        archive_ref = user_ref.collection("voice_session_state").document("archive")
        archive = archive_ref.get()
        archive_data = archive.to_dict() or {} if archive.exists else {}
        if not snap.exists:
            if _archive_owns(archive_data, voice_run_id=voice_run_id):
                _delete_refs(db, [archive_ref])
                return DeleteResult(ok=True, voice_runs=1)
            return DeleteResult(ok=False)
        data = snap.to_dict() or {}
        conversation_id = str(data.get(vf.CONVERSATION_ID) or "")
        raw_turns = data.get("raw_turns") if isinstance(data.get("raw_turns"), list) else []
        refs: list[object] = [session_ref]
        message_count = 0

        if conversation_id:
            messages = (
                user_ref.collection("chat_sessions").document(conversation_id)
                .collection("messages")
            )
            for message in messages.stream():
                message_data = message.to_dict() or {}
                if _belongs_to_voice_run(
                    doc_id=message.id,
                    data=message_data,
                    conversation_id=conversation_id,
                    voice_run_id=voice_run_id,
                    raw_turns=raw_turns,
                ):
                    refs.append(message.reference)
                    message_count += 1

        latest_ref = user_ref.collection("voice_session_state").document("latest")
        latest = latest_ref.get()
        if latest.exists and (latest.to_dict() or {}).get("last_session_id") == voice_run_id:
            refs.append(latest_ref)
        if _archive_owns(archive_data, voice_run_id=voice_run_id):
            refs.append(archive_ref)

        _delete_refs(db, refs)
        return DeleteResult(ok=True, messages=message_count, voice_runs=1)

    try:
        return await asyncio.to_thread(_delete)
    except Exception as exc:
        logger.warn("History: voice-run delete failed", {
            "user_id": user_id, "voice_run_id": voice_run_id, "error": str(exc),
        })
        return DeleteResult(ok=False)


async def delete_conversation_data(user_id: str, conversation_id: str) -> DeleteResult:
    """Delete a whole canonical chat thread and every linked active voice run."""
    def _delete() -> DeleteResult:
        db = admin_firestore()
        user_ref = db.collection("users").document(user_id)
        chat_ref = user_ref.collection("chat_sessions").document(conversation_id)
        refs: list[object] = []
        message_count = 0
        for message in chat_ref.collection("messages").stream():
            refs.append(message.reference)
            message_count += 1

        run_ids: list[str] = []
        runs = user_ref.collection("voice_sessions").where(
            filter=FieldFilter(vf.CONVERSATION_ID, "==", conversation_id)
        )
        for run in runs.stream():
            refs.append(run.reference)
            run_ids.append(run.id)

        latest_ref = user_ref.collection("voice_session_state").document("latest")
        latest = latest_ref.get()
        if latest.exists and (latest.to_dict() or {}).get("last_session_id") in set(run_ids):
            refs.append(latest_ref)
        archive_ref = user_ref.collection("voice_session_state").document("archive")
        archive = archive_ref.get()
        archive_data = archive.to_dict() or {} if archive.exists else {}
        if _archive_owns(archive_data, conversation_id=conversation_id):
            # The archive is one synthesized aggregate. Without raw archived sessions it
            # cannot be surgically regenerated, so privacy deletion drops the aggregate.
            refs.append(archive_ref)
        refs.append(chat_ref)
        _delete_refs(db, refs)
        return DeleteResult(
            ok=True,
            messages=message_count,
            voice_runs=len(run_ids),
            conversation_deleted=True,
        )

    try:
        return await asyncio.to_thread(_delete)
    except Exception as exc:
        logger.warn("History: conversation delete failed", {
            "user_id": user_id, "conversation_id": conversation_id, "error": str(exc),
        })
        return DeleteResult(ok=False)
