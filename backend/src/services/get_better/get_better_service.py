from __future__ import annotations

from .catalog import get_catalog
from .models import GetBetterCatalog, GetBetterFeed


def build_feed(catalog: GetBetterCatalog) -> GetBetterFeed:
    """Map the canonical catalog into the backward-compatible feed contract."""

    published = catalog.published_stories
    banner = next(story for story in published if story.featured)
    ideas = [story for story in published if story.id != banner.id]
    return GetBetterFeed(
        headline=catalog.headline,
        intro=catalog.intro,
        banner=banner,
        ideas=ideas,
        next_cursor=0,
        generated_at=catalog.published_at.isoformat(),
        catalog_version=catalog.catalog_version,
    )


async def get_feed() -> GetBetterFeed:
    """Return reviewed content only. This path intentionally has no LLM calls."""

    return build_feed(await get_catalog())
