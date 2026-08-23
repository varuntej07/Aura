"""Versioned data-channel contract for exact, copyable desktop artifacts.

Speech never belongs in this payload. The voice worker may speak a short
acknowledgement or delivery result through the LiveKit audio path, but the
overlay receives only exact copyable content plus rendering metadata.

The top-level ``type`` and legacy payload aliases remain during the desktop
rollout so already-installed clients continue to render one card. New clients
read ``payload.artifact`` and ignore the aliases.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ARTIFACT_SCHEMA_VERSION = 2

ArtifactKind = Literal[
    "outbound_message",
    "command",
    "code",
    "config",
    "prompt",
    "steps",
    "checklist",
    "note",
]
ArtifactFormat = Literal["plain_text", "markdown", "code"]
DraftChannel = Literal["on_screen", "email_reply", "cold_dm", "snippet"]
DraftLength = Literal["short", "medium", "detailed"]


class OverlayArtifact(BaseModel):
    """The only object whose body may be rendered or copied by the overlay."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    kind: ArtifactKind
    channel: DraftChannel
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=32_000)
    format: ArtifactFormat
    language: str | None = Field(default=None, max_length=32)
    copy_mode: Literal["exact"] = "exact"
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def artifact_ready_event(
    *,
    request_id: str,
    artifact_id: str,
    revision: int,
    kind: ArtifactKind,
    channel: DraftChannel,
    length: DraftLength,
    title: str,
    body: str,
    content_format: ArtifactFormat,
    language: str | None,
    persisted: bool,
    context_summary: str = "",
    recipient_hint: str = "",
    skill_id: str = "general",
) -> dict:
    """Build one ready event with a strict v2 artifact and v1 aliases.

    ``context_summary`` and ``recipient_hint`` are legacy refine/dashboard
    fields. They are not part of ``artifact`` and must never be rendered as the
    copyable body.
    """

    artifact = OverlayArtifact(
        id=artifact_id,
        revision=revision,
        kind=kind,
        channel=channel,
        title=title,
        body=body,
        format=content_format,
        language=language or None,
        body_sha256=body_sha256(body),
    )
    artifact_payload = artifact.model_dump(mode="json")
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "event": "overlay.artifact.ready",
        "request_id": request_id,
        "type": "draft.created" if revision == 1 else "draft.updated",
        "payload": {
            "request_id": request_id,
            "artifact": artifact_payload,
            # Backward-compatible aliases for installed desktop clients.
            "draft_id": artifact.id,
            "revision": artifact.revision,
            "channel": artifact.channel,
            "length": length,
            "text": artifact.body,
            "context_summary": context_summary,
            "recipient_hint": recipient_hint,
            "skill_id": skill_id,
            "artifact_kind": None if kind == "outbound_message" else kind,
            "content_format": artifact.format,
            "title": artifact.title,
            "language": artifact.language or "",
            "persisted": persisted,
        },
    }


def artifact_generating_event(
    *,
    request_id: str,
    artifact_id: str,
    channel: DraftChannel,
    length: DraftLength,
    mode: Literal["new", "refine"],
    kind: ArtifactKind,
    title: str,
    skill_id: str = "general",
) -> dict:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "event": "overlay.artifact.generating",
        "request_id": request_id,
        "type": "draft.generating",
        "payload": {
            "request_id": request_id,
            "draft_id": artifact_id,
            "channel": channel,
            "length": length,
            "mode": mode,
            "skill_id": skill_id,
            "artifact": {
                "id": artifact_id,
                "kind": kind,
                "title": title,
            },
        },
    }


def artifact_failed_event(
    *,
    request_id: str,
    artifact_id: str | None,
    reason: str,
    retryable: bool,
) -> dict:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "event": "overlay.artifact.failed",
        "request_id": request_id,
        "type": "draft.failed",
        "payload": {
            "request_id": request_id,
            "draft_id": artifact_id,
            "reason": reason,
            "error": {
                "code": reason,
                "retryable": retryable,
            },
        },
    }
