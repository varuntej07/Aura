"""Recognizes the turn that OPENS a card. Everything after it is session state.

The system prompt and tool skills route "draft me a prompt / command / code" to
present_visible_artifact (see voice_prompt.py). This module is the finalized
request-intent safety net for turns where the model narrates that copyable text
anyway. Output shape alone never authorizes a card.

Scope note, because this module used to try to do more. It once also had to
recognize FOLLOW-UP turns: "make it shorter", "rewrite that", "don't read it
out loud". It could not, and the attempt is what shipped the bug that
`artifact_session` now fixes. Two things defeat a lexicon there:

* Revision turns do not restate the noun. "Why don't you make it a bit longer"
  and "Where is the greeting? Where is the hook?" name no artifact at all.
* Endpointing splits one spoken thought across several finalized messages, so
  the matched string is often not the string the generation runs against. In
  the motivating session "Give me a draft" matched correctly and was then
  discarded, because two more STT fragments arrived behind it.

So follow-up recognition moved to `ArtifactSession`, which needs no wording at
all. What is left here is the opening turn, where the user does say the noun,
plus the output-shape helpers used by the last-resort backstop.

Pure functions only; the wiring that calls them lives in buddy_agent.llm_node.
"""

from __future__ import annotations

import re

# Verb + copyable-noun request, e.g. "draft me a prompt", "give me the command",
# "write the script", "make a prompt for Claude Code".
_ARTIFACT_VERB = (
    r"(?:draft|write|give|gimme|make|create|generate|compose|prepare|produce|craft|show)"
)
_ARTIFACT_NOUN = (
    r"(?:prompt|command|cmd|code|script|snippet|config|configuration|query|regex|"
    r"json|yaml|function|css|html|sql|markdown|url|link|web address|draft|text to (?:paste|copy)|"
    r"message to (?:paste|copy))"
)
# Outbound-message nouns, deliberately kept OUT of the verb-agnostic pattern
# below. "he sent me a message" and "she left me a comment" are ordinary talk,
# and arming on those would buffer a normal reply and card it. Behind an
# explicit compose verb ("write me a message") the intent is unambiguous.
_OUTBOUND_NOUN = (
    r"(?:tweet|thread|post|dm|direct message|message|email|reply|comment|"
    r"caption|bio)"
)
_WANT_ARTIFACT_RE = re.compile(
    rf"\b{_ARTIFACT_VERB}\b[^.?!]{{0,80}}?\b(?:{_ARTIFACT_NOUN}|{_OUTBOUND_NOUN})\b",
    re.IGNORECASE,
)

# The compose verb doubles as the noun: "tweet something about this", "post
# about this". Aura cannot publish to any of these places, so the only useful
# outcome is exact text on screen for the user to copy.
# Split by how ambiguous the word is on its own. "tweet" and "post" are rarely
# nouns in conversation, so they can take "this/that". "message", "email" and
# "dm" are everyday nouns, and allowing "this" there armed on "he sent me a
# message this morning", so they only take an explicit object or "about".
_SOCIAL_ACTION_RE = re.compile(
    r"\b(?:tweet|post)\s+(?:something|anything|about|out|this|that)\b"
    r"|\b(?:dm|email|message)\s+(?:something|anything|about|out|him|her|them)\b",
    re.IGNORECASE,
)

# Verb-agnostic form, for when speech recognition mangles the verb: the live
# session that motivated this module transcribed "draft me a prompt" as "test me
# a prompt", and no verb list would have caught it. "me a <copyable noun>" is a
# request for one in practice, whatever verb precedes it.
_WANT_ARTIFACT_INDIRECT_RE = re.compile(
    rf"\b(?:me|us)\s+(?:a|an|the|one)\s+(?:\w+\s+){{0,2}}{_ARTIFACT_NOUN}\b",
    re.IGNORECASE,
)

# Question form, including the short deictic request that motivated the guard:
# "what is the command for that?" The requested object is still explicit even
# though English does not use an imperative verb.
_WANT_ARTIFACT_QUERY_RE = re.compile(
    rf"\b(?:what(?:'s| is)|which)\b[^.?!]{{0,60}}\b{_ARTIFACT_NOUN}\b",
    re.IGNORECASE,
)

# The correction a user makes when Buddy recited instead of carding and NO card
# exists yet: "put it on screen", "don't read it out loud", "I asked you to
# draft". Kept, unlike the follow-up patterns, because with the session closed
# this is an OPENING turn - it is the user's first successful request for a card
# after a failed one, and nothing else will recognize it.
_CORRECTION_RE = re.compile(
    r"(?:"
    r"don'?t (?:read|say|speak)|stop (?:reading|saying|speaking)|"
    r"put it on (?:the )?screen|on (?:the )?screen|not out loud|"
    r"spitting it out|reading it out|spelling it out|"
    r"i (?:just )?asked (?:you )?(?:to |for )(?:a |the )?(?:draft|prompt|command|code)|"
    r"give me (?:a|the) (?:draft|prompt|command|code|script)|"
    r"draft me\b"
    r")",
    re.IGNORECASE,
)

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


def wants_copyable_artifact(transcript: str) -> bool:
    """True when the finalized user turn clearly asked for copyable text.

    Deliberately narrow: a verb+noun request or an explicit "put it on screen /
    don't read it out" correction. Ordinary conversation does not match, so the
    backstop stays quiet unless the user really asked for something to copy.
    """
    if not transcript:
        return False
    return bool(
        _WANT_ARTIFACT_RE.search(transcript)
        or _WANT_ARTIFACT_INDIRECT_RE.search(transcript)
        or _WANT_ARTIFACT_QUERY_RE.search(transcript)
        or _SOCIAL_ACTION_RE.search(transcript)
        or _CORRECTION_RE.search(transcript)
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


def artifact_kind_for(transcript: str) -> tuple[str, str]:
    """Pick a present_visible_artifact (kind, title) from the request wording.

    The user's explicit noun wins, so "a prompt for Claude Code" is a prompt, not
    code, even though "code" appears in the product name.
    """
    lowered = transcript.lower()
    if re.search(r"\bprompt\b", lowered):
        return "prompt", "Prompt"
    if re.search(r"\b(command|cmd|powershell|terminal|bash|shell)\b", lowered):
        return "command", "Command"
    if re.search(r"\b(code|script|function|regex|css|html|sql|json|yaml)\b", lowered):
        return "code", "Snippet"
    # Outbound wording is checked before the generic "draft" fallback so "draft a
    # tweet" titles the card Tweet rather than Draft. present_visible_artifact has
    # no outbound_message kind (see visible_artifacts.ARTIFACT_KINDS), so these all
    # ride as notes: the card is exact text to copy, not a send.
    for pattern, title in (
        (r"\b(tweet|thread)\b", "Tweet"),
        (r"\bpost\b", "Post"),
        (r"\b(dm|direct message|message)\b", "Message"),
        (r"\bemail\b", "Email"),
        (r"\b(reply|comment)\b", "Reply"),
        (r"\bcaption\b", "Caption"),
        (r"\bbio\b", "Bio"),
    ):
        if re.search(pattern, lowered):
            return "note", title
    if re.search(r"\bdrafts?\b", lowered):
        return "note", "Draft"
    return "prompt", "Prompt"
