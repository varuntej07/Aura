"""Ephemeral visible text artifacts published from the voice worker.

The voice model already produced the exact content as structured tool
arguments. This module validates and publishes it directly, avoiding a second
LLM call and keeping commands, prompts, and multi-step guidance fast.

The wire event deliberately extends the existing ``draft.created`` shape. Old
Aura Desktop builds ignore the optional metadata and still show ``text`` as a
plain snippet. New builds use ``artifact_kind`` and ``content_format`` for a
richer renderer. These artifacts are never persisted or executed.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import TYPE_CHECKING

from livekit.agents import get_job_context

if TYPE_CHECKING:
    from .artifact_delivery import ArtifactDeliveryTracker

from ...lib.logger import logger
from .artifact_contract import artifact_ready_event, new_request_id

ARTIFACT_KINDS: frozenset[str] = frozenset(
    {"command", "code", "config", "prompt", "steps", "checklist", "note"}
)
CODE_ARTIFACT_KINDS: frozenset[str] = frozenset({"command", "code", "config"})

# LiveKit recommends reliable data packets stay under 15 KiB. Keep a full KiB
# for routing headers and future optional metadata, and reject rather than
# silently truncate copy-exact content.
MAX_EVENT_UTF8_BYTES = 14 * 1024
MAX_TITLE_CHARS = 80
MAX_LANGUAGE_CHARS = 32
PUBLISH_TIMEOUT_S = 1.5
_LANGUAGE = re.compile(r"^[A-Za-z0-9_+.-]+$")

SPOKEN_ARTIFACT_READY = "Done, it's on your screen."
SPOKEN_ARTIFACT_INVALID = "I couldn't format that for the screen. Give me one more try?"
SPOKEN_ARTIFACT_TOO_LARGE = "That's too long for one card. Ask me to split it into smaller parts."
SPOKEN_ARTIFACT_DELIVERY_FAILED = "I couldn't get the card onto your screen. Give me one more try?"


def _normalized_language(value: str) -> str:
    language = (value or "").strip()[:MAX_LANGUAGE_CHARS]
    return language if not language or _LANGUAGE.fullmatch(language) else ""


def build_visible_artifact_event(
    *, kind: str, title: str, content: str, language: str
) -> tuple[dict | None, str | None]:
    """Build a legacy-compatible event or return a coded validation failure."""
    artifact_kind = (kind or "").strip().lower()
    artifact_title = " ".join((title or "").split())[:MAX_TITLE_CHARS]
    artifact_content = content or ""
    if artifact_kind not in ARTIFACT_KINDS or not artifact_title or not artifact_content.strip():
        return None, "invalid_request"

    event = artifact_ready_event(
        request_id=new_request_id(),
        artifact_id=uuid.uuid4().hex,
        revision=1,
        kind=artifact_kind,  # type: ignore[arg-type]
        channel="snippet",
        length="short",
        title=artifact_title,
        body=artifact_content,
        content_format=("code" if artifact_kind in CODE_ARTIFACT_KINDS else "markdown"),
        language=_normalized_language(language) or None,
        persisted=False,
    )
    encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_EVENT_UTF8_BYTES:
        return None, "too_large"
    return event, None


async def present_visible_artifact(
    *,
    user_id: str,
    session_id: str,
    kind: str,
    title: str,
    content: str,
    language: str = "",
    delivery: "ArtifactDeliveryTracker | None" = None,
) -> str:
    """Validate and publish one visible artifact. Never raises.

    With a capable ``delivery`` tracker the returned line reflects what the
    client confirmed drawing rather than what the worker managed to send.
    Legacy clients omit the capability and retain optimistic delivery.
    """
    event, reason = build_visible_artifact_event(
        kind=kind, title=title, content=content, language=language
    )
    if event is None:
        logger.info(
            "visible_artifact: rejected",
            {
                "user_id": user_id,
                "session_id": session_id,
                "kind": (kind or "")[:32],
                "reason": reason,
                "content_chars": len(content or ""),
            },
        )
        return SPOKEN_ARTIFACT_TOO_LARGE if reason == "too_large" else SPOKEN_ARTIFACT_INVALID

    data = json.dumps(event, ensure_ascii=False).encode("utf-8")
    artifact = event["payload"]["artifact"]

    async def _publish() -> bool:
        try:
            room = get_job_context().room
            await asyncio.wait_for(
                room.local_participant.publish_data(data, reliable=True),
                timeout=PUBLISH_TIMEOUT_S,
            )
            return True
        except Exception as exc:
            logger.warn(
                "visible_artifact: publish failed",
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "kind": event["payload"]["artifact_kind"],
                    "error_type": type(exc).__name__,
                },
            )
            return False

    key = (
        delivery.expect(artifact["id"], artifact["revision"])
        if delivery is not None
        else None
    )
    try:
        if not await _publish():
            return SPOKEN_ARTIFACT_DELIVERY_FAILED
        confirmed = True
        if delivery is not None and key is not None:
            if delivery.ack_required:
                # Capable builds prove the first card too. Silence is evidence.
                confirmed = await delivery.wait(key)
                if not confirmed and await _publish():
                    # Same id and revision, so a card that did render is deduped
                    # by the client rather than drawn twice.
                    confirmed = await delivery.wait(key)
            else:
                # Legacy clients omitted the capability and retain the shipped
                # optimistic behavior. A late valid ack promotes the session.
                confirmed = True
    finally:
        if delivery is not None and key is not None:
            delivery.release(key)

    logger.info(
        "visible_artifact: published",
        {
            "user_id": user_id,
            "session_id": session_id,
            "draft_id": event["payload"]["draft_id"],
            "kind": event["payload"]["artifact_kind"],
            "content_chars": len(event["payload"]["text"]),
            "event_bytes": len(data),
            "delivery_confirmed": confirmed,
            "ack_required": delivery.ack_required if delivery is not None else None,
            "client_proven": delivery.client_proven if delivery is not None else None,
        },
    )
    return SPOKEN_ARTIFACT_READY if confirmed else SPOKEN_ARTIFACT_DELIVERY_FAILED
