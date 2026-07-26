"""Deterministic backstop for copyable answers that get spoken instead of carded.

The system prompt and tool skills route "draft me a prompt / command / code" to
present_visible_artifact (see voice_prompt.py). This module is the safety net for
the turns where the model narrates that copyable text anyway: it is a narrow,
OUTPUT-side intent match (it never removes tools pre-inference, only adds a card
after the fact) so the user always ends up with something to copy, even on the
turn the model spoke it.

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
    r"json|yaml|function|css|html|sql|markdown|text to (?:paste|copy)|"
    r"message to (?:paste|copy))"
)
_WANT_ARTIFACT_RE = re.compile(
    rf"\b{_ARTIFACT_VERB}\b[^.?!]{{0,80}}?\b{_ARTIFACT_NOUN}\b",
    re.IGNORECASE,
)

# Explicit corrections a frustrated user makes when it keeps speaking copyable
# content: "I asked you to draft", "put it on screen", "don't read it out loud",
# "why are you spitting it out", "give me a prompt".
_CORRECTION_RE = re.compile(
    r"(?:"
    r"don'?t (?:read|say|speak)|stop (?:reading|saying|speaking)|"
    r"put it on (?:the )?screen|on (?:the )?screen|not out loud|"
    r"spitting it out|reading it out|spelling it out|"
    r"i (?:just )?asked (?:you )?(?:to |for )(?:a |the )?(?:draft|prompt|command|code)|"
    r"give me (?:a|the) (?:prompt|command|code|script)"
    r")",
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
    return bool(_WANT_ARTIFACT_RE.search(transcript) or _CORRECTION_RE.search(transcript))


def looks_copyable(text: str) -> bool:
    """True when spoken text is substantial enough to be worth a card.

    Guards against carding a short confirmation ("sure, one sec"). A code fence,
    a line break, or a reasonably long block all qualify as copyable content.
    """
    if not text:
        return False
    stripped = text.strip()
    if "```" in stripped or "\n" in stripped:
        return True
    return len(stripped) >= 120


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
    return "prompt", "Prompt"
