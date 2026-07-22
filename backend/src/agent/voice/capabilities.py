"""Typed registry for tools exposed by the LiveKit voice worker.

This is policy metadata only. Tool implementations and backend validation stay
in their existing modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VoiceSurface(StrEnum):
    APP = "app"
    KEYBOARD = "keyboard"
    DESKTOP = "desktop"


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    PRESENT = "present"


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


ALL_SURFACES = frozenset(VoiceSurface)
DESKTOP_ONLY = frozenset({VoiceSurface.DESKTOP})


@dataclass(frozen=True, slots=True)
class VoiceToolCapability:
    name: str
    capability: Capability
    effect: ToolEffect
    allowed_surfaces: frozenset[VoiceSurface]
    requires_fresh_desktop_frame: bool
    safe_concurrently: bool
    complex_lane_eligible: bool
    required_fields: frozenset[str] = frozenset()
    skill_name: str | None = None


def _tool(
    name: str,
    capability: Capability,
    effect: ToolEffect,
    *,
    surfaces: frozenset[VoiceSurface] = ALL_SURFACES,
    frame: bool = False,
    concurrent: bool = True,
    complex_eligible: bool = False,
    required: tuple[str, ...] = (),
    skill: str | None = None,
) -> VoiceToolCapability:
    return VoiceToolCapability(
        name=name,
        capability=capability,
        effect=effect,
        allowed_surfaces=surfaces,
        requires_fresh_desktop_frame=frame,
        safe_concurrently=concurrent,
        complex_lane_eligible=complex_eligible,
        required_fields=frozenset(required),
        skill_name=skill,
    )


VOICE_TOOL_REGISTRY: dict[str, VoiceToolCapability] = {
    item.name: item
    for item in (
        _tool(
            "list_reminders",
            Capability.REMINDER_READ,
            ToolEffect.READ,
            complex_eligible=True,
            skill="reminder_read",
        ),
        _tool(
            "set_reminder",
            Capability.REMINDER_WRITE,
            ToolEffect.WRITE,
            concurrent=False,
            complex_eligible=True,
            required=("message", "scheduled_at"),
            skill="reminder_write",
        ),
        _tool(
            "cancel_reminder",
            Capability.REMINDER_WRITE,
            ToolEffect.WRITE,
            concurrent=False,
            complex_eligible=True,
            required=("reminder_id",),
            skill="reminder_write",
        ),
        _tool(
            "get_upcoming_events",
            Capability.CALENDAR_READ,
            ToolEffect.READ,
            complex_eligible=True,
            skill="calendar_read",
        ),
        _tool(
            "create_calendar_event",
            Capability.CALENDAR_WRITE,
            ToolEffect.WRITE,
            concurrent=False,
            complex_eligible=True,
            required=("title", "start_time"),
            skill="calendar_write",
        ),
        _tool(
            "query_memory",
            Capability.MEMORY_READ,
            ToolEffect.READ,
            complex_eligible=True,
        ),
        _tool(
            "store_memory",
            Capability.MEMORY_WRITE,
            ToolEffect.WRITE,
            concurrent=False,
            complex_eligible=True,
            required=("key", "value", "category"),
        ),
        _tool("web_surf", Capability.WEB_READ, ToolEffect.READ, complex_eligible=True),
        _tool(
            "get_user_context",
            Capability.USER_CONTEXT_READ,
            ToolEffect.READ,
            complex_eligible=True,
        ),
        _tool(
            "report_feedback",
            Capability.FEEDBACK_WRITE,
            ToolEffect.WRITE,
            concurrent=True,
            complex_eligible=True,
            required=("category", "about", "summary", "verbatim_quote"),
        ),
        _tool(
            "track_topic",
            Capability.TRACKING_WRITE,
            ToolEffect.WRITE,
            concurrent=False,
            complex_eligible=True,
            required=("request",),
        ),
        _tool(
            "save_screen_item",
            Capability.SCREEN_SAVE,
            ToolEffect.WRITE,
            surfaces=DESKTOP_ONLY,
            frame=True,
            concurrent=False,
            complex_eligible=True,
            required=("title", "collection_name"),
            skill="screen_save",
        ),
        _tool(
            "draft_outbound_message",
            Capability.OUTBOUND_DRAFT,
            ToolEffect.WRITE,
            surfaces=DESKTOP_ONLY,
            frame=True,
            concurrent=False,
            complex_eligible=True,
            # No required field: channel/length are optional hints the drafter
            # infers from the screen, so Buddy can draft from just the intent
            # (or nothing) without the old "email or new message?" bounce.
            required=(),
            skill="outbound_draft",
        ),
        _tool(
            "present_visible_artifact",
            Capability.VISIBLE_ARTIFACT,
            ToolEffect.PRESENT,
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            complex_eligible=True,
            required=("kind", "title", "content"),
            skill="visible_artifact",
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
