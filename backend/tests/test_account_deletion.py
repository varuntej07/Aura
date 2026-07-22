from __future__ import annotations

import asyncio
import json

from src.handlers import account
from src.services.meetings import gcs_audio


class _Request:
    headers: dict[str, str] = {}


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_account_deletion_removes_meeting_audio_before_data_and_auth(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(account, "decode_firebase_claims", lambda headers: {"uid": "user-1"})

    async def delete_audio(uid: str) -> int:
        calls.append(f"audio:{uid}")
        return 2

    monkeypatch.setattr(account.meeting_audio, "delete_user_audio", delete_audio)
    monkeypatch.setattr(account, "_delete_all_user_data", lambda uid: calls.append(f"data:{uid}"))
    monkeypatch.setattr(
        account,
        "_delete_firebase_auth_user",
        lambda uid: calls.append(f"auth:{uid}"),
    )

    response = asyncio.run(account.handle_delete_account(_Request()))

    assert response.status_code == 200
    assert calls == ["audio:user-1", "data:user-1", "auth:user-1"]


def test_account_deletion_keeps_data_and_auth_when_audio_cleanup_fails(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(account, "decode_firebase_claims", lambda headers: {"uid": "user-1"})

    async def delete_audio(uid: str) -> int:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(account.meeting_audio, "delete_user_audio", delete_audio)
    monkeypatch.setattr(account, "_delete_all_user_data", lambda uid: calls.append("data"))
    monkeypatch.setattr(account, "_delete_firebase_auth_user", lambda uid: calls.append("auth"))

    response = asyncio.run(account.handle_delete_account(_Request()))

    assert response.status_code == 500
    assert _body(response)["error"] == "Deletion failed. Please try again."
    assert calls == []


def test_user_audio_cleanup_deletes_only_the_users_prefix(monkeypatch):
    deleted: list[str] = []

    class _Blob:
        def __init__(self, name: str):
            self.name = name

    class _Bucket:
        def blob(self, name: str):
            class _DeleteRef:
                def delete(self) -> None:
                    deleted.append(name)

            return _DeleteRef()

    class _Client:
        def bucket(self, name: str) -> _Bucket:
            assert name == gcs_audio.bucket_name()
            return _Bucket()

        def list_blobs(self, name: str, *, prefix: str):
            assert name == gcs_audio.bucket_name()
            assert prefix == "meetings/user-1/"
            return [
                _Blob("meetings/user-1/meeting-a/0000.flac"),
                _Blob("meetings/user-1/meeting-b/0000.flac"),
            ]

    monkeypatch.setattr(gcs_audio, "_client", lambda: _Client())

    count = asyncio.run(gcs_audio.delete_user_audio("user-1"))

    assert count == 2
    assert deleted == [
        "meetings/user-1/meeting-a/0000.flac",
        "meetings/user-1/meeting-b/0000.flac",
    ]


def test_document_delete_recurses_through_session_turns():
    deleted: list[str] = []

    class _Ref:
        def __init__(self, name: str, children=None):
            self.name = name
            self._children = children or []

        def collections(self):
            return [_Collection(self._children)] if self._children else []

        def delete(self):
            deleted.append(self.name)

    class _Snap:
        def __init__(self, ref):
            self.reference = ref

    class _Collection:
        def __init__(self, refs):
            self._refs = refs

        def stream(self):
            return [_Snap(ref) for ref in self._refs]

    turn = _Ref("turn")
    session = _Ref("session", [turn])
    root = _Ref("UserAura", [session])

    account._delete_document_and_subcollections(None, root)

    assert deleted == ["turn", "session", "UserAura"]
