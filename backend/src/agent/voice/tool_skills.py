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
                "an email reply or DM in this tool. The tool owns acknowledgement and "
                "completion speech. After calling it, emit no conversational text and "
                "never recite, preview, or summarize the artifact. A single simple "
                "action or a conversational explanation can stay spoken."
            ),
        ),
        VoiceToolSkill(
            name="outbound_draft",
            instruction=(
                "Use draft_outbound_message ONLY when the useful answer is a message "
                "addressed to a person that they will SEND: an email reply, a DM or chat "
                "message, a comment, a post, a review, or a bio. The test is the "
                "destination, the words go TO someone. If the words instead go INTO "
                "another tool, a terminal, a field they act on, or another AI (a prompt, "
                "a command, code, config, a script), that is present_visible_artifact, "
                "not this tool. The verb does not decide it: \"draft me a prompt\" or "
                "\"draft me a command\" is a visible artifact, never an outbound message. "
                "A script or prompt the user will feed to another AI, a video or UGC "
                "generator, or any creator tool is ALWAYS present_visible_artifact "
                "(kind prompt), never this tool, even mid-conversation about a 'draft' "
                "or after you already made one: it goes INTO a tool, it is not sent TO "
                "a person. If it is not addressed to a human recipient who will send "
                "it, it is not an outbound message. "
                "You can see their screen, so read it to work out what is being asked and "
                "follow their spoken instructions on tone, length, and content. Call it "
                "right away with whatever they gave you; every argument is optional and "
                "inferred from the screen. Never ask a clarifying question whose answer "
                "is on the screen: never ask whether it's an email or a new message, and "
                "never ask how long it should be. The text is written to their screen as "
                "a card, so never speak the draft itself, not even a preview. The tool "
                "owns acknowledgement and completion speech; after calling it, emit no "
                "conversational text. A draft or card "
                "is never a substitute for a real action: if they ask you to create an "
                "event, a reminder, or a tracker, call that action tool instead."
            ),
        ),
        VoiceToolSkill(
            name="guide_control",
            instruction=(
                "Use set_guide_mode to turn Guide Mode on or off when the user asks "
                "for it ('start guide mode', 'turn on guide mode', 'stop guiding'): "
                "call set_guide_mode(enable=true) to start it and "
                "set_guide_mode(enable=false) to stop it. You do NOT control Guide "
                "Mode any other way, so NEVER say you turned it on or off, flipped a "
                "switch, or that it is now on, unless you actually called this tool "
                "this turn and it returned success. It only REQUESTS the change; the "
                "desktop arms it and shows a dot when it is truly watching. After the "
                "call, say only the short line the tool returns, and do not claim it "
                "is already active."
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
        "they would scan or copy? Then it is a card whose tool owns all lifecycle speech. "
        "Otherwise just talk. When a write tool's result includes a `say` field, that "
        "line is the truth of what happened: speak it in your own warm voice, never a "
        "grander claim than it makes. "
        + " ".join(instructions)
        + "</tool_skills>"
    )
