"""
Coverage for the outbound drafter + POST /desktop/draft-outbound/refine handler.

Pins the contracts that matter for a screen-derived, privacy-sensitive surface:
  - invalid channel/length/inputs return coded reasons without a model call;
  - the initial draft rides the expert tier WITH the frame image, refines ride
    the balanced tier text-only;
  - the voice digest shapes the system prompt; an empty profile falls back to
    the default voice paragraph, never an error;
  - a model timeout or failure returns an empty result with a coded reason;
  - screen-derived context is wrapped in <untrusted_input> on the refine path;
  - the handler requires auth, validates input, and its funnel event never
    carries the draft text or anything read off the screen.
"""

from __future__ import annotations

import asyncio

from src.handlers import draft_outbound as handler
from src.services.outbound_draft import drafter


class _FakeProvider:
    """Stand-in for the ModelProvider singleton: records expert()/balanced()
    calls and returns a canned draft (or raises a supplied error)."""

    def __init__(self, message="hey, sounds great", summary="reply to Sarah", raises=None):
        self._message = message
        self._summary = summary
        self._raises = raises
        self.expert_calls: list[dict] = []
        self.balanced_calls: list[dict] = []

    async def expert(
        self, prompt, *, system=None, images=None, response_model=None, temperature=0.7
    ):
        self.expert_calls.append({"prompt": prompt, "system": system, "images": images})
        if self._raises is not None:
            raise self._raises
        return drafter._DraftOutput(message=self._message, context_summary=self._summary)

    async def balanced(
        self, prompt, *, system=None, response_model=None, temperature=0.7
    ):
        self.balanced_calls.append({"prompt": prompt, "system": system})
        if self._raises is not None:
            raise self._raises
        # A frameless snippet draft rides balanced() with the DRAFT shape.
        if response_model is drafter._DraftOutput:
            return drafter._DraftOutput(
                message=self._message, context_summary=self._summary
            )
        return drafter._RefineOutput(message=self._message)


class _Req:
    """Minimal stand-in for a FastAPI Request: only json() is read (auth is
    monkeypatched, so headers are irrelevant)."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


_VALID_REFINE_BODY = {
    "channel": "email_reply",
    "length": "short",
    "prior_draft": "Hi Sarah, thanks for the invite but I have to pass.",
    "refine_instruction": "warmer",
    "context_summary": "Declining Sarah's invite to the Friday sync.",
    "instruction_kind": "warmer",
}


# --- Drafter unit tests ----------------------------------------------------------


async def test_invalid_channel_skips_model(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="carrier_pigeon",
        length="short",
        recipient_hint="",
        intent="say hi",
        jpeg_base64="abc",
        jpeg_width=100,
        jpeg_height=100,
        voice_lines=[],
        display_name="",
    )

    assert result.reason == drafter.REASON_INVALID
    assert fake.expert_calls == []


async def test_missing_frame_skips_model(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="email_reply",
        length="short",
        recipient_hint="",
        intent="decline",
        jpeg_base64="",
        jpeg_width=None,
        jpeg_height=None,
        voice_lines=[],
        display_name="",
    )

    assert result.reason == drafter.REASON_NO_FRAME
    assert fake.expert_calls == []


async def test_draft_rides_expert_with_frame_and_voice(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="email_reply",
        length="medium",
        recipient_hint="Sarah",
        intent="politely decline",
        jpeg_base64="ZmFrZWpwZWc=",
        jpeg_width=1280,
        jpeg_height=720,
        voice_lines=["Their natural register is terse and to the point."],
        display_name="Varun",
    )

    assert result.reason == drafter.REASON_OK
    assert result.text == "hey, sounds great"
    assert result.context_summary == "reply to Sarah"
    call = fake.expert_calls[0]
    # The frame went along as an image, base64 untouched.
    assert call["images"] == [{"media_type": "image/jpeg", "data": "ZmFrZWpwZWc="}]
    # Voice digest and length norm shaped the system prompt.
    assert "terse and to the point" in call["system"]
    assert "80-120 words" in call["system"]
    # Spoken hints reached the user prompt.
    assert "Sarah" in call["prompt"] and "politely decline" in call["prompt"]


async def test_empty_profile_gets_default_voice(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    await drafter.draft_outbound(
        "uid1",
        channel="cold_dm",
        length="short",
        recipient_hint="this recruiter",
        intent="ask about the role",
        jpeg_base64="ZmFrZQ==",
        jpeg_width=None,
        jpeg_height=None,
        voice_lines=[],
        display_name="",
    )

    system = fake.expert_calls[0]["system"]
    assert "Default voice" in system
    assert "The user's writing voice" not in system


async def test_draft_timeout_returns_coded_reason(monkeypatch):
    fake = _FakeProvider(raises=asyncio.TimeoutError())
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="email_reply",
        length="short",
        recipient_hint="",
        intent="decline",
        jpeg_base64="ZmFrZQ==",
        jpeg_width=None,
        jpeg_height=None,
        voice_lines=[],
        display_name="",
    )

    assert result.text == ""
    assert result.reason == drafter.REASON_TIMEOUT


async def test_blank_model_message_is_model_error(monkeypatch):
    fake = _FakeProvider(message="   ")
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="email_reply",
        length="short",
        recipient_hint="",
        intent="decline",
        jpeg_base64="ZmFrZQ==",
        jpeg_width=None,
        jpeg_height=None,
        voice_lines=[],
        display_name="",
    )

    assert result.reason == drafter.REASON_MODEL_ERROR


async def test_refine_rides_balanced_with_untrusted_context(monkeypatch):
    fake = _FakeProvider(message="Hi Sarah, thank you so much for thinking of me.")
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    summary = "Declining Sarah's invite to the Friday sync."
    result = await drafter.refine_outbound(
        "uid1",
        channel="email_reply",
        length="short",
        prior_draft="Hi Sarah, thanks but I have to pass.",
        refine_instruction="warmer",
        context_summary=summary,
        voice_lines=[],
    )

    assert result.reason == drafter.REASON_OK
    assert result.text.startswith("Hi Sarah")
    # The summary rides back unchanged so the client keeps a stable context.
    assert result.context_summary == summary
    assert fake.expert_calls == []  # refines never touch the expert tier
    prompt = fake.balanced_calls[0]["prompt"]
    # Prior draft is delimited, and the screen-derived summary is untrusted-wrapped.
    assert "<prior_draft>" in prompt
    assert drafter._UNTRUSTED_INPUT_OPEN in prompt
    assert summary in prompt
    assert "warmer" in prompt


async def test_refine_requires_prior_draft_and_instruction(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.refine_outbound(
        "uid1",
        channel="email_reply",
        length="short",
        prior_draft="   ",
        refine_instruction="warmer",
        context_summary="",
        voice_lines=[],
    )

    assert result.reason == drafter.REASON_INVALID
    assert fake.balanced_calls == []


async def test_snippet_without_frame_rides_balanced(monkeypatch):
    fake = _FakeProvider(
        message='Add-Content $PROFILE "Set-Location C:\\Users\\varun\\MobileApps"',
        summary="Appends a Set-Location line to the PowerShell profile.",
    )
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="snippet",
        length="short",
        recipient_hint="",
        intent="make PowerShell open in MobileApps by default",
        jpeg_base64="",
        jpeg_width=None,
        jpeg_height=None,
        voice_lines=["Their natural register is terse and to the point."],
        display_name="Varun",
    )

    assert result.reason == drafter.REASON_OK
    assert result.text.startswith("Add-Content")
    # No frame: text-only balanced tier, never expert.
    assert fake.expert_calls == []
    call = fake.balanced_calls[0]
    # The snippet system prompt has no persona and no length ladder.
    system = call["system"]
    assert "runnable" in system
    assert "writing voice" not in system and "Default voice" not in system
    assert "50 words" not in system
    # The user prompt is the spoken spec, with no screenshot or sign-off lines.
    prompt = call["prompt"]
    assert "MobileApps" in prompt
    assert "screenshot" not in prompt.lower()
    assert "Varun" not in prompt


async def test_snippet_with_frame_rides_expert(monkeypatch):
    fake = _FakeProvider(message="npm run tauri dev", summary="Runs the app.")
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.draft_outbound(
        "uid1",
        channel="snippet",
        length="short",
        recipient_hint="",
        intent="the command to run this app",
        jpeg_base64="ZmFrZQ==",
        jpeg_width=1280,
        jpeg_height=720,
        voice_lines=[],
        display_name="",
    )

    assert result.reason == drafter.REASON_OK
    assert fake.balanced_calls == []
    call = fake.expert_calls[0]
    assert call["images"] == [{"media_type": "image/jpeg", "data": "ZmFrZQ=="}]
    assert "screenshot" in call["prompt"]


async def test_snippet_refine_is_valid_and_skips_length(monkeypatch):
    fake = _FakeProvider(message="setx WORKSPACE C:\\Users\\varun\\MobileApps")
    monkeypatch.setattr(drafter, "get_model_provider", lambda: fake)

    result = await drafter.refine_outbound(
        "uid1",
        channel="snippet",
        length="short",
        prior_draft='Add-Content $PROFILE "Set-Location ..."',
        refine_instruction="use setx instead",
        context_summary="Appends a Set-Location line to the profile.",
        voice_lines=[],
    )

    assert result.reason == drafter.REASON_OK
    prompt = fake.balanced_calls[0]["prompt"]
    # The length ladder is message-only; a snippet refine must not carry it.
    assert "TARGET LENGTH" not in prompt
    assert "use setx instead" in prompt


def test_writing_voice_lines_maps_tone_and_caps(monkeypatch):
    monkeypatch.setattr(
        drafter,
        "interest_prompt_lines",
        lambda profile, *a, **k: [f"interest {i}" for i in range(10)],
    )

    lines = drafter.writing_voice_lines({"dominant_tone": "terse"})

    assert lines[0] == "Their natural register is terse and to the point."
    assert len(lines) == drafter.VOICE_LINES_MAX


def test_writing_voice_lines_empty_profile():
    assert drafter.writing_voice_lines({}) == []


# --- Handler tests ---------------------------------------------------------------


async def test_handler_requires_auth(monkeypatch):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda r: None)
    resp = await handler.handle_draft_outbound_refine(_Req(_VALID_REFINE_BODY))
    assert resp.status_code == 401


async def test_handler_rejects_missing_prior_draft(monkeypatch):
    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda r: "uid1")
    body = dict(_VALID_REFINE_BODY)
    del body["prior_draft"]
    resp = await handler.handle_draft_outbound_refine(_Req(body))
    assert resp.status_code == 400


async def test_handler_analytics_never_carries_draft_content(monkeypatch):
    secret = "MEET ME AT 5 BEHIND THE BANK"
    captured: dict = {}

    async def _fake_refine(uid, **kwargs):
        return drafter.OutboundDraftResult(
            text="all good, see you there", reason=drafter.REASON_OK
        )

    async def _fake_capture(*, distinct_id, event, properties=None):
        captured["event"] = event
        captured["properties"] = properties or {}

    async def _fake_fetch(uid):
        return {}, []

    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda r: "uid1")
    monkeypatch.setattr(handler, "refine_outbound", _fake_refine)
    monkeypatch.setattr(handler, "capture_event", _fake_capture)
    monkeypatch.setattr(handler, "fetch_cached_aura_data", _fake_fetch)

    body = dict(_VALID_REFINE_BODY)
    body["prior_draft"] = secret
    body["context_summary"] = secret
    body["instruction_kind"] = "definitely_not_a_chip"
    resp = await handler.handle_draft_outbound_refine(_Req(body))

    assert resp.status_code == 200
    assert captured["event"] == "desktop_draft_refined"
    # Only breakdown dimensions are captured; the draft leaks nowhere.
    assert set(captured["properties"].keys()) == {
        "channel", "length", "mode", "instruction_kind",
    }
    assert captured["properties"]["instruction_kind"] == "custom"
    assert secret not in str(captured)


async def test_handler_profile_read_failure_degrades_gracefully(monkeypatch):
    seen: dict = {}

    async def _fake_refine(uid, **kwargs):
        seen["voice_lines"] = kwargs["voice_lines"]
        return drafter.OutboundDraftResult(text="ok", reason=drafter.REASON_OK)

    async def _fake_capture(**kwargs):
        return None

    async def _boom(uid):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(handler, "resolve_user_id_from_request", lambda r: "uid1")
    monkeypatch.setattr(handler, "refine_outbound", _fake_refine)
    monkeypatch.setattr(handler, "capture_event", _fake_capture)
    monkeypatch.setattr(handler, "fetch_cached_aura_data", _boom)

    resp = await handler.handle_draft_outbound_refine(_Req(_VALID_REFINE_BODY))

    assert resp.status_code == 200
    assert seen["voice_lines"] == []
