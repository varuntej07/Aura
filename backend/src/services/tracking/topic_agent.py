"""Topic research agent — the generic brain that turns ANY "keep me posted on X"
request into a structured plan.

ONE function, ``research_topic``, runs a live research pass and returns a
``TopicResearch``: a stable ``topic_key`` (so two users on the same public event
share one shared doc), the topic ``kind`` and ``end_condition``, its lifespan, and
the upcoming dated ``events`` the schedule builder turns into checkpoints. There is
NO per-topic code — a tournament, an election, a product launch, a court case all go
through the same pass.

Primary source is ``ModelProvider.grounded`` (live web search + synthesis in one
call), which is uniquely good at "list the upcoming fixtures/dates with times".
Grounding and a forced JSON ``response_model`` are mutually exclusive in the SDK, so
we prompt for JSON and parse the free text ourselves. If grounded is down, we fall
back to the cheap fetch chain (``topic_fetcher``) for raw material and synthesize
the same JSON with a cheap model — so a grounding outage degrades, never blocks.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from ...lib.logger import logger
from ..model_provider import ModelProvider, get_model_provider
from . import fields as f
from .models import ScheduledEvent, TopicResearch, _coerce_datetime
from .topic_fetcher import fetch_topic

# Hard lifespan backstop: a topic with no detectable end (open_interest) still
# auto-completes after this, so a tracker can never run forever.
_DEFAULT_LIFESPAN_DAYS = 60

# Only materialize events within this forward window per research pass; the daily
# reconcile extends it (new rounds appear as the event approaches). Bounds the doc.
_EVENT_HORIZON_DAYS = 21
_MAX_EVENTS = 20

_VALID_KINDS = {
    f.TOPIC_KIND_BOUNDED_EVENT,
    f.TOPIC_KIND_RECURRING_SEASON,
    f.TOPIC_KIND_OPEN_INTEREST,
}

_SCHEMA_INSTRUCTION = """\
You are setting up a live "keep me posted" tracker for a user. Research the topic on
the web and return ONLY a single JSON object (no prose, no markdown fences) with:

{
  "topic_key": "stable kebab-case slug identifying the PUBLIC event/topic, e.g. \
\"fifa-world-cup-2026\" or \"india-general-election-2026\" (NOT user-specific)",
  "title": "short human title, e.g. \"USA at the FIFA World Cup 2026\"",
  "kind": "one of: bounded_event (has a clear end) | recurring_season (a league/\
season with many sub-events) | open_interest (a team/person, no natural end)",
  "research_query": "the best short web-search query to track this going forward",
  "end_condition": "plain text describing when updates should STOP, e.g. \"when the \
tournament final is played or the user's team is eliminated\"",
  "starts_at": "ISO 8601 UTC when the topic/event begins, or null",
  "ends_at": "ISO 8601 UTC when it is expected to end, or null",
  "timezone": "IANA timezone most relevant to the event, or \"UTC\"",
  "confidence": 0.0-1.0 (how sure you are about the schedule),
  "events": [
    {"label": "specific upcoming beat, e.g. \"USA vs Australia\"",
     "start_at": "ISO 8601 UTC start/kickoff time",
     "end_at": "ISO 8601 UTC end time or null"}
  ]
}

Rules: all datetimes ISO 8601 with a UTC offset. List only CONCRETE upcoming events \
you can date; omit speculative ones. If the topic is open-ended with no scheduled \
events, return an empty events array. Return the JSON object and nothing else."""


def _slugify(text: str, *, fallback: str) -> str:
    def _norm(s: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower())
        return re.sub(r"-{2,}", "-", s).strip("-")[:80].strip("-")

    return _norm(text) or _norm(fallback) or "topic"


def _parse_research(text: str, *, now: datetime, request: str) -> TopicResearch | None:
    """Tolerant parse of the model's JSON into a TopicResearch. Returns None when no
    JSON object can be recovered (the caller then treats research as failed)."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    # Grab the outermost {...} so leading/trailing stray text never breaks the parse.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    title = str(data.get("title", "")).strip() or request.strip()[:120]
    topic_key = _slugify(str(data.get("topic_key", "")).strip(), fallback=title)

    kind = str(data.get("kind", "")).strip().lower()
    if kind not in _VALID_KINDS:
        kind = f.TOPIC_KIND_OPEN_INTEREST

    starts_at = _coerce_datetime(data.get("starts_at"))
    ends_at = _coerce_datetime(data.get("ends_at"))
    # Hard backstop so an open-ended topic still auto-completes.
    expires_at = ends_at or (now + timedelta(days=_DEFAULT_LIFESPAN_DAYS))

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    horizon = now + timedelta(days=_EVENT_HORIZON_DAYS)
    events: list[ScheduledEvent] = []
    for raw in data.get("events", []) or []:
        if not isinstance(raw, dict):
            continue
        start_at = _coerce_datetime(raw.get("start_at"))
        label = str(raw.get("label", "")).strip()
        # Only keep concrete, upcoming, in-horizon events; a past or undated one is noise.
        if not start_at or not label or start_at < now or start_at > horizon:
            continue
        events.append(ScheduledEvent(
            label=label, start_at=start_at, end_at=_coerce_datetime(raw.get("end_at")),
        ))
        if len(events) >= _MAX_EVENTS:
            break

    return TopicResearch(
        topic_key=topic_key,
        title=title,
        kind=kind,
        research_query=str(data.get("research_query", "")).strip() or title,
        end_condition=str(data.get("end_condition", "")).strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        expires_at=expires_at,
        timezone=str(data.get("timezone", "UTC")).strip() or "UTC",
        confidence=confidence,
        events=events,
    )


async def research_topic(
    request: str,
    *,
    models: ModelProvider | None = None,
    now: datetime | None = None,
) -> TopicResearch | None:
    """Research ``request`` and return a structured TopicResearch, or None if both
    the grounded pass and the cheap fallback fail to yield parseable JSON (the caller
    then tells the user it couldn't set the tracker up — never a silent no-op).

    ``last_research_tier`` is carried on the result-adjacent log line so a degraded
    research source is visible.
    """
    request = (request or "").strip()
    if not request:
        return None
    models = models or get_model_provider()
    now = now or datetime.now(UTC)

    prompt = f"{_SCHEMA_INSTRUCTION}\n\nTopic to track: {request}\nCurrent time (UTC): {now.isoformat()}"

    # Primary: grounded live search + synthesis.
    try:
        grounded = await models.grounded(prompt)
        research = _parse_research(grounded.text, now=now, request=request)
        if research is not None:
            logger.info("topic_agent: researched via grounded", {
                "topic_key": research.topic_key, "kind": research.kind,
                "events": len(research.events), "confidence": research.confidence,
            })
            return research
        logger.warn("topic_agent: grounded returned unparseable JSON — trying cheap fallback", {
            "request": request[:120],
        })
    except Exception as exc:
        logger.warn("topic_agent: grounded research failed — trying cheap fallback", {
            "request": request[:120], "error": str(exc),
        })

    # Fallback: cheap fetch chain for raw material + a cheap model to synthesize JSON.
    fetched = await fetch_topic(request)
    context = fetched.text if fetched.ok else "(no web context available)"
    try:
        raw = await models.cheap(
            f"{_SCHEMA_INSTRUCTION}\n\nTopic to track: {request}\n"
            f"Current time (UTC): {now.isoformat()}\n\nWeb context:\n{context}",
            temperature=0.3,
        )
        research = _parse_research(str(raw), now=now, request=request)
        if research is not None:
            logger.info("topic_agent: researched via cheap fallback", {
                "topic_key": research.topic_key, "events": len(research.events),
                "fetch_tier": fetched.tier,
            })
            return research
    except Exception as exc:
        logger.warn("topic_agent: cheap fallback research failed", {
            "request": request[:120], "error": str(exc),
        })

    logger.warn("topic_agent: could not research topic (both passes failed)", {"request": request[:120]})
    return None
