"""Deterministic per-turn capability and execution policy for voice only."""

from __future__ import annotations

import json
import re
from ast import literal_eval
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from livekit.agents import llm as lk_llm

from .capabilities import (
    LOW_CONFIDENCE_SAFE_READ_TOOL_NAMES,
    VOICE_TOOL_REGISTRY,
    Capability,
    ToolEffect,
    VoiceSurface,
)
from .tool_skills import instructions_for_skill_names

ACTION_POLICY_VERSION = "2026-07-14.1"


class ActionMode(StrEnum):
    FAST = "fast"
    COMPLEX = "complex"


class WriteAuthorization(StrEnum):
    NONE = "none"
    AUTHORIZED = "authorized"
    NEEDS_CLARIFICATION = "needs_clarification"
    STALE_TURN = "stale_turn"


@dataclass(frozen=True, slots=True)
class UnresolvedActionState:
    source_message_id: str = ""
    source_turn_index: int = 0
    capabilities: frozenset[Capability] = frozenset()
    missing_slots: tuple[str, ...] = ()
    created_at_turn: int = 0
    write_authorized: bool = False


@dataclass(frozen=True, slots=True)
class PlanStep:
    id: str
    tool: str
    effect: ToolEffect
    depends_on: tuple[str, ...] = ()
    on_failure: str = "halt"


@dataclass(frozen=True, slots=True)
class ComplexActionPlan:
    capabilities: tuple[Capability, ...]
    clarification_required: bool
    clarification_question: str | None
    missing_slots: tuple[str, ...]
    steps: tuple[PlanStep, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capabilities": [value.value for value in self.capabilities],
            "clarification": {
                "required": self.clarification_required,
                "question": self.clarification_question,
                "missing_slots": list(self.missing_slots),
            },
            "steps": [
                {
                    "id": step.id,
                    "tool": step.tool,
                    "effect": step.effect.value,
                    "depends_on": list(step.depends_on),
                    "on_failure": step.on_failure,
                }
                for step in self.steps
            ],
        }


@dataclass(frozen=True, slots=True)
class TurnCapabilityPolicy:
    capabilities: frozenset[Capability]
    allowed_tools: frozenset[str]
    action_mode: ActionMode
    write_authorization: WriteAuthorization
    reason_codes: tuple[str, ...]
    clarification_question: str | None = None
    clarification_owner: str | None = None
    missing_slots: tuple[str, ...] = ()
    plan: ComplexActionPlan | None = None
    finalized_turn: bool = True

    def transient_instruction(self) -> str:
        capabilities = ", ".join(sorted(item.value for item in self.capabilities)) or "none"
        allowed = ", ".join(sorted(self.allowed_tools)) or "none"
        base = (
            "<turn_capability_policy>Voice-only transient policy for this inference. "
            f"Detected capabilities: {capabilities}. Available tools: {allowed}. "
        )
        if self.clarification_question:
            base += (
                "Do not perform a write yet. Ask one short, natural clarification covering: "
                f"{self.clarification_question} "
            )
        if self.action_mode is ActionMode.COMPLEX:
            base += (
                "This is a dependent request. Use at most the next safe prerequisite tool, "
                "then continue only after its result. Never emit dependent writes together. "
            )
        if Capability.MEMORY_READ in self.capabilities:
            base += "If memory retrieval finds no match, ask one natural clarification. "
        if self.write_authorization is WriteAuthorization.STALE_TURN:
            base += "This generation is not tied to a finalized turn, so do not write. "
        skill_names = [
            registration.skill_name
            for registration in VOICE_TOOL_REGISTRY.values()
            if registration.capability in self.capabilities and registration.skill_name
        ]
        skills = instructions_for_skill_names(
            skill_names,
            visible_output_required=bool(
                {"visible_output_repair", "visible_output_repeat"} & set(self.reason_codes)
            ),
        )
        return base + "Keep the spoken response warm and natural.</turn_capability_policy>" + skills


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    allowed: bool
    reason_code: str


_EXACT_TIME = re.compile(
    r"\b(?:at\s+)?(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b|"
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\bnoon\b|\bmidnight\b",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
    re.IGNORECASE,
)
_DEPENDENCY = re.compile(
    r"\b(?:then|after|before|if\s+(?:i(?:'m| am)?\s+)?free|once that is done)\b",
    re.IGNORECASE,
)
_REMIND_MEMORY = re.compile(r"\bremind me\s+(?:what|who|where|when|about)\b", re.IGNORECASE)
_HYPOTHETICAL_WRITE = re.compile(r"^\s*(?:why not|should (?:i|we)|what if)\b", re.IGNORECASE)
_VISIBLE_OUTPUT_REPAIR = re.compile(
    r"\b(?:stop|don't|dont|do not)\b.{0,35}\b(?:say|speak|read|dictate)\b"
    r".{0,35}\b(?:it|that|command|code|prompt|steps?|text|out loud)\b|"
    r"\b(?:not|instead of)\s+(?:out\s+)?loud\b|"
    r"\b(?:put|show|display|write|type)\b.{0,30}\b(?:on|onto)\s+(?:the|my)\s+screen\b|"
    r"\bi\s+(?:said|asked|told you)\b.{0,45}\b(?:draft|write|show|command|code|prompt|steps?)\b",
    re.IGNORECASE,
)
_VISIBLE_OUTPUT_RETRY = re.compile(
    r"\b(?:again|retry|try again|same thing|like i said|i already asked|i asked before|"
    r"put it up|show it|display it|write it down)\b",
    re.IGNORECASE,
)
_VISIBLE_OUTPUT_REQUEST = re.compile(
    r"\b(?:give|show|display|generate|make|create|draft|write|compose|put|type)\b"
    r".{0,55}\b(?:snippet|command|script|code|config(?:uration)?|prompt|checklist|steps?|instructions?)\b|"
    r"\b(?:what|which)\b.{0,30}\b(?:command|script|prompt)\b|"
    r"\b(?:command|script|code|prompt)\b.{0,40}\b(?:fix|use|run|paste|copy|need)\b|"
    r"\b(?:copy|paste|copyable|verbatim)\b|"
    r"\b(?:next steps?|what (?:do|should) i do next|walk me through|how do i fix)\b",
    re.IGNORECASE,
)
_CORRECTION_OR_TOPIC_CHANGE = re.compile(
    r"\b(?:no|nope|actually|instead|that's not what i asked|that is not what i asked|"
    r"i asked (?:for|you to)|forget (?:that|it)|cancel (?:that|it)|never mind|nevermind)\b",
    re.IGNORECASE,
)
_TIMEZONE_NAME = re.compile(
    r"\b(?:eastern|central|mountain|pacific|atlantic|alaska|hawaii|"
    r"est|edt|cst|cdt|mst|mdt|pst|pdt|utc|gmt)\b(?:\s+time)?|"
    r"\b(?:America|Europe|Asia|Australia|Africa)/[A-Za-z_]+(?:/[A-Za-z_]+)?\b",
    re.IGNORECASE,
)


def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _classify_capabilities(text: str) -> tuple[set[Capability], list[str]]:
    capabilities: set[Capability] = set()
    reasons: list[str] = []

    reminder_as_memory = bool(_REMIND_MEMORY.search(text))
    if reminder_as_memory:
        capabilities.add(Capability.MEMORY_READ)
        reasons.append("remind_memory_retrieval")
    elif _has_any(
        text,
        # "schedule" belongs here for parity with the chat gate and the write-auth
        # verbs below; without it "schedule a reminder" got no reminder capability
        # at all, so the tool was never exposed.
        (r"\bremind me\b", r"\b(?:set|create|schedule|add) (?:a )?reminder\b"),
    ):
        capabilities.add(Capability.REMINDER_WRITE)
        reasons.append("reminder_request")
    if _has_any(text, (r"\b(?:list|show|what).{0,20}reminders?\b", r"\bcancel.{0,20}reminder\b")):
        capability = (
            Capability.REMINDER_WRITE if "cancel" in text.lower() else Capability.REMINDER_READ
        )
        capabilities.add(capability)
        reasons.append("reminder_management")

    calendar_read = _has_any(
        text,
        (
            r"\b(?:calendar|schedule)\b.{0,30}\b(?:free|available|what|check|show)\b",
            r"\bcheck (?:if|whether).{0,20}\bfree\b",
            r"\bwhat(?:'s| is) on my calendar\b",
        ),
    )
    calendar_write = _has_any(
        text,
        (
            r"\b(?:schedule|book|create|add)\b.{0,45}"
            r"\b(?:breakfast|lunch|dinner|coffee|call|meeting|event|"
            r"appointment|calendar)\b",
        ),
    ) and not _has_any(text, (r"\bwhat(?:'s| is) scheduled\b", r"\bmy schedule\b"))
    if calendar_read:
        capabilities.add(Capability.CALENDAR_READ)
        reasons.append("calendar_read_request")
    if calendar_write:
        capabilities.add(Capability.CALENDAR_WRITE)
        reasons.append("calendar_write_request")

    if _has_any(text, (r"\b(?:remember|recall|what did|what .* said)\b",)):
        capabilities.add(Capability.MEMORY_READ)
        reasons.append("memory_retrieval_request")
    if _has_any(text, (r"\b(?:remember|store|save) (?:that|this fact|my preference)\b",)):
        capabilities.add(Capability.MEMORY_WRITE)
        reasons.append("memory_store_request")
    if _has_any(text, (r"\b(?:search|look up|latest|current|news|score|price|weather)\b",)):
        capabilities.add(Capability.WEB_READ)
        reasons.append("web_lookup_request")
    if _has_any(text, (r"\bkeep me posted\b", r"\bfollow .{1,50} for me\b")):
        capabilities.add(Capability.TRACKING_WRITE)
        reasons.append("tracking_request")
    if _has_any(text, (r"\b(?:feedback|feature request|bug in aura)\b",)):
        capabilities.add(Capability.FEEDBACK_WRITE)
        reasons.append("product_feedback")
    if _has_any(text, (r"\b(?:save|bookmark|keep) (?:this|that|what(?:'s| is) on my screen)\b",)):
        capabilities.add(Capability.SCREEN_SAVE)
        reasons.append("screen_save_request")
    visible_output_repair = bool(_VISIBLE_OUTPUT_REPAIR.search(text))
    visible_output_request = bool(_VISIBLE_OUTPUT_REQUEST.search(text))
    if visible_output_repair or visible_output_request:
        capabilities.add(Capability.VISIBLE_ARTIFACT)
        reasons.append(
            "visible_output_repair" if visible_output_repair else "visible_output_request"
        )

    if _has_any(
        text,
        (
            r"\b(?:draft|write|compose)\b.{0,35}"
            r"\b(?:reply|response|email|dm|message)\b",
        ),
    ):
        capabilities.add(Capability.OUTBOUND_DRAFT)
        reasons.append("outbound_draft_request")

    return capabilities, reasons


def _requested_write_count(capabilities: set[Capability]) -> int:
    return sum(
        any(
            item.capability is capability and item.effect is ToolEffect.WRITE
            for item in VOICE_TOOL_REGISTRY.values()
        )
        for capability in capabilities
    )


def _missing_slots(text: str, capabilities: set[Capability]) -> list[str]:
    missing: list[str] = []
    if Capability.REMINDER_WRITE in capabilities and not _EXACT_TIME.search(text):
        missing.append("reminder_exact_time")
    if Capability.REMINDER_WRITE in capabilities and len(_timezone_values(text)) > 1:
        missing.append("reminder_timezone")
    if Capability.CALENDAR_WRITE in capabilities:
        if not _DATE.search(text):
            missing.append("calendar_date")
        if not _EXACT_TIME.search(text):
            missing.append("calendar_time")
    return missing


def _timezone_values(text: str) -> tuple[str, ...]:
    """Return distinct timezone mentions in spoken order, normalized for comparison."""
    values: list[str] = []
    for match in _TIMEZONE_NAME.finditer(text):
        value = re.sub(
            r"\s+time$", "", match.group(0).strip(), flags=re.IGNORECASE,
        ).casefold()
        if value not in values:
            values.append(value)
    return tuple(values)


def _merge_unresolved_missing(
    text: str,
    missing: list[str],
    unresolved: UnresolvedActionState,
    capabilities: set[Capability],
) -> list[str]:
    for slot in unresolved.missing_slots:
        owner_active = (
            slot == "reminder_exact_time" and Capability.REMINDER_WRITE in capabilities
        ) or (slot.startswith("calendar_") and Capability.CALENDAR_WRITE in capabilities)
        if not owner_active:
            continue
        resolved = (slot == "calendar_date" and bool(_DATE.search(text))) or (
            slot in {"calendar_time", "reminder_exact_time"} and bool(_EXACT_TIME.search(text))
        ) or (
            slot == "reminder_timezone" and len(_timezone_values(text)) == 1
        )
        if not resolved and slot not in missing:
            missing.append(slot)
    return missing


def _unresolved_state_is_current(
    unresolved: UnresolvedActionState,
    *,
    source_message_id: str,
    turn_index: int,
) -> bool:
    """Accept state only on the one user turn immediately after its owner."""
    return bool(
        unresolved.source_message_id
        and unresolved.capabilities
        and unresolved.missing_slots
        and source_message_id
        and source_message_id != unresolved.source_message_id
        and turn_index > 0
        and unresolved.source_turn_index == turn_index - 1
        and unresolved.created_at_turn == unresolved.source_turn_index
    )


def _is_plausible_slot_answer(text: str, missing_slots: tuple[str, ...]) -> bool:
    matched = False
    for slot in missing_slots:
        if slot == "calendar_date" and _DATE.search(text):
            matched = True
        elif slot in {"calendar_time", "reminder_exact_time"} and _EXACT_TIME.search(text):
            matched = True
        elif slot == "reminder_timezone" and len(_timezone_values(text)) == 1:
            matched = True
    if matched:
        return True
    if _CORRECTION_OR_TOPIC_CHANGE.search(text):
        return False
    return matched


def _continuing_unresolved_action(
    text: str,
    current_capabilities: set[Capability],
    unresolved: UnresolvedActionState,
    *,
    source_message_id: str,
    turn_index: int,
) -> bool:
    if not _unresolved_state_is_current(
        unresolved,
        source_message_id=source_message_id,
        turn_index=turn_index,
    ):
        return False
    if current_capabilities:
        return False
    return _is_plausible_slot_answer(text, unresolved.missing_slots)


def _has_explicit_write_authorization(text: str) -> bool:
    if _HYPOTHETICAL_WRITE.search(text):
        return False
    if re.search(r"\b(?:do not|don't|dont)\b", text, re.IGNORECASE):
        return False
    return _has_any(
        text,
        (
            r"\bremind me\b",
            # "set" grants a write ONLY next to "reminder" - bare "set" is too common
            # ("set that aside") to treat as authorization like the verbs below. Parity
            # with the chat gate (action_intent_policy._EXPLICIT_REMINDER_CREATE, which
            # already accepts "set a reminder"): the voice gate omitting it withheld
            # set_reminder and let Buddy narrate a reminder it never wrote (2026-07-16).
            r"\bset\b.{0,15}\breminder\b",
            r"\b(?:schedule|book|create|add|cancel|save|store|draft|write|compose)\b",
            r"\bkeep me posted\b",
            r"\bfollow .{1,50} for me\b",
        ),
    )


def _clarification_for(missing: list[str]) -> str | None:
    if not missing:
        return None
    if set(missing) == {"calendar_date", "calendar_time", "reminder_exact_time"}:
        return "the lunch date and time, plus the exact reminder time"
    labels = {
        "calendar_date": "which date",
        "calendar_time": "what exact time",
        "reminder_exact_time": "what exact time the reminder should fire",
        "reminder_timezone": "which timezone the reminder time should use",
    }
    return ", and ".join(labels[item] for item in missing)


def _clarification_owner(missing: list[str]) -> str | None:
    owners: list[str] = []
    if any(slot.startswith("calendar_") for slot in missing):
        owners.append(Capability.CALENDAR_WRITE.value)
    if {"reminder_exact_time", "reminder_timezone"} & set(missing):
        owners.append(Capability.REMINDER_WRITE.value)
    return ",".join(owners) or None


def _build_plan(text: str, capabilities: set[Capability], missing: list[str]) -> ComplexActionPlan:
    candidates = [
        (Capability.CALENDAR_READ, "availability", "get_upcoming_events", ToolEffect.READ),
        (Capability.REMINDER_READ, "reminder_list", "list_reminders", ToolEffect.READ),
        (Capability.MEMORY_READ, "memory_lookup", "query_memory", ToolEffect.READ),
        (Capability.WEB_READ, "web_lookup", "web_surf", ToolEffect.READ),
        (
            Capability.USER_CONTEXT_READ,
            "user_context",
            "get_user_context",
            ToolEffect.READ,
        ),
        (
            Capability.VISIBLE_ARTIFACT,
            "visible_artifact",
            "present_visible_artifact",
            ToolEffect.PRESENT,
        ),
        (
            Capability.CALENDAR_WRITE,
            "calendar_event",
            "create_calendar_event",
            ToolEffect.WRITE,
        ),
        (Capability.MEMORY_WRITE, "memory_store", "store_memory", ToolEffect.WRITE),
        (Capability.TRACKING_WRITE, "topic_tracking", "track_topic", ToolEffect.WRITE),
        (Capability.SCREEN_SAVE, "screen_save", "save_screen_item", ToolEffect.WRITE),
        (Capability.OUTBOUND_DRAFT, "draft", "draft_outbound_message", ToolEffect.WRITE),
        (
            Capability.REMINDER_WRITE,
            "reminder",
            "cancel_reminder" if re.search(r"\bcancel\b", text, re.IGNORECASE) else "set_reminder",
            ToolEffect.WRITE,
        ),
        (Capability.FEEDBACK_WRITE, "feedback", "report_feedback", ToolEffect.WRITE),
    ]
    steps: list[PlanStep] = []
    for capability, step_id, tool, effect in candidates:
        if capability not in capabilities:
            continue
        dependencies = (steps[-1].id,) if steps else ()
        steps.append(PlanStep(step_id, tool, effect, dependencies))
    return ComplexActionPlan(
        capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
        clarification_required=bool(missing),
        clarification_question=_clarification_for(missing),
        missing_slots=tuple(missing),
        steps=tuple(steps),
    )


def validate_plan(plan: ComplexActionPlan, *, authorized: bool) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    seen: set[str] = set()
    for step in plan.steps:
        if step.id in seen or any(dependency not in seen for dependency in step.depends_on):
            reasons.append("invalid_dependency")
        registration = VOICE_TOOL_REGISTRY.get(step.tool)
        if registration is None or not registration.complex_lane_eligible:
            reasons.append("ineligible_complex_tool")
        if (
            step.effect is ToolEffect.WRITE
            and registration is not None
            and registration.requires_explicit_authorization
            and not authorized
        ):
            reasons.append("write_not_authorized")
        seen.add(step.id)
    return not reasons, tuple(dict.fromkeys(reasons))


def derive_turn_policy(
    transcript: str,
    chat_ctx: lk_llm.ChatContext,
    surface: VoiceSurface,
    fresh_frame_available: bool,
    unresolved: UnresolvedActionState = UnresolvedActionState(),
    *,
    finalized_turn: bool = True,
    previous_visible_output_failed: bool = False,
    source_message_id: str = "",
    turn_index: int = 0,
) -> TurnCapabilityPolicy:
    """Return a conservative multi-label union from trusted, immutable turn facts."""
    text = (transcript or "").strip()
    capabilities, reasons = _classify_capabilities(text)
    if previous_visible_output_failed and (
        Capability.VISIBLE_ARTIFACT in capabilities or _VISIBLE_OUTPUT_RETRY.search(text)
    ):
        capabilities.add(Capability.VISIBLE_ARTIFACT)
        reasons.append("visible_output_repeat")
    continuing_unresolved = _continuing_unresolved_action(
        text,
        capabilities,
        unresolved,
        source_message_id=source_message_id,
        turn_index=turn_index,
    )
    active_unresolved = unresolved if continuing_unresolved else UnresolvedActionState()
    if not capabilities and continuing_unresolved:
        capabilities.update(unresolved.capabilities)
        reasons.append("continued_unresolved_action")
    elif unresolved.capabilities and not continuing_unresolved:
        reasons.append("unresolved_action_cleared")

    # A slot-only continuation is evaluated against the slots the prior turn owned.
    # Re-running every required-field detector on the fragment "Central time" would
    # incorrectly invent a new missing clock-time slot even though the owner turn had 5 pm.
    current_missing = [] if continuing_unresolved else _missing_slots(text, capabilities)
    missing = _merge_unresolved_missing(
        text, current_missing, active_unresolved, capabilities,
    )
    explicitly_authorized = (
        _has_explicit_write_authorization(text) or active_unresolved.write_authorized
    )
    if explicitly_authorized and _requested_write_count(capabilities):
        reasons.append("explicit_write_request")
    complex_mode = (
        bool(_DEPENDENCY.search(text)) and len(capabilities) > 1
    ) or _requested_write_count(capabilities) > 1
    plan = _build_plan(text, capabilities, missing) if complex_mode else None

    allowed: set[str] = set()
    for name, registration in VOICE_TOOL_REGISTRY.items():
        if registration.capability not in capabilities:
            continue
        if surface not in registration.allowed_surfaces:
            reasons.append(f"surface_blocked:{name}")
            continue
        if registration.requires_fresh_desktop_frame and not fresh_frame_available:
            reasons.append(f"fresh_frame_required:{name}")
            continue
        capability_missing = (
            name == "set_reminder"
            and bool({"reminder_exact_time", "reminder_timezone"} & set(missing))
        ) or (
            registration.capability is Capability.CALENDAR_WRITE
            and any(slot.startswith("calendar_") for slot in missing)
        )
        if registration.effect is ToolEffect.WRITE and (
            capability_missing
            or not finalized_turn
            or (registration.requires_explicit_authorization and not explicitly_authorized)
        ):
            continue
        allowed.add(name)

    # Presentation is a response modality, not an external side effect. Keep it
    # available on every finalized Desktop turn so the model can move exact or
    # multi-step text on screen even when a narrow intent classifier has no label.
    if (
        surface is VoiceSurface.DESKTOP
        and finalized_turn
        and (not complex_mode or Capability.VISIBLE_ARTIFACT in capabilities)
    ):
        allowed.add("present_visible_artifact")

    if Capability.REMINDER_WRITE in capabilities:
        if re.search(r"\bcancel\b", text, re.IGNORECASE):
            allowed.discard("set_reminder")
        else:
            allowed.discard("cancel_reminder")

    if not capabilities:
        allowed.update(LOW_CONFIDENCE_SAFE_READ_TOOL_NAMES)
        reasons.append("low_confidence_safe_reads")
    if Capability.MEMORY_READ in capabilities:
        allowed.discard("set_reminder")

    if not finalized_turn:
        allowed.clear()

    if not finalized_turn:
        authorization = WriteAuthorization.STALE_TURN
        reasons.append("turn_not_finalized")
    elif missing:
        authorization = WriteAuthorization.NEEDS_CLARIFICATION
        reasons.append("missing_required_slots")
    elif _requested_write_count(capabilities) and not explicitly_authorized:
        authorization = WriteAuthorization.NEEDS_CLARIFICATION
        reasons.append("explicit_write_authorization_required")
    elif any(
        item.effect is ToolEffect.WRITE and item.capability in capabilities
        for item in VOICE_TOOL_REGISTRY.values()
    ):
        authorization = WriteAuthorization.AUTHORIZED
    else:
        authorization = WriteAuthorization.NONE

    return TurnCapabilityPolicy(
        capabilities=frozenset(capabilities),
        allowed_tools=frozenset(allowed),
        action_mode=ActionMode.COMPLEX if complex_mode else ActionMode.FAST,
        write_authorization=authorization,
        reason_codes=tuple(dict.fromkeys(reasons)),
        clarification_question=_clarification_for(missing),
        clarification_owner=_clarification_owner(missing),
        missing_slots=tuple(missing),
        plan=plan,
        finalized_turn=finalized_turn,
    )


def completed_tool_results(chat_ctx: lk_llm.ChatContext) -> dict[str, bool]:
    """Return tool success after the most recent user message in the copied context."""
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


def next_complex_tool(policy: TurnCapabilityPolicy, chat_ctx: lk_llm.ChatContext) -> str | None:
    if policy.plan is None:
        return None
    results = completed_tool_results(chat_ctx)
    completed_ids: set[str] = set()
    for step in policy.plan.steps:
        failed_dependency = any(
            results.get(prerequisite.tool) is False
            for prerequisite in policy.plan.steps
            if prerequisite.id in step.depends_on
        )
        if failed_dependency:
            return None
        if step.tool in results and results[step.tool]:
            completed_ids.add(step.id)
            continue
        if step.tool in results and results[step.tool] is False:
            return None
        if policy.plan.clarification_required:
            reminder_only_missing = set(policy.missing_slots).issubset(
                {"reminder_exact_time", "reminder_timezone"}
            )
            if not (reminder_only_missing and step.tool == "draft_outbound_message"):
                return None
        if all(dependency in completed_ids for dependency in step.depends_on):
            return step.tool
        return None
    return None


def evaluate_execution(
    tool_name: str,
    arguments: str,
    policy: TurnCapabilityPolicy,
    chat_ctx: lk_llm.ChatContext,
) -> ExecutionDecision:
    registration = VOICE_TOOL_REGISTRY.get(tool_name)
    if registration is None:
        return ExecutionDecision(False, "unregistered_voice_tool")
    if tool_name not in policy.allowed_tools:
        return ExecutionDecision(False, "tool_not_exposed_for_turn")
    if registration.effect is ToolEffect.WRITE and not policy.finalized_turn:
        return ExecutionDecision(False, "stale_turn_write")
    if registration.effect is ToolEffect.PRESENT and not policy.finalized_turn:
        return ExecutionDecision(False, "stale_turn_presentation")
    if registration.requires_explicit_authorization and policy.write_authorization in {
        WriteAuthorization.NONE,
        WriteAuthorization.STALE_TURN,
    }:
        return ExecutionDecision(False, "write_not_authorized")
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return ExecutionDecision(False, "invalid_tool_arguments")
    if any(not parsed.get(field_name) for field_name in registration.required_fields):
        return ExecutionDecision(False, "missing_required_tool_field")
    if policy.action_mode is ActionMode.COMPLEX:
        expected = next_complex_tool(policy, chat_ctx)
        if tool_name != expected:
            return ExecutionDecision(False, "dependent_action_deferred")
    return ExecutionDecision(True, "execution_allowed")
