"""Deterministic current-turn authorization for text-chat reminder writes."""

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


def excluded_tools_for_text_turn(message: str) -> frozenset[str]:
    """Hide reminder creation unless the current turn explicitly requests it."""
    if explicitly_requests_reminder_create(message):
        return frozenset()
    if _REMINDER_CONTEXT.search(message or ""):
        logger.info(
            "reminder_write_tool_denied_by_intent_gate",
            {"reason": "no_explicit_current_turn_create"},
        )
    return frozenset({SET_REMINDER_TOOL})
