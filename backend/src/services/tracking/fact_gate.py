"""Fact-transition gate — the send decision for tracker moments, as pure functions.

The old engine gated sends on TEXT: a sha256 of the composed summary as the dedup
key, exact string equality against the last sent summary. LLM compositions are never
byte-identical, so every reworded fetch of the SAME state passed every layer — the
2026-07-10 incident delivered the fact "Spain & Belgium advanced" six times in six
wordings. The perverse property: the more fluent the model, the more duplicates.

This module inverts that. Identity lives in STRUCTURED STATE:

  - A fixture carries a persisted ``FactState`` (status / score / winner / note).
  - A result moment may send only when the freshly-extracted facts TRANSITION the
    fixture's status to a send-worthy destination (finished, cancelled).
  - The orchestrator dedup key derives from ``(topic_key, fixture_id, destination)``
    — wording-independent, so the composer is free to write however it likes and the
    same real-world state can still never send twice.

Everything here is pure (no I/O) so the whole transition table is unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import fields as f

# Content published before (fixture start - this margin) cannot describe the fixture's
# own outcome — it is preview/context material at best. Used both by the fetcher's
# not_before cutoff and by content_within_window below.
CONTENT_WINDOW_LEAD = timedelta(hours=2)

_VALID_FACT_STATUSES = {
    f.FIXTURE_STATUS_SCHEDULED,
    f.FIXTURE_STATUS_LIVE,
    f.FIXTURE_STATUS_FINISHED,
    f.FIXTURE_STATUS_CANCELLED,
}

# Destinations worth a push from a RESULT moment. A move to ``live`` is real state
# (persisted on the fixture) but not result-worthy — the kickoff moment owns "it
# started", so pushing it from a result check would duplicate that moment.
SEND_WORTHY_RESULT_DESTINATIONS = {
    f.FIXTURE_STATUS_FINISHED,
    f.FIXTURE_STATUS_CANCELLED,
}


@dataclass(frozen=True)
class FactState:
    """The structured facts of one fixture at one moment in time."""

    status: str = f.FIXTURE_STATUS_SCHEDULED
    score: str = ""
    winner: str = ""
    note: str = ""

    def as_map(self) -> dict[str, str]:
        """Flat map shape for the fire-audit record."""
        return {
            "status": self.status,
            "score": self.score,
            "winner": self.winner,
            "note": self.note,
        }


def coerce_fact_status(raw: str) -> str:
    """Normalize a model-reported status to the closed set, defaulting to scheduled.
    An off-list status must coerce (never flow raw into the fixture doc) or the
    transition table stops being a closed state machine."""
    value = (raw or "").strip().lower()
    return value if value in _VALID_FACT_STATUSES else f.FIXTURE_STATUS_SCHEDULED


def extract_transition(prior: FactState, seen: FactState) -> str | None:
    """The status transition ``seen`` implies over ``prior``, or None when the facts
    do not move the fixture forward.

    Only FORWARD movement counts: scheduled -> live -> finished, or any -> cancelled.
    A seen status equal to or BEHIND the prior one (a stale article describing the
    match as upcoming after it finished — the "kicks off soon, 40 minutes after
    kickoff" bug) is None, never a transition back."""
    order = {
        f.FIXTURE_STATUS_SCHEDULED: 0,
        f.FIXTURE_STATUS_LIVE: 1,
        f.FIXTURE_STATUS_FINISHED: 2,
    }
    if prior.status == f.FIXTURE_STATUS_CANCELLED:
        return None  # terminal; nothing moves a cancelled fixture
    if seen.status == f.FIXTURE_STATUS_CANCELLED:
        return f"{prior.status}->{f.FIXTURE_STATUS_CANCELLED}"
    if order.get(seen.status, 0) > order.get(prior.status, 0):
        return f"{prior.status}->{seen.status}"
    return None


def transition_destination(transition: str) -> str:
    """The destination status of a transition string ("scheduled->finished" -> "finished")."""
    return transition.rsplit("->", 1)[-1] if transition else ""


def is_result_send_worthy(transition: str | None) -> bool:
    """True when a result moment should push for this transition."""
    return transition is not None and transition_destination(transition) in SEND_WORTHY_RESULT_DESTINATIONS


def result_dedup_key(topic_key: str, fixture_id: str, transition: str) -> str:
    """Orchestrator dedup key for a result push. Keyed on the DESTINATION state, not
    the edge: "scheduled->finished" and "live->finished" describe the same real-world
    send-worthy fact (the fixture finished), so both map to one key and the second
    can never send."""
    return f"tracker_{topic_key}_{fixture_id}_{transition_destination(transition)}"


def moment_dedup_key(topic_key: str, fixture_id: str, moment: str) -> str:
    """Orchestrator dedup key for the fetchless moments (pre, kickoff). One per
    (fixture, moment) by construction."""
    return f"tracker_{topic_key}_{fixture_id}_{moment}"


def development_dedup_key(topic_key: str, development_key: str) -> str:
    """Orchestrator dedup key for a pulse development push."""
    return f"tracker_{topic_key}_dev_{development_key}"


def slug_development_key(text: str) -> str:
    """Normalize the pulse compose's development_key so trivial rewordings collide:
    lowercased, alphanumeric tokens joined by '-', capped. An empty result means the
    model gave nothing concrete — the caller abstains."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return re.sub(r"-{2,}", "-", slug).strip("-")[:64].strip("-")


def content_window_start(fixture_start_at: datetime) -> datetime:
    """The earliest publish time an article may carry and still describe this
    fixture's own outcome (rather than a preview of it or a recap of another one)."""
    return fixture_start_at - CONTENT_WINDOW_LEAD


def content_within_window(published: datetime | None, *, fixture_start_at: datetime) -> bool:
    """Whether fetched content could plausibly describe THIS fixture. ``None`` (a
    tier with no publish dates, e.g. brave/grounded) passes — the extraction LLM's
    refers_to_this_fixture judgment is the gate there instead."""
    if published is None:
        return True
    return published >= content_window_start(fixture_start_at)
