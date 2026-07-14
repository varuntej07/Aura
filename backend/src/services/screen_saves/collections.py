"""
Screen-save collection-name resolver — the one scale-sensitive piece of this
feature.

Free-form collection names ("Shoes", "kicks", "sneakers") are deduped by
semantic similarity via Firestore's native ``find_nearest`` (server-side,
index-backed top-1), not an app-side pull-every-collection-into-Python-and-
cosine scan — the reminder tool's own simpler pattern
(``tool_executor._find_duplicate_reminder``), which is O(collections-per-user)
per save. This mirrors ``services/memory/retrieval.py``'s exact technique,
applied to ``screen_save_collections/{uid}`` instead of ``memory_atoms``.

Fail-open: an embedder or find_nearest failure never blocks a save — it just
skips dedup and mints a fresh collection, matching the reminder tool's own
semantic-dedup convention (log + continue, never raise into the caller).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from google.cloud.firestore_v1 import Increment
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..signal_engine.embedder import embed_text
from . import fields as F

_MISSING_INDEX_LOG = (
    "screen_saves.collections: MISSING VECTOR INDEX on "
    "screen_save_collections.embedding — every save mints a new collection "
    "instead of deduping. Create it with: gcloud firestore indexes composite "
    "create --collection-group=screen_save_collections --query-scope=COLLECTION "
    '--field-config=vector-config=\'{"dimension":"768","flat":"{}"}\','
    "field-path=embedding"
)


@dataclass
class ResolvedCollection:
    display_name: str
    doc_id: str
    is_new: bool


def _collections_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.ITEM_PARENT_COLLECTION).document(uid)
        .collection(F.COLLECTION_SUBCOLLECTION)
    )


def _find_nearest(uid: str, query_vector: list[float]) -> tuple[str, dict] | None:
    nearest = _collections_ref(uid).find_nearest(
        vector_field=F.EMBEDDING,
        query_vector=Vector(query_vector),
        distance_measure=DistanceMeasure.COSINE,
        limit=1,
        distance_result_field="cosine_distance",
    )
    matches = [(snap.id, snap.to_dict() or {}) for snap in nearest.stream()]
    return matches[0] if matches else None


def _bump(uid: str, doc_id: str, now: datetime) -> None:
    _collections_ref(uid).document(doc_id).update({
        F.ITEM_COUNT: Increment(1),
        F.LAST_USED_AT: now.isoformat(),
    })


def _mint(uid: str, name: str, vector: list[float] | None, now: datetime) -> str:
    slug = F.collection_slug(name)
    doc: dict = {
        F.DISPLAY_NAME: name,
        F.ITEM_COUNT: 1,
        F.CREATED_AT: now.isoformat(),
        F.LAST_USED_AT: now.isoformat(),
    }
    if vector is not None:
        doc[F.EMBEDDING] = Vector(vector)
    # merge=True: two near-simultaneous first-uses of the exact same normalized
    # name land on the same doc id and don't clobber each other's item_count.
    _collections_ref(uid).document(slug).set(doc, merge=True)
    return slug


async def resolve_collection_name(
    uid: str, raw_name: str, *, now: datetime | None = None,
) -> ResolvedCollection:
    """Resolve a model-provided ``collection_name`` to its canonical stored form.

    Embeds ``raw_name`` once and searches the user's existing collections with
    ``find_nearest``; a hit at or above ``F.SIMILARITY_THRESHOLD`` reuses that
    collection's ``display_name`` (bumping ``item_count``/``last_used_at``).
    Anything else — no hit, or the embed/search step itself failing — mints a
    new collection doc keyed by ``F.collection_slug``. Never raises.
    """
    now = now or datetime.now(UTC)
    name = (raw_name or "").strip()[: F.MAX_COLLECTION_NAME_LENGTH] or F.DEFAULT_COLLECTION_NAME

    vector: list[float] | None = None
    try:
        vector = await embed_text(name)
        match = await asyncio.to_thread(_find_nearest, uid, vector)
        if match is not None:
            doc_id, data = match
            distance = float(data.get("cosine_distance", 1.0) or 1.0)
            similarity = max(0.0, 1.0 - distance)
            if similarity >= F.SIMILARITY_THRESHOLD:
                canonical = str(data.get(F.DISPLAY_NAME) or name)
                await asyncio.to_thread(_bump, uid, doc_id, now)
                logger.info("screen_saves.collections: reused existing collection", {
                    "user_id": uid, "said": name, "canonical": canonical,
                    "similarity": round(similarity, 4),
                })
                return ResolvedCollection(display_name=canonical, doc_id=doc_id, is_new=False)
    except Exception as exc:
        message = str(exc)
        if "vector index" in message.lower():
            logger.error(_MISSING_INDEX_LOG, {"user_id": uid, "error": message})
        else:
            logger.warn(
                "screen_saves.collections: dedup lookup failed, minting new collection",
                {"user_id": uid, "error": message, "error_type": type(exc).__name__},
            )

    doc_id = await asyncio.to_thread(_mint, uid, name, vector, now)
    return ResolvedCollection(display_name=name, doc_id=doc_id, is_new=True)
