"""Deterministic answer to "can you see my screen?".

The screen policy in prompts.py already says to answer only from this turn's
evidence and to say so when none arrived. A live session showed that is not
enough: with nothing captured at all, Buddy answered "Yeah, I can see your
screen right now", and the user spent the next four minutes trying to fix a
feature that had never been on.

An instruction the model can ignore is not a guarantee, so the answer to this
one question is taken out of the model's hands whenever there is no evidence to
answer it from. The reverse case is deliberately left alone: when evidence IS
present the model already has it in context and should answer naturally, and a
canned "yes" would make the good path sound robotic to fix a bug that only
happens on the bad one.

Pure functions only, matching screen_capture_command.py; the wiring lives in
buddy_agent.llm_node.
"""

from __future__ import annotations

import re

# "you" as the one doing the seeing, then a screen object within a short window.
# Deliberately tolerant of the shapes a frustrated user actually produces:
# "can you not see my screen", "why is it not letting you see my screen",
# "I just want you to see my screen", "can you not still see my screen".
# Bounded by [^.?!] so it never reaches across sentences for its object.
_VISIBILITY_RE = re.compile(
    r"\byou\b[^.?!]{0,24}?\b(?:see|seeing|view|read)\b[^.?!]{0,34}?"
    r"\b(?:my|the|this|that)\s+(?:screen|display|monitor)\b",
    re.IGNORECASE,
)

# The user is asking Buddy to look at something rather than asking whether it
# can, which deserves the same truthful answer when nothing arrived.
_LOOK_REQUEST_RE = re.compile(
    r"\b(?:look\s+at|check|read)\s+(?:my|the|this)\s+(?:screen|display|monitor)\b",
    re.IGNORECASE,
)

# One sentence, no hedging, and no shortcut named: bindings are user-rebindable
# and this worker cannot see which one they set.
NO_SCREEN_EVIDENCE_REPLY = "I don't actually have your screen right now, so I can't see it."


def asks_about_screen_visibility(transcript: str) -> bool:
    """True when the turn hinges on whether Buddy can currently see the screen."""
    if not transcript:
        return False
    return bool(
        _VISIBILITY_RE.search(transcript) or _LOOK_REQUEST_RE.search(transcript)
    )


def screen_visibility_remainder(transcript: str) -> str:
    """Return additional intent after removing the current-screen clause."""
    if not transcript:
        return ""
    matches = [
        match
        for pattern in (_VISIBILITY_RE, _LOOK_REQUEST_RE)
        if (match := pattern.search(transcript)) is not None
    ]
    if not matches:
        return transcript.strip()
    match = min(matches, key=lambda item: item.start())
    prefix = transcript[:match.start()]
    boundaries = list(
        re.finditer(r"[.?!;]|\b(?:and|but|then|also)\b", prefix, re.IGNORECASE)
    )
    clause_start = boundaries[-1].start() if boundaries else 0
    suffix = transcript[match.end():]
    sentence_end = re.search(r"[.?!;]", suffix)
    qualifier = suffix[: sentence_end.start() if sentence_end else len(suffix)]
    if re.fullmatch(r"[\s,]*(?:right now|now|currently|at the moment)?[\s,]*", qualifier, re.IGNORECASE):
        suffix = suffix[sentence_end.end():] if sentence_end else ""
    remainder = f"{transcript[:clause_start]} {suffix}"
    remainder = re.sub(r"^[\s,;:.!?-]*(?:and|but|then|also)\b", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\b(?:and|but|then|also)[\s,;:.!?-]*$", "", remainder, flags=re.IGNORECASE)
    return remainder.strip(" \t\r\n,;:.!?-")
