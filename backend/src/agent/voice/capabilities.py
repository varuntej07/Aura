"""Typed registry for tools exposed by the LiveKit voice worker.

This is policy metadata only. Tool implementations and backend validation stay
in their existing modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ...shared.tools import (
    CREATE_CALENDAR_EVENT_TOOL_DEFINITION,
    UPDATE_CALENDAR_EVENT_TOOL_DEFINITION,
)


class VoiceSurface(StrEnum):
    APP = "app"
    KEYBOARD = "keyboard"
    DESKTOP = "desktop"


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    PRESENT = "present"


class ToolRisk(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class ToolLatency(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolConcurrency(StrEnum):
    PARALLEL_SAFE = "parallel_safe"
    SERIAL = "serial"


class ToolRollout(StrEnum):
    GENERAL = "general"
    LIMITED = "limited"
    DISABLED = "disabled"


class ToolPrerequisite(StrEnum):
    AUTHENTICATED = "authenticated"
    FINALIZED_REQUEST = "finalized_request"
    FRESH_DESKTOP_FRAME = "fresh_desktop_frame"


class Capability(StrEnum):
    REMINDER_READ = "reminder_read"
    REMINDER_WRITE = "reminder_write"
    CALENDAR_READ = "calendar_read"
    CALENDAR_WRITE = "calendar_write"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    WEB_READ = "web_read"
    USER_CONTEXT_READ = "user_context_read"
    FEEDBACK_WRITE = "feedback_write"
    TRACKING_WRITE = "tracking_write"
    SCREEN_SAVE = "screen_save"
    OUTBOUND_DRAFT = "outbound_draft"
    VISIBLE_ARTIFACT = "visible_artifact"
    GUIDE_CONTROL = "guide_control"
    # Not a topic. The channel Buddy speaks on when a turn is constrained to
    # emit a tool and the right answer is ordinary speech (a clarifying
    # question, a short reply). See speak_only in the registry below.
    SPEECH_CHANNEL = "speech_channel"


ALL_SURFACES = frozenset(VoiceSurface)
DESKTOP_ONLY = frozenset({VoiceSurface.DESKTOP})


@dataclass(frozen=True, slots=True)
class VoiceToolCapability:
    name: str
    capability: Capability
    namespace: str
    effect: ToolEffect
    allowed_surfaces: frozenset[VoiceSurface]
    requires_fresh_desktop_frame: bool
    safe_concurrently: bool
    complex_lane_eligible: bool
    prerequisites: frozenset[ToolPrerequisite]
    required_connectors: frozenset[str]
    risk: ToolRisk
    latency: ToolLatency
    version: str
    rollout_state: ToolRollout
    feature_rollout: str | None
    required_fields: frozenset[str] = frozenset()
    empty_allowed_fields: frozenset[str] = frozenset()

    @property
    def concurrency(self) -> ToolConcurrency:
        return (
            ToolConcurrency.PARALLEL_SAFE
            if self.safe_concurrently
            else ToolConcurrency.SERIAL
        )


def _tool(
    name: str,
    capability: Capability,
    effect: ToolEffect,
    *,
    namespace: str,
    surfaces: frozenset[VoiceSurface] = ALL_SURFACES,
    frame: bool = False,
    concurrent: bool = True,
    complex_eligible: bool = False,
    connectors: tuple[str, ...] = (),
    risk: ToolRisk | None = None,
    latency: ToolLatency = ToolLatency.LOW,
    version: str = "1",
    rollout: ToolRollout = ToolRollout.GENERAL,
    feature_rollout: str | None = None,
    required: tuple[str, ...] = (),
    empty_allowed: tuple[str, ...] = (),
) -> VoiceToolCapability:
    prerequisites = {ToolPrerequisite.AUTHENTICATED}
    if effect is not ToolEffect.READ:
        prerequisites.add(ToolPrerequisite.FINALIZED_REQUEST)
    if frame:
        prerequisites.add(ToolPrerequisite.FRESH_DESKTOP_FRAME)
    return VoiceToolCapability(
        name=name,
        capability=capability,
        namespace=namespace,
        effect=effect,
        allowed_surfaces=surfaces,
        requires_fresh_desktop_frame=frame,
        safe_concurrently=concurrent,
        complex_lane_eligible=complex_eligible,
        prerequisites=frozenset(prerequisites),
        required_connectors=frozenset(connectors),
        risk=risk or (ToolRisk.LOW if effect is ToolEffect.READ else ToolRisk.MODERATE),
        latency=latency,
        version=version,
        rollout_state=rollout,
        feature_rollout=feature_rollout,
        required_fields=frozenset(required),
        empty_allowed_fields=frozenset(empty_allowed),
    )


VOICE_TOOL_REGISTRY: dict[str, VoiceToolCapability] = {
    item.name: item
    for item in (
        _tool(
            "list_reminders",
            Capability.REMINDER_READ,
            ToolEffect.READ,
            namespace="productivity.reminders",
            complex_eligible=True,
        ),
        _tool(
            "set_reminder",
            Capability.REMINDER_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.reminders",
            concurrent=False,
            complex_eligible=True,
            required=("message", "when"),
        ),
        _tool(
            "cancel_reminder",
            Capability.REMINDER_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.reminders",
            concurrent=False,
            complex_eligible=True,
            required=("reminder_id",),
        ),
        _tool(
            "get_upcoming_events",
            Capability.CALENDAR_READ,
            ToolEffect.READ,
            namespace="productivity.calendar",
            connectors=("google_calendar",),
            complex_eligible=True,
        ),
        _tool(
            "create_calendar_event",
            Capability.CALENDAR_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.calendar",
            connectors=("google_calendar",),
            latency=ToolLatency.MEDIUM,
            concurrent=False,
            complex_eligible=True,
            required=tuple(
                CREATE_CALENDAR_EVENT_TOOL_DEFINITION["inputSchema"]["required"]
            ),
            empty_allowed=("description", "location", "attendees"),
        ),
        _tool(
            "update_calendar_event",
            Capability.CALENDAR_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.calendar",
            connectors=("google_calendar",),
            latency=ToolLatency.MEDIUM,
            concurrent=False,
            complex_eligible=True,
            required=tuple(
                UPDATE_CALENDAR_EVENT_TOOL_DEFINITION["inputSchema"]["required"]
            ),
            # Only event_id must carry a value. Every other field is a partial-update
            # slot where empty means "leave this alone", so the structural gate must
            # not read an empty string as a missing argument.
            empty_allowed=("title", "when", "description", "location", "attendees"),
        ),
        _tool(
            "query_memory",
            Capability.MEMORY_READ,
            ToolEffect.READ,
            namespace="personal.memory",
            complex_eligible=True,
        ),
        _tool(
            "store_memory",
            Capability.MEMORY_WRITE,
            ToolEffect.WRITE,
            namespace="personal.memory",
            concurrent=False,
            complex_eligible=True,
            required=("key", "value", "category"),
        ),
        _tool(
            "delete_memory",
            Capability.MEMORY_WRITE,
            ToolEffect.WRITE,
            namespace="personal.memory",
            risk=ToolRisk.HIGH,
            concurrent=False,
            complex_eligible=True,
            required=("memory_id",),
        ),
        _tool(
            "web_surf",
            Capability.WEB_READ,
            ToolEffect.READ,
            namespace="research.web",
            latency=ToolLatency.HIGH,
            complex_eligible=True,
        ),
        _tool(
            "get_user_context",
            Capability.USER_CONTEXT_READ,
            ToolEffect.READ,
            namespace="personal.context",
            complex_eligible=True,
        ),
        _tool(
            "report_feedback",
            Capability.FEEDBACK_WRITE,
            ToolEffect.WRITE,
            namespace="feedback",
            concurrent=True,
            complex_eligible=True,
            required=("category", "about", "summary", "severity"),
        ),
        _tool(
            "track_topic",
            Capability.TRACKING_WRITE,
            ToolEffect.WRITE,
            namespace="research.tracking",
            latency=ToolLatency.HIGH,
            concurrent=False,
            complex_eligible=True,
            required=("request",),
        ),
        _tool(
            "draft_outbound_message",
            Capability.OUTBOUND_DRAFT,
            ToolEffect.WRITE,
            namespace="desktop.screen",
            surfaces=DESKTOP_ONLY,
            frame=True,
            concurrent=False,
            complex_eligible=True,
            required=("operation",),
        ),
        _tool(
            "present_visible_artifact",
            Capability.VISIBLE_ARTIFACT,
            ToolEffect.PRESENT,
            namespace="desktop.screen",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            complex_eligible=True,
            required=("kind", "title", "content"),
        ),
        _tool(
            # Requests the desktop arm/disarm Guide Mode. No frame needed: arming
            # only asks; the desktop stays the sole arming authority (it pins the
            # cursor's monitor and checks the signed-in session). enable is a bool,
            # not required-empty-checked (False is a valid, non-empty value).
            "set_guide_mode",
            Capability.GUIDE_CONTROL,
            ToolEffect.WRITE,
            namespace="desktop.guide",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            required=("enable",),
        ),
        _tool(
            # The speech channel, exposed ONLY on turns that are constrained to
            # emit a tool (see BuddyAgent.llm_node). On those turns plain prose
            # is not a representable answer, which is the point: it is how a
            # recited draft becomes impossible rather than merely discouraged.
            # Without this tool that constraint would also make an ordinary
            # clarifying question impossible, and Buddy would be forced to
            # render its own question to a card.
            #
            # concurrent=False because speaking ends the turn; pairing it with
            # another call would strand that call's result.
            "speak_only",
            Capability.SPEECH_CHANNEL,
            ToolEffect.READ,
            namespace="voice.speech",
            concurrent=False,
            required=("text",),
        ),
    )
}


READ_TOOL_NAMES = frozenset(
    name for name, item in VOICE_TOOL_REGISTRY.items() if item.effect is ToolEffect.READ
)
LOW_CONFIDENCE_SAFE_READ_TOOL_NAMES = frozenset(
    {"query_memory", "web_surf", "get_user_context"}
)
WRITE_TOOL_NAMES = frozenset(
    name for name, item in VOICE_TOOL_REGISTRY.items() if item.effect is ToolEffect.WRITE
)


def tool_name(tool: object) -> str:
    """Return a LiveKit tool's registered name without depending on its concrete type."""
    info = getattr(tool, "info", None)
    return str(getattr(info, "name", "") or getattr(tool, "name", "") or "")
