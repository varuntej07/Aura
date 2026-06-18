"""Free-tier voice budget: entitlement round-trip + the warn nudge coroutine.

Guards the users/{uid}/usage/daily_voice {date, seconds} contract (writer and reader live in
entitlement.py) and the nudge's fire/skip/turn-boundary behavior.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.agent.voice.free_tier_limit import (
    FREE_TIER_VOICE_WARNING_INSTRUCTIONS,
    run_free_tier_voice_nudge,
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
    # A read failure must return None so the caller SKIPS the nudge, never falsely warns.
    with patch("src.services.firebase.admin_firestore", side_effect=RuntimeError("boom")):
        assert asyncio.run(get_remaining_free_voice_seconds("u")) is None


# --- nudge coroutine --------------------------------------------------------------------------

class _FakeSession:
    """Minimal AgentSession stand-in: settable agent_state queue + recorded generate_reply calls."""

    def __init__(self, states: list[str]) -> None:
        self._states = list(states)
        self.generate_reply_calls: list[str] = []

    @property
    def agent_state(self) -> str:
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0] if self._states else ""

    async def generate_reply(self, *, instructions: str) -> None:
        self.generate_reply_calls.append(instructions)


def _run_nudge(monkeypatch, *, session, remaining):
    async def _noop_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)
    asyncio.run(
        run_free_tier_voice_nudge(
            session, remaining_seconds=remaining, session_id="s", user_id="u"
        )
    )


def test_nudge_fires_once_when_listening(monkeypatch):
    sess = _FakeSession(["listening"])
    _run_nudge(monkeypatch, session=sess, remaining=70)
    assert sess.generate_reply_calls == [FREE_TIER_VOICE_WARNING_INSTRUCTIONS]


def test_nudge_waits_for_listening_then_fires(monkeypatch):
    sess = _FakeSession(["speaking", "speaking", "listening"])
    _run_nudge(monkeypatch, session=sess, remaining=120)
    assert sess.generate_reply_calls == [FREE_TIER_VOICE_WARNING_INSTRUCTIONS]


def test_nudge_skipped_when_remaining_none(monkeypatch):
    sess = _FakeSession(["listening"])
    _run_nudge(monkeypatch, session=sess, remaining=None)
    assert sess.generate_reply_calls == []


def test_nudge_skipped_when_already_over_budget(monkeypatch):
    sess = _FakeSession(["listening"])
    _run_nudge(monkeypatch, session=sess, remaining=0)
    assert sess.generate_reply_calls == []
