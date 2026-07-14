"""Visible artifact validation, compatibility, limits, and publish behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agent.voice import visible_artifacts as va


def test_command_event_is_legacy_compatible_and_ephemeral():
    event, reason = va.build_visible_artifact_event(
        kind="command",
        title="Fix execution policy",
        content="Set-ExecutionPolicy -Scope CurrentUser RemoteSigned",
        language="powershell",
    )
    assert reason is None and event is not None
    payload = event["payload"]
    assert event["type"] == "draft.created"
    assert payload["channel"] == "snippet"
    assert payload["length"] == "short"
    assert payload["text"] == "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
    assert payload["artifact_kind"] == "command"
    assert payload["content_format"] == "code"
    assert payload["persisted"] is False
    assert len(payload["draft_id"]) == 32


def test_prompt_and_steps_use_markdown_without_duplicate_display_copy():
    for kind in ("prompt", "steps", "checklist", "note"):
        event, reason = va.build_visible_artifact_event(
            kind=kind,
            title="Next steps",
            content="## Do this\n\n1. First\n2. Second",
            language="",
        )
        assert reason is None and event is not None
        assert event["payload"]["content_format"] == "markdown"
        assert "display_markdown" not in event["payload"]


def test_invalid_kind_empty_content_and_unsafe_language_are_bounded():
    event, reason = va.build_visible_artifact_event(
        kind="email",
        title="Nope",
        content="hello",
        language="",
    )
    assert event is None and reason == "invalid_request"

    event, reason = va.build_visible_artifact_event(
        kind="code",
        title="  A title with   extra spacing  ",
        content="print('ok')",
        language="python<script>",
    )
    assert reason is None and event is not None
    assert event["payload"]["title"] == "A title with extra spacing"
    assert event["payload"]["language"] == ""


def test_oversize_event_is_rejected_not_truncated():
    content = "x" * va.MAX_EVENT_UTF8_BYTES
    event, reason = va.build_visible_artifact_event(
        kind="prompt", title="Large prompt", content=content, language=""
    )
    assert event is None and reason == "too_large"


def test_copy_exact_code_preserves_leading_and_trailing_whitespace():
    content = "  function Test-It {\n    Write-Output 'ok'\n  }\n"
    event, reason = va.build_visible_artifact_event(
        kind="code", title="PowerShell function", content=content, language="powershell"
    )
    assert reason is None and event is not None
    assert event["payload"]["text"] == content


async def test_publish_success_sends_one_reliable_packet(monkeypatch):
    published: list[tuple[bytes, bool]] = []

    async def _publish(data, reliable):
        published.append((data, reliable))

    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=_publish))
    monkeypatch.setattr(va, "get_job_context", lambda: SimpleNamespace(room=room))

    spoken = await va.present_visible_artifact(
        user_id="u",
        session_id="s",
        kind="prompt",
        title="Investigate this",
        content="# Task\nFind the root cause.",
    )
    assert spoken == va.SPOKEN_ARTIFACT_READY
    assert len(published) == 1 and published[0][1] is True
    event = json.loads(published[0][0])
    assert event["payload"]["text"] == "# Task\nFind the root cause."


async def test_publish_failure_never_claims_the_card_is_visible(monkeypatch):
    async def _publish(_data, _reliable):
        raise RuntimeError("room disconnected")

    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=_publish))
    monkeypatch.setattr(va, "get_job_context", lambda: SimpleNamespace(room=room))

    spoken = await va.present_visible_artifact(
        user_id="u",
        session_id="s",
        kind="steps",
        title="Next steps",
        content="1. Retry\n2. Check logs",
    )
    assert spoken == va.SPOKEN_ARTIFACT_DELIVERY_FAILED
