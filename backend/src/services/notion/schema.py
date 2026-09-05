"""Data source schema snapshots with a short in-process TTL cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..notion_connector import NotionConnector
from .cache_policy import CACHE_TTL_S as _CACHE_TTL_S
from .cache_policy import evict_oldest_if_full

# (uid, data_source_id) -> (fetched_at_monotonic, {property_name: property_type})
# Keyed by uid as well as id: entries are fetched with one user's credentials
# and must never be served across users, even for a shared workspace where the
# ids coincide. Bounded via cache_policy so it cannot grow with instance age.
_SCHEMA_CACHE: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


def invalidate_schema_cache(uid: str) -> None:
    """Drop every cached schema for one user (e.g. after a database create)."""
    for key in [key for key in _SCHEMA_CACHE if key[0] == uid]:
        _SCHEMA_CACHE.pop(key, None)


def _fetch_schema(connector: NotionConnector, data_source_id: str) -> dict[str, str]:
    response = connector.authorized_request("GET", f"/v1/data_sources/{data_source_id}")
    if response.status_code != 200:
        raise ValueError(f"Notion data source fetch failed ({response.status_code})")
    properties: dict[str, Any] = response.json().get("properties") or {}
    return {
        str(name): str(spec.get("type") or "")
        for name, spec in properties.items()
        if isinstance(spec, dict)
    }


async def data_source_schema(uid: str, data_source_id: str) -> dict[str, str]:
    """Property name -> property type for one data source."""
    cache_key = (uid, data_source_id)
    cached = _SCHEMA_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
        return cached[1]
    schema = await asyncio.to_thread(_fetch_schema, NotionConnector(uid), data_source_id)
    _SCHEMA_CACHE[cache_key] = (time.monotonic(), schema)
    evict_oldest_if_full(_SCHEMA_CACHE)
    return schema
