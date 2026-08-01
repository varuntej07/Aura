"""Strict separation between overlay artifacts and spoken lifecycle text."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from src.agent.voice.artifact_contract import (
    ARTIFACT_SCHEMA_VERSION,
    OverlayArtifact,
    artifact_ready_event,
)


def test_ready_event_has_exact_copy_body_and_no_speech_fields():
    body = "Hi Sarah,\n\nThanks for thinking of me. I have to pass this week."
    event = artifact_ready_event(
        request_id="a" * 32,
        artifact_id="b" * 32,
        revision=1,
        kind="outbound_message",
        channel="email_reply",
        length="short",
        title="Draft reply",
        body=body,
        content_format="plain_text",
        language=None,
        persisted=True,
        context_summary="Private refine context.",
        recipient_hint="Sarah",
    )

    assert event["schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert event["event"] == "overlay.artifact.ready"
    artifact = event["payload"]["artifact"]
    assert artifact["body"] == body
    assert artifact["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert artifact["copy_mode"] == "exact"
    assert not {"speech", "say", "acknowledgement"} & artifact.keys()
    assert "context_summary" not in artifact


def test_artifact_schema_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        OverlayArtifact(
            id="b" * 32,
            revision=1,
            kind="prompt",
            channel="snippet",
            title="Prompt",
            body="Exact prompt",
            format="markdown",
            copy_mode="exact",
            body_sha256=hashlib.sha256(b"Exact prompt").hexdigest(),
            speech="Here it is",
        )
