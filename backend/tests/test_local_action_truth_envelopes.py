"""Action Truth envelopes returned by local, in-process voice tools."""

from __future__ import annotations

from types import SimpleNamespace

from src.agent import buddy_agent as buddy


class _Span:
    def finish(self, **_kwargs):
        return None


async def test_visible_artifact_returns_card_post_call_contract(monkeypatch):
    async def _present(**_kwargs):
        return buddy.SPOKEN_ARTIFACT_READY

    monkeypatch.setattr(buddy, "_present_visible_artifact", _present)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    agent = SimpleNamespace(_user_id="u", _session_id="s")

    result = await buddy.BuddyAgent.present_visible_artifact.__wrapped__(
        agent,
        None,
        "prompt",
        "Investigate",
        "Find the root cause.",
    )

    assert result["ok"] is True
    assert result["say"] == buddy.SPOKEN_ARTIFACT_READY
    assert result["render"] == {"mode": "verbatim", "channel": "card"}
    assert "never recite, preview, or summarize the artifact" in result["then"]


async def test_visible_artifact_failure_never_claims_card_rendered(monkeypatch):
    async def _present(**_kwargs):
        return "I couldn't get the card onto your screen."

    monkeypatch.setattr(buddy, "_present_visible_artifact", _present)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    agent = SimpleNamespace(_user_id="u", _session_id="s")

    result = await buddy.BuddyAgent.present_visible_artifact.__wrapped__(
        agent,
        None,
        "prompt",
        "Investigate",
        "Find the root cause.",
    )

    assert result["ok"] is False
    assert result["render"] == {"mode": "verbatim", "channel": "voice"}
    assert result["then"] == "Speak only `say` and do not imply a card is visible."


async def test_outbound_draft_returns_card_silence_contract(monkeypatch):
    captured = {}

    async def _draft(*_args, **kwargs):
        captured.update(kwargs)
        return buddy.SPOKEN_DRAFT_READY

    monkeypatch.setattr(buddy, "run_draft_tool", _draft)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    agent = SimpleNamespace(
        _user_id="u",
        _draft_outbound=object(),
        _screen_frames=object(),
        _finalized_transcript="decline politely",
    )

    result = await buddy.BuddyAgent.draft_outbound_message.__wrapped__(
        agent,
        None,
        {"operation": "new"},
    )

    assert result["ok"] is True
    assert captured["operation"] == "new"
    assert captured["transcript"] == "decline politely"
    assert result["render"] == {"mode": "verbatim", "channel": "card"}
    assert "never recite, preview, or summarize the draft" in result["then"]


async def test_guide_result_keeps_activation_caveat_with_result(monkeypatch):
    async def _request(**_kwargs):
        return "Starting guide mode."

    monkeypatch.setattr(buddy, "request_guide_mode", _request)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    agent = SimpleNamespace(_user_id="u", _session_id="s")

    result = await buddy.BuddyAgent.set_guide_mode.__wrapped__(agent, True)

    assert result["ok"] is True
    assert result["render"] == {"mode": "verbatim", "channel": "voice"}
    assert "not that Guide Mode is already active" in result["then"]
