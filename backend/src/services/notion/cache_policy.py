"""Shared in-process cache policy for the Notion service modules.

resolve.py (title embeddings) and schema.py (data source schemas) each keep a
small module-global TTL cache. The TTL and the size bound live here so the two
caches cannot drift, and so both stay bounded on a long-lived Cloud Run
instance serving many users.
"""

from __future__ import annotations

CACHE_TTL_S = 15 * 60
# Entries, not bytes: each entry is at most ~100 titles x 768 floats (resolve)
# or a property-name map (schema). Evicting the oldest entry beyond the bound
# costs one refetch on that user's next capture, never correctness.
MAX_CACHED_USERS = 256


def evict_oldest_if_full(cache: dict, *, max_entries: int = MAX_CACHED_USERS) -> None:
    """Drop oldest-by-fetch-time entries until the cache fits the bound.

    Every cache in this package stores ``key -> (fetched_at_monotonic, value)``.
    """
    while len(cache) > max_entries:
        oldest_key = min(cache, key=lambda key: cache[key][0])
        cache.pop(oldest_key, None)
