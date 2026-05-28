"""
content_candidates collection — the shared pool of notifiable items.

Fetchers (HN, arXiv, jobs, sports, RSS) add items here after each fetch.
The scoring loop reads from here via find_nearest using Firestore native
vector search against the user's user_vector.

"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ...lib.logger import logger
from ..firebase import admin_firestore
from .embedder import embed_texts

# Default candidate lifetime. News, HN, sports go stale fast. Jobs survive
# the standard week. Per-source overrides are passed in via add_candidates.
DEFAULT_CONTENT_TTL_HOURS = 36

# Hard ceiling on how many candidates find_nearest pulls back per user per scoring tick. 
# 50 is generous for diversity post-scoring without bloating the per-tick wire payload.
MAX_NEAREST_CANDIDATES = 50


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


def _build_content_id(source: str, url: str, title: str) -> str:
    """Stable ID derived from source + url so the same item is not embedded twice."""
    key = (url.strip() or title.strip()).lower()
    digest = hashlib.sha256(f"{source}|{key}".encode()).hexdigest()[:24]
    return f"{source}_{digest}"


def _candidate_doc_ref(content_id: str):
    return admin_firestore().collection("content_candidates").document(content_id)


def _content_text_for_embedding(title: str, body: str) -> str:
    """The string handed to text-embedding-004 for a candidate."""
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
            doc = {
                "source": item.source,
                "category": item.category,
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
            ))
        return results

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        logger.error("content_pool.find_nearest_for_user failed", {"error": str(exc)})
        return []


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
        )

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("content_pool.get_candidate failed", {
            "content_id": content_id,
            "error": str(exc),
        })
        return None
