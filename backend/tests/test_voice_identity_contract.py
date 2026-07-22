import json
from types import SimpleNamespace

from src.agent.voice_agent import _resolve_participant_metadata


def _ctx(metadata: dict):
    participant = SimpleNamespace(metadata=json.dumps(metadata))
    return SimpleNamespace(
        room=SimpleNamespace(remote_participants={"user": participant})
    )


def test_worker_reads_conversation_and_surface_from_same_token_metadata():
    assert _resolve_participant_metadata(_ctx({
        "surface": "desktop",
        "conversation_id": "e6f4550d-4c80-4db8-a08a-8e2ef781eb3c",
    })) == ("desktop", "e6f4550d-4c80-4db8-a08a-8e2ef781eb3c")


def test_worker_rejects_malformed_schema_v2_identity():
    assert _resolve_participant_metadata(_ctx({
        "surface": "watch",
        "conversation_id": "../../other-user",
    })) == (None, "")
