"""Topic tracking engine — the two scheduler-driven loops, rebuilt around fixtures,
moments, and fact transitions (2026-07-10 redesign; see fact_gate / fixture_matcher /
moments for the pieces).

run_checkpoint_tick()  (every minute): drain the due-queue. A due doc is either a
  fixture MOMENT (pre | kickoff | result) or the topic's recurring PULSE:

    pre / kickoff  fetchless — the fixture doc itself is the fact source; hard
                   temporal guards abstain when the moment's usefulness passed.
    result         the one moment that fetches the web (temporally filtered),
                   extracts STRUCTURED facts at temp 0, and pushes ONLY when the
                   facts TRANSITION the fixture forward (fact_gate). Not
                   determinable yet -> bounded re-arm, the only polling left.
    pulse          developments between fixtures, gated by a development-key
                   novelty check against the topic's recent history.

  One fetch + one compose serve every subscriber (cost tracks topics, not users);
  each subscriber's delivery claims a slot under the per-user-per-topic daily cap.
  Every fixture fire writes an AUDIT row (sent or abstained) under the fixture.

run_reconcile_tick()  (daily per topic): re-research with the stored fixtures
  INJECTED so the model echoes ids it recognizes; fixture_matcher updates fixtures
  in place (a reworded label can never fork a parallel series), and moments upsert
  onto deterministic ids.

Legacy poll-grid checkpoints (pre-redesign docs) are expired on sight, which is what
makes the cutover deploy safe on live prod state before any migration runs.

Both loops are fire-and-forget from the scheduler and isolate every per-item failure
so one bad topic can never stop the loop or fail the reminder tick.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..model_provider import ModelProvider, get_model_provider
from ..notifications import orchestrator
from ..notifications.proposal import (
    SOURCE_TRACKING,
    Disposition,
    NotificationProposal,
    ProposalKind,
)
from . import fields as f
from . import tracking_store as store
from .fact_gate import (
    FactState,
    coerce_fact_status,
    content_window_start,
    development_dedup_key,
    extract_transition,
    is_result_send_worthy,
    moment_dedup_key,
    result_dedup_key,
    slug_development_key,
)
from .fixture_matcher import reconcile_fixtures
from .models import Checkpoint, Fixture, TrackedTopic, Tracker
from .moments import (
    KICKOFF_USEFUL_FOR,
    MAX_RECENT_DEVELOPMENT_KEYS,
    MAX_RESULT_CHECKS,
    PRE_USEFUL_UNTIL_AFTER_START,
    PULSE_INTERVAL_INITIAL_S,
    RESULT_RECHECK_DELAY,
    build_fetch_query,
    build_moments,
    build_pulse_checkpoint,
    clean_topic_descriptor,
    is_legacy_poll_phase,
    moment_id,
    next_pulse_interval,
    next_window_open,
    within_notify_window,
)
from .topic_agent import _slugify, research_topic
from .topic_fetcher import fetch_topic

# Max checkpoints / topics processed simultaneously
_CONCURRENCY = 10

# Re-research cadence + how many straight failures retire a topic (stop burning calls).
_RECONCILE_INTERVAL = timedelta(hours=24)
_MAX_RECONCILE_FAILURES = 5
_LLM_CALL_TIMEOUT_S = 10.0

# Lifespan backstop when research couldn't determine an end (mirrors topic_agent).
_FALLBACK_LIFESPAN = timedelta(days=60)

# Per-user-per-topic daily push ceiling (founder decision 2026-07-10). A
# wake_override RESULT (a final's outcome) bypasses the cap but still counts.
TRACKER_DAILY_SEND_CAP = 8

# Freshness window for a tracker push (orchestrator hard gate). Generous: the fact
# gate already prevents stale re-sends; this only drops a fetch surfacing day-old
# material as a live result.
_TRACKER_FRESHNESS_MAX_AGE = timedelta(hours=24)

# A span fixture confirmed LIVE keeps re-checking without spending the bounded
# result-check budget (waiting on a running match is correct, not uncertainty) — but
# never past this hard deadline after kickoff, so a wrongly-live fixture can't poll
# forever.
_RESULT_HARD_DEADLINE_AFTER_START = timedelta(hours=6)

# Bound the in-chat research wait so a slow grounded call never hangs the chat turn.
_PROVISION_RESEARCH_TIMEOUT_S = 30.0


@dataclass
class CheckpointTickSummary:
    enabled: bool = True
    due: int = 0
    fired: int = 0
    sent: int = 0
    skipped_dedup: int = 0     # abstained: no fact transition / repeat development
    skipped_claimed: int = 0   # lost the atomic claim to a concurrent tick (benign)
    skipped_quiet: int = 0     # held: fire landed in the topic's notify quiet hours (benign)
    skipped_cap: int = 0       # a subscriber hit the per-topic daily ceiling
    rearmed_result: int = 0    # result not determinable yet; re-check scheduled (benign)
    expired_legacy: int = 0    # pre-redesign poll-grid docs drained harmlessly
    failed: int = 0
    expired: int = 0


@dataclass
class ReconcileTickSummary:
    enabled: bool = True
    topics: int = 0
    reconciled: int = 0
    completed: int = 0
    failed: int = 0
    stale: int = 0
    fixtures_updated: int = 0
    fixtures_created: int = 0
    fixtures_cancelled: int = 0
    checkpoints_upserted: int = 0


@dataclass
class _PushCopy:
    title: str
    body: str
    opening_chat_message: str
    summary: str = ""   # short factual line for the tracker's display cursor


@dataclass
class _ExtractedFacts:
    refers_to_this_fixture: bool
    facts: FactState


# ── LLM prompts ──────────────────────────────────────────────────────────────
_EXTRACT_FACTS_SYSTEM = """\
You extract STRUCTURED FACTS about one specific scheduled fixture from web search
context. You are a fact extractor, not a writer — precision over completeness, and
"I can't tell" is a valid answer. Return ONLY JSON:

{"refers_to_this_fixture": true/false — true ONLY if the context contains information
about THIS fixture (the named teams/parties at this date). Coverage of OTHER fixtures,
earlier rounds, previews of a different day, or general tournament news is false,
"status": "scheduled | live | finished | cancelled" — what the context shows for THIS
fixture. "cancelled" covers postponed/called off. When the context doesn't clearly
establish a status, use "scheduled",
"score": "the current/final score if stated, e.g. \\"1-0\\", else \\"\\"",
"winner": "the winner/advancing side ONLY if the fixture is finished and it is stated, else \\"\\"",
"note": "one short extra fact worth carrying (e.g. \\"Merino scored 88'\\", \\"postponed to Saturday\\"), else \\"\\""}

The current known state is given for reference — do NOT echo it back as if the web
context confirmed it; report only what the context itself shows."""

_COMPOSE_RESULT_SYSTEM = """\
You are Buddy, a warm AI companion, writing ONE push notification about a fixture the
user asked you to track. You are given verified facts — use ONLY them, never invent
details. Return ONLY JSON:

{"title":"<=45 char push title",
"body":"1-2 warm sentences with the concrete outcome",
"opening_chat_message":"a friendly chat opener continuing this update if the user taps",
"summary":"<=100 char plain factual restatement, e.g. 'Spain beat Belgium 1-0, face France in semis'"}"""

_COMPOSE_PULSE_SYSTEM = """\
You are Buddy, a warm AI companion, checking whether there is ONE genuinely new
development on a topic the user asked you to track. Return ONLY JSON:

{"development_key":"a 3-8 word slug NAMING the concrete new fact (e.g.
'semifinal draw spain france'), or \\"\\" if the web context contains nothing genuinely
new versus the recent developments listed below",
"title":"<=45 char push title",
"body":"1-2 warm sentences with the concrete development",
"opening_chat_message":"a friendly chat opener if the user taps",
"summary":"<=100 char plain factual restatement"}

A reworded version of a recent development is NOT new — return an empty
development_key for it. Err toward empty whenever the context doesn't clearly move
the story forward."""


def _parse_json_object(raw: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_push_copy(raw: str) -> _PushCopy | None:
    data = _parse_json_object(raw)
    if data is None:
        return None
    title = str(data.get("title", "")).strip()[:60]
    body = str(data.get("body", "")).strip()
    if not title or not body:
        return None
    return _PushCopy(
        title=title,
        body=body,
        opening_chat_message=str(data.get("opening_chat_message", "")).strip() or body,
        summary=str(data.get("summary", "")).strip() or title,
    )


async def _extract_facts(
    models: ModelProvider, fixture: Fixture, context: str, now: datetime,
) -> _ExtractedFacts | None:
    """Temp-0 structured extraction — the trust boundary between the open web and the
    fixture's fact state. Returns None when the model failed/unparseable (the caller
    re-arms; never guesses)."""
    prompt = (
        f"Fixture: {fixture.label}\n"
        f"Scheduled start (UTC): {fixture.start_at.isoformat()}\n"
        f"Now (UTC): {now.isoformat()}\n"
        f"Current known state: status={fixture.status}"
        f"{f', score={fixture.fact_score}' if fixture.fact_score else ''}"
        f"{f', winner={fixture.fact_winner}' if fixture.fact_winner else ''}\n\n"
        f"Web context:\n{context}"
    )
    try:
        raw = await asyncio.wait_for(
            models.cheap(prompt, system=_EXTRACT_FACTS_SYSTEM, temperature=0.0),
            timeout=_LLM_CALL_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("tracking_engine: fact extraction failed", {
            "fixture_id": fixture.id, "topic_key": fixture.topic_key, "error": str(exc),
        })
        return None
    data = _parse_json_object(str(raw))
    if data is None:
        return None
    return _ExtractedFacts(
        refers_to_this_fixture=bool(data.get("refers_to_this_fixture", False)),
        facts=FactState(
            status=coerce_fact_status(str(data.get("status", ""))),
            score=str(data.get("score", "")).strip()[:20],
            winner=str(data.get("winner", "")).strip()[:80],
            note=str(data.get("note", "")).strip()[:160],
        ),
    )


async def _compose_result_push(
    models: ModelProvider, topic: TrackedTopic, fixture: Fixture,
    seen: FactState, transition: str,
) -> _PushCopy:
    """Wording is free to vary — it is DOWNSTREAM of the fact gate now. A compose
    failure falls back to plain deterministic copy: a committed result must never be
    lost to an LLM hiccup."""
    outcome_bits = [bit for bit in (
        f"winner: {seen.winner}" if seen.winner else "",
        f"score: {seen.score}" if seen.score else "",
        seen.note,
    ) if bit]
    prompt = (
        f"Topic: {topic.title}\n"
        f"Fixture: {fixture.label}\n"
        f"What just happened: {transition.replace('->', ' -> ')}\n"
        f"Verified facts: {'; '.join(outcome_bits) or 'no further detail available'}"
    )
    try:
        raw = await asyncio.wait_for(
            models.cheap(prompt, system=_COMPOSE_RESULT_SYSTEM, temperature=0.4),
            timeout=_LLM_CALL_TIMEOUT_S,
        )
        composed = _parse_push_copy(str(raw))
        if composed is not None:
            return composed
    except Exception as exc:
        logger.warn("tracking_engine: result compose failed, using fallback copy", {
            "fixture_id": fixture.id, "error": str(exc),
        })
    if seen.status == f.FIXTURE_STATUS_CANCELLED:
        fallback_body = f"{fixture.label} is off — {seen.note or 'it was cancelled or postponed'}."
    elif seen.winner:
        fallback_body = f"{seen.winner} won{f' {seen.score}' if seen.score else ''} — {fixture.label}."
    else:
        fallback_body = f"{fixture.label} has finished{f' ({seen.score})' if seen.score else ''}."
    return _PushCopy(
        title=fixture.label[:60] or "Tracker update",
        body=fallback_body,
        opening_chat_message=fallback_body,
        summary=fallback_body[:100],
    )


async def _compose_fixture_moment_push(
    models: ModelProvider, topic: TrackedTopic, fixture: Fixture,
    *, moment: str, now: datetime,
) -> _PushCopy:
    """Pre/kickoff copy from the fixture doc alone — no fetch, the schedule IS the
    fact. Deterministic fallback so the heads-up never dies to an LLM hiccup."""
    minutes_to_start = max(0, int((fixture.start_at - now).total_seconds() // 60))
    if moment == f.CHECKPOINT_PHASE_PRE:
        situation = f"starts in about {minutes_to_start} minutes"
        fallback_body = f"{fixture.label} kicks off in about {minutes_to_start} minutes!"
    else:
        situation = "is starting right now"
        fallback_body = f"{fixture.label} is underway!"
    prompt = (
        f"Topic: {topic.title}\n"
        f"Fixture: {fixture.label}\n"
        f"Moment: it {situation}."
    )
    try:
        raw = await asyncio.wait_for(
            models.cheap(prompt, system=_COMPOSE_RESULT_SYSTEM, temperature=0.4),
            timeout=_LLM_CALL_TIMEOUT_S,
        )
        composed = _parse_push_copy(str(raw))
        if composed is not None:
            return composed
    except Exception as exc:
        logger.warn("tracking_engine: moment compose failed, using fallback copy", {
            "fixture_id": fixture.id, "moment": moment, "error": str(exc),
        })
    return _PushCopy(
        title=fixture.label[:60] or "Heads up",
        body=fallback_body,
        opening_chat_message=fallback_body,
        summary=fallback_body[:100],
    )


# ── checkpoint tick ──────────────────────────────────────────────────────────
async def run_checkpoint_tick() -> CheckpointTickSummary:
    """Public entrypoint, called from the scheduler tick."""
    summary = CheckpointTickSummary()
    now = datetime.now(UTC)
    models = get_model_provider()
    sem = asyncio.Semaphore(_CONCURRENCY)

    due = await store.fetch_due_checkpoints(now)
    summary.due = len(due)
    if due:
        async def _process(cp: Checkpoint) -> None:
            async with sem:
                try:
                    await _fire_checkpoint(cp, models, now, summary)
                except Exception as exc:
                    logger.exception("tracking_engine: checkpoint failure", {
                        "checkpoint_id": cp.id, "error": str(exc),
                    })

        await asyncio.gather(*[_process(cp) for cp in due])

    stats = {
        "due": summary.due, "fired": summary.fired, "sent": summary.sent,
        "skipped_dedup": summary.skipped_dedup, "skipped_claimed": summary.skipped_claimed,
        "skipped_quiet": summary.skipped_quiet, "skipped_cap": summary.skipped_cap,
        "rearmed_result": summary.rearmed_result, "expired_legacy": summary.expired_legacy,
        "failed": summary.failed, "expired": summary.expired,
    }
    # Fail-loud (CLAUDE.md): "healthy, nothing new" and "silently broken" must not look
    # identical. Only a tick with real failures/expirations and NOTHING sent is suspect.
    had_real_failure = summary.failed > 0 or summary.expired > 0
    if summary.due > 0 and summary.sent == 0 and had_real_failure:
        logger.warn("tracking_engine: checkpoints due but nothing sent", stats)
    else:
        logger.info("tracking_engine: checkpoint tick complete", stats)
    return summary


async def _fire_checkpoint(
    cp: Checkpoint, models: ModelProvider, now: datetime, summary: CheckpointTickSummary,
) -> None:
    # Atomic claim — only one tick fires a given checkpoint.
    if not await store.claim_checkpoint(cp.id):
        summary.skipped_claimed += 1
        return

    # Pre-redesign poll-grid docs drain harmlessly here (the safe-cutover guarantee):
    # the migration script sweeps the bulk, this catches every straggler.
    if is_legacy_poll_phase(cp.phase, cp.fixture_id):
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_EXPIRED)
        summary.expired_legacy += 1
        return

    topic = await store.get_tracked_topic(cp.topic_key)
    if topic is None or topic.status != f.TOPIC_STATUS_ACTIVE:
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_EXPIRED)
        summary.expired += 1
        return

    # Lifespan backstop: a topic past its expires_at is auto-completed here too.
    if topic.expires_at is not None and now > topic.expires_at:
        await store.set_topic_status(cp.topic_key, f.TOPIC_STATUS_COMPLETED)
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_EXPIRED)
        summary.expired += 1
        return

    subscribers = await store.list_active_subscribers(cp.topic_key)
    if not subscribers:
        # No one to notify. A pulse re-arms (looser) WITHOUT a fetch; reconcile
        # retires the topic (stale) and the status guard above then expires it.
        if cp.phase == f.CHECKPOINT_PHASE_PULSE:
            await store.rearm_pulse(
                cp.id,
                fire_at=now + timedelta(seconds=next_pulse_interval(
                    topic.pulse_interval_seconds, found_new=False,
                )),
                tier=cp.last_fetch_tier or f.TIER_NONE, at=now,
            )
        else:
            await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        return

    if cp.phase == f.CHECKPOINT_PHASE_PULSE:
        await _fire_pulse(cp, topic, subscribers, models, now, summary)
        return

    fixture = await store.get_fixture(cp.topic_key, cp.fixture_id)
    if fixture is None:
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_EXPIRED)
        summary.expired += 1
        return

    # Notify-window guard, BEFORE any fetch/LLM so quiet hours cost nothing. A
    # pre/kickoff outside the window is skipped (its usefulness won't survive the
    # deferral anyway); the RESULT — the valuable moment — is deferred to the window
    # open, never dropped. wake_override bypasses (a can't-miss final).
    if not cp.wake_override and not within_notify_window(
        now, tz_name=topic.timezone,
        start_hour=topic.notify_start_hour, end_hour=topic.notify_end_hour,
    ):
        if cp.phase == f.CHECKPOINT_PHASE_RESULT:
            await store.mark_checkpoint(
                cp.id, f.CHECKPOINT_STATUS_PENDING,
                **{
                    f.CHECKPOINT_FIRE_AT: next_window_open(
                        now, tz_name=topic.timezone,
                        start_hour=topic.notify_start_hour, end_hour=topic.notify_end_hour,
                    ),
                    f.CHECKPOINT_CLAIMED_AT: None,
                },
            )
        else:
            await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        summary.skipped_quiet += 1
        return

    if cp.phase == f.CHECKPOINT_PHASE_PRE:
        await _fire_pre(cp, topic, fixture, subscribers, models, now, summary)
    elif cp.phase == f.CHECKPOINT_PHASE_KICKOFF:
        await _fire_kickoff(cp, topic, fixture, subscribers, models, now, summary)
    elif cp.phase == f.CHECKPOINT_PHASE_RESULT:
        await _fire_result(cp, topic, fixture, subscribers, models, now, summary)
    else:
        # Unknown phase (a future writer bug) — never silently loop on it.
        logger.warn("tracking_engine: unknown checkpoint phase, expiring", {
            "checkpoint_id": cp.id, "phase": cp.phase,
        })
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_EXPIRED)
        summary.expired += 1


async def _fire_pre(
    cp: Checkpoint, topic: TrackedTopic, fixture: Fixture, subscribers: list[Tracker],
    models: ModelProvider, now: datetime, summary: CheckpointTickSummary,
) -> None:
    """The fetchless heads-up. Hard temporal guard: a pre delivered after kickoff is
    worse than none (the exact "kicks off soon, 40 minutes late" incident push)."""
    too_late = now > fixture.start_at + PRE_USEFUL_UNTIL_AFTER_START
    if fixture.status != f.FIXTURE_STATUS_SCHEDULED or too_late:
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        await store.record_fire_audit(
            cp.topic_key, fixture.id,
            moment=cp.phase, fired_at=now,
            prior_facts={"status": fixture.status},
            decision=(
                f.AUDIT_DECISION_ABSTAIN_TOO_LATE if too_late
                else f.AUDIT_DECISION_ABSTAIN_NO_TRANSITION
            ),
        )
        summary.skipped_dedup += 1
        return

    copy = await _compose_fixture_moment_push(models, topic, fixture, moment=cp.phase, now=now)
    delivered = await _fan_out(
        subscribers, topic, copy,
        dedup_key=moment_dedup_key(cp.topic_key, fixture.id, cp.phase),
        now=now, summary=summary,
    )
    await store.mark_checkpoint(
        cp.id, f.CHECKPOINT_STATUS_FIRED, **{f.CHECKPOINT_FIRED_AT: now},
    )
    await store.record_fire_audit(
        cp.topic_key, fixture.id,
        moment=cp.phase, fired_at=now,
        prior_facts={"status": fixture.status},
        decision=f.AUDIT_DECISION_SENT, sent_count=delivered, title=copy.title,
    )
    summary.fired += 1


async def _fire_kickoff(
    cp: Checkpoint, topic: TrackedTopic, fixture: Fixture, subscribers: list[Tracker],
    models: ModelProvider, now: datetime, summary: CheckpointTickSummary,
) -> None:
    """Fetchless "it started". Sends ONLY if it wins the scheduled->live transition on
    the fixture — so a fixture already known live/finished (a raced result check, a
    backlogged queue) can never get a bogus kickoff push."""
    if now > fixture.start_at + KICKOFF_USEFUL_FOR:
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            prior_facts={"status": fixture.status},
            decision=f.AUDIT_DECISION_ABSTAIN_TOO_LATE,
        )
        summary.skipped_dedup += 1
        return

    transition = await store.commit_fact_transition(
        cp.topic_key, fixture.id, FactState(status=f.FIXTURE_STATUS_LIVE), now=now,
    )
    if transition is None:
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            prior_facts={"status": fixture.status},
            decision=f.AUDIT_DECISION_ABSTAIN_RACE_LOST,
        )
        summary.skipped_dedup += 1
        return

    copy = await _compose_fixture_moment_push(models, topic, fixture, moment=cp.phase, now=now)
    delivered = await _fan_out(
        subscribers, topic, copy,
        dedup_key=moment_dedup_key(cp.topic_key, fixture.id, cp.phase),
        now=now, summary=summary,
    )
    await store.mark_checkpoint(
        cp.id, f.CHECKPOINT_STATUS_FIRED, **{f.CHECKPOINT_FIRED_AT: now},
    )
    await store.record_fire_audit(
        cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
        prior_facts={"status": f.FIXTURE_STATUS_SCHEDULED}, transition=transition,
        decision=f.AUDIT_DECISION_SENT, sent_count=delivered, title=copy.title,
    )
    summary.fired += 1


async def _rearm_result(
    cp: Checkpoint, now: datetime, summary: CheckpointTickSummary,
    *, spend_check: bool,
) -> None:
    """Schedule the result's next look. ``spend_check`` False means the wait is
    CONFIRMED correct (fixture verified live / hasn't started) and doesn't consume
    the bounded uncertainty budget."""
    updates: dict = {
        f.CHECKPOINT_FIRE_AT: now + RESULT_RECHECK_DELAY,
        f.CHECKPOINT_CLAIMED_AT: None,
    }
    if spend_check:
        updates[f.CHECKPOINT_RESULT_CHECKS] = fs.Increment(1)
    await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_PENDING, **updates)
    summary.rearmed_result += 1


async def _fire_result(
    cp: Checkpoint, topic: TrackedTopic, fixture: Fixture, subscribers: list[Tracker],
    models: ModelProvider, now: datetime, summary: CheckpointTickSummary,
) -> None:
    """The one fetching moment: temporally-filtered fetch -> temp-0 fact extraction
    -> fact-transition gate -> compose -> CAS commit -> fan out. Every branch writes
    an audit row. Not determinable yet -> bounded re-arm (the only polling left)."""
    if fixture.status in (f.FIXTURE_STATUS_FINISHED, f.FIXTURE_STATUS_CANCELLED):
        # Already settled (a raced sibling or the migration) — nothing to do.
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            prior_facts={"status": fixture.status},
            decision=f.AUDIT_DECISION_ABSTAIN_NO_TRANSITION,
        )
        summary.skipped_dedup += 1
        return

    if now < fixture.start_at:
        # Rescheduled fixture whose result doc kept an old fire time — wait, free.
        await store.mark_checkpoint(
            cp.id, f.CHECKPOINT_STATUS_PENDING,
            **{f.CHECKPOINT_FIRE_AT: fixture.expected_end_at or fixture.start_at,
               f.CHECKPOINT_CLAIMED_AT: None},
        )
        summary.rearmed_result += 1
        return

    checks_spent = cp.result_checks >= MAX_RESULT_CHECKS
    prior = FactState(
        status=fixture.status, score=fixture.fact_score,
        winner=fixture.fact_winner, note=fixture.fact_note,
    )

    fetched = await fetch_topic(
        build_fetch_query(
            event_label=fixture.label,
            research_query=topic.research_query,
            title=topic.title,
        ),
        country=topic.country,
        language=topic.language,
        not_before=content_window_start(fixture.start_at),
    )
    if not fetched.ok:
        if checks_spent:
            await store.mark_checkpoint(
                cp.id, f.CHECKPOINT_STATUS_FAILED,
                **{f.CHECKPOINT_LAST_ERROR: "no in-window content from any fetch tier"},
            )
            summary.failed += 1
        else:
            await _rearm_result(cp, now, summary, spend_check=True)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            query=fixture.label, fetch_tier=fetched.tier,
            prior_facts=prior.as_map(),
            decision=f.AUDIT_DECISION_FAILED_FETCH if checks_spent else f.AUDIT_DECISION_REARMED,
        )
        return

    extracted = await _extract_facts(models, fixture, fetched.text, now)
    if extracted is None or not extracted.refers_to_this_fixture:
        wrong_fixture = extracted is not None
        if checks_spent:
            await store.mark_checkpoint(
                cp.id, f.CHECKPOINT_STATUS_FAILED,
                **{f.CHECKPOINT_LAST_ERROR: "result not determinable within the recheck budget"},
            )
            summary.failed += 1
        else:
            await _rearm_result(cp, now, summary, spend_check=True)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            query=fixture.label, fetch_tier=fetched.tier,
            prior_facts=prior.as_map(),
            seen_facts=extracted.facts.as_map() if extracted else {},
            decision=(
                f.AUDIT_DECISION_ABSTAIN_WRONG_FIXTURE if wrong_fixture
                else f.AUDIT_DECISION_ABSTAIN_STALE_CONTENT
            ),
        )
        return

    transition_preview = extract_transition(prior, extracted.facts)
    if transition_preview is None or not is_result_send_worthy(transition_preview):
        # The fixture is confirmed live (or nothing moved): waiting is CORRECT while
        # the span can still plausibly be running; commit the live status so pre/
        # kickoff guards see the truth, then re-check without spending the budget.
        confirmed_live = (
            extracted.facts.status == f.FIXTURE_STATUS_LIVE
            and now < fixture.start_at + _RESULT_HARD_DEADLINE_AFTER_START
        )
        if confirmed_live:
            await store.commit_fact_transition(cp.topic_key, fixture.id, extracted.facts, now=now)
            await _rearm_result(cp, now, summary, spend_check=False)
        elif checks_spent:
            await store.mark_checkpoint(
                cp.id, f.CHECKPOINT_STATUS_FAILED,
                **{f.CHECKPOINT_LAST_ERROR: "result not determinable within the recheck budget"},
            )
            summary.failed += 1
        else:
            await _rearm_result(cp, now, summary, spend_check=True)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            query=fixture.label, fetch_tier=fetched.tier,
            prior_facts=prior.as_map(), seen_facts=extracted.facts.as_map(),
            decision=f.AUDIT_DECISION_REARMED if (confirmed_live or not checks_spent)
            else f.AUDIT_DECISION_ABSTAIN_NO_TRANSITION,
        )
        return

    # Send-worthy: compose FIRST (a compose failure must not consume the transition),
    # then CAS-commit — the race loser abstains — then fan out.
    copy = await _compose_result_push(models, topic, fixture, extracted.facts, transition_preview)
    transition = await store.commit_fact_transition(cp.topic_key, fixture.id, extracted.facts, now=now)
    if transition is None:
        await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        await store.record_fire_audit(
            cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
            query=fixture.label, fetch_tier=fetched.tier,
            prior_facts=prior.as_map(), seen_facts=extracted.facts.as_map(),
            decision=f.AUDIT_DECISION_ABSTAIN_RACE_LOST,
        )
        summary.skipped_dedup += 1
        return

    delivered = await _fan_out(
        subscribers, topic, copy,
        dedup_key=result_dedup_key(cp.topic_key, fixture.id, transition),
        now=now, summary=summary,
        content_timestamp=fetched.latest_published,
        cap_bypass=fixture.wake_override,
    )
    await store.update_topic_live_cache(
        cp.topic_key, summary=copy.summary, fetched_at=now, tier=fetched.tier,
    )
    await store.mark_checkpoint(
        cp.id, f.CHECKPOINT_STATUS_FIRED,
        **{
            f.CHECKPOINT_FIRED_AT: now,
            f.CHECKPOINT_LAST_FETCH_TIER: fetched.tier,
            f.CHECKPOINT_LAST_FETCH_AT: now,
        },
    )
    await store.record_fire_audit(
        cp.topic_key, fixture.id, moment=cp.phase, fired_at=now,
        query=fixture.label, fetch_tier=fetched.tier,
        prior_facts=prior.as_map(), seen_facts=extracted.facts.as_map(),
        transition=transition,
        decision=f.AUDIT_DECISION_SENT, sent_count=delivered, title=copy.title,
    )
    summary.fired += 1


async def _fire_pulse(
    cp: Checkpoint, topic: TrackedTopic, subscribers: list[Tracker],
    models: ModelProvider, now: datetime, summary: CheckpointTickSummary,
) -> None:
    """The between-fixtures heartbeat, novelty-gated by development keys: the compose
    must NAME the concrete new fact as a slug, and one already in the topic's recent
    list is a reworded repeat that abstains — no more string-equality on prose."""
    fetched = await fetch_topic(
        build_fetch_query(event_label="", research_query=topic.research_query, title=topic.title),
        country=topic.country,
        language=topic.language,
    )
    if not fetched.ok:
        await store.rearm_pulse(
            cp.id,
            fire_at=now + timedelta(seconds=next_pulse_interval(
                topic.pulse_interval_seconds, found_new=False,
            )),
            tier=fetched.tier, at=now,
        )
        await store.update_tracked_topic(cp.topic_key, {
            f.TOPIC_PULSE_INTERVAL_SECONDS: next_pulse_interval(
                topic.pulse_interval_seconds, found_new=False,
            ),
        })
        summary.failed += 1
        return

    recent_lines = "\n".join(f"  - {key}" for key in topic.recent_development_keys) or "  (none yet)"
    prompt = (
        f"Topic: {topic.title}\n"
        f"Recent developments already sent (do NOT repeat these):\n{recent_lines}\n\n"
        f"Web context:\n{fetched.text}"
    )
    development_key = ""
    copy: _PushCopy | None = None
    try:
        raw = await asyncio.wait_for(
            models.cheap(prompt, system=_COMPOSE_PULSE_SYSTEM, temperature=0.4),
            timeout=_LLM_CALL_TIMEOUT_S,
        )
        data = _parse_json_object(str(raw))
        if data is not None:
            development_key = slug_development_key(str(data.get("development_key", "")))
            copy = _parse_push_copy(str(raw))
    except Exception as exc:
        logger.warn("tracking_engine: pulse compose failed", {
            "topic_key": cp.topic_key, "error": str(exc),
        })

    is_new = bool(development_key) and development_key not in topic.recent_development_keys
    if not is_new or copy is None:
        next_seconds = next_pulse_interval(topic.pulse_interval_seconds, found_new=False)
        await store.rearm_pulse(cp.id, fire_at=now + timedelta(seconds=next_seconds), tier=fetched.tier, at=now)
        await store.update_tracked_topic(cp.topic_key, {f.TOPIC_PULSE_INTERVAL_SECONDS: next_seconds})
        summary.skipped_dedup += 1
        return

    delivered = await _fan_out(
        subscribers, topic, copy,
        dedup_key=development_dedup_key(cp.topic_key, development_key),
        now=now, summary=summary,
        content_timestamp=fetched.latest_published,
    )
    next_seconds = next_pulse_interval(topic.pulse_interval_seconds, found_new=True)
    await store.rearm_pulse(
        cp.id, fire_at=now + timedelta(seconds=next_seconds),
        tier=fetched.tier, at=now, summary=copy.summary,
    )
    await store.update_tracked_topic(cp.topic_key, {
        f.TOPIC_PULSE_INTERVAL_SECONDS: next_seconds,
        f.TOPIC_LIVE_SUMMARY: copy.summary,
        f.TOPIC_LIVE_FETCHED_AT: now,
        f.TOPIC_LIVE_SOURCE_TIER: fetched.tier,
        f.TOPIC_RECENT_DEVELOPMENT_KEYS: (
            topic.recent_development_keys + [development_key]
        )[-MAX_RECENT_DEVELOPMENT_KEYS:],
    })
    logger.info("tracking_engine: pulse development sent", {
        "topic_key": cp.topic_key, "development_key": development_key, "delivered": delivered,
    })
    summary.fired += 1


# ── delivery ─────────────────────────────────────────────────────────────────
async def _send_tracker_push(
    *, user_id: str, topic_key: str, tracker_id: str,
    title: str, body: str, opening_chat_message: str, dedup_key: str,
    content_timestamp: datetime | None = None,
):
    """Hand one tracker update to the orchestrator's committed lane (the user asked
    to be kept posted, so it sends inline; freshness + dedup still apply). The
    orchestrator is the only thing that touches FCM."""
    proposal = NotificationProposal(
        user_id=user_id,
        source=SOURCE_TRACKING,
        kind=ProposalKind.COMMITTED,
        dedup_key=dedup_key,
        title=title,
        body=body,
        data={
            "notification_type": f.NOTIFICATION_TYPE_TRACKER_UPDATE,
            "notification_origin": f.DECISION_ORIGIN_TRACKER,
            "topic_key": topic_key,
            "tracker_id": tracker_id,
            "opening_chat_message": opening_chat_message,
        },
        notification_type=f.NOTIFICATION_TYPE_TRACKER_UPDATE,
        collapse_key=f"tracker_{tracker_id}",
        content_timestamp=content_timestamp,
        freshness_max_age=_TRACKER_FRESHNESS_MAX_AGE,
    )
    return await orchestrator.submit(proposal)


async def _fan_out(
    subscribers: list[Tracker], topic: TrackedTopic, copy: _PushCopy,
    *, dedup_key: str, now: datetime, summary: CheckpointTickSummary,
    content_timestamp: datetime | None = None, cap_bypass: bool = False,
) -> int:
    """Deliver one composed update to every active subscriber. Each delivery claims a
    slot under the per-user-per-topic daily ceiling (TRACKER_DAILY_SEND_CAP);
    ``cap_bypass`` (a wake_override result — a final's outcome must land) skips the
    ceiling but still counts toward it. Returns how many were actually delivered."""
    today = now.strftime("%Y-%m-%d")
    delivered_count = 0
    for sub in subscribers:
        try:
            claimed = await store.try_claim_tracker_daily_slot(
                sub.id, today=today, cap=TRACKER_DAILY_SEND_CAP, force=cap_bypass,
            )
            if not claimed:
                logger.info("tracking_engine: daily cap reached, holding tracker push", {
                    "tracker_id": sub.id, "topic_key": topic.topic_key, "cap": TRACKER_DAILY_SEND_CAP,
                })
                summary.skipped_cap += 1
                continue
            decision = await _send_tracker_push(
                user_id=sub.user_id, topic_key=topic.topic_key, tracker_id=sub.id,
                title=copy.title, body=copy.body,
                opening_chat_message=copy.opening_chat_message,
                dedup_key=dedup_key,
                content_timestamp=content_timestamp,
            )
            if decision.disposition == Disposition.SEND and decision.delivered:
                await store.record_tracker_outcome(sub.id, summary=copy.summary, at=now)
                delivered_count += 1
                summary.sent += 1
        except Exception as exc:
            logger.warn("tracking_engine: subscriber delivery failed", {
                "tracker_id": sub.id, "topic_key": topic.topic_key, "error": str(exc),
            })
    return delivered_count


# ── reconcile tick ───────────────────────────────────────────────────────────
async def run_reconcile_tick() -> ReconcileTickSummary:
    """Re-research each active topic and self-heal its fixtures + moments."""
    summary = ReconcileTickSummary()
    now = datetime.now(UTC)
    topics = await store.list_topics_due_for_reconcile(now)
    summary.topics = len(topics)
    if not topics:
        return summary

    models = get_model_provider()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _process(topic: TrackedTopic) -> None:
        async with sem:
            try:
                await _reconcile_topic(topic, models, now, summary)
            except Exception as exc:
                logger.exception("tracking_engine: reconcile failure", {
                    "topic_key": topic.topic_key, "error": str(exc),
                })

    await asyncio.gather(*[_process(t) for t in topics])

    logger.info("tracking_engine: reconcile tick complete", {
        "topics": summary.topics, "reconciled": summary.reconciled,
        "completed": summary.completed, "failed": summary.failed,
        "stale": summary.stale,
        "fixtures_updated": summary.fixtures_updated,
        "fixtures_created": summary.fixtures_created,
        "fixtures_cancelled": summary.fixtures_cancelled,
        "checkpoints_upserted": summary.checkpoints_upserted,
    })
    return summary


async def _reconcile_topic(
    topic: TrackedTopic, models: ModelProvider, now: datetime, summary: ReconcileTickSummary,
) -> None:
    # Lifespan backstop first — never spend a research call on a finished topic.
    if topic.expires_at is not None and now > topic.expires_at:
        await store.set_topic_status(topic.topic_key, f.TOPIC_STATUS_COMPLETED)
        summary.completed += 1
        return

    # No active subscribers left (all cancelled) — retire the topic before spending a
    # grounded research call on it.
    if not await store.list_active_subscribers(topic.topic_key):
        await store.set_topic_status(topic.topic_key, f.TOPIC_STATUS_STALE)
        summary.stale += 1
        return

    existing_fixtures = await store.list_fixtures(topic.topic_key)
    research = await research_topic(
        topic.research_query or topic.title, models=models, now=now,
        existing_fixtures=existing_fixtures,
    )
    if research is None:
        failures = topic.consecutive_reconcile_failures + 1
        updates = {
            f.TOPIC_LAST_RECONCILE_STATUS: "failed",
            f.TOPIC_LAST_RECONCILE_ERROR: "research returned no parseable result",
            f.TOPIC_CONSECUTIVE_RECONCILE_FAILURES: failures,
            f.TOPIC_NEXT_RECONCILE_AT: now + _RECONCILE_INTERVAL,
            f.TOPIC_HEALTH: f.TOPIC_HEALTH_STALLED,
        }
        if failures >= _MAX_RECONCILE_FAILURES:
            updates[f.TOPIC_STATUS] = f.TOPIC_STATUS_FAILED
        await store.update_tracked_topic(topic.topic_key, updates)
        summary.failed += 1
        return

    plan = reconcile_fixtures(existing_fixtures, research.fixtures, topic_key=topic.topic_key, now=now)
    await store.upsert_fixtures(topic.topic_key, plan.updates + plan.creates)
    moments = [m for fx in plan.updates + plan.creates for m in build_moments(fx, now=now)]
    await store.upsert_moment_schedule(moments)
    for fixture_id in plan.cancel_ids:
        await store.cancel_fixture(topic.topic_key, fixture_id)
        for phase in (f.CHECKPOINT_PHASE_PRE, f.CHECKPOINT_PHASE_KICKOFF, f.CHECKPOINT_PHASE_RESULT):
            await store.expire_checkpoint_if_pending(moment_id(topic.topic_key, fixture_id, phase))
    summary.fixtures_updated += len(plan.updates)
    summary.fixtures_created += len(plan.creates)
    summary.fixtures_cancelled += len(plan.cancel_ids)
    summary.checkpoints_upserted += len(moments)

    # Refresh lifespan from the fresh research; complete if it has now passed.
    new_expires = research.expires_at or topic.expires_at
    if new_expires is not None and now > new_expires:
        await store.update_tracked_topic(topic.topic_key, {
            f.TOPIC_STATUS: f.TOPIC_STATUS_COMPLETED,
            f.TOPIC_ENDS_AT: research.ends_at,
            f.TOPIC_EXPIRES_AT: new_expires,
        })
        summary.completed += 1
        return

    health = f.TOPIC_HEALTH_HEALTHY if research.confidence >= 0.4 else f.TOPIC_HEALTH_DEGRADED
    reconcile_updates = {
        f.TOPIC_LAST_RECONCILED_AT: now,
        f.TOPIC_RECONCILE_COUNT: topic.reconcile_count + 1,
        f.TOPIC_NEXT_RECONCILE_AT: now + _RECONCILE_INTERVAL,
        f.TOPIC_LAST_RECONCILE_STATUS: "ok",
        f.TOPIC_LAST_RECONCILE_ERROR: None,
        f.TOPIC_CONSECUTIVE_RECONCILE_FAILURES: 0,
        f.TOPIC_RESEARCH_CONFIDENCE: research.confidence,
        f.TOPIC_ENDS_AT: research.ends_at,
        f.TOPIC_EXPIRES_AT: new_expires,
        f.TOPIC_HEALTH: health,
        f.TOPIC_CHECKPOINTS_TOTAL: len(moments),
        # Refresh the search query + human title from the fresh pass (a topic born
        # from a provision-research timeout keeps the raw sentence otherwise).
        f.TOPIC_RESEARCH_QUERY: research.research_query,
        f.TOPIC_TITLE: research.title,
        # Once a fresh pass finds the date of a previously-undated event, this flips
        # false and the fixtures above lay the real schedule — the two-phase handoff.
        f.TOPIC_AWAITING_DATE: research.awaiting_date,
    }
    # Refine locale/notify-window only when the fresh pass returned usable values —
    # never clobber good stored values with empties.
    if research.country:
        reconcile_updates[f.TOPIC_COUNTRY] = research.country
    if research.language:
        reconcile_updates[f.TOPIC_LANGUAGE] = research.language
    if research.notify_start_hour >= 0:
        reconcile_updates[f.TOPIC_NOTIFY_START_HOUR] = research.notify_start_hour
    if research.notify_end_hour >= 0:
        reconcile_updates[f.TOPIC_NOTIFY_END_HOUR] = research.notify_end_hour
    await store.update_tracked_topic(topic.topic_key, reconcile_updates)
    # Self-heal: a topic whose pulse was never seeded picks one up here (idempotent).
    await _ensure_pulse(topic.topic_key, interval_seconds=topic.pulse_interval_seconds, now=now)
    summary.reconciled += 1


# ── provisioning (called from the track_topic chat tool) ─────────────────────
def _iso(value: datetime | None) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


async def _ensure_pulse(topic_key: str, *, interval_seconds: int, now: datetime) -> None:
    """Seed the recurring heartbeat for a topic if it has none yet (idempotent — a live
    pulse is never reset). Called from both provisioning and the reconcile self-heal."""
    interval = interval_seconds if interval_seconds > 0 else PULSE_INTERVAL_INITIAL_S
    cp = build_pulse_checkpoint(topic_key, fire_at=now + timedelta(seconds=interval), now=now)
    await store.create_checkpoint_if_absent(cp)


async def provision_tracker(user_id: str, request: str, *, created_via: str = "text") -> dict:
    """Research a topic and subscribe the user. Reuses an existing shared
    ``tracked_topics`` doc when one already covers the same public event (so two
    users on one event share research). Research runs under a bounded wait so the
    chat turn never hangs; if it times out or fails, a minimal topic is created with
    ``next_reconcile_at = now`` so the reconcile loop fills the schedule shortly.
    Returns a plain dict the chat tool formats into a confirmation. Never raises."""
    request = (request or "").strip()
    if not request:
        return {"ok": False, "message": "Tell me what you want me to keep you posted on."}

    now = datetime.now(UTC)
    models = get_model_provider()

    # Voice provisions on a strict latency budget: the /mcp voice-tool cap is ~8s and a
    # multi-second research pause is dead air on a live call. So voice skips the inline
    # research pass entirely and takes the same minimal-topic path as a research timeout
    # (next_reconcile_at = now); the every-15-min reconcile re-researches and lays the
    # fixtures, and the pulse heartbeat drives updates in the meantime. Text chat keeps
    # the full inline research. Both share the identical minimal-topic construction.
    research = None
    if created_via != "voice":
        try:
            research = await asyncio.wait_for(
                research_topic(request, models=models, now=now),
                timeout=_PROVISION_RESEARCH_TIMEOUT_S,
            )
        except Exception as exc:
            logger.warn("tracking_engine: provision research failed/timed out, minimal setup", {
                "user_id": user_id, "request": request[:120], "error": str(exc),
            })

    topic_key = research.topic_key if research else _slugify(request, fallback="topic")

    topic = await store.get_tracked_topic(topic_key)
    if topic is None:
        topic = TrackedTopic(
            topic_key=topic_key,
            title=(research.title if research else request[:120]),
            kind=(research.kind if research else f.TOPIC_KIND_OPEN_INTEREST),
            # Research succeeded -> its clean short query. Research failed (timed out) ->
            # sanitize the raw request so the fetch searches the subject, not the whole
            # "keep me posted on …" sentence; reconcile later replaces it from research.
            research_query=(research.research_query if research else clean_topic_descriptor(request)),
            end_condition=(research.end_condition if research else ""),
            starts_at=(research.starts_at if research else None),
            ends_at=(research.ends_at if research else None),
            expires_at=(research.expires_at if research else now + _FALLBACK_LIFESPAN),
            timezone=(research.timezone if research else "UTC"),
            country=(research.country if research else ""),
            language=(research.language if research else ""),
            status=f.TOPIC_STATUS_ACTIVE,
            health=f.TOPIC_HEALTH_HEALTHY,
            research_confidence=(research.confidence if research else 0.0),
            # Heartbeat starts at the agent's chosen idle cadence (it still adapts from
            # there); falls back to the engine default when research gave none / failed.
            pulse_interval_seconds=(
                research.idle_poll_minutes * 60
                if research and research.idle_poll_minutes > 0
                else PULSE_INTERVAL_INITIAL_S
            ),
            notify_start_hour=(
                research.notify_start_hour
                if research and research.notify_start_hour >= 0
                else f.DEFAULT_NOTIFY_START_HOUR
            ),
            notify_end_hour=(
                research.notify_end_hour
                if research and research.notify_end_hour >= 0
                else f.DEFAULT_NOTIFY_END_HOUR
            ),
            awaiting_date=(research.awaiting_date if research else False),
            # If research failed, reconcile ASAP to build the schedule; else in 24h.
            next_reconcile_at=(now + _RECONCILE_INTERVAL if research else now),
            last_reconciled_at=(now if research else None),
            reconcile_count=(1 if research else 0),
            created_at=now,
            updated_at=now,
        )
        await store.set_tracked_topic(topic)
        if research is not None and research.fixtures:
            plan = reconcile_fixtures([], research.fixtures, topic_key=topic_key, now=now)
            await store.upsert_fixtures(topic_key, plan.creates)
            moments = [m for fx in plan.creates for m in build_moments(fx, now=now)]
            await store.upsert_moment_schedule(moments)
            if moments:
                await store.update_tracked_topic(topic_key, {f.TOPIC_CHECKPOINTS_TOTAL: len(moments)})

    # Seed the recurring heartbeat (idempotent) — developments between fixtures, and
    # the only coverage an open-ended topic (a person, a story) has.
    await _ensure_pulse(topic_key, interval_seconds=topic.pulse_interval_seconds, now=now)

    # Already subscribed? (idempotent - don't double-count or duplicate.)
    for existing in await store.list_trackers_for_user(user_id):
        if existing.topic_key == topic_key and existing.status == f.TRACKER_STATUS_ACTIVE:
            return {
                "ok": True, "already": True, "title": topic.title,
                "tracker_id": existing.id, "end_condition": topic.end_condition,
                "ends_at": _iso(topic.ends_at),
            }

    tracker = Tracker(
        id=str(uuid.uuid4()), user_id=user_id, topic_key=topic_key,
        status=f.TRACKER_STATUS_ACTIVE, created_via=created_via,
        created_at=now, updated_at=now,
    )
    await store.create_tracker(tracker)
    await store.adjust_subscriber_count(topic_key, 1)

    logger.info("tracking_engine: tracker provisioned", {
        "user_id": user_id, "topic_key": topic_key, "tracker_id": tracker.id,
        "researched": research is not None,
    })
    return {
        "ok": True, "already": False, "title": topic.title,
        "tracker_id": tracker.id, "kind": topic.kind,
        "end_condition": topic.end_condition, "ends_at": _iso(topic.ends_at),
        "researched": research is not None,
    }
