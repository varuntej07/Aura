"""Tool exposure and execution safety for the LiveKit voice agent."""

from __future__ import annotations

import json
import re
from ast import literal_eval
from dataclasses import dataclass
from typing import Any

from livekit.agents import llm as lk_llm

from .capabilities import VOICE_TOOL_REGISTRY, Capability, ToolEffect, VoiceSurface

ACTION_POLICY_VERSION = "2026-08-18.1"
UNTRUSTED_READ_TOOLS = frozenset({"web_surf", "query_memory", "get_user_context"})
GUIDE_START_PATTERN = re.compile(
    r"^\s*(?:guide\s+me|walk\s+me\s+through)\b", re.IGNORECASE
)

# Below this confidence the transcript is not trustworthy enough to carry a side
# effect, so writes are suppressed regardless of what the words appear to say. Guide
# start is the sole product-requested literal phrase exception; other tools remain
# semantically selected by the existing model.
WRITE_INTENT_MIN_STT_CONFIDENCE = 0.65


@dataclass(frozen=True, slots=True)
class TurnCapabilityPolicy:
    """Tools that are structurally available for one generation."""

    capabilities: frozenset[Capability]
    allowed_tools: frozenset[str]
    reason_codes: tuple[str, ...]
    finalized_turn: bool
    guide_active: bool = False
    guide_start_requested: bool = False
    required_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    allowed: bool
    reason_code: str


def derive_turn_policy(
    transcript: str,
    chat_ctx: lk_llm.ChatContext,
    surface: VoiceSurface,
    fresh_frame_available: bool,
    *,
    finalized_turn: bool = True,
    previous_visible_output_failed: bool = False,
    source_message_id: str = "",
    turn_index: int = 0,
    stt_confidence: float | None = None,
    guide_active: bool = False,
) -> TurnCapabilityPolicy:
    """Expose structurally eligible tools for the existing Buddy model.

    Guide start is the one explicit phrase-gated exception requested by the product
    owner. Every other tool remains independent of literal wording.
    """
    del chat_ctx, previous_visible_output_failed, source_message_id, turn_index

    low_confidence_turn = (
        stt_confidence is not None and stt_confidence < WRITE_INTENT_MIN_STT_CONFIDENCE
    )

    allowed: set[str] = set()
    reasons: list[str] = ["stable_surface_toolset"]
    required: set[str] = set()
    guide_start_requested = bool(
        surface is VoiceSurface.DESKTOP
        and finalized_turn
        and not low_confidence_turn
        and not guide_active
        and GUIDE_START_PATTERN.match(transcript)
    )
    for name, registration in VOICE_TOOL_REGISTRY.items():
        if surface not in registration.allowed_surfaces:
            reasons.append(f"surface_blocked:{name}")
            continue
        if registration.requires_fresh_desktop_frame and not fresh_frame_available:
            reasons.append(f"fresh_frame_required:{name}")
            continue
        if not finalized_turn and registration.effect is not ToolEffect.READ:
            reasons.append(f"finalized_turn_required:{name}")
            continue
        if low_confidence_turn and registration.effect is not ToolEffect.READ:
            reasons.append(f"low_stt_confidence_write_suppressed:{name}")
            continue
        if name == "set_guide_mode" and not (guide_start_requested or guide_active):
            reasons.append("guide_start_phrase_required")
            continue
        if name == "set_guide_mode" and guide_start_requested:
            required.add(name)
        allowed.add(name)

    if not finalized_turn:
        reasons.append("turn_not_finalized")

    capabilities = frozenset(VOICE_TOOL_REGISTRY[name].capability for name in allowed)
    return TurnCapabilityPolicy(
        capabilities=capabilities,
        allowed_tools=frozenset(allowed),
        reason_codes=tuple(reasons),
        finalized_turn=finalized_turn,
        guide_active=guide_active,
        guide_start_requested=guide_start_requested,
        required_tools=frozenset(required),
    )


def completed_tool_results(chat_ctx: lk_llm.ChatContext) -> dict[str, bool]:
    """Return tool success after the most recent user message in copied context."""
    latest_user = -1
    for index, item in enumerate(chat_ctx.items):
        if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
            latest_user = index
    results: dict[str, bool] = {}
    for item in chat_ctx.items[latest_user + 1 :]:
        if isinstance(item, lk_llm.FunctionCallOutput) and item.name:
            results[item.name] = tool_output_succeeded(item)
    return results


def verbatim_voice_result(chat_ctx: lk_llm.ChatContext) -> str | None:
    """Return exact speech required by the latest Action Truth tool result."""
    latest_user = -1
    for index, item in enumerate(chat_ctx.items):
        if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
            latest_user = index
    for item in reversed(chat_ctx.items[latest_user + 1 :]):
        if not isinstance(item, lk_llm.FunctionCallOutput) or item.is_error:
            continue
        parsed: Any
        try:
            parsed = json.loads(item.output)
        except (TypeError, json.JSONDecodeError):
            try:
                parsed = literal_eval(item.output)
            except (ValueError, SyntaxError):
                continue
        if not isinstance(parsed, dict):
            continue
        render = parsed.get("render")
        say = parsed.get("say")
        if (
            isinstance(render, dict)
            and render.get("mode") == "verbatim"
            and render.get("channel") == "voice"
            and isinstance(say, str)
            and say.strip()
        ):
            return say.strip()
    return None


def tool_output_succeeded(output: lk_llm.FunctionCallOutput) -> bool:
    """Recognize both LiveKit errors and existing tools' structured error returns."""
    if output.is_error:
        return False
    parsed: Any
    try:
        parsed = json.loads(output.output)
    except (TypeError, json.JSONDecodeError):
        try:
            parsed = literal_eval(output.output)
        except (ValueError, SyntaxError):
            return True
    if not isinstance(parsed, dict):
        return True
    if parsed.get("ok") is False or parsed.get("error") is True:
        return False
    if parsed.get("configured") is False or parsed.get("approval_required") is True:
        return False
    return True


def evaluate_execution(
    tool_name: str,
    arguments: str,
    policy: TurnCapabilityPolicy,
    chat_ctx: lk_llm.ChatContext,
) -> ExecutionDecision:
    """Validate a model-emitted call against immutable turn authorization."""
    registration = VOICE_TOOL_REGISTRY.get(tool_name)
    if registration is None:
        return ExecutionDecision(False, "unregistered_voice_tool")
    if tool_name not in policy.allowed_tools:
        return ExecutionDecision(False, "tool_not_exposed_for_turn")
    if registration.effect is not ToolEffect.READ and not policy.finalized_turn:
        return ExecutionDecision(False, "stale_turn_side_effect")
    # Deliberately narrower than "not READ". This rule exists so text fetched
    # from the web or recalled from memory cannot steer a persistent side effect
    # in the same turn; a SESSION_CONTROL handoff persists nothing and the user
    # can ask to come straight back, so it is not the thing this protects.
    if registration.effect in (ToolEffect.WRITE, ToolEffect.PRESENT):
        recent_results = completed_tool_results(chat_ctx)
        if any(name in UNTRUSTED_READ_TOOLS for name in recent_results):
            return ExecutionDecision(False, "fresh_turn_required_after_untrusted_read")
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return ExecutionDecision(False, "invalid_tool_arguments")
    if not isinstance(parsed, dict):
        return ExecutionDecision(False, "invalid_tool_arguments")
    if any(
        field_name not in parsed
        or parsed[field_name] is None
        or (
            parsed[field_name] == ""
            and field_name not in registration.empty_allowed_fields
        )
        for field_name in registration.required_fields
    ):
        return ExecutionDecision(False, "missing_required_tool_field")
    if tool_name == "set_guide_mode":
        enable = parsed.get("enable")
        if enable is True and not policy.guide_start_requested:
            return ExecutionDecision(False, "guide_start_phrase_required")
        if enable is False and not policy.guide_active:
            return ExecutionDecision(False, "guide_not_active")
    return ExecutionDecision(True, "execution_allowed")
