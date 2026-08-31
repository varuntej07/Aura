"""Backward-compatible Action Truth Contract helpers for voice tools."""

from __future__ import annotations

import json
from ast import literal_eval
from typing import Any, Literal

RenderMode = Literal["verbatim", "summary"]
RenderChannel = Literal["card", "voice"]


def action_truth_envelope(
    result: dict[str, Any] | None = None,
    *,
    ok: bool | None = None,
    say: str | None = None,
    render_mode: RenderMode | None = None,
    render_channel: RenderChannel | None = None,
    then: str | None = None,
) -> dict[str, Any]:
    """Add optional Action Truth fields without removing legacy result fields."""
    envelope = dict(result or {})
    if ok is None:
        ok = not (
            envelope.get("ok") is False
            or bool(envelope.get("error"))
            or envelope.get("configured") is False
        )
    envelope["ok"] = ok

    spoken_line = say
    if spoken_line is None and not ok:
        candidate = envelope.get("user_message") or envelope.get("message")
        if isinstance(candidate, str) and candidate.strip():
            spoken_line = candidate
    if isinstance(spoken_line, str) and spoken_line.strip():
        envelope["say"] = spoken_line.strip()

    if (render_mode is None) != (render_channel is None):
        raise ValueError("render_mode and render_channel must be provided together")
    if render_mode is not None and render_channel is not None:
        envelope["render"] = {
            "mode": render_mode,
            "channel": render_channel,
        }
    envelope["then"] = then
    return envelope


def parse_tool_output(output: object) -> dict[str, Any] | None:
    """Decode a tool result's ``output`` payload, or None when it is not a dict.

    Tool results reach us as a string that is USUALLY JSON but is sometimes a
    Python repr, because some tools return a dict that gets str()-ed on the way
    through. Both are accepted, in that order.

    None means "no structured result to read": unparseable, or parsed to
    something that is not a dict. The distinction between those two cases has
    never been actioned by any caller, and each caller has its own idea of what
    the absence should mean (True for success, {} for fields, None for a pending
    requirement, skip for verbatim speech), so the mapping stays at the call site
    and this returns one unambiguous value.
    """
    raw = getattr(output, "output", "")
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        try:
            parsed = literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
    return parsed if isinstance(parsed, dict) else None
