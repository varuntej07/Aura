"""Output-shape helpers for the last-resort card backstop.

The system prompt and tool skills route "draft me a prompt / command / code" to
present_visible_artifact (see voice_prompt.py), which is a registered voice tool
the model selects semantically. Nothing here inspects the user's wording.

This module used to also carry a request-intent lexicon that armed the card from
the opening turn. It is gone. Three things defeated it, and the third is why no
replacement list belongs here:

* Revision turns do not restate the noun. "Why don't you make it a bit longer"
  and "Where is the greeting? Where is the hook?" name no artifact at all. That
  case moved to `ArtifactSession`, which needs no wording.
* Endpointing splits one spoken thought across several finalized messages, so
  the matched string is often not the string the generation runs against.
* Every noun on the list is also an ordinary English word. "he sent me a
  message", "what's the link", "post about it" are conversation, and arming on
  them buffered normal replies and carded them.

What is left is shape: given a body Buddy already produced, is it a question it
should ask aloud, or a payload the user needs to copy. Those read Buddy's own
output, never the user's request.

Pure functions only; the wiring that calls them lives in buddy_agent.llm_node.
"""

from __future__ import annotations

import re

# Ceiling for treating a question-final reply as a question rather than a body.
# Two sentences of asking comfortably fits; a delivered draft does not.
_MAX_QUESTION_CHARS = 240

_SHORT_CONFIRMATION_RE = re.compile(
    r"(?:"
    r"(?:sure|okay|ok|absolutely|of course)(?:[,.!]\s*(?:one moment|one sec(?:ond)?))?|"
    r"here you go|done|working on it|one moment|one sec(?:ond)?"
    r")[.!]?",
    re.IGNORECASE,
)


def is_question_to_user(text: str) -> bool:
    """True when the reply is Buddy asking, not Buddy delivering.

    A clarifying question is speech, never a card. Buddy asking "what tone do
    you want for this?" and having that question silently rendered to the
    screen instead of asked out loud is a worse failure than the recitation
    this module exists to stop: the user is left waiting on a conversation that
    already happened somewhere they were not looking.

    Deliberately structural rather than a phrase list. A reply that ends on a
    question mark and carries no body is a question; a draft that happens to
    contain a question in the middle ("...are you open to it? Best, Varun") is
    not, because it does not END there. The length ceiling is what separates
    "what vibe do you want?" from a long answer with a rhetorical closer.
    """
    stripped = (text or "").strip()
    if not stripped.endswith("?"):
        return False
    if len(stripped) > _MAX_QUESTION_CHARS:
        return False
    # A body that merely finishes on a question still reads as a body if it is
    # multi-paragraph or fenced. Those are shapes speech destroys.
    return "\n\n" not in stripped and "```" not in stripped


def looks_copyable(text: str) -> bool:
    """True for any non-empty requested body except a bare progress acknowledgement."""
    if not text:
        return False
    if is_question_to_user(text):
        return False
    stripped = text.strip()
    return bool(stripped and not _SHORT_CONFIRMATION_RE.fullmatch(stripped))


# Kind and title for a body the model narrated instead of carding. This path is
# reached only when the model declined the tool that would have named the card,
# so there is nothing to classify from: "note" is the honest, neutral shape, and
# guessing one from the user's nouns is what this module no longer does.
DIVERTED_ARTIFACT_KIND = "note"
DIVERTED_ARTIFACT_TITLE = "Draft"
