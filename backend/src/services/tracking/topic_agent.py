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
from .models import Fixture, ResearchedFixture, TopicResearch, _coerce_datetime
from .topic_fetcher import fetch_topic

# Hard lifespan backstop: a topic with no detectable end (open_interest) still
# auto-completes after this, so a tracker can never run forever.
_DEFAULT_LIFESPAN_DAYS = 60

# Event selection per research pass. We keep the SOONEST _MAX_EVENTS the model dated and
# drop only past events and absurd far-future noise (beyond _EVENT_FAR_HORIZON_DAYS). The
# old design used a tight 21-day cutoff that SILENTLY discarded a correctly-found schedule
# whose next beat was just over three weeks out (a Fed decision ~5 weeks away returned 8
# events, all dropped -> zero checkpoints -> silent). Keeping the nearest events regardless
# of a fixed window, then letting the daily reconcile roll the window forward for dense
# topics, fixes both the sparse case (Fed) and the dense case (a tournament keeps its
# nearest ~24 matches now, the rest as they approach).
_EVENT_FAR_HORIZON_DAYS = 120
_MAX_EVENTS = 24

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
  "country": "ISO 3166-1 alpha-2 country code where this topic is mainly followed, e.g. \
\"IN\", \"BR\", \"GB\", \"JP\"; use \"US\" only when it is genuinely US-centric or global",
  "language": "ISO 639-1 code of the language this topic is mostly covered in, e.g. \
\"hi\", \"pt\", \"te\", \"ja\"; \"en\" if mainly English",
  "confidence": 0.0-1.0 (how sure you are about the schedule),
  "idle_poll_minutes": how often to check the web on a day with NOTHING scheduled, in \
minutes — pick by how fast the topic moves (live tournament between match-days ~180-360; \
a slow legal case ~720-1440; a fast-breaking story ~60-120),
  "notify_start_hour": local hour 0-23 it is OK to START sending pushes (default 8),
  "notify_end_hour": local hour 0-23 to STOP sending pushes overnight (default 23),
  "awaiting_date": true ONLY if this is a real future event whose DATE is not announced \
yet (e.g. an IPO with no set date) — then leave "fixtures" empty; the daily re-check catches the date,
  "fixtures": [
    {"label": "ONE specific upcoming fixture, e.g. \"USA vs Australia\" or \"SpaceX IPO opens\"",
     "kind": "span (has duration, e.g. a match) | point (instantaneous, e.g. a verdict, \
a product launch, a result announcement)",
     "start_at": "ISO 8601 UTC start/kickoff time",
     "end_at": "ISO 8601 UTC end time, or null (only spans have an end)",
     "lead_minutes": how long before start to send the heads-up, e.g. 30 (15-120),
     "wake_override": true ONLY for a can't-miss moment (a final, a championship decider, \
a verdict) that may notify outside the notify window; false for routine fixtures}
  ]
}

Rules: all datetimes ISO 8601 with a UTC offset. List the SOONEST upcoming individual \
fixtures you can date, nearest first, each as its OWN entry — if a day has three matches, \
return three fixtures with their own kickoff times; NEVER lump many games into one "round" \
entry. You do NOT need to enumerate an entire long season or tournament: list only the \
nearest ones you are confident of (roughly the next 20), and the tracker re-checks daily \
to pick up later fixtures as they approach — a short, reliable list beats a giant one. Omit \
speculative/undated ones. If the topic has no scheduled fixtures (a person, a company, a \
developing story), return an empty fixtures array — it is still followed by the recurring \
heartbeat at idle_poll_minutes. Return the JSON object and nothing else."""

# Appended to the schema when re-researching a topic that already has stored fixtures.
# The model ECHOES the id of any fixture it recognizes — the primary defense against a
# reworded label forking a parallel series ("Match 98" resolving to "Spain vs Belgium"
# is a label update on the SAME id, a connection only the bracket structure knows).
_KNOWN_FIXTURES_INSTRUCTION = """\

KNOWN FIXTURES already being tracked (id | start UTC | label):
{fixture_lines}

For each fixture in your answer, ALSO include "fixture_id": the id from this list when it
is the same real-world fixture (even if the teams/label/time have since changed or been
confirmed — a bracket slot like "Winner A vs Winner B" that has resolved into real team
names is still the SAME fixture), or "new" when it is genuinely not in the list."""


def _known_fixtures_block(existing_fixtures: list[Fixture] | None) -> str:
    """The KNOWN FIXTURES prompt section for a re-research pass, or "" when there is
    nothing stored to match against (provision, or a topic with no fixtures yet).
    Settled fixtures are listed too — the model must be able to echo a finished
    fixture rather than re-report it as new."""
    if not existing_fixtures:
        return ""
    lines = "\n".join(
        f"  {fx.id} | {fx.start_at.isoformat()} | {fx.label}"
        for fx in sorted(existing_fixtures, key=lambda fx: fx.start_at)
    )
    return _KNOWN_FIXTURES_INSTRUCTION.format(fixture_lines=lines)


def _slugify(text: str, *, fallback: str) -> str:
    def _norm(s: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower())
        return re.sub(r"-{2,}", "-", s).strip("-")[:80].strip("-")

    return _norm(text) or _norm(fallback) or "topic"


def _safe_int(value: object, default: int = 0) -> int:
    """Best-effort int from model JSON (which may give a string, float, or null)."""
    try:
        return int(float(str(value)))  # tolerate "30", 30.0, "30.0"
    except (TypeError, ValueError):
        return default


def _repair_truncated_json(blob: str) -> str | None:
    """Best-effort repair of JSON the model cut off mid-structure (a long fixtures list can
    overrun the output-token limit). Walks the string tracking quote state + bracket depth,
    cuts at the last container/element boundary, and appends the matching closers — so a
    truncated ``"events":[{...},{...`` still yields the fields + the events that DID arrive
    instead of discarding the entire research pass. Returns None when nothing is recoverable."""
    stack: list[str] = []
    in_str = False
    esc = False
    cut: tuple[int, list[str]] | None = None  # (index_to_cut_exclusive, open-closer snapshot)
    for i, ch in enumerate(blob):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            cut = (i + 1, list(stack))   # safe to cut right after a completed element
        elif ch == ",":
            cut = (i, list(stack))       # safe to cut right before a separator (drop the partial)
    if cut is None:
        return None
    idx, open_stack = cut
    return blob[:idx] + "".join(reversed(open_stack))


def _loads_tolerant(blob: str) -> object | None:
    """``json.loads`` that also recovers a truncated payload via ``_repair_truncated_json``.
    Returns None when neither the raw nor the repaired text parses."""
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = _repair_truncated_json(blob)
    if repaired is None:
        return None
    try:
        return json.loads(repaired)
    except (json.JSONDecodeError, ValueError):
        return None


# A fixture that started recently may still be live or just-finished — it must stay
# in the reconcile result (echo-able, updatable) rather than being dropped as "past",
# or the daily pass would orphan every in-flight fixture.
_FIXTURE_PAST_GRACE = timedelta(hours=6)


def _coerce_fixtures(raw_fixtures: object, *, now: datetime) -> list[ResearchedFixture]:
    """Turn the model's raw fixtures array into validated ResearchedFixtures, kept
    SOONEST-first up to _MAX_EVENTS. Drops undated, long-past, and absurd-far-future
    (> far horizon) entries — never the nearest fixtures for being "too far", so a
    sparse topic whose next beat is weeks out is still scheduled. Shared by the
    primary parse and the follow-up pass."""
    far_horizon = now + timedelta(days=_EVENT_FAR_HORIZON_DAYS)
    out: list[ResearchedFixture] = []
    for raw in raw_fixtures or []:  # type: ignore[union-attr]
        if not isinstance(raw, dict):
            continue
        start_at = _coerce_datetime(raw.get("start_at"))
        label = str(raw.get("label", "")).strip()
        if not start_at or not label:
            continue
        if start_at < now - _FIXTURE_PAST_GRACE or start_at > far_horizon:
            continue
        kind_raw = str(raw.get("kind", "")).strip().lower()
        event_kind = f.EVENT_KIND_POINT if kind_raw == f.EVENT_KIND_POINT else f.EVENT_KIND_SPAN
        lead_raw = _safe_int(raw.get("lead_minutes"), 0)
        lead_minutes = max(0, min(1440, lead_raw)) if lead_raw > 0 else 0
        echoed = str(raw.get("fixture_id", "")).strip()
        out.append(ResearchedFixture(
            label=label, start_at=start_at, end_at=_coerce_datetime(raw.get("end_at")),
            event_kind=event_kind,
            lead_minutes=lead_minutes,
            wake_override=bool(raw.get("wake_override", False)),
            echoed_fixture_id="" if echoed.lower() in ("", "new", "null", "none") else echoed,
        ))
    out.sort(key=lambda fx: fx.start_at)
    return out[:_MAX_EVENTS]


def _parse_research(text: str, *, now: datetime, request: str) -> TopicResearch | None:
    """Tolerant parse of the model's JSON into a TopicResearch. Returns None when no
    JSON object can be recovered (the caller then treats research as failed)."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    # Grab the outermost {...}. When the model truncated mid-object there is no closing
    # brace, so fall back to "from the first brace to the end" and let _loads_tolerant
    # repair + close it rather than discarding a partial-but-usable schedule.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1:
        return None
    blob = cleaned[start : end + 1] if end > start else cleaned[start:]
    data = _loads_tolerant(blob)
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

    # Idle heartbeat cadence the agent chose (1h..24h). 0 keeps the engine's default.
    idle_raw = _safe_int(data.get("idle_poll_minutes"), 0)
    idle_poll_minutes = max(60, min(1440, idle_raw)) if idle_raw > 0 else 0
    # Notify-window hours: -1 = "agent gave nothing usable", caller applies defaults.
    start_raw = _safe_int(data.get("notify_start_hour"), -1)
    end_raw = _safe_int(data.get("notify_end_hour"), -1)
    notify_start_hour = start_raw if 0 <= start_raw <= 23 else -1
    notify_end_hour = end_raw if 0 <= end_raw <= 23 else -1
    awaiting_date = bool(data.get("awaiting_date", False))

    # Locale codes drive the localized fetch. Sanitize to letters only; leave blank if the
    # model gave nothing usable so the fetcher applies its US/en default rather than a bad code.
    country = re.sub(r"[^A-Za-z]", "", str(data.get("country", ""))).upper()[:2]
    language = re.sub(r"[^A-Za-z]", "", str(data.get("language", ""))).lower()[:2]

    fixtures = _coerce_fixtures(data.get("fixtures") or data.get("events"), now=now)

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
        country=country,
        language=language,
        confidence=confidence,
        fixtures=fixtures,
        idle_poll_minutes=idle_poll_minutes,
        notify_start_hour=notify_start_hour,
        notify_end_hour=notify_end_hour,
        awaiting_date=awaiting_date,
    )


_FIXTURES_FOLLOWUP_INSTRUCTION = """\
List the specific scheduled fixtures for this topic happening in the NEXT 10 DAYS, each as
its OWN entry with an exact date and UTC time. Return ONLY a JSON array (no prose, no
markdown fences) of objects:
[{"label": "ONE specific fixture, e.g. \\"United States vs Australia\\"",
  "kind": "span (has duration, e.g. a match) | point (instantaneous, e.g. a verdict/launch)",
  "start_at": "ISO 8601 UTC", "end_at": "ISO 8601 UTC or null",
  "lead_minutes": heads-up before start, e.g. 30,
  "wake_override": true only for a can't-miss moment}]
If there are genuinely no dated fixtures in the next 10 days, return []."""


def _parse_fixtures_array(text: str, *, now: datetime) -> list[ResearchedFixture]:
    """Parse the follow-up pass's bare JSON array of fixtures (tolerant of fences/truncation)."""
    if not text:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1:
        return []
    blob = cleaned[start : end + 1] if end > start else cleaned[start:]
    data = _loads_tolerant(blob)
    if not isinstance(data, list):
        return []
    return _coerce_fixtures(data, now=now)


async def _research_fixtures_followup(
    title: str, request: str, models: ModelProvider, now: datetime,
) -> list[ResearchedFixture]:
    """One focused grounded pass for JUST the nearest fixtures, used when the primary research
    came back with zero fixtures for a topic that clearly has a live schedule. Narrow + small,
    so the model reliably enumerates them where the broad prompt punted. Never raises."""
    prompt = (
        f"{_FIXTURES_FOLLOWUP_INSTRUCTION}\n\nTopic: {title or request}\n"
        f"Current time (UTC): {now.isoformat()}"
    )
    try:
        grounded = await models.grounded(prompt)
    except Exception as exc:
        logger.warn("topic_agent: fixtures follow-up pass failed", {
            "topic": (title or request)[:80], "error": str(exc),
        })
        return []
    return _parse_fixtures_array(grounded.text, now=now)


# Below this many concrete fixtures, an ACTIVE tournament/season is treated as under-populated
# and gets the targeted fixtures pass. Catches both the "fixtures: []" case (broad prompt punted)
# and the "one generic placeholder" case (the cheap fallback synthesized 'Group Stage Match'
# from headlines), both of which leave a live topic with nothing specific to schedule.
_MIN_DENSE_FIXTURES = 2


async def _ensure_fixtures(
    research: TopicResearch, *, request: str, models: ModelProvider, now: datetime,
) -> TopicResearch:
    """Backstop the empty/thin-schedule failure: an active bounded/recurring topic that came
    back with too few concrete fixtures (the grounded model punted on a large schedule — the
    FIFA "fixtures: []" case — or the cheap fallback invented a single generic placeholder) gets
    one focused follow-up pass for the nearest specific fixtures, so a live tournament is never
    left with only the slow heartbeat. Open-interest topics (a person, a developing story)
    legitimately have no schedule and are left to the pulse. The follow-up REPLACES the thin
    set only when it found strictly more — so a topic whose single fixture was already
    specific (a lone Fed decision) is never degraded."""
    if research.awaiting_date:
        return research
    if research.kind not in (f.TOPIC_KIND_BOUNDED_EVENT, f.TOPIC_KIND_RECURRING_SEASON):
        return research
    if research.ends_at is not None and research.ends_at <= now:
        return research  # already over — nothing upcoming to schedule
    if len(research.fixtures) >= _MIN_DENSE_FIXTURES:
        return research
    fixtures = await _research_fixtures_followup(research.title, request, models, now)
    if len(fixtures) > len(research.fixtures):
        research.fixtures = fixtures
        logger.info("topic_agent: fixtures follow-up recovered fixtures", {
            "topic_key": research.topic_key, "fixtures": len(fixtures),
        })
    return research


async def research_topic(
    request: str,
    *,
    models: ModelProvider | None = None,
    now: datetime | None = None,
    existing_fixtures: list[Fixture] | None = None,
) -> TopicResearch | None:
    """Research ``request`` and return a structured TopicResearch, or None if both
    the grounded pass and the cheap fallback fail to yield parseable JSON (the caller
    then tells the user it couldn't set the tracker up — never a silent no-op).

    ``existing_fixtures`` (the reconcile path) is shown to the model so it can ECHO
    the stored id of any fixture it recognizes; fixture_matcher then updates those in
    place instead of ever forking a reworded series. Provision passes None (nothing
    stored yet).

    ``last_research_tier`` is carried on the result-adjacent log line so a degraded
    research source is visible.
    """
    request = (request or "").strip()
    if not request:
        return None
    models = models or get_model_provider()
    now = now or datetime.now(UTC)

    prompt = f"{_SCHEMA_INSTRUCTION}{_known_fixtures_block(existing_fixtures)}\n\nTopic to track: {request}\nCurrent time (UTC): {now.isoformat()}"

    # Primary: grounded live search + synthesis.
    try:
        grounded = await models.grounded(prompt)
        research = _parse_research(grounded.text, now=now, request=request)
        if research is not None:
            research = await _ensure_fixtures(research, request=request, models=models, now=now)
            logger.info("topic_agent: researched via grounded", {
                "topic_key": research.topic_key, "kind": research.kind,
                "fixtures": len(research.fixtures), "confidence": research.confidence,
            })
            return research
        logger.warn("topic_agent: grounded returned unparseable JSON, trying cheap fallback", {
            "request": request[:120],
        })
    except Exception as exc:
        logger.warn("topic_agent: grounded research failed, trying cheap fallback", {
            "request": request[:120], "error": str(exc),
        })

    # Fallback: cheap fetch chain for raw material + a cheap model to synthesize JSON.
    fetched = await fetch_topic(request)
    context = fetched.text if fetched.ok else "(no web context available)"
    try:
        raw = await models.cheap(
            f"{_SCHEMA_INSTRUCTION}{_known_fixtures_block(existing_fixtures)}\n\nTopic to track: {request}\n"
            f"Current time (UTC): {now.isoformat()}\n\nWeb context:\n{context}",
            temperature=0.3,
        )
        research = _parse_research(str(raw), now=now, request=request)
        if research is not None:
            # The cheap path can only synthesize a generic fixture from headlines (it has no
            # fixture list), so a dense topic still routes through the targeted fixtures pass.
            research = await _ensure_fixtures(research, request=request, models=models, now=now)
            logger.info("topic_agent: researched via cheap fallback", {
                "topic_key": research.topic_key, "fixtures": len(research.fixtures),
                "fetch_tier": fetched.tier,
            })
            return research
    except Exception as exc:
        logger.warn("topic_agent: cheap fallback research failed", {
            "request": request[:120], "error": str(exc),
        })

    logger.warn("topic_agent: could not research topic (both passes failed)", {"request": request[:120]})
    return None
