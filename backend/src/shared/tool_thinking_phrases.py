"""Buddy's in-persona acknowledgments for slow tool calls, shared by voice + text.

One source of truth so both surfaces feel like the same Buddy:
- Voice (``agent/voice/tool_filler.py``) SPEAKS one of these while a slow tool runs.
- Text chat (``services/claude_client.py``) STREAMS one as a ``tool_status`` event so the
  chat bubble shows "one sec, looking that up" instead of blank typing dots while the
  1-7s web/vision/LLM call is in flight.

Keep every phrase short and casual: on voice it must finish before the tool does, and on
text it should read like Buddy talking, not a system label. Fast tools (set_reminder,
store_memory, ...) are deliberately absent: a filler there would only delay their instant
confirmation, so their absence means "stay silent", on both surfaces.
"""

from __future__ import annotations

import zlib

# Tools whose round-trip is long enough (multi-second LLM/vision/web work, up
# to the 8s MCP cap) that the user needs Buddy to acknowledge before the real
# reply. Keyed by the tool name as it appears in the model's tool call, so a
# key must match the exact tool name the LLM emits (voice + chat share `web_surf`).
SLOW_TOOL_THINKING_PHRASES: dict[str, list[str]] = {
    # draft_outbound_message is deliberately NOT listed: it is an async tool
    # (ctx.update in draft_outbound.py speaks a contextual acknowledgment in
    # Buddy's persona); a canned phrase here would double-speak on top of it.
    "create_calendar_event": [
        "cool, popping that on your calendar",
        "alright, getting that on the calendar",
        "on it, adding that now",
    ],
    "query_memory": [
        "lemme think back for a sec",
        "digging through what I remember",
        "one sec, jogging my memory",
    ],
    "get_user_context": [
        "lemme pull up your stuff real quick",
        "one sec, grabbing your details",
    ],
    "web_surf": [
        "ooh good question, lemme look that up",
        "hang on, let me actually check that",
        "one sec, looking that up real quick",
        "lemme make sure I get this right",
    ],
    "start_research": [
        "got it, setting up the research",
        "on it, scoping that research now",
    ],
}


# UI labels for the desktop activity rail, which is a different job from the
# phrases above and must not reuse them. A phrase is Buddy TALKING ("lemme think
# back for a sec") and only exists for slow tools; a label is the CLIENT naming a
# step in a list ("Checking your memory"), so every tool the surface allows needs
# one or its row renders unnamed. Present participle, sentence case, no trailing
# period: these are read as a running list of steps, not as sentences.
TOOL_ACTIVITY_LABELS: dict[str, str] = {
    "ask_clarification": "Asking a question",
    "cancel_reminder": "Cancelling a reminder",
    "cancel_tracker": "Cancelling a tracker",
    "create_calendar_event": "Adding to your calendar",
    "delete_memory": "Forgetting something",
    "get_upcoming_events": "Checking your calendar",
    "get_aura_product_info": "Checking Aura's product guide",
    "get_user_context": "Pulling up your details",
    "list_emails": "Checking your inbox",
    "list_reminders": "Checking your reminders",
    "list_trackers": "Checking your trackers",
    "query_memory": "Checking your memory",
    "read_email": "Reading an email",
    "report_feedback": "Filing your feedback",
    "send_email": "Sending an email",
    "set_reminder": "Setting a reminder",
    "store_memory": "Saving that to memory",
    "track_topic": "Setting up a tracker",
    "update_calendar_event": "Updating your calendar",
    "web_surf": "Searching the web",
    "start_research": "Starting research",
}

# Which tools may echo one argument into the transcript, and which argument.
#
# STRICT ALLOWLIST, and it must stay that way. "Searching the web for X" is
# genuinely useful to the user and X is their own phrasing anyway. Everything
# else is not: `read_email` would leak a message id, `list_emails` a query that
# may name a person, `query_memory` the retrieval probe. A tool absent from this
# map shows its label alone, which is the safe default for anything added later.
_ECHOABLE_TOOL_ARGUMENT: dict[str, str] = {
    "web_surf": "query",
    "start_research": "request",
}

_MAX_DETAIL_CHARS = 80


def tool_activity_label(tool_name: str) -> str:
    """The client-facing name for one step. Never empty, so a row always renders."""
    return TOOL_ACTIVITY_LABELS.get(tool_name) or "Working on it"


def tool_activity_detail(tool_name: str, tool_input: object) -> str:
    """The one argument this tool may show the user, or "" for every other tool.

    Fail-closed on anything unexpected: a non-dict input, a missing key, or a
    non-string value all yield "". The rail is a transparency feature, and a
    transparency feature that leaks an argument it should not have shown is worse
    than one that shows nothing.
    """
    key = _ECHOABLE_TOOL_ARGUMENT.get(tool_name)
    if not key or not isinstance(tool_input, dict):
        return ""
    value = tool_input.get(key)
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_DETAIL_CHARS]


def pick_tool_thinking_phrase(tool_name: str, seed: str) -> str | None:
    """Deterministically choose one phrase for ``tool_name`` from ``seed``.

    Returns None for fast/unknown tools (not in the map), so callers use "None means
    stay silent" the same way the voice path does. The choice is a pure function of
    (tool_name, seed): a stable per-turn seed (e.g. the message id) yields the SAME
    phrase every time, so the streamed status never flickers if the emit site is hit
    more than once in a turn. crc32 is used instead of the builtin ``hash`` because
    the latter is salted per process (``PYTHONHASHSEED``) and would not be stable.
    """
    phrases = SLOW_TOOL_THINKING_PHRASES.get(tool_name)
    if not phrases:
        return None
    index = zlib.crc32(f"{tool_name}:{seed}".encode()) % len(phrases)
    return phrases[index]
