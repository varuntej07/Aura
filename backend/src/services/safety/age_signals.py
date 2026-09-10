"""Append-only capture of age statements that contradict the declared account age.

WHY THIS EXISTS
On 2026-09-08 a user whose account declared a date of birth making them 20 told
Buddy in chat that they were 15. Buddy said "Locked in.", and the statement was
written to ``users/{uid}/memories`` as ``key: "age", value: "15 years old",
category: "facts"`` - a PERSONALIZATION fact. Nothing else happened. Consent
stayed granted, profiling stayed on, no one was told.

That is the worst of the three available states. Not knowing is defensible.
Knowing and acting is defensible. Knowing, writing it durably to disk, and
treating it as a preference is neither, and it converts an unknown into evidence
that the system held the information and did nothing with it.

WHAT THIS DOES, AND DELIBERATELY DOES NOT DO
It records the signal and raises it for a human. It changes NOTHING at runtime:
no band is overridden, no consent is revoked, no feature is withdrawn, no
account is touched. That is a deliberate product decision, not an omission.

The reason is precision. The trigger is the model choosing a reserved key on the
memory tool, which is a good signal but not a certain one: it cannot distinguish
"I am 15" from a quoted line, a past-tense reminiscence, or a figure of speech,
and a third-party age ("my son is 15") is only excluded because it would not
exact-match a reserved key. Pairing an imprecise signal with an automatic
restriction would land false positives on real users as an unexplained
downgrade. Pairing it with human review costs one glance and harms nobody.

Rows are append-only and never overwritten, so the review trail is the history
rather than the latest state.

WHAT MAY PRODUCE A SIGNAL
Only the user's own conversational turn, by way of a tool call the model made on
that turn. Text arriving inside ``<screen_ui_context>``, web results,
attachments or documents is untrusted third-party content by contract and must
never reach this module; an age read off somebody else's screen says nothing
about the account holder.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ...lib.logger import logger
from ..firebase import admin_firestore
from .age_band import AgeBand, AgeBandDecision

AGE_SIGNALS_COLLECTION = "age_signals"

# Memory keys that describe how old the user is. A `store_memory` call naming
# one of these is refused and routed here instead.
#
# EXACT MATCH ONLY, never substring. "age" is a substring of "agenda",
# "average_bedtime", "management" and "language", all of which are legitimate
# things to remember about somebody. A substring rule would refuse them and
# would be the second literal-match-over-free-text mistake this codebase has
# written up (see lessons-learnt.text, "A literal-match rule over user speech is
# a guess wearing the costume of a check").
#
# This gate is NOT that mistake, and the distinction is the reason it is
# allowed: it does not read the user's words at all. It reads a structured
# argument the MODEL chose, which the same lesson classifies as a structural
# fact worth gating on. The judgement half - deciding whether a sentence was
# about the user's age - stays with the model, expressed through the tool
# description in shared/tools.py.
RESERVED_AGE_KEYS = frozenset({
    "age",
    "user_age",
    "my_age",
    "dob",
    "date_of_birth",
    "birth_date",
    "birthdate",
    "birthday",
    "birth_year",
    "year_of_birth",
    "grade",
    "school_grade",
    "grade_level",
})


def normalize_memory_key(key: str) -> str:
    """Fold a model-chosen key to its comparison form.

    Spaces and hyphens become underscores so "date of birth", "date-of-birth"
    and "date_of_birth" are one key rather than three ways past the gate.
    """
    return "_".join(key.strip().lower().replace("-", " ").replace("_", " ").split())


def is_reserved_age_key(key: str) -> bool:
    """True when this memory key describes the user's own age."""
    return normalize_memory_key(key) in RESERVED_AGE_KEYS

# One stable, greppable marker so an alert can be built on the logs alone,
# before any dashboard exists. Do not reword it; it is the query.
LOG_CODE_AGE_SIGNAL = "age_signal_recorded"

# Schema version on every row. Reviewing a mixed-shape collection months later
# is the thing that makes an audit trail useless, so stamp it from row one.
AGE_SIGNAL_SCHEMA_VERSION = 1


async def record_age_signal(
    uid: str,
    *,
    stated_value: str,
    declared: AgeBandDecision,
    surface: str,
    session_id: str = "",
    memory_key: str = "",
) -> str | None:
    """Append one age signal. Best effort: never raises, never blocks the turn.

    Returns the new document id, or None if the write failed.

    Failure is swallowed on purpose. This runs inside a live conversational turn
    and a Firestore blip must not cost the user their reply. The compensating
    control is the log line, which is emitted BEFORE the write is attempted, so
    a lost write still leaves evidence that the signal occurred.
    """
    now = datetime.now(UTC)
    # `conflict` is the whole reason a human is being asked to look. An account
    # that already declares a minor age and says so again is consistent, not
    # contradictory, and should not compete for review attention.
    conflict = declared.band == AgeBand.ADULT

    # Logged first, and without the stated value. The value is a minor's
    # self-reported age: it belongs in the reviewable row, not in log
    # aggregation, which has a different retention and audience.
    logger.warn(
        "safety: age signal recorded from conversation",
        {
            "error_code": LOG_CODE_AGE_SIGNAL,
            "user_id": uid,
            "declared_band": declared.band.value,
            "declared_age": declared.declared_age,
            "conflict": conflict,
            "surface": surface,
            "memory_key": memory_key,
        },
    )

    payload: dict[str, Any] = {
        "schema_version": AGE_SIGNAL_SCHEMA_VERSION,
        "stated_value": stated_value[:200],
        "memory_key": memory_key[:120],
        "declared_band": declared.band.value,
        "declared_age": declared.declared_age,
        "date_of_birth": declared.date_of_birth,
        "conflict": conflict,
        "surface": surface,
        "session_id": session_id,
        "created_at": now.isoformat(),
        # Review state. Nothing in the runtime reads these; they exist so a
        # human can close the loop and so a second signal from the same user
        # does not look unhandled forever.
        "resolved": False,
        "resolution": None,
    }

    signal_id = str(uuid4())

    def _write() -> None:
        (
            admin_firestore()
            .collection("users")
            .document(uid)
            .collection(AGE_SIGNALS_COLLECTION)
            .document(signal_id)
            .set(payload)
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error(
            "safety: age signal write failed, log line is the only record",
            {"error_code": "age_signal_write_failed", "user_id": uid, "error": str(exc)},
        )
        return None
    return signal_id
