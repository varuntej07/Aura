from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.handlers.get_better import (
    handle_post_get_better_catalog,
    handle_post_get_better_ideas,
)
from src.services.get_better import catalog as catalog_module
from src.services.get_better.get_better_service import build_feed
from src.services.get_better.models import GetBetterCatalog


class _Request:
    def __init__(self, body: object) -> None:
        self._body = body

    async def json(self) -> object:
        return self._body


def test_packaged_catalog_is_complete_and_relationships_are_valid() -> None:
    catalog = catalog_module.load_packaged_catalog()

    assert len(catalog.published_stories) >= 20
    assert len({story.id for story in catalog.published_stories}) == len(
        catalog.published_stories
    )
    assert sum(story.featured for story in catalog.published_stories) == 1
    assert all(story.narrative and story.what_it_means for story in catalog.published_stories)


def test_build_feed_preserves_legacy_fields_without_personalization() -> None:
    catalog = catalog_module.load_packaged_catalog()

    feed = build_feed(catalog)

    assert feed.catalog_version == catalog.catalog_version
    assert feed.banner.featured is True
    assert len(feed.ideas) == len(catalog.published_stories) - 1
    assert all(story.personalized is False for story in [feed.banner, *feed.ideas])
    assert all(len(story.steps) >= 3 for story in [feed.banner, *feed.ideas])


@pytest.mark.asyncio
async def test_catalog_cache_avoids_firestore_reads_inside_ttl() -> None:
    packaged = catalog_module.load_packaged_catalog()
    with patch.object(
        catalog_module,
        "_read_published_catalog",
        return_value=packaged,
    ) as read_published:
        first = await catalog_module.get_catalog()
        second = await catalog_module.get_catalog()

    assert first is second
    assert read_published.call_count == 1


@pytest.mark.asyncio
async def test_catalog_refresh_keeps_last_valid_catalog_on_failure() -> None:
    packaged = catalog_module.load_packaged_catalog()
    with patch.object(
        catalog_module,
        "_read_published_catalog",
        side_effect=[packaged, RuntimeError("Firestore unavailable")],
    ):
        first = await catalog_module.get_catalog(force_refresh=True)
        second = await catalog_module.get_catalog(force_refresh=True)

    assert second.catalog_version == first.catalog_version


@pytest.mark.asyncio
async def test_known_catalog_version_returns_not_modified_without_feed() -> None:
    catalog = catalog_module.load_packaged_catalog()
    with (
        patch(
            "src.handlers.get_better.resolve_user_id_from_request",
            return_value="user-1",
        ),
        patch(
            "src.services.get_better.get_better_service.get_catalog",
            return_value=catalog,
        ),
    ):
        response = await handle_post_get_better_catalog(
            _Request({"known_catalog_version": catalog.catalog_version})  # type: ignore[arg-type]
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body == {
        "not_modified": True,
        "catalog_version": catalog.catalog_version,
    }


@pytest.mark.asyncio
async def test_legacy_ideas_endpoint_keeps_direct_feed_shape() -> None:
    catalog = catalog_module.load_packaged_catalog()
    with (
        patch(
            "src.handlers.get_better.resolve_user_id_from_request",
            return_value="user-1",
        ),
        patch(
            "src.services.get_better.get_better_service.get_catalog",
            return_value=catalog,
        ),
    ):
        response = await handle_post_get_better_ideas(_Request({}))  # type: ignore[arg-type]

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["feed"]["catalog_version"] == catalog.catalog_version
    assert "banner" in body["feed"]


def test_catalog_rejects_missing_related_story() -> None:
    payload = catalog_module.load_packaged_catalog().model_dump(mode="json")
    payload["published_at"] = datetime.now(UTC).isoformat()
    payload["stories"][0]["related_story_ids"] = ["not_a_story"]

    with pytest.raises(ValueError, match="missing stories"):
        GetBetterCatalog.model_validate(payload)


def test_firestore_catalog_read_uses_exact_story_refs_not_a_query() -> None:
    packaged = catalog_module.load_packaged_catalog()
    database = MagicMock()
    metadata_snapshot = MagicMock()
    metadata_snapshot.exists = True
    metadata_snapshot.to_dict.return_value = {
        "catalog_version": packaged.catalog_version,
        "published_at": packaged.published_at,
        "headline": packaged.headline,
        "intro": packaged.intro,
        "story_ids": [story.id for story in packaged.published_stories],
    }
    metadata_ref = MagicMock()
    metadata_ref.get.return_value = metadata_snapshot
    metadata_collection = MagicMock()
    metadata_collection.document.return_value = metadata_ref
    story_collection = MagicMock()

    def collection(name: str) -> MagicMock:
        if name == catalog_module.CATALOG_METADATA_COLLECTION:
            return metadata_collection
        if name == catalog_module.STORY_COLLECTION:
            return story_collection
        raise AssertionError(name)

    database.collection.side_effect = collection
    snapshots = []
    for story in packaged.published_stories:
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {
            **story.model_dump(mode="json"),
            "catalog_version": packaged.catalog_version,
        }
        snapshots.append(snapshot)
    database.get_all.return_value = snapshots

    with patch.object(catalog_module, "admin_firestore", return_value=database):
        loaded = catalog_module._read_published_catalog(None)

    assert loaded is not None
    assert loaded.catalog_version == packaged.catalog_version
    database.get_all.assert_called_once()
    assert not story_collection.where.called
