"""Tool exposure and execution safety for the LiveKit voice agent."""

from __future__ import annotations

import json
from ast import literal_eval
from dataclasses import dataclass
from typing import Any

from livekit.agents import llm as lk_llm

from .capabilities import VOICE_TOOL_REGISTRY, Capability, ToolEffect, VoiceSurface
from .guide_intent import (
    GuideDecisionIdentity,
    GuideIntentDecision,
    guide_start_admission,
)

ACTION_POLICY_VERSION = "2026-08-16.2"
UNTRUSTED_READ_TOOLS = frozenset({"web_surf", "query_memory", "get_user_context"})

# Signal quality, not wording. Below this the transcript is not trustworthy enough to
# carry a side effect, so writes are suppressed for the turn regardless of what the
# words appear to say. This is deliberately the ONLY transcript-derived input to tool
# exposure: which tools are SUGGESTED is decided by semantic selection and the model,
# never by matching literal words against a hand-written list.
WRITE_INTENT_MIN_STT_CONFIDENCE = 0.65


@dataclass(frozen=True, slots=True)
class TurnCapabilityPolicy:
    """Tools that are structurally available for one generation."""

    capabilities: frozenset[Capability]
    allowed_tools: frozenset[str]
    reason_codes: tuple[str, ...]
    finalized_turn: bool
    guide_intent_decision: GuideIntentDecision | None = None
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
    guide_intent_decision: GuideIntentDecision | None = None,
    guide_decision_identity: GuideDecisionIdentity | None = None,
    stt_confidence: float | None = None,
) -> TurnCapabilityPolicy:
    """Expose structurally eligible tools plus one trusted Guide admission.

    Nothing here reads the wording of the transcript. Exposure is decided by surface,
    frame freshness, turn finalization, and transcript signal quality; which of the
    exposed tools is right for this turn is the selector's and the model's job.
    """
    del transcript, chat_ctx, previous_visible_output_failed, source_message_id, turn_index

    low_confidence_turn = (
        stt_confidence is not None and stt_confidence < WRITE_INTENT_MIN_STT_CONFIDENCE
    )

    allowed: set[str] = set()
    reasons: list[str] = ["stable_surface_toolset"]
    required: set[str] = set()
    guide_admission = guide_start_admission(
        guide_intent_decision, guide_decision_identity
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
        if name == "set_guide_mode":
            if not guide_admission.allowed:
                reasons.append(guide_admission.reason_code)
                continue
            required.add(name)
            reasons.append(guide_admission.reason_code)
        allowed.add(name)

    if not finalized_turn:
        reasons.append("turn_not_finalized")

    capabilities = frozenset(VOICE_TOOL_REGISTRY[name].capability for name in allowed)
    return TurnCapabilityPolicy(
        capabilities=capabilities,
        allowed_tools=frozenset(allowed),
        reason_codes=tuple(reasons),
        finalized_turn=finalized_turn,
        guide_intent_decision=guide_intent_decision,
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
    *,
    guide_decision_identity: GuideDecisionIdentity | None = None,
) -> ExecutionDecision:
    """Validate a model-emitted call against immutable turn authorization."""
    registration = VOICE_TOOL_REGISTRY.get(tool_name)
    if registration is None:
        return ExecutionDecision(False, "unregistered_voice_tool")
    if tool_name not in policy.allowed_tools:
        return ExecutionDecision(False, "tool_not_exposed_for_turn")
    if registration.effect is not ToolEffect.READ and not policy.finalized_turn:
        return ExecutionDecision(False, "stale_turn_side_effect")
    if registration.effect is not ToolEffect.READ:
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
        if parsed.get("enable") is not True:
            return ExecutionDecision(False, "guide_start_only")
        guide_admission = guide_start_admission(
            policy.guide_intent_decision,
            guide_decision_identity,
        )
        if not guide_admission.allowed:
            return ExecutionDecision(False, guide_admission.reason_code)
    return ExecutionDecision(True, "execution_allowed")
