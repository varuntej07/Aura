"""Deterministic finalized-speech routing for explicit desktop screen captures.

This is deliberately not an LLM tool. Only the authenticated user's finalized
transcript is inspected; interim STT, screen text, memory, and assistant output
can never authorize persistence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPACE_RE = re.compile(r"\s+")
_CLAUSE_RE = re.compile(r"\s*(?:[.!?;]+|\b(?:and then|then|also|and)\b)\s*", re.I)
_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|dont|never|never\s+mind|nevermind|stop|cancel)\b",
    re.I,
)
_META_RE = re.compile(
    r"^(?:why|how|what\s+(?:does|happens)|do\s+you\s+have|is\s+there|"
    r"tell\s+me\s+about|explain)\b",
    re.I,
)
_QUOTED_CONTEXT_RE = re.compile(
    r"^(?:he|she|they|someone)\s+(?:said|asked)|^(?:say|quote|the\s+phrase)\b",
    re.I,
)
_CAPABILITY_META_RE = re.compile(r"\b(?:tool|ability|capability)\b", re.I)

_SCREEN_OBJECT = r"(?:(?:my|this|the|current|a)\s+)?(?:screen|screen\s+shot|screenshot)"
_POLITE_SUFFIX = (
    r"(?:\s+(?:for\s+me|now|right\s+now|please|again|another\s+copy))*"
)
_ACTION = (
    rf"(?:save|capture)\s+(?:{_SCREEN_OBJECT}|what(?:'s|\s+is)\s+on\s+my\s+screen)"
    rf"{_POLITE_SUFFIX}"
    rf"|take\s+(?:(?:a|the|this)\s+)?(?:screen\s+shot|screenshot)"
    rf"(?:\s+of\s+(?:my|the|this)\s+screen)?{_POLITE_SUFFIX}"
    rf"|screenshot\s+(?:this|my\s+screen|the\s+screen){_POLITE_SUFFIX}"
)
_REQUEST_RE = re.compile(
    rf"^(?:(?:okay|ok|yes|yeah|no|hey\s+buddy|buddy)\s+)?(?:please\s+)?(?:"
    rf"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:{_ACTION})"
    rf"|i\s+(?:want|need)\s+you\s+to\s+(?:please\s+)?(?:{_ACTION})"
    rf"|i\s+told\s+you\s+to\s+(?:please\s+)?(?:{_ACTION})"
    rf")(?:\s+please)?$",
    re.I,
)
_DEICTIC_RE = re.compile(
    r"^(?:please\s+)?(?:save(?:\s+(?:it|this))?|do\s+it|try\s+again)(?:\s+please)?$",
    re.I,
)
_REINFORCEMENT = frozenset(
    {
        "save",
        "do it",
        "right now",
        "for me",
        "i told you",
        "yes",
        "yeah",
        "no",
        "okay",
        "ok",
    }
)


@dataclass(frozen=True, slots=True)
class ScreenCaptureCommand:
    """One accepted capture clause plus any unrelated request that remains."""

    normalized_transcript: str
    remainder: str
    command_only: bool
    allow_duplicate: bool


def match_screen_capture_command(
    transcript: str,
    *,
    allow_deictic_retry: bool = False,
) -> ScreenCaptureCommand | None:
    """Recognize a narrow explicit screen-capture request without semantic inference."""
    original = (transcript or "").strip()
    normalized = _normalize(original)
    if not normalized:
        return None
    if _NEGATION_RE.search(normalized):
        return None
    if _META_RE.search(normalized) or _CAPABILITY_META_RE.search(normalized):
        return None
    if _QUOTED_CONTEXT_RE.search(normalized):
        return None
    if any(mark in original for mark in ('"', '“', '”')):
        return None

    clauses = tuple(clause for clause in _CLAUSE_RE.split(normalized) if clause)
    matched: list[str] = []
    remainder: list[str] = []
    for clause in clauses:
        if _REQUEST_RE.fullmatch(clause):
            matched.append(clause)
        elif allow_deictic_retry and _DEICTIC_RE.fullmatch(clause):
            matched.append(clause)
        else:
            remainder.append(clause)

    if not matched:
        return None

    meaningful_remainder = [part for part in remainder if part not in _REINFORCEMENT]
    return ScreenCaptureCommand(
        normalized_transcript=normalized,
        remainder=". ".join(meaningful_remainder),
        command_only=not meaningful_remainder,
        allow_duplicate="another copy" in normalized,
    )


def _normalize(value: str) -> str:
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^a-z0-9'\s.!?;]", " ", value)
    return _SPACE_RE.sub(" ", value).strip(" .!?;")
