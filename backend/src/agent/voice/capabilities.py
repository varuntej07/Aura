"""Typed registry for tools exposed by the LiveKit voice worker.

This is policy metadata only. Tool implementations and backend validation stay
in their existing modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...shared.tools import (
    CREATE_CALENDAR_EVENT_TOOL_DEFINITION,
    GET_AURA_PRODUCT_INFO_TOOL_DEFINITION,
    SET_REMINDER_TOOL_DEFINITION,
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
    # Changes who owns the conversation for the rest of the session: a handoff to
    # another agent. Not a WRITE. Nothing is persisted, nothing leaves the
    # process, and the user can undo it by asking to go back, so treating it as a
    # write earned it an action receipt it should never have had and blocked it
    # behind rules written for irreversible side effects.
    #
    # It is still not a READ, and everything keyed on "not READ" applies
    # deliberately: it needs a finalized turn, it needs trustworthy STT, it
    # invalidates speculation, and it is the only action its turn may emit.
    SESSION_CONTROL = "session_control"


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
    PRODUCT_INFO_READ = "product_info_read"
    REMINDER_READ = "reminder_read"
    REMINDER_WRITE = "reminder_write"
    CALENDAR_READ = "calendar_read"
    CALENDAR_WRITE = "calendar_write"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    WEB_READ = "web_read"
    RESEARCH_WRITE = "research_write"
    USER_CONTEXT_READ = "user_context_read"
    FEEDBACK_WRITE = "feedback_write"
    TRACKING_WRITE = "tracking_write"
    SCREEN_SAVE = "screen_save"
    NOTION_CAPTURE = "notion_capture"
    OUTBOUND_DRAFT = "outbound_draft"
    VISIBLE_ARTIFACT = "visible_artifact"
    GUIDE_CONTROL = "guide_control"
    INTERVIEW_SESSION = "interview_session"
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


def _execution_required_fields(definition: dict[str, Any]) -> tuple[str, ...]:
    """The canonical required fields, minus the ones that carry a default.

    `required_fields` is an execution gate: a name listed here and absent from the
    model's arguments blocks the call outright (action_policy.py). A property that
    declares a default is never blocking, because the executor fills it in during
    validation. Listing one here would refuse a perfectly answerable call over a
    field the schema already knows how to supply, which on set_reminder means a
    voice request to be woken up is silently dropped instead of degrading to a
    quiet reminder. Derived rather than retyped so the two paths cannot drift.
    """
    schema = definition["inputSchema"]
    properties = schema.get("properties", {})
    return tuple(
        field
        for field in schema.get("required", ())
        if "default" not in properties.get(field, {})
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
            "get_aura_product_info",
            Capability.PRODUCT_INFO_READ,
            ToolEffect.READ,
            namespace="product.help",
            concurrent=False,
            required=_execution_required_fields(GET_AURA_PRODUCT_INFO_TOOL_DEFINITION),
        ),
        _tool(
            "list_reminders",
            Capability.REMINDER_READ,
            ToolEffect.READ,
            namespace="productivity.reminders",
        ),
        _tool(
            "set_reminder",
            Capability.REMINDER_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.reminders",
            concurrent=False,
            # Derived, not retyped. This drifted once already: the canonical schema
            # grew `tier` and this copy did not, so the voice path and the chat path
            # disagreed about what a valid set_reminder call looks like.
            required=_execution_required_fields(SET_REMINDER_TOOL_DEFINITION),
        ),
        _tool(
            "cancel_reminder",
            Capability.REMINDER_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.reminders",
            concurrent=False,
            required=("reminder_id",),
        ),
        _tool(
            "get_upcoming_events",
            Capability.CALENDAR_READ,
            ToolEffect.READ,
            namespace="productivity.calendar",
            connectors=("google_calendar",),
        ),
        _tool(
            "create_calendar_event",
            Capability.CALENDAR_WRITE,
            ToolEffect.WRITE,
            namespace="productivity.calendar",
            connectors=("google_calendar",),
            latency=ToolLatency.MEDIUM,
            concurrent=False,
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
        ),
        _tool(
            "store_memory",
            Capability.MEMORY_WRITE,
            ToolEffect.WRITE,
            namespace="personal.memory",
            concurrent=False,
            required=("key", "value", "category"),
        ),
        _tool(
            "delete_memory",
            Capability.MEMORY_WRITE,
            ToolEffect.WRITE,
            namespace="personal.memory",
            risk=ToolRisk.HIGH,
            concurrent=False,
            required=("memory_id",),
        ),
        _tool(
            "web_surf",
            Capability.WEB_READ,
            ToolEffect.READ,
            namespace="research.web",
            latency=ToolLatency.HIGH,
        ),
        _tool(
            "start_research",
            Capability.RESEARCH_WRITE,
            ToolEffect.WRITE,
            namespace="research.background",
            surfaces=DESKTOP_ONLY,
            latency=ToolLatency.HIGH,
            concurrent=False,
            required=("request", "depth"),
        ),
        _tool(
            "get_user_context",
            Capability.USER_CONTEXT_READ,
            ToolEffect.READ,
            namespace="personal.context",
        ),
        _tool(
            "report_feedback",
            Capability.FEEDBACK_WRITE,
            ToolEffect.WRITE,
            namespace="feedback",
            concurrent=True,
            required=("category", "about", "summary", "severity"),
        ),
        _tool(
            "track_topic",
            Capability.TRACKING_WRITE,
            ToolEffect.WRITE,
            namespace="research.tracking",
            latency=ToolLatency.HIGH,
            concurrent=False,
            required=("request",),
        ),
        _tool(
            "draft_outbound_message",
            Capability.OUTBOUND_DRAFT,
            ToolEffect.WRITE,
            namespace="writing.drafts",
            concurrent=False,
            required=("operation", "skill_id"),
        ),
        _tool(
            "present_visible_artifact",
            Capability.VISIBLE_ARTIFACT,
            ToolEffect.PRESENT,
            namespace="desktop.screen",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            required=("kind", "title", "content"),
        ),
        _tool(
            # Saves the screen the user is looking at. Reached through the model
            # like any other tool: this used to be a finalized-speech grammar that
            # fired persistence without a schema, which meant "don't screenshot
            # that" and "how do screenshots work" both had to be excluded by hand.
            # No fresh frame is required because the capture path saves the most
            # recent retained frame (screen_frames.latest_for_save).
            "save_screen_item",
            Capability.SCREEN_SAVE,
            ToolEffect.WRITE,
            namespace="desktop.screen",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
        ),
        _tool(
            # Saves what is on screen as a structured record in the user's own
            # Notion, when the utterance names or implies a Notion destination.
            # Like save_screen_item, no fresh frame is required: the capture
            # path reads the most recent retained payload (structured tree
            # first, JPEG fallback). Gated on the notion connector being
            # enabled; disambiguation and propose-create round-trips return a
            # spoken question instead of a write.
            "save_to_notion",
            Capability.NOTION_CAPTURE,
            ToolEffect.WRITE,
            namespace="desktop.notion",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            connectors=("notion",),
            latency=ToolLatency.HIGH,
            required=("intent", "destination"),
        ),
        _tool(
            # Archives the session's most recent Notion save (reversible from
            # Notion's trash). A WRITE: it changes external state and deserves
            # the finalized-turn and STT gates.
            "undo_notion_save",
            Capability.NOTION_CAPTURE,
            ToolEffect.WRITE,
            namespace="desktop.notion",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            connectors=("notion",),
        ),
        _tool(
            # Requests the desktop to arm Guide Mode. Like Interview Mode, this is
            # model-selected session control rather than a persistent write or a
            # phrase-gated action. The desktop stays the sole arming authority: it
            # pins the cursor's monitor and checks the signed-in session.
            "set_guide_mode",
            Capability.GUIDE_CONTROL,
            ToolEffect.SESSION_CONTROL,
            namespace="desktop.guide",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
            required=("enable",),
        ),
        _tool(
            # A handoff, not a write: it hands the conversation to
            # InterviewSupervisorAgent and persists nothing. SESSION_CONTROL keeps
            # the finalized-turn and STT-confidence protection that every non-READ
            # tool gets, while keeping it out of action receipts and out of the
            # rules that exist for irreversible writes. See ToolEffect above.
            "start_mock_interview",
            Capability.INTERVIEW_SESSION,
            ToolEffect.SESSION_CONTROL,
            namespace="voice.interview",
            surfaces=DESKTOP_ONLY,
            concurrent=False,
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



def tool_name(tool: object) -> str:
    """Return a LiveKit tool's registered name without depending on its concrete type."""
    info = getattr(tool, "info", None)
    return str(getattr(info, "name", "") or getattr(tool, "name", "") or "")
