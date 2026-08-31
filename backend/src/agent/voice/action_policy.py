"""Tool exposure and execution safety for the LiveKit voice agent."""

from __future__ import annotations

import json
from dataclasses import dataclass

from livekit.agents import llm as lk_llm

from .capabilities import VOICE_TOOL_REGISTRY, Capability, ToolEffect, VoiceSurface
from .chat_context import latest_user_index
from .tool_result import parse_tool_output

ACTION_POLICY_VERSION = "2026-08-20.1"
UNTRUSTED_READ_TOOLS = frozenset({"web_surf", "query_memory", "get_user_context"})

# Below this confidence the transcript is not trustworthy enough to carry a side
# effect, so writes and session-control handoffs are suppressed regardless of what
# the words appear to say. Intent remains the existing model's decision through each
# tool description; policy never interprets the user's wording.
WRITE_INTENT_MIN_STT_CONFIDENCE = 0.65


@dataclass(frozen=True, slots=True)
class TurnCapabilityPolicy:
    """Tools that are structurally available for one generation."""

    capabilities: frozenset[Capability]
    allowed_tools: frozenset[str]
    reason_codes: tuple[str, ...]
    finalized_turn: bool


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    allowed: bool
    reason_code: str


def derive_turn_policy(
    surface: VoiceSurface,
    fresh_frame_available: bool,
    *,
    finalized_turn: bool = True,
    stt_confidence: float | None = None,
) -> TurnCapabilityPolicy:
    """Expose structurally eligible tools for the existing Buddy model.

    This layer validates surface, finalization, confidence, and runtime prerequisites.
    The model selects tools from semantic descriptions, exactly as it does for
    Interview Mode; policy never classifies intent from transcript text. It takes
    neither the transcript nor the chat context ON PURPOSE: there is nothing here
    that may read what the user said, and not accepting those arguments is what
    makes that structural rather than a promise.
    """
    low_confidence_turn = (
        stt_confidence is not None and stt_confidence < WRITE_INTENT_MIN_STT_CONFIDENCE
    )

    allowed: set[str] = set()
    reasons: list[str] = ["stable_surface_toolset"]
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
        allowed.add(name)

    if not finalized_turn:
        reasons.append("turn_not_finalized")

    capabilities = frozenset(VOICE_TOOL_REGISTRY[name].capability for name in allowed)
    return TurnCapabilityPolicy(
        capabilities=capabilities,
        allowed_tools=frozenset(allowed),
        reason_codes=tuple(reasons),
        finalized_turn=finalized_turn,
    )


def completed_tool_results(chat_ctx: lk_llm.ChatContext) -> dict[str, bool]:
    """Return tool success after the most recent user message in copied context."""
    latest_user = latest_user_index(chat_ctx)
    results: dict[str, bool] = {}
    for item in chat_ctx.items[latest_user + 1 :]:
        if isinstance(item, lk_llm.FunctionCallOutput) and item.name:
            results[item.name] = tool_output_succeeded(item)
    return results


def verbatim_voice_result(chat_ctx: lk_llm.ChatContext) -> str | None:
    """Return exact speech required by the latest Action Truth tool result."""
    latest_user = latest_user_index(chat_ctx)
    for item in reversed(chat_ctx.items[latest_user + 1 :]):
        if not isinstance(item, lk_llm.FunctionCallOutput) or item.is_error:
            continue
        parsed = parse_tool_output(item)
        if parsed is None:
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
    parsed = parse_tool_output(output)
    # No structured body is not a failure: plenty of tools return bare prose.
    if parsed is None:
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
        # Buddy owns only the start handoff. Active Guide Mode has a separate
        # stop_guide_mode tool on its isolated supervisor, so false is never a
        # valid argument on this surface. This validates the structured call;
        # it does not inspect or reinterpret what the user said.
        if parsed.get("enable") is not True:
            return ExecutionDecision(False, "guide_start_only")
    return ExecutionDecision(True, "execution_allowed")
