"""Tests for POST /devices/register (backend/src/handlers/devices.py)."""

from __future__ import annotations

from src.handlers import devices


class _Req:
    """Minimal stand-in for a FastAPI Request: only json() is read (auth is
    monkeypatched, so headers are irrelevant)."""

    client = None

    def __init__(self, body: dict | None = None):
        self._body = body or {}

    async def json(self):
        return self._body


async def test_register_calls_welcome_after_token_registered(monkeypatch):
    monkeypatch.setattr(devices, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(devices, "register_token", lambda uid, token, platform: None)
    calls = []

    async def _fake_welcome(user_id):
        calls.append(user_id)

    monkeypatch.setattr(devices, "maybe_send_welcome_notification", _fake_welcome)

    resp = await devices.register_device(_Req({"token": "tok123", "platform": "android"}))

    assert resp.status_code == 200
    assert calls == ["u1"]


async def test_welcome_failure_does_not_break_the_200(monkeypatch):
    # A notification-side failure (Firestore blip, FCM outage) must never turn a
    # successful token registration into a 500.
    monkeypatch.setattr(devices, "resolve_user_id_from_request", lambda r: "u1")
    monkeypatch.setattr(devices, "register_token", lambda uid, token, platform: None)

    async def _boom(user_id):
        raise RuntimeError("fcm down")

    monkeypatch.setattr(devices, "maybe_send_welcome_notification", _boom)

    resp = await devices.register_device(_Req({"token": "tok123", "platform": "android"}))

    assert resp.status_code == 200
