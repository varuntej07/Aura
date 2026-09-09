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
    # `mode: "summary"` on the voice channel used to be a label nothing read. Only
    # "verbatim" had a consumer (verbatim_voice_result in action_policy), so a summary
    # result handed the model its full raw JSON with a grounding rule and no length
    # rule, and the model read the research out loud in headed, bulleted paragraphs.
    # The sanitizer strips the bullet CHARACTERS on the way to TTS, so every word
    # still gets spoken and the markup evidence is destroyed before anyone can see it.
    # The budget rides on `then` because that is the field the model already reads for
    # this result; it constrains this answer only, and never the persona's room to be
    # warm or serious at length when the moment is about the person rather than a
    # lookup.
    if render_mode == "summary" and render_channel == "voice":
        # Deliberately a spoken-REGISTER rule rather than a sentence count. The same
        # envelope carries a web lookup and a reminder list, and "one or two sentences"
        # is wrong for the second: someone who asked what is on their list wants the
        # list, spoken as a person would say it. What is always wrong is reading a
        # result out as a document.
        # The close is a prohibition, not an invitation. This line used to end
        # "Offer to go further rather than going further unasked", which the model
        # obeyed: every tool-backed turn in a live 2026-09 session ended in "if you
        # want X, just say so". `then` is an instruction the model is told to follow
        # (_EVIDENCE_AND_ACTIONS), so it outranked the persona and contradicted
        # get_user_context's own "...then stop" inside the same turn. Nothing else in
        # the prompt asked for a trailing offer; this one line was the whole source.
        # Kept short on purpose. This string is a literal field inside EVERY summary
        # envelope, six MCP tools declare that mode, and nothing dedupes them, so a
        # four-tool conversation carries four verbatim copies inside the raw window
        # that context_compaction keeps (SOFT_RETAINED_RAW_TURNS = 8). At the original
        # 81 tokens that was ~320 tokens of the same sentence competing with the
        # persona for attention. The trailing-offer half moved to
        # VOICE_TURN_CLOSING_CHECK, which is stated once, at the end of context, and
        # covers every turn rather than only tool-backed ones. What is left here is the
        # only thing that is specific to a lookup result: do not read it as a document.
        #
        # Deduping the older copies would mean mutating existing FunctionCallOutput
        # content, which fails LiveKit's is_equivalent check and kills the speculative
        # reply. That is a latency-gate decision, not a free win.
        spoken_budget = (
            "This is read aloud in a live call. Plain speech only: no headings, "
            "bullets, lists, markup, URLs, or source names. Say the sentences that "
            "answer them, then stop."
        )
        then = f"{then} {spoken_budget}" if then else spoken_budget
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
