from unittest.mock import MagicMock, patch

import pytest

from src.services.voice_transcript_reconciliation import reconcile_voice_transcript


def _db_for_rows(rows: dict[str, dict | None]):
    db = MagicMock()
    messages = MagicMock()
    refs = {}
    for message_id, data in rows.items():
        ref = MagicMock()
        snap = MagicMock()
        snap.exists = data is not None
        snap.to_dict.return_value = data or {}
        ref.get.return_value = snap
        refs[message_id] = ref
    messages.document.side_effect = lambda message_id: refs[message_id]
    (
        db.collection.return_value.document.return_value
        .collection.return_value.document.return_value
        .collection
    ).return_value = messages
    batch = MagicMock()
    db.batch.return_value = batch
    return db, refs, batch


@pytest.mark.asyncio
async def test_reconciliation_inserts_only_missing_deterministic_id():
    conversation_id = "conversation-1"
    db, _, batch = _db_for_rows({
        f"{conversation_id}__v0": {"role": "user", "text": "Hello there"},
        f"{conversation_id}__v1": None,
    })
    with patch(
        "src.services.voice_transcript_reconciliation.admin_firestore",
        return_value=db,
    ):
        result = await reconcile_voice_transcript(
            user_id="u1",
            conversation_id=conversation_id,
            voice_run_id="run-1",
            turns=[
                {"role": "user", "text": "Hello there", "timestamp": "t1"},
                {"role": "assistant", "text": "Hey!", "timestamp": "t2"},
            ],
        )

    assert result.status == "repaired"
    assert result.matched == 1
    assert result.inserted == 1
    batch.set.assert_called_once()
    payload = batch.set.call_args.args[1]
    assert payload["voice_run_id"] == "run-1"
    assert payload["sequence"] == 2
    batch.commit.assert_called_once()


@pytest.mark.asyncio
async def test_reconciliation_conflict_fails_closed_before_any_write():
    conversation_id = "conversation-1"
    db, _, batch = _db_for_rows({
        f"{conversation_id}__v0": {"role": "user", "text": "different"},
        f"{conversation_id}__v1": None,
    })
    with patch(
        "src.services.voice_transcript_reconciliation.admin_firestore",
        return_value=db,
    ):
        result = await reconcile_voice_transcript(
            user_id="u1",
            conversation_id=conversation_id,
            voice_run_id="run-1",
            turns=[
                {"role": "user", "text": "expected"},
                {"role": "assistant", "text": "reply"},
            ],
        )

    assert result.status == "conflict"
    assert result.conflicts == 1
    batch.set.assert_not_called()
    batch.commit.assert_not_called()
