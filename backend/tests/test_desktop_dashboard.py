"""Desktop dashboard route contracts and owner-scoped handler projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from src.handlers import desktop_dashboard, desktop_profile
from src.main import app


def _client() -> TestClient:
    return TestClient(app)


def _as_owner(monkeypatch, uid: str = "user-a") -> None:
    monkeypatch.setattr(desktop_dashboard, "resolve_user_id_from_request", lambda _request: uid)
    monkeypatch.setattr(desktop_profile, "resolve_user_id_from_request", lambda _request: uid)


def test_desktop_routes_require_authentication():
    client = _client()
    for path in (
        "/devices/profile",
        "/desktop/home/stats",
        "/desktop/activity",
        "/desktop/conversations",
        "/desktop/saved",
        "/desktop/usage",
    ):
        response = client.post(path, json={}) if path == "/devices/profile" else client.get(path)
        assert response.status_code == 401


def test_profile_writes_only_authenticated_users_record(monkeypatch):
    _as_owner(monkeypatch, "user-a")
    writes: list[tuple[str, dict, bool]] = []

    class _Doc:
        def __init__(self, uid: str):
            self.uid = uid

        def set(self, data, merge=False):
            writes.append((self.uid, data, merge))

    class _Users:
        def document(self, uid):
            return _Doc(uid)

    class _Db:
        def collection(self, _name):
            return _Users()

    monkeypatch.setattr(desktop_profile, "admin_firestore", lambda: _Db())
    response = _client().post(
        "/devices/profile",
        json={
            "where_heard": "friend",
            "where_heard_other": None,
            "role": "developer",
            "role_other": None,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert writes == [
        (
            "user-a",
            {
                "where_heard": "friend",
                "where_heard_other": None,
                "role": "developer",
                "role_other": None,
            },
            True,
        )
    ]


def test_dashboard_empty_states_have_exact_valid_shapes(monkeypatch):
    _as_owner(monkeypatch)

    async def no_sessions(_uid, _limit):
        return []

    async def no_drafts(_uid, *, limit):
        return []

    async def no_atoms(_uid, *, limit):
        return []

    monkeypatch.setattr(desktop_dashboard, "_recent_voice_sessions", no_sessions)
    monkeypatch.setattr(desktop_dashboard.draft_store, "list_drafts", no_drafts)
    monkeypatch.setattr(desktop_dashboard, "list_atoms", no_atoms)
    client = _client()
    assert client.get("/desktop/home/stats").json() == {
        "last_used_at": None,
        "last_session_seconds": None,
        "sessions_this_week": 0,
    }
    assert client.get("/desktop/activity").json() == {"items": []}
    assert client.get("/desktop/conversations").json() == {"items": []}
    assert client.get("/desktop/saved").json() == {"items": []}


def test_dashboard_projects_only_the_callers_data_and_shapes(monkeypatch):
    _as_owner(monkeypatch, "user-a")
    now = datetime.now(UTC)
    seen: list[str] = []

    async def sessions(uid, _limit):
        seen.append(uid)
        return (
            [
                {
                    "id": "a-voice",
                    "surface": "desktop",
                    "started_at": now.isoformat(),
                    "duration_ms": 90_000,
                    "recap": "Discussed the launch.",
                },
            ]
            if uid == "user-a"
            else [{"id": "b-voice", "surface": "desktop", "started_at": now.isoformat()}]
        )

    async def drafts(uid, *, limit):
        seen.append(uid)
        return [
            {
                "draft_id": "a-draft",
                "channel": "email_reply",
                "text": "Hello there",
                "created_at": (now - timedelta(minutes=1)).isoformat(),
                "updated_at": now.isoformat(),
            }
        ]

    async def atoms(uid, *, limit):
        seen.append(uid)
        return [
            {
                "id": "a-memory",
                "text": "Prefers morning calls",
                "atom_type": "fact",
                "categories": [],
                "last_seen": (now - timedelta(minutes=2)).isoformat(),
            }
        ]

    monkeypatch.setattr(desktop_dashboard, "_recent_voice_sessions", sessions)
    monkeypatch.setattr(desktop_dashboard.draft_store, "list_drafts", drafts)
    monkeypatch.setattr(desktop_dashboard, "list_atoms", atoms)
    client = _client()
    stats = client.get("/desktop/home/stats").json()
    assert stats == {
        "last_used_at": now.isoformat(),
        "last_session_seconds": 90,
        "sessions_this_week": 1,
    }
    activity = client.get("/desktop/activity?limit=50").json()["items"]
    assert [item["id"] for item in activity] == ["a-voice", "a-draft", "a-memory"]
    conversations = client.get("/desktop/conversations").json()["items"]
    assert conversations == [
        {
            "id": "a-voice",
            "title": "Voice conversation",
            "preview": "Discussed the launch.",
            "started_at": now.isoformat(),
            "duration_seconds": 90,
        }
    ]
    saved = client.get("/desktop/saved").json()["items"]
    assert saved == [
        {
            "id": "a-memory",
            "label": "Prefers morning calls",
            "value": None,
            "saved_at": (now - timedelta(minutes=2)).isoformat(),
        }
    ]
    assert seen and set(seen) == {"user-a"}


def test_desktop_usage_reuses_daily_quota_counters(monkeypatch):
    _as_owner(monkeypatch)

    async def entitlement(_uid):
        return {"tier": "free", "status": "expired"}

    def usage(uid, doc_id):
        assert uid == "user-a"
        return {
            "date": datetime.now(UTC).strftime("%Y-%m-%d"),
            ("seconds" if doc_id == "daily_voice" else "count"): 120
            if doc_id == "daily_voice"
            else 3,
        }

    monkeypatch.setattr(desktop_dashboard, "ensure_entitlement_doc", entitlement)
    monkeypatch.setattr(desktop_dashboard, "_usage_doc", usage)
    payload = _client().get("/desktop/usage").json()
    assert payload["voice_minutes_used"] == 2
    assert payload["voice_minutes_limit"] == 10
    assert payload["drafts_used"] == 3
    assert payload["drafts_limit"] == 5
    assert payload["period_start"].endswith("+00:00")
    assert payload["period_end"].endswith("+00:00")
