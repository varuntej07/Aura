"""POST /threads/reply — the silent shade-reply ingest path.

Covers auth, validation, and the happy path: the user's answer and Buddy's reply
are both persisted to the server-authoritative thread conversation, the thread is
flipped to engaged, the aura extractor is fired, and Buddy's reply is returned for
the notification shade.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from src.handlers import threads as handler
from src.services.threads.models import ThreadStatus


class _FakeRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _patch_common(monkeypatch, *, uid="u_1", reply_text="love that, tell me more"):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda req: uid)
    monkeypatch.setattr(handler.thread_store, "get_thread", AsyncMock(return_value=None))
    append = AsyncMock()
    set_status = AsyncMock()
    monkeypatch.setattr(handler.thread_store, "append_message", append)
    monkeypatch.setattr(handler.thread_store, "set_status", set_status)
    monkeypatch.setattr(handler, "get_model_provider", lambda: MagicMock())
    monkeypatch.setattr(handler, "generate_thread_reply", AsyncMock(return_value=reply_text))
    monkeypatch.setattr(handler, "extract_and_update_user_aura", AsyncMock(return_value=None))
    # Buddy's reply now also goes out as a follow-up push; stub it so no real FCM
    # send happens. Tests read it back via ``handler.send_notification``.
    monkeypatch.setattr(handler, "send_notification", AsyncMock(return_value=None))
    return append, set_status


async def test_unauthorized_without_user(monkeypatch):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda req: None)
    resp = await handler.handle_thread_reply(_FakeRequest({"thread_id": "t", "reply": "x"}))
    assert resp.status_code == 401


async def test_missing_thread_id_is_400(monkeypatch):
    _patch_common(monkeypatch)
    resp = await handler.handle_thread_reply(_FakeRequest({"reply": "x"}))
    assert resp.status_code == 400


async def test_missing_reply_is_400(monkeypatch):
    _patch_common(monkeypatch)
    resp = await handler.handle_thread_reply(_FakeRequest({"thread_id": "t1"}))
    assert resp.status_code == 400


async def test_invalid_json_is_400(monkeypatch):
    _patch_common(monkeypatch)
    resp = await handler.handle_thread_reply(_FakeRequest(ValueError("bad json")))
    assert resp.status_code == 400


async def test_happy_path_persists_both_turns_and_returns_reply(monkeypatch):
    append, set_status = _patch_common(monkeypatch, reply_text="oh nice, what's it for?")

    resp = await handler.handle_thread_reply(_FakeRequest({
        "thread_id": "rem_77",
        "question": "what are you building?",
        "reply": "a side project",
    }))

    assert resp.status_code == 200
    assert json.loads(resp.body)["reply"] == "oh nice, what's it for?"

    # Two turns persisted: the user's answer, then Buddy's reply.
    roles = [c.kwargs["role"] for c in append.await_args_list]
    assert roles == ["user", "assistant"]

    # Thread flipped to engaged.
    status_arg = set_status.await_args_list[0].args[2]
    assert status_arg == ThreadStatus.ENGAGED


async def test_buddy_reply_pushed_to_shade_as_followup(monkeypatch):
    _patch_common(monkeypatch, reply_text="oh nice, what's it for?")

    resp = await handler.handle_thread_reply(_FakeRequest({
        "thread_id": "rem_88",
        "question": "what are you building?",
        "reply": "a side project",
    }))

    assert resp.status_code == 200

    # Buddy's reply is delivered as a data-only thread_followup push tagged
    # kind=reply, so the client updates the shade even if its isolate was reaped.
    push = handler.send_notification
    push.assert_awaited_once()
    assert push.await_args.args[0] == "u_1"  # user_id
    kwargs = push.await_args.kwargs
    assert kwargs["notification_type"] == "thread_followup"
    assert kwargs["data_only"] is True
    assert kwargs["collapse_key"] == "thread_rem_88"
    assert kwargs["data"]["kind"] == "reply"
    assert kwargs["data"]["thread_id"] == "rem_88"
    assert kwargs["data"]["buddy_reply"] == "oh nice, what's it for?"


async def test_push_failure_does_not_fail_reply(monkeypatch):
    _patch_common(monkeypatch, reply_text="got it")
    monkeypatch.setattr(handler, "send_notification", AsyncMock(side_effect=RuntimeError("fcm down")))

    resp = await handler.handle_thread_reply(_FakeRequest({
        "thread_id": "rem_99",
        "reply": "yes",
    }))

    # The answer was still persisted and the endpoint still returns Buddy's reply.
    assert resp.status_code == 200
    assert json.loads(resp.body)["reply"] == "got it"


async def test_reply_too_long_is_400(monkeypatch):
    _patch_common(monkeypatch)
    resp = await handler.handle_thread_reply(_FakeRequest({
        "thread_id": "t1",
        "reply": "x" * (handler.MAX_REPLY_CHARS + 1),
    }))
    assert resp.status_code == 400


# ── GET /threads/{id}/messages ───────────────────────────────────────────────

async def test_messages_unauthorized(monkeypatch):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda req: None)
    resp = await handler.handle_thread_messages(_FakeRequest({}), "t1")
    assert resp.status_code == 401


async def test_messages_missing_thread_id_is_400(monkeypatch):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda req: "u_1")
    resp = await handler.handle_thread_messages(_FakeRequest({}), "  ")
    assert resp.status_code == 400


async def test_messages_returns_conversation(monkeypatch):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda req: "u_1")
    convo = [
        {"role": "assistant", "content": "what are you building?", "created_at": "2026-06-10T12:00:00+00:00"},
        {"role": "user", "content": "a side project", "created_at": "2026-06-10T12:01:00+00:00"},
    ]
    monkeypatch.setattr(handler.thread_store, "list_messages", AsyncMock(return_value=convo))
    resp = await handler.handle_thread_messages(_FakeRequest({}), "rem_77")
    assert resp.status_code == 200
    assert json.loads(resp.body)["messages"] == convo
