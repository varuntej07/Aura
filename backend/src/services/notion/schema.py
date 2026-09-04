"""Data source schema snapshots with a short in-process TTL cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..notion_connector import NotionConnector

_CACHE_TTL_S = 15 * 60

# data_source_id -> (fetched_at_monotonic, {property_name: property_type})
_SCHEMA_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


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
    cached = _SCHEMA_CACHE.get(data_source_id)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
        return cached[1]
    schema = await asyncio.to_thread(_fetch_schema, NotionConnector(uid), data_source_id)
    _SCHEMA_CACHE[data_source_id] = (time.monotonic(), schema)
    return schema
