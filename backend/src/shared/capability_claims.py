"""Detect Buddy claiming a limitation Aura does not have.

Read-only. Nothing here changes what the user sees; it exists so the failure is
COUNTABLE. The class of bug it watches for was invisible for weeks: Buddy told a user
"the desktop and mobile apps don't sync your schedule automatically, they're kind of like
separate notebooks right now" while twelve reminders they made on desktop were already
firing on both devices, and separately "I don't have a set_reminder tool exposed to me
right now" while that tool sat in its own tool list. Neither produced an error, a metric,
or a log line. The only reason we know is that the user got angry enough to complain.

Two verdicts, and the difference matters:

``confirmed_false``  The reply denies a tool that IS in this turn's exposed list. We hold
                     the ground truth, so this is a fact, not a heuristic. Any nonzero
                     count is a live bug in tool exposure.
``suspected``        The reply asserts a cross-device or sync limitation. There is no
                     runtime oracle for prose, so this is a lead to read, not a verdict.
                     Judge these by reading the quotes, never by the count alone.

Deliberately not a rewriter. A wrong rewrite mid-answer is worse than the claim it
replaces, and until the counts say prevention failed there is nothing to rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONFIRMED_FALSE = "confirmed_false"
SUSPECTED = "suspected"

# Sentence splitter matching action_intent_policy's, so both guards agree on what a
# sentence is.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# "I don't have a set_reminder tool", "there's no reminder tool available to me",
# "that tool isn't exposed to me right now", "I can't see a calendar tool".
_TOOL_DENIAL = re.compile(
    r"\b(?:i (?:don'?t|do not) have|i (?:don'?t|do not) see|i lack|i can'?t see|"
    r"there(?:'|’)?s no|there is no|no)\b[^.!?]{0,60}?\btool\b|"
    r"\btool\b[^.!?]{0,40}?\b(?:is|isn'?t|is not|not)\b[^.!?]{0,25}?"
    r"\b(?:exposed|available|enabled|accessible)\b",
    re.I,
)

# Which capability the denial is about. Maps the words Buddy actually uses onto the tool
# names it was given, since it says "reminder tool" far more often than "set_reminder".
_CAPABILITY_ALIASES: dict[str, frozenset[str]] = {
    "set_reminder": frozenset({"set_reminder", "reminder", "reminders", "remind"}),
    "list_reminders": frozenset({"list_reminders", "reminder", "reminders"}),
    "cancel_reminder": frozenset({"cancel_reminder", "reminder", "reminders"}),
    "get_upcoming_events": frozenset({"get_upcoming_events", "calendar", "schedule"}),
    "create_calendar_event": frozenset({"create_calendar_event", "calendar"}),
    "update_calendar_event": frozenset({"update_calendar_event", "calendar"}),
    "store_memory": frozenset({"store_memory", "memory", "remember"}),
    "query_memory": frozenset({"query_memory", "memory"}),
    "delete_memory": frozenset({"delete_memory", "memory", "forget"}),
    "web_surf": frozenset({"web_surf", "search", "web"}),
    "send_email": frozenset({"send_email", "email"}),
    "list_emails": frozenset({"list_emails", "email", "inbox"}),
    "read_email": frozenset({"read_email", "email"}),
    "track_topic": frozenset({"track_topic", "track", "tracking"}),
    "start_research": frozenset({"start_research", "research"}),
}

# A negative claim about how Aura's surfaces relate to each other.
#
# The negation has to be ADJACENT to the continuity word, not merely present in the same
# sentence. "Both apps share the same account, so nothing is separate" contains both a
# negation and a continuity word while asserting the exact opposite of a limitation, and
# the product-truth prompt block makes Buddy say sentences like that on purpose. Flagging
# them would bury the real signal under the fix's own output.
_NEGATION = (
    r"(?:do(?:n'?t| not)|does(?:n'?t| not)|did(?:n'?t| not)|can'?t|cannot|"
    r"won'?t|will not|isn'?t|is not|aren'?t|are not|no|not|never)"
)
_CONTINUITY = (
    r"(?:sync(?:ed|s|ing)?|carry over|carries over|shared?|transfer(?:s|red)?|"
    r"talk to each other|see each other|connected|the same)"
)
_CONTINUITY_LIMITATION = re.compile(
    # "don't sync", "won't carry over", "not shared"
    rf"\b{_NEGATION}\b[^.!?]{{0,30}}?\b{_CONTINUITY}\b|"
    # "sync ... isn't automatic", "shared? no"
    rf"\b{_CONTINUITY}\b[^.!?]{{0,20}}?\b{_NEGATION}\b|"
    # "separate notebooks", "two different apps", "each app is independent". The
    # separateness word must attach to a thing, or "nothing is separate" (an
    # affirmation of continuity) reads as its own opposite.
    r"\b(?:separate|independent|isolated|standalone|different)\b[^.!?]{0,20}?"
    r"\b(?:apps?|devices?|notebooks?|versions?|accounts?|copies|lists?|worlds?)\b|"
    r"\b(?:apps?|devices?|notebooks?|versions?)\b[^.!?]{0,20}?"
    r"\b(?:are|is|stay|remain)\b[^.!?]{0,15}?"
    r"\b(?:separate|independent|isolated|standalone)\b",
    re.I,
)
_SURFACE_NOUN = re.compile(
    r"\bdesktop\b|\bmobile\b|\bphone\b|\bpc\b|\bcomputer\b|\blaptop\b|"
    r"\bdevices?\b|\bapps\b|\bkeyboard\b|\bversions?\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class CapabilityClaim:
    """One sentence worth investigating, with why it was flagged."""

    verdict: str
    sentence: str
    tool: str | None = None


def _denied_tool(sentence: str, exposed_tools: frozenset[str]) -> str | None:
    """The exposed tool this sentence denies having, if any.

    Only returns a name when the tool is genuinely in ``exposed_tools``: Buddy saying it
    has no email tool while it really has none is honest, and flagging that would bury
    the real signal.

    An exact tool name in the sentence wins. Failing that the match is by category word
    ("reminder"), which several tools share, so it reports the CATEGORY rather than
    picking one of them. A log that names set_reminder when the sentence said "reminder"
    is inventing detail, which is the same sin this module exists to catch.
    """
    words = set(re.findall(r"[a-z_]+", sentence.casefold()))
    for tool in sorted(exposed_tools):
        if tool in words:
            return tool
    for tool in sorted(exposed_tools):
        aliases = _CAPABILITY_ALIASES.get(tool)
        if aliases:
            matched = words & aliases
            if matched:
                return sorted(matched)[0]
    return None


def detect_false_capability_claims(
    text: str, *, exposed_tools: frozenset[str] = frozenset()
) -> list[CapabilityClaim]:
    """Flag sentences where Buddy states a boundary Aura does not actually have."""
    claims: list[CapabilityClaim] = []
    for sentence in _SENTENCE_SPLIT.split((text or "").strip()):
        stripped = sentence.strip()
        if not stripped:
            continue
        if _TOOL_DENIAL.search(stripped):
            tool = _denied_tool(stripped, exposed_tools)
            if tool is not None:
                claims.append(
                    CapabilityClaim(
                        verdict=CONFIRMED_FALSE, sentence=stripped, tool=tool
                    )
                )
                continue
        if _SURFACE_NOUN.search(stripped) and _CONTINUITY_LIMITATION.search(stripped):
            claims.append(CapabilityClaim(verdict=SUSPECTED, sentence=stripped))
    return claims


def log_false_capability_claims(
    text: str,
    *,
    exposed_tools: frozenset[str] = frozenset(),
    surface: str,
    user_id: str = "",
    session_id: str = "",
) -> list[CapabilityClaim]:
    """Detect and log. Returns the claims so a caller can add its own telemetry.

    Logged at warn with the verbatim sentence on purpose. A count alone cannot tell a
    real confabulation from a regex artefact, and the whole point of this module is to
    end an era where we found out from an angry user instead of from a log.
    """
    from ..lib.logger import logger

    claims = detect_false_capability_claims(text, exposed_tools=exposed_tools)
    for claim in claims:
        logger.warn(
            "buddy_invented_aura_limitation",
            {
                "verdict": claim.verdict,
                "sentence": claim.sentence,
                "tool": claim.tool,
                "surface": surface,
                "user_id": user_id,
                "session_id": session_id,
                "exposed_tool_count": len(exposed_tools),
            },
        )
    return claims
