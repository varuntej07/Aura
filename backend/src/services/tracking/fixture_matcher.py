"""Fixture identity — minting stable fixture IDs and matching a fresh research pass
onto the stored fixtures WITHOUT ever forking a parallel series.

The 2026-07-10 incident: the daily reconcile reworded one real match's label across
passes ("Quarterfinal 3" -> "Quarter-final - Match 98" -> "Quarter-final:
Portugal/Spain Winner vs USA/Belgium Winner" -> "Quarter-final: Spain vs Belgium").
Checkpoint identity derived from a slug of that label, so each rewording minted a
brand-new parallel polling series for the SAME match — 4+ series, 1,190 checkpoint
docs, 19 pushes in a day. No string-matching can unify "Match 98" with "Spain vs
Belgium"; the connection exists only in the bracket's structure, which text loses.

So identity is minted ONCE, from the fixture's start slot, and never re-derived:

  mint_fixture_id      "20260710-1800" (+ "-b"/"-c" on a same-slot collision)
  reconcile_fixtures   resolves each freshly-researched fixture to a stored one by
                       (1) the fixture_id the reconcile LLM echoed, then
                       (2) start-time proximity + label-token overlap (the
                           deterministic backstop, lifted from the old poll-grid
                           engine's anchor matcher), and only then
                       (3) mints a new id.

Pure functions (no I/O) — the engine reads/writes Firestore around these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import fields as f
from .models import Fixture, ResearchedFixture

# How close two start times must be to consider "same fixture, reworded label".
FIXTURE_MATCH_WINDOW = timedelta(hours=3)
# A bracket-slot placeholder's estimated kickoff can be hours off (observed ~4h in
# prod) since it is guessed before the fixture is confirmed — widened window, and the
# token-overlap requirement is skipped (a placeholder shares zero tokens with real
# team names by construction).
PLACEHOLDER_MATCH_WINDOW = timedelta(hours=8)

# A stored fixture this far in the future, unmatched by a substantial fresh pass, is
# treated as dropped from the schedule (cancelled). Anything nearer is left alone —
# research passes routinely omit imminent fixtures they consider "already covered",
# and cancelling a fixture hours before kickoff on that noise would kill its moments.
CANCEL_UNMATCHED_HORIZON = timedelta(hours=48)
# ...and only when the fresh pass returned at least this many fixtures (an empty or
# one-line pass is "no information", never "everything else is cancelled").
CANCEL_MIN_FRESH_FIXTURES = 2

# Typical span duration when research gave no end time: a match/ceremony runs ~2h and
# the result is determinable shortly after.
DEFAULT_SPAN_DURATION = timedelta(hours=2, minutes=15)
# A point fixture (verdict, launch) has its result determinable right at the moment;
# the result moment fires shortly after to let coverage appear.
POINT_RESULT_LAG = timedelta(minutes=5)

_PLACEHOLDER_WORDS = re.compile(r"(?i)\b(?:winner|loser|tbd)\b")
_LABEL_STOPWORDS = {
    "vs", "v", "the", "of", "match", "round", "group", "stage", "final",
    "semifinal", "semi", "quarterfinal", "quarter", "winner", "loser", "tbd",
}

_MINT_SUFFIXES = "abcdefghij"


@dataclass
class FixturePlan:
    """Output of one reconcile pass. ``updates`` are stored fixtures refreshed in
    place (label/times, id unchanged); ``creates`` are genuinely new fixtures with
    freshly-minted ids; ``cancel_ids`` are stored fixture ids confidently dropped
    from the schedule."""

    updates: list[Fixture] = field(default_factory=list)
    creates: list[Fixture] = field(default_factory=list)
    cancel_ids: list[str] = field(default_factory=list)


def _label_tokens(label: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (label or "").lower())
    return {w for w in words if w and w not in _LABEL_STOPWORDS}


def _is_placeholder_label(label: str) -> bool:
    return bool(_PLACEHOLDER_WORDS.search(label or ""))


def _is_generic_label(label: str) -> bool:
    """A label with no distinctive ALPHABETIC token — "Quarterfinal 3", "Quarter-final
    - Match 98" — names a slot, not the teams in it. Such a label can never share a
    token with the resolved wording ("Spain vs Belgium"), so requiring overlap would
    fork the series exactly the way prod did; a generic label matches on start-time
    proximity alone instead."""
    return not any(not token.isdigit() for token in _label_tokens(label))


def mint_fixture_id(start_at: datetime, existing_ids: set[str]) -> str:
    """A stable id from the fixture's UTC start slot, never from its label. Two
    fixtures genuinely sharing a slot (simultaneous kickoffs) get a deterministic
    letter suffix. Exhausting the suffixes (11+ simultaneous fixtures) appends a
    numeric counter rather than colliding."""
    base = start_at.strftime("%Y%m%d-%H%M")
    if base not in existing_ids:
        return base
    for suffix in _MINT_SUFFIXES:
        candidate = f"{base}-{suffix}"
        if candidate not in existing_ids:
            return candidate
    counter = 2
    while f"{base}-x{counter}" in existing_ids:
        counter += 1
    return f"{base}-x{counter}"


def expected_end_of(researched: ResearchedFixture) -> datetime:
    """When the fixture's outcome should be determinable — research's own end time
    when it gave one, else start + a typical duration (span) or a short lag (point)."""
    if researched.end_at is not None and researched.end_at > researched.start_at:
        return researched.end_at
    if researched.event_kind == f.EVENT_KIND_POINT:
        return researched.start_at + POINT_RESULT_LAG
    return researched.start_at + DEFAULT_SPAN_DURATION


def _as_new_fixture(
    researched: ResearchedFixture, *, topic_key: str, fixture_id: str, now: datetime,
) -> Fixture:
    return Fixture(
        id=fixture_id,
        topic_key=topic_key,
        label=researched.label,
        start_at=researched.start_at,
        expected_end_at=expected_end_of(researched),
        kind=researched.event_kind,
        lead_minutes=researched.lead_minutes,
        wake_override=researched.wake_override,
        status=f.FIXTURE_STATUS_SCHEDULED,
        created_at=now,
        updated_at=now,
    )


def _refreshed(existing: Fixture, researched: ResearchedFixture, *, now: datetime) -> Fixture:
    """The stored fixture with the fresh pass's label/times applied IN PLACE. Fact
    state and id are untouched — a reconcile refreshes the schedule, it never
    rewrites what already happened."""
    return Fixture(
        id=existing.id,
        topic_key=existing.topic_key,
        label=researched.label,
        start_at=researched.start_at,
        expected_end_at=expected_end_of(researched),
        kind=researched.event_kind,
        lead_minutes=researched.lead_minutes or existing.lead_minutes,
        # wake_override only ever escalates: research marking a fixture can't-miss
        # sticks even if a later pass forgets it (a final never becomes routine).
        wake_override=existing.wake_override or researched.wake_override,
        status=existing.status,
        fact_score=existing.fact_score,
        fact_winner=existing.fact_winner,
        fact_note=existing.fact_note,
        facts_updated_at=existing.facts_updated_at,
        last_transition=existing.last_transition,
        created_at=existing.created_at,
        updated_at=now,
    )


def _match_by_time_and_tokens(
    researched: ResearchedFixture, candidates: dict[str, Fixture],
) -> str | None:
    """The deterministic backstop matcher (the LLM's echoed id claims first, before
    this runs). A stored fixture matches when its start is within the window AND:

      - the labels share a token ("Spain vs Belgium" ~ "Quarter-final: Spain vs
        Belgium") — the strongest signal, preferred over the loose matches below; or
      - either label is a bracket-slot placeholder ("Portugal/Spain Winner vs …"),
        which shares zero tokens with the resolved teams by construction — widened
        window, since a placeholder's estimated kickoff ran ~4h off in prod; or
      - either label is generic ("Quarterfinal 3", "Match 98") — a slot name, not
        team names, so start-time proximity is the only signal there is.

    Token-overlap matches are preferred over loose ones, then nearest start time —
    so two simultaneous kickoffs with one distinctive label each still pair up
    correctly before the generic leftovers claim by time."""
    best_key: tuple[int, timedelta] | None = None
    best_id: str | None = None
    fresh_tokens = _label_tokens(researched.label)
    fresh_loose = _is_placeholder_label(researched.label) or _is_generic_label(researched.label)
    for fixture_id, fixture in candidates.items():
        placeholder = _is_placeholder_label(fixture.label) or _is_placeholder_label(researched.label)
        window = PLACEHOLDER_MATCH_WINDOW if placeholder else FIXTURE_MATCH_WINDOW
        delta = abs(researched.start_at - fixture.start_at)
        if delta > window:
            continue
        if fresh_tokens & _label_tokens(fixture.label):
            tier = 0
        elif placeholder or fresh_loose or _is_generic_label(fixture.label):
            tier = 1
        else:
            continue
        key = (tier, delta)
        if best_key is None or key < best_key:
            best_key, best_id = key, fixture_id
    return best_id


def reconcile_fixtures(
    existing: list[Fixture],
    fresh: list[ResearchedFixture],
    *,
    topic_key: str,
    now: datetime,
) -> FixturePlan:
    """Resolve a fresh research pass onto the stored fixtures.

    Each fresh fixture is matched to at most one stored fixture (greedy,
    echoed-id first, then nearest start time); each stored fixture is claimed at
    most once. Unmatched fresh fixtures are created with minted ids. Stored
    SCHEDULED fixtures far enough out (beyond CANCEL_UNMATCHED_HORIZON) that a
    substantial fresh pass no longer lists are cancelled; anything nearer, or
    already live/finished, is never touched by absence — an empty or thin research
    pass means "no information", not "the schedule vanished" (the same never-nuke
    rule the old plan_reconcile learned the hard way)."""
    plan = FixturePlan()
    available: dict[str, Fixture] = {fx.id: fx for fx in existing}
    taken_ids: set[str] = set(available.keys())

    # Echoed ids claim first — the LLM was shown the stored fixtures and recognized
    # these outright, so they must not lose their match to a nearer-in-time sibling.
    ordered = sorted(
        range(len(fresh)),
        key=lambda i: (0 if fresh[i].echoed_fixture_id in available else 1, fresh[i].start_at),
    )
    for idx in ordered:
        researched = fresh[idx]
        matched_id = (
            researched.echoed_fixture_id
            if researched.echoed_fixture_id in available
            else _match_by_time_and_tokens(researched, available)
        )
        if matched_id is not None:
            plan.updates.append(_refreshed(available.pop(matched_id), researched, now=now))
            continue
        new_id = mint_fixture_id(researched.start_at, taken_ids)
        taken_ids.add(new_id)
        plan.creates.append(_as_new_fixture(researched, topic_key=topic_key, fixture_id=new_id, now=now))

    if len(fresh) >= CANCEL_MIN_FRESH_FIXTURES:
        for fixture in available.values():
            if (
                fixture.status == f.FIXTURE_STATUS_SCHEDULED
                and fixture.start_at > now + CANCEL_UNMATCHED_HORIZON
            ):
                plan.cancel_ids.append(fixture.id)

    return plan
