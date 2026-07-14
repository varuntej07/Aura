"""Focused, per-tool skill briefs for the LiveKit voice model.

The permanent voice prompt should stay lean. These briefs are injected only on
turns where their capability is active, so a tool can carry the category test,
formatting rules, and recovery behavior it needs without making every voice
turn pay for every tool's instructions.

This is model guidance, not authorization. ``action_policy.py`` still owns the
deterministic surface, freshness, finalized-turn, and side-effect gates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceToolSkill:
    name: str
    instruction: str


VOICE_TOOL_SKILLS: dict[str, VoiceToolSkill] = {
    skill.name: skill
    for skill in (
        VoiceToolSkill(
            name="reminder_read",
            instruction=(
                "Use list_reminders only when the finalized user turn asks to read or "
                "show reminders. Report only what the tool returns. Do not turn a read "
                "request into a new reminder or ask for a reminder time."
            ),
        ),
        VoiceToolSkill(
            name="reminder_write",
            instruction=(
                "Reminder clarification is owned by the current turn policy. Ask only "
                "for the missing field named there, in one short natural sentence. "
                "Never invent a date or time, never add a new clarification category, "
                "and never continue a reminder from conversational history or a session "
                "summary. When set_reminder is available, confirm the exact local date "
                "and time briefly and call it once."
            ),
        ),
        VoiceToolSkill(
            name="calendar_read",
            instruction=(
                "Use get_upcoming_events only when the finalized user turn asks about "
                "their calendar or availability. Report the returned local times as-is "
                "and do not create an event from a read request."
            ),
        ),
        VoiceToolSkill(
            name="calendar_write",
            instruction=(
                "Calendar clarification is owned by the current turn policy. Ask only "
                "for the missing date or exact start time named there. Never invent a "
                "date, time, title, or new clarification category. Resolve relative days "
                "against the current session date, confirm the actual local date and "
                "time briefly, and call create_calendar_event once when it is available."
            ),
        ),
        VoiceToolSkill(
            name="visible_artifact",
            instruction=(
                "Use present_visible_artifact whenever the useful answer is text the "
                "user must copy exactly or scan visually: terminal commands, code, "
                "configuration, prompts for another agent, or two or more ordered next "
                "steps. It is also the repair path when they say not to read something "
                "out loud, ask for it on screen, or repeat a copyable-text request. "
                "Choose command or code for runnable text, prompt for text they will "
                "paste into another AI, steps or checklist for multi-step guidance, and "
                "note for other reusable text. Put the complete useful content in the "
                "tool, never a summary or placeholder. Never put an email reply or DM "
                "in this tool. After it succeeds, speak only a short confirmation and "
                "never recite the artifact. A single simple action or a conversational "
                "explanation can stay spoken."
            ),
        ),
        VoiceToolSkill(
            name="outbound_draft",
            instruction=(
                "Use draft_outbound_message only for an email reply or a DM/message "
                "to another person. The message is written from the current screen, so "
                "do not substitute a spoken draft. Use present_visible_artifact for "
                "commands, code, prompts, configuration, and procedural steps."
            ),
        ),
        VoiceToolSkill(
            name="screen_save",
            instruction=(
                "Use save_screen_item only when the user explicitly asks to save, "
                "bookmark, or remember the specific thing visible in the fresh screen "
                "frame. Do not use it for presenting text or for general memory."
            ),
        ),
    )
}


def instructions_for_skill_names(skill_names: list[str], *, visible_output_required: bool) -> str:
    """Render a compact XML block for the exposed tools' registered skills."""
    instructions = [
        VOICE_TOOL_SKILLS[name].instruction
        for name in dict.fromkeys(skill_names)
        if name in VOICE_TOOL_SKILLS
    ]
    if not instructions:
        return ""
    requirement = (
        " The user is correcting a spoken-output failure on this turn. You MUST use "
        "present_visible_artifact and must not speak the requested content."
        if visible_output_required
        else ""
    )
    return (
        "<tool_skills>Focused instructions for tools available on this turn. "
        + " ".join(instructions)
        + requirement
        + "</tool_skills>"
    )
