"""Topic tracking engine — the two scheduler-ridden loops.

run_checkpoint_tick()  (every minute): drain the due-queue. For each due checkpoint,
  claim it (atomic, no double-fire), fetch the topic's LIVE state ONCE through the
  tiered fetch chain, compose one update, dedup it at the topic level, then FAN OUT to
  the topic's active subscribers — each gated only by a per-user dedup (no gatekeeper,
  no cap: a user-requested update is always sent). One fetch + one compose serve every
  subscriber (the scale lever: cost tracks topics, not users).

run_reconcile_tick()  (daily per topic): re-research each active topic, upsert its
  checkpoints (new rounds appear, shifted times update, dropped events expire), and
  auto-complete it past its lifespan.

Both are fire-and-forget from the scheduler, and isolate every per-item failure so one
bad topic can never stop the loop or fail the reminder tick — mirroring icebreaker_engine
/ briefing_engine.
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
from ..notification_budget import record_committed_send
from ..notification_service import send_notification
from . import fields as f
from . import tracking_store as store
from .models import Checkpoint, TrackedTopic, Tracker
from .schedule_builder import (
    PULSE_INTERVAL_INITIAL_S,
    build_checkpoints,
    build_pulse_checkpoint,
    next_pulse_interval,
    plan_reconcile,
)
from .topic_agent import _slugify, research_topic
from .topic_fetcher import fetch_topic

# Max checkpoints / topics processed simultaneously
_CONCURRENCY = 10

# Re-research cadence + how many straight failures retire a topic (stop burning calls).
_RECONCILE_INTERVAL = timedelta(hours=24)
_MAX_RECONCILE_FAILURES = 5
_COMPOSE_TIMEOUT_S = 10.0

# Lifespan backstop when research couldn't determine an end (mirrors topic_agent).
_FALLBACK_LIFESPAN = timedelta(days=60)

# Bound the in-chat research wait so a slow grounded call never hangs the chat turn.
_PROVISION_RESEARCH_TIMEOUT_S = 15.0


@dataclass
class CheckpointTickSummary:
    enabled: bool = True
    due: int = 0
    fired: int = 0
    sent: int = 0
    skipped_dedup: int = 0
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
    checkpoints_upserted: int = 0
    checkpoints_expired: int = 0


@dataclass
class _ComposedUpdate:
    summary: str        # canonical factual state, for dedup
    title: str
    body: str
    opening_chat_message: str


_COMPOSE_SYSTEM = """\
        You are Buddy, a warm AI companion, writing ONE short live-update push for a topic the
        user asked you to keep them posted on. From the web context, write the update for this
        moment. Return ONLY JSON:
        
        {"summary":"<=120 char canonical factual state, for de-duplication (e.g. 'USA 2-1 AUS, full time')",
        
        "title":"<=45 char push title",
        "body":"1-2 warm sentences with the concrete update",
        "opening_chat_message":"a friendly chat opener continuing this update if the user taps"}
        If the web context has nothing genuinely new or concrete, set summary to "" (empty).
        """


def _parse_composed(raw: str) -> _ComposedUpdate | None:
    cleaned = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    
    summary = str(data.get("summary", "")).strip()
    if not summary:
        return None
    return _ComposedUpdate(
        summary=summary,
        title=str(data.get("title", "")).strip()[:60] or "Update",
        body=str(data.get("body", "")).strip() or summary,
        opening_chat_message=str(data.get("opening_chat_message", "")).strip() or summary,
    )


async def _compose_update(
    models: ModelProvider, topic: TrackedTopic, checkpoint: Checkpoint, context: str,
) -> _ComposedUpdate | None:
    """One cheap LLM call turns the live web context into a push + a dedup summary.
    Returns None when the model judges there is nothing new/concrete to say."""
    event_line = f"Event: {checkpoint.event_label}\n" if checkpoint.event_label else ""
    prompt = (
        f"Topic: {topic.title}\n"
        f"{event_line}"
        f"Phase: {checkpoint.phase} (pre=just before it starts, live=happening now, "
        f"post=just finished, milestone=the moment it happens, pulse=a routine check-in "
        f"on an ongoing topic)\n"
        f"Last known state (avoid repeating verbatim): {topic.live_summary or '(none)'}\n\n"
        f"Web context:\n{context}"
    )
    try:
        raw = await asyncio.wait_for(
            models.cheap(prompt, system=_COMPOSE_SYSTEM, temperature=0.4),
            timeout=_COMPOSE_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn("tracking_engine: compose LLM failed", {
            "topic_key": topic.topic_key, "checkpoint_id": checkpoint.id, "error": str(exc),
        })
        return None
    return _parse_composed(str(raw))


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

    logger.info("tracking_engine: checkpoint tick complete", {
        "due": summary.due, "fired": summary.fired, "sent": summary.sent,
        "skipped_dedup": summary.skipped_dedup, "failed": summary.failed,
        "expired": summary.expired,
    })
    return summary


async def _rearm_or_settle(
    cp: Checkpoint,
    topic: TrackedTopic,
    *,
    outcome: str,
    tier: str,
    now: datetime,
    summary_text: str | None = None,
) -> None:
    """Close out a fired checkpoint. A PULSE is recurring: it is re-armed to PENDING with
    an adaptively-chosen next fire_at (tighter when it found something new, looser when it
    did not) and never goes terminal here. An EVENT checkpoint is one-shot: it is moved to
    its terminal state (fired | skipped | failed)."""
    if cp.phase == f.CHECKPOINT_PHASE_PULSE:
        found_new = outcome == f.CHECKPOINT_STATUS_FIRED
        next_seconds = next_pulse_interval(
            topic.pulse_interval_seconds or PULSE_INTERVAL_INITIAL_S, found_new=found_new,
        )
        await store.rearm_pulse(
            cp.id, fire_at=now + timedelta(seconds=next_seconds),
            tier=tier, at=now, summary=summary_text,
        )
        await store.update_tracked_topic(
            cp.topic_key, {f.TOPIC_PULSE_INTERVAL_SECONDS: next_seconds},
        )
        return

    extra: dict = {f.CHECKPOINT_LAST_FETCH_TIER: tier, f.CHECKPOINT_LAST_FETCH_AT: now}
    if outcome == f.CHECKPOINT_STATUS_FAILED:
        extra[f.CHECKPOINT_LAST_ERROR] = "no usable live state from any fetch tier"
    elif outcome == f.CHECKPOINT_STATUS_FIRED:
        extra[f.CHECKPOINT_FIRED_AT] = now
        if summary_text is not None:
            extra[f.CHECKPOINT_LAST_SUMMARY] = summary_text
    await store.mark_checkpoint(cp.id, outcome, **extra)


async def _fire_checkpoint(
    cp: Checkpoint, models: ModelProvider, now: datetime, summary: CheckpointTickSummary,
) -> None:
    is_pulse = cp.phase == f.CHECKPOINT_PHASE_PULSE

    # Atomic claim — only one tick fires a given checkpoint.
    if not await store.claim_checkpoint(cp.id):
        return

    topic = await store.get_tracked_topic(cp.topic_key)
    if topic is None or topic.status != f.TOPIC_STATUS_ACTIVE:
        # Topic gone/retired: the pulse winds down here too (it is not re-armed).
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
        # No one to notify. A pulse is re-armed (looser) WITHOUT a fetch, so a transient
        # empty read can't permanently kill the heartbeat and we burn no fetch on a
        # topic nobody follows; reconcile retires the topic (stale) and the status guard
        # above then expires the pulse. An event checkpoint is one-shot, so it is skipped.
        if is_pulse:
            await _rearm_or_settle(
                cp, topic, outcome=f.CHECKPOINT_STATUS_SKIPPED,
                tier=cp.last_fetch_tier or f.TIER_NONE, now=now,
            )
        else:
            await store.mark_checkpoint(cp.id, f.CHECKPOINT_STATUS_SKIPPED)
        return

    # ONE live fetch for the whole fan-out, localized to the topic's region.
    fetched = await fetch_topic(
        topic.research_query or topic.title, country=topic.country, language=topic.language,
    )
    if not fetched.ok:
        await _rearm_or_settle(cp, topic, outcome=f.CHECKPOINT_STATUS_FAILED, tier=fetched.tier, now=now)
        if not is_pulse:
            await store.update_tracked_topic(cp.topic_key, {f.TOPIC_CHECKPOINTS_FAILED: fs.Increment(1)})
        summary.failed += 1
        return

    composed = await _compose_update(models, topic, cp, fetched.text)
    if composed is None or composed.summary == topic.live_summary:
        # Nothing new since the last fetch of this topic — restraint, no fan-out.
        await _rearm_or_settle(cp, topic, outcome=f.CHECKPOINT_STATUS_SKIPPED, tier=fetched.tier, now=now)
        summary.skipped_dedup += 1
        return

    # New state: update the shared cache once, then fan out.
    await store.update_topic_live_cache(
        cp.topic_key, summary=composed.summary, fetched_at=now, tier=fetched.tier,
    )

    for sub in subscribers:
        try:
            await _deliver_to_subscriber(sub, topic, composed, now, summary)
        except Exception as exc:
            logger.warn("tracking_engine: subscriber delivery failed", {
                "tracker_id": sub.id, "topic_key": cp.topic_key, "error": str(exc),
            })

    await _rearm_or_settle(
        cp, topic, outcome=f.CHECKPOINT_STATUS_FIRED, tier=fetched.tier, now=now,
        summary_text=composed.summary,
    )
    summary.fired += 1


async def _send_tracker_push(
    *, user_id: str, topic_key: str, tracker_id: str,
    title: str, body: str, opening_chat_message: str,
):
    """Single FCM send for a tracker update (live fan-out and deferred re-delivery share
    this exact payload shape)."""
    return await send_notification(
        user_id,
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
    )


async def _deliver_to_subscriber(sub, topic, composed, now, summary) -> None:
    # Per-user dedup: this user already got this exact state from another checkpoint.
    # This is correctness (never send the identical update twice), NOT gating — a
    # genuinely-new update always goes out. No gatekeeper, no cap: the user asked to be
    # kept posted, so every new beat is delivered.
    if sub.last_sent_summary == composed.summary:
        summary.skipped_dedup += 1
        return

    result = await _send_tracker_push(
        user_id=sub.user_id, topic_key=topic.topic_key, tracker_id=sub.id,
        title=composed.title, body=composed.body, opening_chat_message=composed.opening_chat_message,
    )
    if not result.delivered:
        return

    # Advance the per-user dedup cursor + bookkeeping (sent count, last_sent_summary).
    await store.record_tracker_outcome(sub.id, summary=composed.summary, at=now)
    # Recorded against the shared budget purely so other proactive deciders can see the
    # activity; a tracker update is never itself blocked (mirrors a committed reminder).
    await record_committed_send(sub.user_id, source="tracker")
    summary.sent += 1


# ── reconcile tick ───────────────────────────────────────────────────────────
async def run_reconcile_tick() -> ReconcileTickSummary:
    """Re-research each active topic and self-heal its schedule."""
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
        "checkpoints_upserted": summary.checkpoints_upserted,
        "checkpoints_expired": summary.checkpoints_expired,
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
    # grounded research call on it. Uses the authoritative subscriber list, not the
    # cached counter, so a drifted count can't prematurely kill a live topic.
    if not await store.list_active_subscribers(topic.topic_key):
        await store.set_topic_status(topic.topic_key, f.TOPIC_STATUS_STALE)
        summary.stale += 1
        return

    research = await research_topic(topic.research_query or topic.title, models=models, now=now)
    if research is None:
        failures = topic.consecutive_reconcile_failures + 1
        updates = {
            f.TOPIC_LAST_RECONCILE_STATUS: "failed",
            f.TOPIC_LAST_RECONCILE_ERROR: "research returned no parseable result",
            f.TOPIC_CONSECUTIVE_RECONCILE_FAILURES: failures,
            f.TOPIC_NEXT_RECONCILE_AT: now + _RECONCILE_INTERVAL,
            f.TOPIC_HEALTH: f.TOPIC_HEALTH_STALLED,
        }
        # Too many straight failures — retire it so it stops burning calls.
        if failures >= _MAX_RECONCILE_FAILURES:
            updates[f.TOPIC_STATUS] = f.TOPIC_STATUS_FAILED
        await store.update_tracked_topic(topic.topic_key, updates)
        summary.failed += 1
        return

    existing = await store.list_checkpoints_for_topic(topic.topic_key)
    upserts, expire_ids = plan_reconcile(research, existing, topic_key=topic.topic_key, now=now)
    await store.upsert_checkpoints(upserts)
    for eid in expire_ids:
        await store.mark_checkpoint(eid, f.CHECKPOINT_STATUS_EXPIRED)
    summary.checkpoints_upserted += len(upserts)
    summary.checkpoints_expired += len(expire_ids)

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
        f.TOPIC_CHECKPOINTS_TOTAL: len(existing) + len(upserts),
    }
    # Refine locale only when the fresh pass actually returned codes — never clobber a
    # good stored locale with an empty one if this reconcile's research omitted them.
    if research.country:
        reconcile_updates[f.TOPIC_COUNTRY] = research.country
    if research.language:
        reconcile_updates[f.TOPIC_LANGUAGE] = research.language
    await store.update_tracked_topic(topic.topic_key, reconcile_updates)
    # Self-heal: a topic created before pulses existed (or whose pulse was never seeded)
    # picks one up here. Idempotent — a live pulse is left untouched.
    await _ensure_pulse(topic.topic_key, interval_seconds=topic.pulse_interval_seconds, now=now)
    summary.reconciled += 1


# ── provisioning (called from the track_topic chat tool) ─────────────────────
def _iso(value: datetime | None) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


async def _ensure_pulse(topic_key: str, *, interval_seconds: int, now: datetime) -> None:
    """Seed the recurring heartbeat for a topic if it has none yet (idempotent — a live
    pulse is never reset). This is what makes an open-ended topic with no dated events
    (a person, a company, a developing story) still get adaptive updates. Called from
    both provisioning and the reconcile self-heal so a topic created before pulses
    existed picks one up on its next reconcile."""
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

    research = None
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
            research_query=(research.research_query if research else request),
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
            pulse_interval_seconds=PULSE_INTERVAL_INITIAL_S,
            # If research failed, reconcile ASAP to build the schedule; else in 24h.
            next_reconcile_at=(now + _RECONCILE_INTERVAL if research else now),
            last_reconciled_at=(now if research else None),
            reconcile_count=(1 if research else 0),
            created_at=now,
            updated_at=now,
        )
        await store.set_tracked_topic(topic)
        if research is not None:
            checkpoints = build_checkpoints(research, topic_key=topic_key, now=now)
            await store.upsert_checkpoints(checkpoints)
            if checkpoints:
                await store.update_tracked_topic(topic_key, {f.TOPIC_CHECKPOINTS_TOTAL: len(checkpoints)})

    # Seed the recurring heartbeat (idempotent). This is what guarantees an ongoing topic
    # with no dated events still gets adaptive updates — the gap that left "keep me posted
    # on <person/company/story>" silent before.
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
