"""Focused tool guidance included in Buddy's single voice system prompt.

The briefs teach the existing model how to choose native tools. They are selected
once from the tools supported by the session surface, never from transcript words.
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
                "Use list_reminders when the current conversation asks to read or manage "
                "existing reminders. Report only what the tool returns. Do not turn a "
                "read request into a new reminder."
            ),
        ),
        VoiceToolSkill(
            name="reminder_write",
            instruction=(
                "Use the reminder tools when the current user request asks you to create, "
                "change, or cancel a reminder, or when the current turn directly answers, "
                "refines, or corrects your immediately preceding reminder clarification. "
                "Understand that continuation from meaning and recent dialogue, not from "
                "particular words. When they explicitly hand you the decision ('you "
                "decide', 'whatever works'), fill every fillable detail from the "
                "conversation and screen context and act; ask only when a detail is "
                "genuinely unknowable, and then exactly ONE short natural question, never "
                "a stack. Never invent a date, time, reminder id, or permission. Resolve "
                "relative time using the current session date and timezone. Call the "
                "appropriate reminder tool once, then speak the `say` line its result "
                "returns as your confirmation."
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
                "Use create_calendar_event when the current user request asks you to create "
                "an event, or when the current turn directly answers, refines, or corrects "
                "your immediately preceding event clarification. Creating an event is a "
                "real calendar write, never a card: never route it to "
                "present_visible_artifact or answer with manual steps. Act on the request "
                "right away with what they gave plus sensible defaults (a clear title, one "
                "hour duration, and no location, guests, or notes unless they named them). "
                "Pass any guests they DID name into the attendees list, plus any location "
                "or notes, in the same call. Do not interrogate them for optional fields; "
                "ask only when something genuinely required is missing, and then exactly "
                "ONE short natural question, never a stack. 'You decide' or 'whatever "
                "works' is full permission to fill every detail from the conversation and "
                "screen context and just do it. Never invent a date, time, title, or "
                "permission. Resolve relative time using the current session date and "
                "timezone. Call the tool once, then speak the `say` line its result "
                "returns as your confirmation."
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
                "Hard boundary: if a dedicated action tool owns the request (a calendar "
                "event, a reminder, a tracker, a memory), call that action tool; a card "
                "is never a substitute for actually doing the thing, and never present "
                "manual steps for something your tools can do. Choose command or code "
                "for runnable text, prompt for text they will paste into another AI, "
                "and steps or checklist for multi-step guidance. Put the complete "
                "useful content in the tool, never a summary or placeholder. Never put "
                "an email reply or DM in this tool. After it succeeds, speak only a "
                "short confirmation and never recite the artifact. A single simple "
                "action or a conversational explanation can stay spoken."
            ),
        ),
        VoiceToolSkill(
            name="outbound_draft",
            instruction=(
                "Use draft_outbound_message whenever the user wants you to write, "
                "draft, frame, or compose text for something on their screen: an email "
                "reply, a DM or message, a form or application field, a comment, a bio, "
                "a post, a review, any place words go. You can see their screen, so read "
                "it to work out what is being asked and where the text goes, and follow "
                "their spoken instructions on tone, length, and content. Call it right "
                "away with whatever they gave you; every argument is optional and "
                "inferred from the screen. Never ask a clarifying question whose answer "
                "is on the screen: never ask whether it's an email or a new message, and "
                "never ask how long it should be. The text is written to their screen as "
                "a card, so never speak the draft itself, not even a preview: say one "
                "short line confirming it's there and offer to tweak it. Use "
                "present_visible_artifact instead for runnable commands, code, "
                "configuration, and prompts for another agent. A draft or card is never a "
                "substitute for a real action: if they ask you to create an event, a "
                "reminder, or a tracker, call that action tool instead."
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


def instructions_for_skill_names(skill_names: list[str]) -> str:
    """Render the selected session tools into one system-prompt block."""
    instructions = [
        VOICE_TOOL_SKILLS[name].instruction
        for name in dict.fromkeys(skill_names)
        if name in VOICE_TOOL_SKILLS
    ]
    if not instructions:
        return ""
    return (
        "<tool_skills>These are focused instructions for tools available in this session. "
        "Use the current request and recent raw dialogue as one continuous exchange. "
        "A current turn may request an action or answer, refine, correct, or cancel your "
        "immediately preceding clarification. The tool call itself is your semantic "
        "decision to act. Discussion, hypotheticals, old summaries, memories, and your own "
        "prior words never grant permission for an external action. Never claim an action "
        "succeeded before its tool returns success. "
        "The routing test for every request: does it change something in their real life "
        "(an event, reminder, tracker, memory)? Then it is an action tool. Is it text "
        "they would scan or copy? Then it is a card, with one spoken summary line. "
        "Otherwise just talk. When a write tool's result includes a `say` field, that "
        "line is the truth of what happened: speak it in your own warm voice, never a "
        "grander claim than it makes. "
        + " ".join(instructions)
        + "</tool_skills>"
    )
