"""Free-tier voice budget: entitlement round-trip + the warn-then-enforce task.

Guards the users/{uid}/usage/daily_voice {date, seconds} contract (writer and reader live in
entitlement.py) and the limit task's behavior: warn at T-60, ONE wind-down line at T-0,
then a server-side close (session.aclose before ctx.delete_room), with None (budget read
failed) disabling enforcement entirely.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.agent.voice.free_tier_limit import (
    FREE_TIER_VOICE_OUT_OF_TIME_INSTRUCTIONS,
    FREE_TIER_VOICE_WARNING_INSTRUCTIONS,
    FREE_TIER_VOICE_WIND_DOWN_INSTRUCTIONS,
    run_free_tier_voice_limit,
    run_out_of_free_time_close,
)
from src.services.entitlement import (
    FREE_TIER_DAILY_VOICE_SECONDS,
    get_remaining_free_voice_seconds,
)

_TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


def _mock_db_with_voice_doc(data: dict) -> MagicMock:
    """A firestore client mock whose users/{uid}/usage/daily_voice get() returns `data`."""
    db = MagicMock()
    snap = MagicMock()
    snap.to_dict.return_value = data
    (
        db.collection.return_value
        .document.return_value
        .collection.return_value
        .document.return_value
        .get.return_value
    ) = snap
    return db


# --- entitlement round-trip / remaining computation -------------------------------------------

def test_remaining_full_when_no_doc():
    with patch("src.services.firebase.admin_firestore", return_value=_mock_db_with_voice_doc({})):
        assert asyncio.run(get_remaining_free_voice_seconds("u")) == FREE_TIER_DAILY_VOICE_SECONDS


def test_remaining_full_when_stale_day():
    with patch(
        "src.services.firebase.admin_firestore",
        return_value=_mock_db_with_voice_doc({"date": "2000-01-01", "seconds": 590}),
    ):
        assert asyncio.run(get_remaining_free_voice_seconds("u")) == FREE_TIER_DAILY_VOICE_SECONDS


def test_remaining_subtracts_today_usage():
    with patch(
        "src.services.firebase.admin_firestore",
        return_value=_mock_db_with_voice_doc({"date": _TODAY, "seconds": 540}),
    ):
        assert asyncio.run(get_remaining_free_voice_seconds("u")) == 60


def test_remaining_clamped_to_zero_when_over():
    with patch(
        "src.services.firebase.admin_firestore",
        return_value=_mock_db_with_voice_doc({"date": _TODAY, "seconds": 700}),
    ):
        assert asyncio.run(get_remaining_free_voice_seconds("u")) == 0


def test_remaining_none_on_firestore_failure():
    # A read failure must return None so the caller never enforces (and never falsely warns).
    with patch("src.services.firebase.admin_firestore", side_effect=RuntimeError("boom")):
        assert asyncio.run(get_remaining_free_voice_seconds("u")) is None


# --- warn-then-enforce task --------------------------------------------------------------------

class _FakeSession:
    """Minimal AgentSession stand-in: settable agent_state queue, recorded
    generate_reply calls, and an aclose that logs into a shared event list."""

    def __init__(self, states: list[str], events: list[str] | None = None) -> None:
        self._states = list(states)
        self.generate_reply_calls: list[str] = []
        self.events = events if events is not None else []

    @property
    def agent_state(self) -> str:
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0] if self._states else ""

    async def generate_reply(self, *, instructions: str, **_kwargs) -> None:
        self.generate_reply_calls.append(instructions)
        self.events.append(f"reply:{instructions[:30]}")

    async def aclose(self) -> None:
        self.events.append("aclose")


class _FakeJobContext:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def delete_room(self) -> None:
        self.events.append("delete_room")


def _run_limit(monkeypatch, *, session, ctx, remaining):
    async def _noop_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)
    asyncio.run(
        run_free_tier_voice_limit(
            session, ctx, remaining_seconds=remaining, session_id="s", user_id="u"
        )
    )


def test_limit_warns_then_winds_down_and_closes(monkeypatch):
    events: list[str] = []
    sess = _FakeSession(["listening"], events)
    ctx = _FakeJobContext(events)
    _run_limit(monkeypatch, session=sess, ctx=ctx, remaining=70)

    assert sess.generate_reply_calls == [
        FREE_TIER_VOICE_WARNING_INSTRUCTIONS,
        FREE_TIER_VOICE_WIND_DOWN_INSTRUCTIONS,
    ]
    # The goodbye is fully spoken before the close, and the session closes
    # before the room is dropped.
    assert events[-2:] == ["aclose", "delete_room"]


def test_limit_waits_for_listening_before_warning(monkeypatch):
    events: list[str] = []
    sess = _FakeSession(["speaking", "speaking", "listening"], events)
    ctx = _FakeJobContext(events)
    _run_limit(monkeypatch, session=sess, ctx=ctx, remaining=120)
    assert sess.generate_reply_calls[0] == FREE_TIER_VOICE_WARNING_INSTRUCTIONS


def test_limit_disabled_when_remaining_none(monkeypatch):
    events: list[str] = []
    sess = _FakeSession(["listening"], events)
    ctx = _FakeJobContext(events)
    _run_limit(monkeypatch, session=sess, ctx=ctx, remaining=None)
    assert sess.generate_reply_calls == []
    assert events == []  # no close either: a read failure never cuts a call


def test_limit_enforces_immediately_when_over_budget(monkeypatch):
    # Defense in depth: voice_agent routes remaining<=0 to the out-of-time path,
    # but if the countdown task ever gets it, it must enforce, not warn.
    events: list[str] = []
    sess = _FakeSession(["listening"], events)
    ctx = _FakeJobContext(events)
    _run_limit(monkeypatch, session=sess, ctx=ctx, remaining=0)
    assert sess.generate_reply_calls == [FREE_TIER_VOICE_OUT_OF_TIME_INSTRUCTIONS]
    assert events[-2:] == ["aclose", "delete_room"]


def test_out_of_time_close_speaks_then_closes(monkeypatch):
    async def _noop_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)
    events: list[str] = []
    sess = _FakeSession(["listening"], events)
    ctx = _FakeJobContext(events)
    asyncio.run(run_out_of_free_time_close(sess, ctx, session_id="s", user_id="u"))
    assert sess.generate_reply_calls == [FREE_TIER_VOICE_OUT_OF_TIME_INSTRUCTIONS]
    assert events[-2:] == ["aclose", "delete_room"]


def test_room_delete_failure_is_swallowed(monkeypatch):
    # aclose already released the entrypoint; a room-delete error must not raise.
    async def _noop_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)
    events: list[str] = []
    sess = _FakeSession(["listening"], events)

    class _FailingCtx:
        async def delete_room(self) -> None:
            raise RuntimeError("room already gone")

    asyncio.run(run_out_of_free_time_close(sess, _FailingCtx(), session_id="s", user_id="u"))
    assert events[-1] == "aclose"
