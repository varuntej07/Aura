"""
content_candidates collection — the shared pool of notifiable items.

Fetchers (HN, arXiv, ESPN Cricinfo RSS, Google News, cricbuzz live) add items
here after each fetch.
The scoring loop reads from here via find_nearest using Firestore native
vector search against the user's user_vector.

"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ...lib.logger import logger
from ..firebase import admin_firestore
from .content_category_map import to_taxonomy_slug
from .embedder import embed_texts

# Default candidate lifetime for any source that does not pass an explicit
# ttl_hours. Per-source overrides are passed in via add_candidates.
DEFAULT_CONTENT_TTL_HOURS = 36

# Hard ceiling on how many candidates find_nearest pulls back per user per scoring tick.
# 50 is generous for diversity post-scoring without bloating the per-tick wire payload.
MAX_NEAREST_CANDIDATES = 50

# Per-call ceiling on the expired-candidate sweep so one scheduler tick can never run
# an unbounded delete inside its 1-minute window. The deploy-day backlog drains across
# a few ticks; steady state removes only what expired since the last sweep.
EXPIRED_SWEEP_MAX_PER_TICK = 1000

# Firestore hard cap on operations in a single batched commit.
_FIRESTORE_BATCH_LIMIT = 500


@dataclass
class CandidateInput:
    """What a fetcher hands to the pool. The pool generates the embedding."""

    source: str
    category: str
    title: str
    body: str
    url: str
    freshness_ts: datetime | None = None
    ttl_hours: int | None = None
    extra: dict[str, Any] | None = None
    sub_category: str = ""  # optional league/tournament tag e.g. "ipl", "premier_league"
    # False marks an item that may appear in the in-app feed but must NEVER fire a
    # push notification — e.g. a story whose body is only engagement counts (no
    # substance to frame). The scoring loop drops these from the notification
    # candidate set; the feed ignores the flag.
    push_eligible: bool = True
    # Global salience 0..1 (how big worldwide, independent of any user). Set at
    # ingest from cross-edition overlap (see signal_engine/salience.py). Drives the
    # breaking lane (a value >= scoring.BREAKING_SALIENCE_BAR can reach every user)
    # and gives a mild nudge to the personal lane. 0 for region-agnostic / single-
    # source items (newsdata, arXiv-era docs), which therefore never go "breaking".
    salience: float = 0.0


@dataclass
class ScoredCandidate:
    """A candidate plus its raw cosine similarity from find_nearest."""

    content_id: str
    source: str
    category: str
    title: str
    body: str
    url: str
    embedding: list[float]
    freshness_ts: datetime
    cosine_similarity: float
    sub_category: str = ""
    # Locale edition the candidate was fetched from ("US" | "IN" | "GB" | ""),
    # carried in the stored doc's extra.region. Drives the scoring loop's soft
    # region preference. Empty when the source is region-agnostic (HN, arXiv).
    region: str = ""
    # Mirrors CandidateInput.push_eligible read back from the stored doc. Legacy docs
    # written before this field default True (so existing content keeps sending). The
    # scoring loop excludes push_eligible=False from the notification path only.
    push_eligible: bool = True
    # Global salience 0..1 read back from the stored doc. Legacy docs without the
    # field default 0.0 — so a pre-deploy candidate is personal-lane only and can
    # never be mistaken for breaking news.
    salience: float = 0.0


def _build_content_id(source: str, url: str, title: str) -> str:
    """Stable ID derived from source + url so the same item is not embedded twice."""
    key = (url.strip() or title.strip()).lower()
    digest = hashlib.sha256(f"{source}|{key}".encode()).hexdigest()[:24]
    return f"{source}_{digest}"


def _candidate_doc_ref(content_id: str):
    return admin_firestore().collection("content_candidates").document(content_id)


def _content_text_for_embedding(title: str, body: str) -> str:
    """The string handed to gemini-embedding-001 for a candidate."""
    title_part = (title or "").strip()
    body_part = (body or "").strip()
    if title_part and body_part:
        return f"{title_part}\n\n{body_part}"
    return title_part or body_part


async def add_candidates(items: list[CandidateInput]) -> int:
    """ Embed and upsert candidates. Returns the count actually written.

        Skips items whose content_id already exists (no re-embedding) and items
        with empty title and body.
    """
    if not items:
        return 0

    cleaned: list[tuple[str, CandidateInput, str]] = []
    for item in items:
        text = _content_text_for_embedding(item.title, item.body)
        if not text:
            continue
        content_id = _build_content_id(item.source, item.url, item.title)
        cleaned.append((content_id, item, text))

    if not cleaned:
        return 0

    # De-dup against existing docs in one batched read so we only embed new ones.
    existing_ids = await _filter_existing_ids([cid for cid, _, _ in cleaned])
    new_only = [(cid, item, text) for cid, item, text in cleaned if cid not in existing_ids]
    if not new_only:
        return 0

    vectors = await embed_texts([text for _, _, text in new_only])

    now = datetime.now(UTC)
    written = 0

    def _put_batch() -> int:
        db = admin_firestore()
        batch = db.batch()
        count = 0
        for (content_id, item, text), vector in zip(new_only, vectors):
            ttl_hours = item.ttl_hours or DEFAULT_CONTENT_TTL_HOURS
            # Normalise the raw fetcher category to a taxonomy slug at this single
            # write choke point so the pool only ever stores one vocabulary — the
            # gate, diversity, affinity, and feed all read taxonomy slugs.
            doc = {
                "source": item.source,
                "category": to_taxonomy_slug(item.category),
                "sub_category": item.sub_category,
                "title": item.title,
                "body": item.body,
                "url": item.url,
                "text": text,
                "embedding": Vector(vector),
                "created_at": now,
                "expires_at": now + timedelta(hours=ttl_hours),
                "freshness_ts": item.freshness_ts or now,
                "extra": item.extra or {},
                "push_eligible": item.push_eligible,
                "salience": float(item.salience),
            }
            batch.set(_candidate_doc_ref(content_id), doc)
            count += 1
        batch.commit()
        return count

    for attempt in range(1, 3):
        try:
            written = await asyncio.to_thread(_put_batch)
            break
        except Exception as exc:
            if attempt == 2:
                logger.error("content_pool.add_candidates failed after retry", {
                    "batch_size": len(new_only),
                    "error": str(exc),
                })
                return 0
            await asyncio.sleep(1.0)

    logger.info("content_pool: candidates added", {
        "added": written,
        "skipped_existing": len(cleaned) - len(new_only),
        "sources": list({item.source for _, item, _ in new_only}),
    })
    return written


async def _filter_existing_ids(content_ids: list[str]) -> set[str]:
    """Return the subset of content_ids that already exist in Firestore."""
    if not content_ids:
        return set()

    def _check() -> set[str]:
        db = admin_firestore()
        refs = [_candidate_doc_ref(cid) for cid in content_ids]
        snaps = db.get_all(refs)
        return {snap.id for snap in snaps if snap.exists}

    try:
        return await asyncio.to_thread(_check)
    except Exception as exc:
        logger.warn("content_pool._filter_existing_ids failed", {"error": str(exc)})
        return set()


async def find_nearest_for_user(
    user_vector: list[float],
    *,
    limit: int = MAX_NEAREST_CANDIDATES,
    now: datetime | None = None,
) -> list[ScoredCandidate]:
    """Vector-search candidates closest to user_vector. Excludes expired items.

    The cosine_similarity field on the returned ScoredCandidate is derived from
    Firestore's COSINE distance (similarity = 1 - distance).
    """
    if not user_vector:
        return []
    current_time = now or datetime.now(UTC)

    def _query() -> list[ScoredCandidate]:
        db = admin_firestore()
        collection = db.collection("content_candidates")
        query = collection.find_nearest(
            vector_field="embedding",
            query_vector=Vector(user_vector),
            distance_measure=DistanceMeasure.COSINE,
            limit=limit,
            distance_result_field="cosine_distance",
        )
        results: list[ScoredCandidate] = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            expires_at = data.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at < current_time:
                continue
            vec_field = data.get("embedding")
            if isinstance(vec_field, Vector):
                embedding = list(vec_field.to_map_value()["value"])  # type: ignore[attr-defined]
            else:
                embedding = [float(x) for x in (vec_field or [])]
            distance = float(data.get("cosine_distance", 1.0))
            similarity = max(0.0, 1.0 - distance)
            extra_field = data.get("extra")
            extra = extra_field if isinstance(extra_field, dict) else {}
            results.append(ScoredCandidate(
                content_id=snap.id,
                source=str(data.get("source", "")),
                category=str(data.get("category", "")),
                sub_category=str(data.get("sub_category", "")),
                title=str(data.get("title", "")),
                body=str(data.get("body", "")),
                url=str(data.get("url", "")),
                embedding=embedding,
                freshness_ts=data.get("freshness_ts") or current_time,
                cosine_similarity=similarity,
                region=str(extra.get("region", "")),
                push_eligible=bool(data.get("push_eligible", True)),
                salience=float(data.get("salience", 0.0) or 0.0),
            ))
        return results

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        message = str(exc)
        if "vector index" in message.lower():
            # This is the difference between "no content matched" and "the engine
            # is structurally dead". A missing vector index makes EVERY user get 0
            # candidates, so notifications silently stop. Log it loud and distinct.
            logger.error(
                "content_pool.find_nearest_for_user: MISSING VECTOR INDEX on "
                "content_candidates.embedding — every user gets 0 candidates and "
                "NO notifications can send. Create it with: gcloud firestore indexes "
                "composite create --collection-group=content_candidates "
                "--query-scope=COLLECTION "
                "--field-config=vector-config='{\"dimension\":\"768\",\"flat\":\"{}\"}'"
                ",field-path=embedding",
                {"error": message},
            )
        else:
            logger.error("content_pool.find_nearest_for_user failed", {"error": message})
        return []


# NOTE: an in-process 3h TTL cache (find_nearest_for_user_cached) used to sit
# here to spare the 15-30 min scoring cron from re-querying an unchanged pool.
# Scoring is now ingest-triggered (one pass per 4h generation, see
# signal_engine/generation_store.py), so every scoring pass genuinely faces a
# refreshed pool and the durable generation guard is the single cost and
# correctness boundary — the cache was removed rather than kept as a second,
# competing mechanism that could serve a pre-ingest snapshot to the pass that
# exists precisely to score the post-ingest pool.


@dataclass
class RecentHeadline:
    """A lightweight pool item for the icebreaker context bundle — just enough to
    turn a fresh local headline into a conversation hook. No embedding/url needed."""

    title: str
    category: str
    region: str
    source: str


async def list_recent_candidates(
    *,
    limit: int = 30,
    region: str | None = None,
    now: datetime | None = None,
) -> list[RecentHeadline]:
    """Most recent non-expired pool items, newest first, optionally region-filtered.

    Orders by ``created_at`` descending only — a single-field order that Firestore
    auto-indexes at collection scope, so this needs NO declared composite index.
    The region filter is applied in Python (not a Firestore ``where``) precisely to
    avoid a composite index, and the result set is small (``limit`` rows). Returns
    an empty list on any error so the icebreaker simply proceeds without headlines.
    """
    current_time = now or datetime.now(UTC)
    target_region = (region or "").strip().upper()

    def _query() -> list[RecentHeadline]:
        db = admin_firestore()
        snaps = (
            db.collection("content_candidates")
            .order_by("created_at", direction="DESCENDING")
            .limit(max(1, limit))
            .stream()
        )
        out: list[RecentHeadline] = []
        for snap in snaps:
            data = snap.to_dict() or {}
            expires_at = data.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at < current_time:
                continue
            extra = data.get("extra")
            item_region = str((extra or {}).get("region", "")).upper() if isinstance(extra, dict) else ""
            # Region filter in Python: keep region-agnostic items (HN/arXiv, region
            # "") and items matching the user's region; drop foreign-region news.
            if target_region and item_region and item_region != target_region:
                continue
            title = str(data.get("title", "")).strip()
            if not title:
                continue
            out.append(RecentHeadline(
                title=title,
                category=str(data.get("category", "")),
                region=item_region,
                source=str(data.get("source", "")),
            ))
        return out

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warn("content_pool.list_recent_candidates failed", {"error": str(exc)})
        return []


async def list_recent_breaking_candidates(
    *,
    min_salience: float,
    limit: int = 40,
    now: datetime | None = None,
) -> list[ScoredCandidate]:
    """Most recent non-expired, push-eligible candidates whose salience clears
    ``min_salience``, newest first. Powers the breaking lane.

    This is deliberately VECTOR-INDEPENDENT: a globally huge story is usually far
    from any one user's interest vector, so ``find_nearest`` would never surface
    it. We instead scan the freshest pool items and filter by salience in Python.

    Orders by ``created_at`` descending only — a single-field order Firestore
    auto-indexes at collection scope, so this needs NO declared composite index
    (same pattern as ``list_recent_candidates``). Returns [] on any error so the
    breaking lane simply falls through to the personal lane."""
    current_time = now or datetime.now(UTC)

    def _query() -> list[ScoredCandidate]:
        db = admin_firestore()
        snaps = (
            db.collection("content_candidates")
            .order_by("created_at", direction="DESCENDING")
            .limit(max(1, limit))
            .stream()
        )
        out: list[ScoredCandidate] = []
        for snap in snaps:
            data = snap.to_dict() or {}
            expires_at = data.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at < current_time:
                continue
            if not bool(data.get("push_eligible", True)):
                continue
            salience = float(data.get("salience", 0.0) or 0.0)
            if salience < min_salience:
                continue
            title = str(data.get("title", "")).strip()
            if not title:
                continue
            vec_field = data.get("embedding")
            if isinstance(vec_field, Vector):
                embedding = list(vec_field.to_map_value()["value"])  # type: ignore[attr-defined]
            else:
                embedding = [float(x) for x in (vec_field or [])]
            extra_field = data.get("extra")
            extra = extra_field if isinstance(extra_field, dict) else {}
            out.append(ScoredCandidate(
                content_id=snap.id,
                source=str(data.get("source", "")),
                category=str(data.get("category", "")),
                sub_category=str(data.get("sub_category", "")),
                title=title,
                body=str(data.get("body", "")),
                url=str(data.get("url", "")),
                embedding=embedding,
                freshness_ts=data.get("freshness_ts") or current_time,
                cosine_similarity=0.0,
                region=str(extra.get("region", "")),
                push_eligible=True,
                salience=salience,
            ))
        return out

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warn("content_pool.list_recent_breaking_candidates failed", {"error": str(exc)})
        return []


async def list_recent_candidates_full(
    *,
    limit: int = 60,
    region: str | None = None,
    now: datetime | None = None,
) -> list[ScoredCandidate]:
    """Most recent non-expired pool items as full ScoredCandidate (body, url, category,
    salience, embedding), newest first, optionally region-filtered.

    Vector-INDEPENDENT (orders by created_at, no find_nearest), so it serves a briefing
    to a cold-start user who has no interest vector yet — the gap that left rank_session
    returning nothing for day-one users. Region filtering keeps region-agnostic items
    (HN/arXiv, region "") plus items matching the user's region and drops foreign-region
    news, so the list never empties out for a region with no edition. created_at DESC is
    auto-indexed at collection scope, so no composite index is needed.
    """
    current_time = now or datetime.now(UTC)
    target_region = (region or "").strip().upper()

    def _query() -> list[ScoredCandidate]:
        db = admin_firestore()
        snaps = (
            db.collection("content_candidates")
            .order_by("created_at", direction="DESCENDING")
            .limit(max(1, limit))
            .stream()
        )
        out: list[ScoredCandidate] = []
        for snap in snaps:
            data = snap.to_dict() or {}
            expires_at = data.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at < current_time:
                continue
            extra_field = data.get("extra")
            extra = extra_field if isinstance(extra_field, dict) else {}
            item_region = str(extra.get("region", "")).upper()
            if target_region and item_region and item_region != target_region:
                continue
            title = str(data.get("title", "")).strip()
            if not title:
                continue
            vec_field = data.get("embedding")
            if isinstance(vec_field, Vector):
                embedding = list(vec_field.to_map_value()["value"])  # type: ignore[attr-defined]
            else:
                embedding = [float(x) for x in (vec_field or [])]
            out.append(ScoredCandidate(
                content_id=snap.id,
                source=str(data.get("source", "")),
                category=str(data.get("category", "")),
                sub_category=str(data.get("sub_category", "")),
                title=title,
                body=str(data.get("body", "")),
                url=str(data.get("url", "")),
                embedding=embedding,
                freshness_ts=data.get("freshness_ts") or current_time,
                cosine_similarity=0.0,
                region=item_region,
                push_eligible=bool(data.get("push_eligible", True)),
                salience=float(data.get("salience", 0.0) or 0.0),
            ))
        return out

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warn("content_pool.list_recent_candidates_full failed", {"error": str(exc)})
        return []


async def count_fresh_candidates(*, limit: int, now: datetime | None = None) -> int:
    """Count NON-expired candidates, capped at ``limit`` (a cheap bounded probe).

    The pool keeps tombstones: a doc lives in content_candidates until a later
    sweep, but find_nearest filters out any candidate past its expires_at, so a
    pool full of expired docs serves ZERO candidates while a naive doc-count still
    looks non-empty. This counts only candidates whose expires_at is still in the
    future — the number that can actually be served — so callers (the ingest floor
    gate, the scoring-loop empty-pool alarm) reason about real, fresh content.

    Bounded: streams at most ``limit`` matching docs, so it is O(limit), not O(pool).
    ``expires_at`` is a single-field inequality → auto-indexed at collection scope,
    so no composite index is needed. Fail-open: on a probe error it returns ``limit``
    (treat the pool as healthy) so a transient Firestore blip never triggers a
    needless paid Brave fallback or a false starvation alarm."""
    current_time = now or datetime.now(UTC)

    def _probe() -> int:
        db = admin_firestore()
        snaps = (
            db.collection("content_candidates")
            .where(filter=FieldFilter("expires_at", ">", current_time))
            .limit(max(1, limit))
            .stream()
        )
        return sum(1 for _ in snaps)

    try:
        return await asyncio.to_thread(_probe)
    except Exception as exc:
        logger.warn("content_pool.count_fresh_candidates probe failed", {"error": str(exc)})
        return limit


async def has_any_candidate(*, now: datetime | None = None) -> bool:
    """True when the pool has at least one NON-expired candidate — content the
    scoring loop can actually serve. Counting fresh (not raw) docs is what lets the
    loop tell a genuinely starved pool apart from "pool has fresh content but nothing
    cleared the threshold": an all-expired pool used to read as non-empty and sent the
    diagnosis chasing a (healthy) vector index instead of the starved ingest
    (2026-06-14). Fail-open via count_fresh_candidates (returns True on a probe error)."""
    return (await count_fresh_candidates(limit=1, now=now)) > 0


async def delete_expired_candidates(
    *,
    max_deletes: int = EXPIRED_SWEEP_MAX_PER_TICK,
    now: datetime | None = None,
) -> int:
    """Hard-delete candidates whose expires_at has passed. Returns the count deleted.

    find_nearest_for_user and every other reader already SKIP expired docs, so this
    changes NO serving behavior — it only stops the expired pile from occupying the
    top-K slots find_nearest pulls back before its in-Python expiry filter. Left
    unswept, that pile crowded out every fresh item for a niche-interest user even
    though the pool had fresh content (the 2026-06-15 "pool HAS fresh content but
    vector search returned nothing for 1 user" warning). A Firestore native TTL
    policy on expires_at is the eventual backstop (best-effort, up to ~72h lag);
    this sweep gives the immediacy that the TTL lag cannot.

    Bounded by max_deletes per call so a single scheduler tick can never run an
    unbounded delete: the deploy-day backlog drains over several ticks, steady state
    removes only the handful expired since the last sweep. expires_at is a single-
    field inequality → auto-indexed at collection scope, so no composite index is
    needed. Never raises (logs and returns the count so far) so it can never fail the
    scheduler tick it piggybacks on.
    """
    current_time = now or datetime.now(UTC)

    def _sweep() -> int:
        db = admin_firestore()
        collection = db.collection("content_candidates")
        deleted = 0
        try:
            while deleted < max_deletes:
                page_limit = min(_FIRESTORE_BATCH_LIMIT, max_deletes - deleted)
                snaps = list(
                    collection
                    .where(filter=FieldFilter("expires_at", "<", current_time))
                    .limit(page_limit)
                    .stream()
                )
                if not snaps:
                    break
                batch = db.batch()
                for snap in snaps:
                    batch.delete(snap.reference)
                batch.commit()
                deleted += len(snaps)
                # Short page → no more expired docs to pull, stop early.
                if len(snaps) < page_limit:
                    break
        except Exception as exc:
            logger.error("content_pool.delete_expired_candidates failed mid-sweep", {
                "error": str(exc),
                "deleted_before_error": deleted,
            })
        return deleted

    try:
        deleted = await asyncio.to_thread(_sweep)
    except Exception as exc:
        logger.error("content_pool.delete_expired_candidates failed", {"error": str(exc)})
        return 0
    if deleted:
        logger.info("content_pool: swept expired candidates", {"deleted": deleted})
    return deleted


async def get_candidate(content_id: str) -> ScoredCandidate | None:
    """Single-doc fetch. Used when a tap event needs to reward the user vector."""
    def _fetch() -> ScoredCandidate | None:
        snap = _candidate_doc_ref(content_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        vec_field = data.get("embedding")
        if isinstance(vec_field, Vector):
            embedding = list(vec_field.to_map_value()["value"])  # type: ignore[attr-defined]
        else:
            embedding = [float(x) for x in (vec_field or [])]
        extra_field = data.get("extra")
        extra = extra_field if isinstance(extra_field, dict) else {}
        return ScoredCandidate(
            content_id=snap.id,
            source=str(data.get("source", "")),
            category=str(data.get("category", "")),
            sub_category=str(data.get("sub_category", "")),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            url=str(data.get("url", "")),
            embedding=embedding,
            freshness_ts=data.get("freshness_ts") or datetime.now(UTC),
            cosine_similarity=0.0,
            region=str(extra.get("region", "")),
            push_eligible=bool(data.get("push_eligible", True)),
            salience=float(data.get("salience", 0.0) or 0.0),
        )

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("content_pool.get_candidate failed", {
            "content_id": content_id,
            "error": str(exc),
        })
        return None
