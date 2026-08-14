"""Deterministic current-turn authorization for text-chat reminder writes.

This module used to decide which tools Buddy was SHOWN. It no longer does, and that
inversion is the whole point of the file.

The old `excluded_tools_for_text_turn` deleted `set_reminder` from the tools array on
every turn whose wording did not match `_EXPLICIT_REMINDER_CREATE`. The model then
truthfully reported what it saw: "I don't have a set_reminder tool exposed to me right
now", fifty minutes after the same account used that tool successfully on another
surface. Worse, a user answering Buddy's own reminder question ("yeah, 7am works") fell
outside the regex, so the follow-up turn documented in architectures/chat-and-tools.md
could not complete the write it was designed for.

The gate had the right worry and the wrong polarity. Requiring a rare positive phrasing
to ALLOW makes capability presence a lottery on wording. Requiring a clear contradiction
to DENY keeps the protection and costs nothing: `_NEGATED` and `_REMINDER_STATUS` are
high-precision for the case that actually matters, a status question or a refusal being
misread as a fresh command.

So the tool is always exposed and the refusal happens at execution, where it can return a
truthful envelope Buddy can speak. Never hide a capability to prevent its misuse.
"""

from __future__ import annotations

import re

from ..lib.logger import logger

SET_REMINDER_TOOL = "set_reminder"

_NEGATED = re.compile(r"\b(?:do not|don't|dont|never)\b.{0,35}\b(?:remind|reminder|set)\b", re.I)
_REMINDER_STATUS = re.compile(
    r"\b(?:did|was|is|has|have)\b.{0,45}\breminder\b.{0,25}\b(?:set|schedule|create|work)|"
    r"\b(?:did|has|have)\b.{0,25}\b(?:set|schedule|create)\b.{0,25}\breminder\b|"
    r"\bwhy\b.{0,40}\b(?:didn(?:'|’)?t|did not|not)\b.{0,30}\b(?:set|schedule|remind)|"
    r"\bwhat happened\b.{0,35}\breminder\b",
    re.I,
)
_EXPLICIT_REMINDER_CREATE = re.compile(
    r"\b(?:please\s+)?(?:set|create|schedule|add)\b.{0,40}\breminder\b|"
    r"\b(?:please\s+)?remind me\b|"
    r"\b(?:can|could|would|will) you\b.{0,20}\bremind me\b|"
    r"\bi (?:need|want) (?:you )?to\b.{0,20}\bremind me\b",
    re.I,
)
_REMINDER_CONTEXT = re.compile(r"\b(?:remind|reminder|scheduled|set it)\b", re.I)
_REMINDER_SUCCESS_CLAIM = re.compile(
    r"\breminder\b.{0,25}\b(?:is\s+)?"
    r"(?:set|scheduled|saved|created|added|locked in|all set|ready|good to go)\b|"
    r"\b(?:set|scheduled|saved|created|added|locked in)\b.{0,25}\breminder\b|"
    r"\b(?:i(?:'|’)ll|i\s+will|i(?:'|’)ve|i\s+have)\b.{0,15}\bremind(?:ed)? you\b|"
    r"\b(?:all set|locked (?:it|that) in|you(?:'|’)re all set|"
    r"got (?:it|that) (?:set|scheduled|locked in)|it(?:'|’)s (?:all )?set)\b",
    re.I,
)
_INTERROGATIVE = re.compile(
    r"\?\s*$|^(?:what|which|when|where|how|should|shall|do|does|did|can|could|would|"
    r"want|is there|are there)\b",
    re.I,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def explicitly_requests_reminder_create(message: str) -> bool:
    """Return true only for a new reminder command in this exact user turn."""
    text = (message or "").strip()
    if not text or _NEGATED.search(text) or _REMINDER_STATUS.search(text):
        return False
    return bool(_EXPLICIT_REMINDER_CREATE.search(text))


def has_unreceipted_reminder_success_claim(text: str) -> bool:
    """Detect a declarative reminder-success claim before it reaches the client."""
    for sentence in _SENTENCE_SPLIT.split((text or "").strip()):
        if sentence and not _INTERROGATIVE.search(sentence):
            if _REMINDER_SUCCESS_CLAIM.search(sentence):
                return True
    return False


def reminder_write_contradicted_by_turn(message: str) -> str | None:
    """Return a reason code when this turn plainly does NOT authorize a new reminder.

    Only the two high-precision negatives fire: an explicit negation ("don't remind me")
    and a status question ("did you set that reminder?"). Everything else, including a
    bare continuation that answers Buddy's own question, is allowed through to the model,
    which the tool description and the prompt's conversation-authority rule already
    govern. This is a backstop against one specific misread, not a general permission
    system.
    """
    text = (message or "").strip()
    if not text:
        return None
    if _NEGATED.search(text):
        return "negated_request"
    if _REMINDER_STATUS.search(text):
        return "status_question"
    return None


def blocked_write_reasons_for_text_turn(message: str) -> dict[str, str]:
    """Per-turn execution denials, keyed by tool name, for the shared ToolExecutor."""
    reason = reminder_write_contradicted_by_turn(message)
    if reason is None:
        return {}
    logger.info(
        "reminder_write_denied_by_turn_contradiction",
        {"reason": reason},
    )
    return {SET_REMINDER_TOOL: reason}


def reminder_receipt_guard_armed(message: str, previous_assistant_reply: str = "") -> bool:
    """Whether to buffer this reply and check any reminder-success claim for a receipt.

    Armed on reminder CONTEXT rather than an explicit create command, in either this turn
    or the reply it answers. A write can now land on a turn that never says the word
    ("yeah, 7am works" after Buddy asked), and that turn is exactly where an unreceipted
    "all set" would slip through. Buffering costs the streaming feel, so it stays scoped
    to turns where a reminder is genuinely in play rather than running on every reply.
    """
    return bool(
        _REMINDER_CONTEXT.search(message or "")
        or _REMINDER_CONTEXT.search(previous_assistant_reply or "")
    )
