"""Action Truth envelopes returned by local, in-process voice tools."""

from __future__ import annotations

import time
from functools import partial
from types import SimpleNamespace

import pytest
from livekit.agents import StopResponse

from src.agent import buddy_agent as buddy
from src.agent.voice.artifact_session import ArtifactSession
from src.agent.voice.guide_intent import (
    GuideDecisionIdentity,
    GuideIntentDecision,
    GuideIntentReason,
    GuideIntentRoute,
)


class _Span:
    def finish(self, **_kwargs):
        return None


class _Session:
    """Captures what Buddy spoke instead of synthesizing it."""

    def __init__(self):
        self.said = []

    def say(self, text, **_kwargs):
        self.said.append(text)
        return None


def _card_agent(**overrides):
    """A card-capable agent stub.

    `_artifact_delivery=None` is the "publish and assume" path, which is what
    these envelope tests are about; the delivery round trip has its own
    behaviour and is not in scope here.
    """
    agent = SimpleNamespace(
        _user_id="u",
        _session_id="s",
        _artifact_delivery=None,
        _artifact_session=ArtifactSession(),
        _turn_metrics=None,
        _action_telemetry=SimpleNamespace(turn_index=0),
    )
    for key, value in overrides.items():
        setattr(agent, key, value)
    # Bind the real acknowledgement path rather than stubbing it: whether
    # success ends the turn is exactly what these tests are pinning.
    agent._speak_card_ack = partial(buddy.BuddyAgent._speak_card_ack, agent)
    return agent


async def test_visible_artifact_speaks_ack_and_ends_turn(monkeypatch):
    """Success renders the card, speaks a short ack, and stops the turn.

    Ending the turn here is the point: it removes the second LLM generation,
    which was both a full round trip and the last place the model could recite
    the body it had just carded.
    """

    async def _present(**_kwargs):
        return buddy.SPOKEN_ARTIFACT_READY

    monkeypatch.setattr(buddy, "_present_visible_artifact", _present)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    agent = _card_agent()
    session = _Session()

    with pytest.raises(StopResponse):
        await buddy.BuddyAgent.present_visible_artifact.__wrapped__(
            agent,
            SimpleNamespace(session=session),
            "prompt",
            "Investigate",
            "Find the root cause.",
        )

    assert len(session.said) == 1
    spoken = session.said[0]
    # The acknowledgement, never the artifact.
    assert "Find the root cause." not in spoken
    assert len(spoken) < 60
    assert agent._artifact_session.is_open
    assert agent._artifact_session.body == "Find the root cause."


async def test_visible_artifact_falls_back_to_envelope_when_ack_cannot_speak(
    monkeypatch,
):
    """A card that rendered but could not be acked still returns the contract.

    Silence after a successful render would leave the user staring at a card
    with no idea it arrived, so the model is handed the turn back.
    """

    async def _present(**_kwargs):
        return buddy.SPOKEN_ARTIFACT_READY

    class _MuteSession:
        def say(self, _text, **_kwargs):
            raise RuntimeError("no audio output")

    monkeypatch.setattr(buddy, "_present_visible_artifact", _present)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())

    result = await buddy.BuddyAgent.present_visible_artifact.__wrapped__(
        _card_agent(),
        SimpleNamespace(session=_MuteSession()),
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
    agent = _card_agent()

    result = await buddy.BuddyAgent.present_visible_artifact.__wrapped__(
        agent,
        None,
        "prompt",
        "Investigate",
        "Find the root cause.",
    )

    assert result["ok"] is False
    assert not agent._artifact_session.is_open
    assert result["render"] == {"mode": "verbatim", "channel": "voice"}
    assert result["then"] == "Speak only `say` and do not imply a card is visible."


async def test_outbound_draft_briefs_drafter_with_whole_turn_not_last_fragment(
    monkeypatch,
):
    """The sub-drafter is briefed with the joined turn, not one STT fragment.

    Endpointing splits a spoken instruction across several finalized messages.
    Passing only the last one is how a refine came to be briefed with a
    fragment that carried none of the actual request.
    """
    captured = {}

    async def _draft(*_args, **kwargs):
        captured.update(kwargs)
        return buddy.SPOKEN_DRAFT_READY

    monkeypatch.setattr(buddy, "run_draft_tool", _draft)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    agent = _card_agent(
        _draft_outbound=SimpleNamespace(current=SimpleNamespace(text="Hi Kai,")),
        _screen_frames=object(),
        _finalized_transcript="Voice applications.",
        _finalized_turn_instruction=(
            "Make it longer, add a greeting and a hook. Voice applications."
        ),
        _current_turn_frame_context_id="",
    )
    session = _Session()

    with pytest.raises(StopResponse):
        await buddy.BuddyAgent.draft_outbound_message.__wrapped__(
            agent,
            SimpleNamespace(session=session),
            {"operation": "new"},
        )

    assert captured["operation"] == "new"
    assert captured["transcript"] == (
        "Make it longer, add a greeting and a hook. Voice applications."
    )
    # The draft body never reaches speech; only the acknowledgement does.
    assert session.said and "Hi Kai," not in session.said[0]
    assert agent._artifact_session.body == "Hi Kai,"


async def test_guide_result_keeps_activation_caveat_with_result(monkeypatch):
    async def _request(**_kwargs):
        return "Starting guide mode."

    monkeypatch.setattr(buddy, "request_guide_mode", _request)
    monkeypatch.setattr(buddy, "start_tool_span", lambda **_kwargs: _Span())
    identity = GuideDecisionIdentity.from_turn(
        message_id="m", transcript="Turn on Guide Mode.", turn_index=1, guide_arm_epoch=0
    )
    decision = GuideIntentDecision(
        route=GuideIntentRoute.START_GUIDE,
        task_summary="start screen guidance",
        evidence_quote="Turn on Guide Mode.",
        semantic_confidence=0.99,
        depends_on_previous_assistant_offer=False,
        previous_assistant_offer_confirmed=False,
        reason_code=GuideIntentReason.EXPLICIT_ONGOING_GUIDANCE,
        identity=identity,
        decision_id="decision",
        issued_at_monotonic=time.monotonic(),
        expires_at_monotonic=time.monotonic() + 10,
        valid=True,
    )
    agent = SimpleNamespace(
        _user_id="u",
        _session_id="s",
        _finalized_guide_decision=decision,
        _current_guide_identity=lambda: identity,
    )

    result = await buddy.BuddyAgent.set_guide_mode.__wrapped__(agent, True)

    assert result["ok"] is True
    assert result["render"] == {"mode": "verbatim", "channel": "voice"}
    assert "not that Guide Mode is already active" in result["then"]
