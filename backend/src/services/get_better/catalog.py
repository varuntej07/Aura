from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...lib.logger import logger
from ..firebase import admin_firestore
from .models import GetBetterCatalog

CATALOG_METADATA_COLLECTION = "get_better_catalog"
CATALOG_METADATA_DOCUMENT = "current"
STORY_COLLECTION = "get_better_stories"
CATALOG_CACHE_TTL_SECONDS = 15 * 60
PACKAGED_CATALOG_PATH = Path(__file__).parent / "content" / "stories_v1.json"


@dataclass(frozen=True)
class _CatalogCache:
    catalog: GetBetterCatalog
    refresh_after_monotonic: float


_catalog_cache: _CatalogCache | None = None
_catalog_cache_lock = asyncio.Lock()


@lru_cache(maxsize=1)
def load_packaged_catalog() -> GetBetterCatalog:
    """Load the reviewed catalog bundled into the backend image."""

    payload = json.loads(PACKAGED_CATALOG_PATH.read_text(encoding="utf-8"))
    return GetBetterCatalog.model_validate(payload)


def clear_catalog_cache_for_testing() -> None:
    global _catalog_cache
    _catalog_cache = None
    load_packaged_catalog.cache_clear()


def _read_published_catalog(cached_version: str | None) -> GetBetterCatalog | None:
    database = admin_firestore()
    metadata_ref = (
        database.collection(CATALOG_METADATA_COLLECTION).document(CATALOG_METADATA_DOCUMENT)
    )
    metadata_snapshot = metadata_ref.get()
    if not metadata_snapshot.exists:
        return None

    metadata = metadata_snapshot.to_dict() or {}
    catalog_version = str(metadata.get("catalog_version") or "").strip()
    if not catalog_version:
        raise ValueError("Published Get Better metadata has no catalog_version")
    if cached_version == catalog_version:
        return None

    raw_story_ids = metadata.get("story_ids")
    if not isinstance(raw_story_ids, list) or not raw_story_ids:
        raise ValueError("Published Get Better metadata has no story_ids")
    story_ids = [str(story_id) for story_id in raw_story_ids]
    story_refs = [
        database.collection(STORY_COLLECTION).document(story_id)
        for story_id in story_ids
    ]
    story_snapshots = list(database.get_all(story_refs))
    stories: list[dict[str, Any]] = []
    missing_story_ids: list[str] = []
    for story_id, snapshot in zip(story_ids, story_snapshots, strict=True):
        if not snapshot.exists:
            missing_story_ids.append(story_id)
            continue
        story = snapshot.to_dict() or {}
        if story.get("catalog_version") != catalog_version:
            raise ValueError(
                f"Story {story_id} does not match catalog version {catalog_version}"
            )
        story.pop("catalog_version", None)
        stories.append(story)

    if missing_story_ids:
        raise ValueError(f"Published Get Better catalog is missing stories: {missing_story_ids}")

    return GetBetterCatalog.model_validate(
        {
            "catalog_version": catalog_version,
            "published_at": metadata.get("published_at"),
            "headline": metadata.get("headline"),
            "intro": metadata.get("intro"),
            "stories": stories,
        }
    )


async def get_catalog(*, force_refresh: bool = False) -> GetBetterCatalog:
    """Return the published catalog with bounded, request-triggered Firestore reads.

    A normal request is memory-only. At most once per cache TTL per backend
    process, one metadata document is read. Story documents are read only when
    that metadata announces a new catalog version.
    """

    global _catalog_cache
    now = time.monotonic()
    if (
        not force_refresh
        and _catalog_cache is not None
        and now < _catalog_cache.refresh_after_monotonic
    ):
        return _catalog_cache.catalog

    async with _catalog_cache_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _catalog_cache is not None
            and now < _catalog_cache.refresh_after_monotonic
        ):
            return _catalog_cache.catalog

        current = _catalog_cache.catalog if _catalog_cache is not None else None
        try:
            published = await asyncio.to_thread(
                _read_published_catalog,
                current.catalog_version if current is not None else None,
            )
            catalog = published or current or load_packaged_catalog()
            if published is not None:
                logger.info(
                    "get_better: published catalog loaded",
                    {
                        "catalog_version": published.catalog_version,
                        "stories": len(published.published_stories),
                    },
                )
        except Exception as exc:
            catalog = current or load_packaged_catalog()
            logger.warn(
                "get_better: catalog refresh failed, serving last valid catalog",
                {
                    "catalog_version": catalog.catalog_version,
                    "error": str(exc),
                },
            )

        _catalog_cache = _CatalogCache(
            catalog=catalog,
            refresh_after_monotonic=time.monotonic() + CATALOG_CACHE_TTL_SECONDS,
        )
        return catalog
