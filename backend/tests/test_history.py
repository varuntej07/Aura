import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.voice_history_store import DeleteResult, _archive_owns, _belongs_to_voice_run


def test_voice_run_ownership_accepts_runtime_tag():
    assert _belongs_to_voice_run(
        doc_id="random-id",
        data={"voice_run_id": "run-1"},
        conversation_id="conversation-1",
        voice_run_id="run-1",
        raw_turns=[],
    )


def test_voice_run_ownership_uses_deterministic_compatibility_ids_only():
    turns = [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hey"},
    ]
    assert _belongs_to_voice_run(
        doc_id="conversation-1__v1",
        data={},
        conversation_id="conversation-1",
        voice_run_id="run-1",
        raw_turns=turns,
    )
    assert not _belongs_to_voice_run(
        doc_id="later-text-message",
        data={},
        conversation_id="conversation-1",
        voice_run_id="run-1",
        raw_turns=turns,
    )


def test_archive_ownership_tracks_run_and_conversation_deletion_targets():
    archive = {
        "voice_run_ids": ["run-1"],
        "conversation_ids": ["conversation-1"],
    }
    assert _archive_owns(archive, voice_run_id="run-1")
    assert _archive_owns(archive, conversation_id="conversation-1")
    assert not _archive_owns(archive, conversation_id="conversation-2")


@pytest.mark.asyncio
async def test_history_detail_prefers_linked_canonical_messages():
    from src.handlers.history import handle_get_session_detail

    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = {
        "voice_run_id": "run-1",
        "conversation_id": "conversation-1",
        "surface": "app",
        "schema_version": 2,
        "recap": "A short recap.",
        "raw_turns": [{"role": "user", "text": "legacy"}],
    }
    collection = MagicMock()
    collection.document.return_value.get.return_value = snap
    canonical = [{
        "message_id": "conversation-1__v0",
        "role": "user",
        "text": "canonical",
        "timestamp": "t1",
        "sequence": 1,
        "voice_run_id": "run-1",
    }]
    with (
        patch("src.handlers.history.resolve_user_id_from_request", return_value="u1"),
        patch("src.handlers.history._sessions_collection", return_value=collection),
        patch("src.handlers.history.load_voice_messages", AsyncMock(return_value=canonical)),
    ):
        response = await handle_get_session_detail(SimpleNamespace(), "run-1")

    body = json.loads(response.body)
    assert body["conversation_id"] == "conversation-1"
    assert body["raw_turns"][0]["text"] == "canonical"
    assert body["messages"] == canonical


@pytest.mark.asyncio
async def test_voice_run_and_conversation_delete_are_distinct_contracts():
    from src.handlers.history import handle_delete_conversation, handle_delete_session

    with (
        patch("src.handlers.history.resolve_user_id_from_request", return_value="u1"),
        patch(
            "src.handlers.history.delete_voice_run_data",
            AsyncMock(return_value=DeleteResult(ok=True, messages=2, voice_runs=1)),
        ) as delete_run,
        patch(
            "src.handlers.history.delete_conversation_data",
            AsyncMock(return_value=DeleteResult(
                ok=True, messages=4, voice_runs=2, conversation_deleted=True,
            )),
        ) as delete_conversation,
    ):
        run_response = await handle_delete_session(SimpleNamespace(), "run-1")
        conversation_response = await handle_delete_conversation(
            SimpleNamespace(), "conversation-1",
        )

    delete_run.assert_awaited_once_with("u1", "run-1")
    delete_conversation.assert_awaited_once_with("u1", "conversation-1")
    assert json.loads(run_response.body)["messages_deleted"] == 2
    assert json.loads(conversation_response.body)["conversation_deleted"] is True
