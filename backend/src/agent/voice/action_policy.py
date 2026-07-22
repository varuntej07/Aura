"""Structural tool exposure and execution safety for the LiveKit voice agent.

This module deliberately does not inspect transcript language. Buddy's existing
system prompt and native tool calling own intent, continuation, clarification,
and tool choice. Code here validates only platform facts and tool-call shape.
"""

from __future__ import annotations

import json
from ast import literal_eval
from dataclasses import dataclass
from typing import Any

from livekit.agents import llm as lk_llm

from .capabilities import VOICE_TOOL_REGISTRY, Capability, ToolEffect, VoiceSurface

ACTION_POLICY_VERSION = "2026-07-19.1"


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
    transcript: str,
    chat_ctx: lk_llm.ChatContext,
    surface: VoiceSurface,
    fresh_frame_available: bool,
    *,
    finalized_turn: bool = True,
    previous_visible_output_failed: bool = False,
    source_message_id: str = "",
    turn_index: int = 0,
) -> TurnCapabilityPolicy:
    """Expose tools from structural runtime state, never from transcript wording.

    The unused conversational parameters remain in the call contract so callers can
    pass finalized-turn context without creating a second semantic interpretation
    path. They must not influence the result.
    """
    del transcript, chat_ctx, previous_visible_output_failed, source_message_id, turn_index

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
        allowed.add(name)

    if not finalized_turn:
        reasons.append("turn_not_finalized")

    capabilities = frozenset(
        VOICE_TOOL_REGISTRY[name].capability for name in allowed
    )
    return TurnCapabilityPolicy(
        capabilities=capabilities,
        allowed_tools=frozenset(allowed),
        reason_codes=tuple(reasons),
        finalized_turn=finalized_turn,
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
    return not (isinstance(parsed, dict) and parsed.get("error") is True)


def evaluate_execution(
    tool_name: str,
    arguments: str,
    policy: TurnCapabilityPolicy,
    chat_ctx: lk_llm.ChatContext,
) -> ExecutionDecision:
    """Validate a model-emitted call without interpreting user language."""
    del chat_ctx
    registration = VOICE_TOOL_REGISTRY.get(tool_name)
    if registration is None:
        return ExecutionDecision(False, "unregistered_voice_tool")
    if tool_name not in policy.allowed_tools:
        return ExecutionDecision(False, "tool_not_exposed_for_turn")
    if registration.effect is not ToolEffect.READ and not policy.finalized_turn:
        return ExecutionDecision(False, "stale_turn_side_effect")
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return ExecutionDecision(False, "invalid_tool_arguments")
    if not isinstance(parsed, dict):
        return ExecutionDecision(False, "invalid_tool_arguments")
    if any(
        field_name not in parsed or parsed[field_name] in (None, "")
        for field_name in registration.required_fields
    ):
        return ExecutionDecision(False, "missing_required_tool_field")
    return ExecutionDecision(True, "execution_allowed")
