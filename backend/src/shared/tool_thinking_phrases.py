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
}


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
