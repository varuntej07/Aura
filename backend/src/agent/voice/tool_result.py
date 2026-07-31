"""Backward-compatible Action Truth Contract helpers for voice tools."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


RenderMode = Literal["verbatim", "summary"]
RenderChannel = Literal["card", "voice"]


class RenderInstruction(TypedDict):
    mode: RenderMode
    channel: RenderChannel


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
