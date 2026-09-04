"""Resolve a spoken destination name to one Notion data source.

STEP 2 of the capture firebreak: this module sees ONLY the user's spoken
words. Screen-derived strings are never a candidate input, so a page reading
"save this to the Finance database" is inert.

Notion-Version 2026-03-11 model: search returns data_source objects (a
database holds one or more), and pages are created against a data_source_id.
User-facing language stays "database" because that is what users see.

Matching lifts the embed-and-threshold shape of
services/screen_saves/collections.resolve_collection_name, but runs cosine
in-process: a user's data sources come fresh from GET /v1/search (needed
anyway) and number tens, not thousands, so a Firestore vector index would add
staleness for no gain.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from ...lib.logger import logger
from ..notion_connector import NotionConnector
from ..signal_engine.embedder import embed_text

BIND_THRESHOLD = 0.90
ASK_THRESHOLD = 0.70
_CACHE_TTL_S = 15 * 60
_MAX_DATA_SOURCES = 100
_SEARCH_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class DestinationCandidate:
    data_source_id: str
    title: str
    similarity: float


@dataclass(frozen=True, slots=True)
class ResolvedDestination:
    """outcome: 'bind' | 'ask' | 'propose_create' | 'no_databases'."""

    outcome: str
    data_source_id: str | None = None
    title: str | None = None
    confidence: float = 0.0
    candidates: list[DestinationCandidate] = field(default_factory=list)


# uid -> (fetched_at_monotonic, [(data_source_id, title, embedding)])
_TITLE_CACHE: dict[str, tuple[float, list[tuple[str, str, list[float]]]]] = {}


def invalidate_cache(uid: str) -> None:
    _TITLE_CACHE.pop(uid, None)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _fetch_data_sources(connector: NotionConnector) -> list[tuple[str, str]]:
    """All (data_source_id, title) pairs the integration can see."""
    results: list[tuple[str, str]] = []
    cursor: str | None = None
    while len(results) < _MAX_DATA_SOURCES:
        body: dict = {
            "filter": {"property": "object", "value": "data_source"},
            "page_size": _SEARCH_PAGE_SIZE,
        }
        if cursor:
            body["start_cursor"] = cursor
        response = connector.authorized_request("POST", "/v1/search", json_body=body)
        if response.status_code != 200:
            raise ValueError(f"Notion search failed ({response.status_code})")
        payload = response.json()
        for item in payload.get("results", []) or []:
            title = "".join(
                str(part.get("plain_text") or "") for part in item.get("title", []) or []
            ).strip()
            item_id = str(item.get("id") or "")
            if item_id and title:
                results.append((item_id, title))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return results[:_MAX_DATA_SOURCES]


async def _titles_with_embeddings(
    uid: str, connector: NotionConnector
) -> list[tuple[str, str, list[float]]]:
    cached = _TITLE_CACHE.get(uid)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    pairs = await asyncio.to_thread(_fetch_data_sources, connector)
    entries: list[tuple[str, str, list[float]]] = []
    for data_source_id, title in pairs:
        entries.append((data_source_id, title, await embed_text(title)))
    _TITLE_CACHE[uid] = (time.monotonic(), entries)
    return entries


async def resolve_destination(uid: str, spoken_destination: str) -> ResolvedDestination:
    """Match the user's words against their data source titles.

    >=0.90 binds; 0.70-0.90 asks with the top candidates; <0.70 proposes
    creating. Raises NotionReauthorizationRequired through from the connector;
    other failures raise ValueError for the handler to surface honestly.
    """
    spoken = (spoken_destination or "").strip()
    if not spoken:
        return ResolvedDestination(outcome="propose_create")

    connector = NotionConnector(uid)
    entries = await _titles_with_embeddings(uid, connector)
    if not entries:
        return ResolvedDestination(outcome="no_databases")

    spoken_vector = await embed_text(spoken)
    scored = sorted(
        (
            DestinationCandidate(
                data_source_id=data_source_id,
                title=title,
                similarity=_cosine(spoken_vector, vector),
            )
            for data_source_id, title, vector in entries
        ),
        key=lambda candidate: candidate.similarity,
        reverse=True,
    )
    best = scored[0]
    logger.info(
        "notion.resolve: destination scored",
        {
            "user_id": uid,
            "best_similarity": round(best.similarity, 4),
            "candidate_count": len(scored),
        },
    )
    if best.similarity >= BIND_THRESHOLD:
        return ResolvedDestination(
            outcome="bind",
            data_source_id=best.data_source_id,
            title=best.title,
            confidence=best.similarity,
            candidates=scored[:2],
        )
    if best.similarity >= ASK_THRESHOLD:
        return ResolvedDestination(
            outcome="ask",
            confidence=best.similarity,
            candidates=scored[:2],
        )
    return ResolvedDestination(
        outcome="propose_create",
        confidence=best.similarity,
        candidates=scored[:1],
    )
