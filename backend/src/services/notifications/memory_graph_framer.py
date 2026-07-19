"""Frame Phase 4 graph candidates from their structured value payload only."""

from __future__ import annotations

import json
import re
from typing import Any, cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..model_provider import get_model_provider
from ..signal_engine.notification_framer import strip_long_dashes, truncate_at_word_boundary

TITLE_MAX_CHARS = 40
BODY_MAX_CHARS = 110
PHASE4_VALUE_TYPES = frozenset({
    "unresolved_action",
    "next_step",
    "cross_memory_connection",
    "deadline",
})
FOLLOWUP_VALUE_TYPES = frozenset({
    *PHASE4_VALUE_TYPES,
    "new_information",
    "prepared_artifact",
})
_COMPLETED_WORK_CLAIM = re.compile(
    r"\b(i|buddy|we)\s+(already\s+)?(built|completed|created|drafted|finished|made|"
    r"mapped|prepared|put together|wrote)\b",
    re.IGNORECASE,
)


class FramedMemoryGraphNotification(BaseModel):
    title: str = Field(..., description="Invitational push title, at most 40 chars.")
    body: str = Field(..., description="Invitational push body, at most 110 chars.")


_SYSTEM_PROMPT = """You frame one short notification from a structured value payload.

Hard rules:
- Use only facts present in VALUE_PAYLOAD. Do not infer from outside context.
- The copy is invitational. Offer to help, explore, map, plan, or pick something up.
- Never claim Buddy completed, prepared, built, drafted, found, or made anything.
- Never use pressure, guilt, accountability language, or imply the user forgot.
- Keep the title at most 40 characters and the body at most 110 characters.
- Output only JSON matching {"title":"string","body":"string"}.
"""


def valid_phase4_payload(value_payload: Any) -> dict[str, Any] | None:
    """Return a normalized payload, or None when Phase 4 is not allowed to frame it."""
    if not isinstance(value_payload, dict):
        return None
    payload_type = str(value_payload.get("type") or "")
    evidence = value_payload.get("evidence")
    if payload_type not in PHASE4_VALUE_TYPES or not evidence:
        return None
    if value_payload.get("artifact_ref") is not None:
        return None
    return {
        **value_payload,
        "type": payload_type,
        "artifact_ref": None,
    }


def valid_followup_payload(value_payload: Any) -> dict[str, Any] | None:
    """Validate Phase 6's six-type contract without enabling Phase 7 artifacts."""
    if not isinstance(value_payload, dict):
        return None
    payload_type = str(value_payload.get("type") or "")
    if payload_type not in FOLLOWUP_VALUE_TYPES or not value_payload.get("evidence"):
        return None
    if value_payload.get("artifact_ref") is not None:
        return None
    return {**value_payload, "type": payload_type, "artifact_ref": None}


async def frame_memory_graph_notification(
    value_payload: dict[str, Any],
    *,
    session_followup: bool = False,
) -> FramedMemoryGraphNotification | None:
    """Make one Flash call. Failure returns None so the candidate can retry later."""
    payload = (
        valid_followup_payload(value_payload)
        if session_followup
        else valid_phase4_payload(value_payload)
    )
    if payload is None:
        return None
    prompt = "VALUE_PAYLOAD\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        result = cast(
            FramedMemoryGraphNotification,
            await get_model_provider().cheap(
                prompt,
                system=_SYSTEM_PROMPT,
                response_model=FramedMemoryGraphNotification,
                temperature=0.5,
            ),
        )
        title = truncate_at_word_boundary(
            strip_long_dashes(result.title.strip()), TITLE_MAX_CHARS
        )
        body = truncate_at_word_boundary(
            strip_long_dashes(result.body.strip()), BODY_MAX_CHARS
        )
        if not title or not body:
            return None
        if _COMPLETED_WORK_CLAIM.search(f"{title} {body}"):
            logger.warn("memory_graph_framer: rejected completed-work claim", {
                "value_type": payload.get("type"),
            })
            return None
        return FramedMemoryGraphNotification(title=title, body=body)
    except Exception as exc:
        logger.warn("memory_graph_framer: framing failed", {
            "value_type": payload.get("type"),
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return None
