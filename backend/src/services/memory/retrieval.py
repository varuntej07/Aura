"""
Query-relevant memory retrieval — the reader.

On a chat turn we embed the user's message and ``find_nearest`` over their UNBOUNDED
``memory_atoms`` subcollection, then composite re-rank in Python:

    score = w_rel * cosine          # raw similarity, NOT min-maxed; magnitude IS the signal
          + w_rec * recency         # decayed_weight_by_kind(1.0, ...): gentle, never buries forever-memory
          + w_imp * importance      # reinforcement count + write-time salience
          + w_aff * affinity        # 1 if the atom's category is a live user interest (the prior)

Relevance leads (settings.MEMORY_W_*). Recency/importance/affinity are tiebreakers, so a
highly relevant OLD memory still surfaces, which is the whole point of "remember forever".

Design notes:
  * find_nearest (server-side top-N) is used because memory is unbounded — we must never
    load the full store into app memory. The composite re-rank runs over just the N
    candidates it returns.
  * Hard wall-clock budget (settings.MEMORY_RETRIEVAL_BUDGET_S): the chat stream must not
    wait on a slow embed/search. On timeout or ANY error we fail-OPEN to [] so the turn
    still streams. We fail-LOUD (WARNING/ERROR) when the store is non-empty but retrieval
    returns nothing, so "zero rows" never silently looks like "no memory".
  * GDPR/cold-start gating lives in the caller: it only calls this when the user has a
    non-empty (consented) profile, so a revoked or brand-new user's message is never embedded.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..signal_engine.embedder import embed_text
from ..user_aura_schema import decayed_weight_by_kind
from . import fields as F
from . import graph_fields as GF

SEED_K = 6
MAX_HOPS = 2
MAX_NODES = 24
FANOUT_CAP = 8
ADJACENCY_CACHE_TTL_S = 60.0
GRAPH_PROXIMITY_WEIGHT = 0.20
VOICE_RETRIEVAL_BUDGET_S = 0.35
INACTIVE_GRAPH_STATUSES = frozenset({
    GF.NODE_STATUS_COMPLETED,
    GF.NODE_STATUS_ABANDONED,
})

_adjacency_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}

# Short acknowledgements/greetings that carry no query to retrieve against. The
# v1 intent gate: skip retrieval (and the embed call) for these so smalltalk turns
# stay clean and cheap. A CATEGORY + test, not an exhaustive list.
_ACK_TOKENS = frozenset({
    "ok", "okay", "k", "kk", "thanks", "thank you", "thx", "ty", "yes", "yeah", "yep",
    "no", "nope", "sure", "got it", "gotcha", "cool", "nice", "great", "same", "lol",
    "lmao", "haha", "hmm", "hi", "hey", "hello", "yo", "sup", "np", "fine",
})


@dataclass
class RetrievedAtom:
    text: str
    atom_type: str
    score: float
    similarity: float
    node_id: str = ""
    status: str = ""
    graph_hops: int = 0


def should_retrieve_for_message(message: str) -> bool:
    """v1 intent gate. False for empty/ack/greeting turns that have nothing to recall
    against; True otherwise. Cheap and same-turn (the LLM extractor runs AFTER the
    stream, so it can only inform the NEXT turn, never gate this one)."""
    text = (message or "").strip()
    if len(text) < 3:
        return False
    return text.casefold().strip("!?.") not in _ACK_TOKENS


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _embedding_of(data: dict) -> list[float]:
    vec_field = data.get(F.EMBEDDING)
    if isinstance(vec_field, Vector):
        return list(vec_field.to_map_value()["value"])  # type: ignore[attr-defined]
    return [float(x) for x in (vec_field or [])]


def _importance_term(data: dict) -> float:
    """Blend write-time salience with reinforcement: an atom mentioned many times reads
    as important even if its stored importance was modest. Bounded to [0,1]."""
    try:
        stored = max(0.0, min(1.0, float(data.get(F.IMPORTANCE, 0.0) or 0.0)))
    except (TypeError, ValueError):
        stored = 0.0
    try:
        weight = float(data.get(F.WEIGHT, 0.0) or 0.0)
    except (TypeError, ValueError):
        weight = 0.0
    reinforced = min(weight / 5.0, 1.0)  # ~5 mentions saturates
    return max(stored, reinforced)


async def _store_is_nonempty(uid: str) -> bool:
    """Cheap existence probe, run ONLY when retrieval returned zero, to tell 'no relevant
    memory' apart from 'the store is silently broken' (the zero-rows==healthy trap)."""
    try:
        def _probe() -> bool:
            snaps = list(
                admin_firestore()
                .collection(F.ATOM_PARENT_COLLECTION).document(uid)
                .collection(F.ATOM_SUBCOLLECTION).limit(1).stream()
            )
            return len(snaps) > 0
        return await asyncio.to_thread(_probe)
    except Exception:
        return False


async def _gather_seed_result(
    uid: str, query: str, k: int, active_slugs: set[str], now: datetime,
) -> tuple[list[float], list[RetrievedAtom]]:
    query_vector = await embed_text(query)

    def _find_nearest() -> list[dict]:
        collection = (
            admin_firestore()
            .collection(F.ATOM_PARENT_COLLECTION).document(uid)
            .collection(F.ATOM_SUBCOLLECTION)
        )
        nearest = collection.find_nearest(
            vector_field=F.EMBEDDING,
            query_vector=Vector(query_vector),
            distance_measure=DistanceMeasure.COSINE,
            limit=settings.MEMORY_RETRIEVAL_CANDIDATES,
            distance_result_field="cosine_distance",
        )
        return [
            {**(snap.to_dict() or {}), "__atom_id": snap.id}
            for snap in nearest.stream()
        ]

    try:
        raw = await asyncio.to_thread(_find_nearest)
    except Exception as exc:
        message = str(exc)
        if "vector index" in message.lower():
            # A missing vector index makes EVERY retrieval return 0 — memory looks dead
            # for every user. Loud + distinct, with the exact fix, like content_pool does.
            logger.error(
                "memory.retrieval: MISSING VECTOR INDEX on memory_atoms.embedding — "
                "every user gets 0 memories. Create it with: gcloud firestore indexes "
                "composite create --collection-group=memory_atoms --query-scope=COLLECTION "
                "--field-config=vector-config='{\"dimension\":\"768\",\"flat\":\"{}\"}'"
                ",field-path=embedding",
                {"error": message},
            )
        else:
            logger.error("memory.retrieval: find_nearest failed", {"user_id": uid, "error": message})
        return query_vector, []

    if not raw:
        if await _store_is_nonempty(uid):
            logger.warn(
                "memory.retrieval: 0 candidates over a NON-EMPTY store — check the vector "
                "index / embedding writes, this should not happen",
                {"user_id": uid},
            )
        return query_vector, []

    # Score every candidate; drop anything below the raw-cosine relevance floor.
    scored: list[tuple[float, list[float], RetrievedAtom]] = []
    for data in raw:
        distance = float(data.get("cosine_distance", 1.0) or 1.0)
        similarity = max(0.0, 1.0 - distance)
        if similarity < settings.MEMORY_RELEVANCE_FLOOR:
            continue
        recency = decayed_weight_by_kind(1.0, data.get(F.LAST_SEEN), data.get(F.DECAY_KIND), now)
        importance = _importance_term(data)
        categories = data.get(F.CATEGORIES) or []
        affinity = 1.0 if active_slugs and (set(categories) & active_slugs) else 0.0
        score = (
            settings.MEMORY_W_RELEVANCE * similarity
            + settings.MEMORY_W_RECENCY * recency
            + settings.MEMORY_W_IMPORTANCE * importance
            + settings.MEMORY_W_AFFINITY * affinity
        )
        scored.append((score, _embedding_of(data), RetrievedAtom(
            text=str(data.get(F.TEXT, "")).strip(),
            atom_type=str(data.get(F.ATOM_TYPE, "")),
            score=score,
            similarity=similarity,
            node_id=str(data.get("__atom_id") or F.atom_id(
                str(data.get(F.ATOM_TYPE, "")),
                str(data.get(F.TEXT, "")).strip(),
            )),
        )))

    scored.sort(key=lambda t: t[0], reverse=True)

    # Greedy top-k with a near-duplicate guard (atom_id upsert already dedups by text;
    # this catches semantically-redundant atoms so the k slots stay diverse).
    selected: list[RetrievedAtom] = []
    selected_vecs: list[list[float]] = []
    for _, emb, atom in scored:
        if not atom.text:
            continue
        if any(_cosine(emb, prev) > settings.MEMORY_DEDUP_COSINE for prev in selected_vecs):
            continue
        selected.append(atom)
        selected_vecs.append(emb)
        if len(selected) >= k:
            break
    return query_vector, selected


async def _gather_and_rank(
    uid: str, query: str, k: int, active_slugs: set[str], now: datetime,
) -> list[RetrievedAtom]:
    _, selected = await _gather_seed_result(uid, query, k, active_slugs, now)
    return selected


def _graph_node_score(
    data: dict[str, Any],
    query_vector: list[float],
    active_slugs: set[str],
    now: datetime,
    hops: int,
) -> tuple[float, float]:
    similarity = max(0.0, _cosine(query_vector, _embedding_of_graph_node(data)))
    recency = decayed_weight_by_kind(
        1.0, data.get(GF.LAST_SEEN), data.get(GF.DECAY_KIND), now,
    )
    importance = _importance_term(data)
    identity = GF.normalized_entity_key(
        str(data.get(GF.ENTITY) or data.get(GF.DISPLAY) or "")
    )
    affinity = 1.0 if identity and identity in active_slugs else 0.0
    proximity = 1.0 / (1.0 + hops)
    score = (
        settings.MEMORY_W_RELEVANCE * similarity
        + settings.MEMORY_W_RECENCY * recency
        + settings.MEMORY_W_IMPORTANCE * importance
        + settings.MEMORY_W_AFFINITY * affinity
        + GRAPH_PROXIMITY_WEIGHT * proximity
    )
    return score, similarity


def _embedding_of_graph_node(data: dict[str, Any]) -> list[float]:
    vector = data.get(GF.EMB768)
    if isinstance(vector, Vector):
        return list(vector.to_map_value()["value"])  # type: ignore[attr-defined]
    try:
        return [float(component) for component in (vector or [])]
    except (TypeError, ValueError):
        return []


def _graph_collections(uid: str):
    parent = admin_firestore().collection(GF.PARENT_COLLECTION).document(uid)
    return (
        parent.collection(GF.NODE_SUBCOLLECTION),
        parent.collection(GF.ADJ_SUBCOLLECTION),
    )


async def _read_graph_nodes(uid: str, node_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not node_ids:
        return {}
    node_collection, _ = _graph_collections(uid)

    def _read() -> dict[str, dict[str, Any]]:
        return {
            snap.id: (snap.to_dict() or {})
            for snap in admin_firestore().get_all(
                [node_collection.document(node_id) for node_id in node_ids]
            )
            if snap.exists
        }

    return await asyncio.to_thread(_read)


async def _read_adjacency(uid: str, node_ids: list[str]) -> dict[str, tuple[str, ...]]:
    now_mono = time.monotonic()
    resolved: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for node_id in node_ids:
        cached = _adjacency_cache.get((uid, node_id))
        if cached and now_mono - cached[0] < ADJACENCY_CACHE_TTL_S:
            resolved[node_id] = cached[1]
        else:
            missing.append(node_id)
    if not missing:
        return resolved

    _, adjacency_collection = _graph_collections(uid)

    def _read() -> dict[str, tuple[str, ...]]:
        rows = {
            snap.id: tuple(
                str(value)
                for value in (snap.to_dict() or {}).get(GF.NEIGHBORS, [])
                if value
            )
            for snap in admin_firestore().get_all(
                [adjacency_collection.document(node_id) for node_id in missing]
            )
            if snap.exists
        }
        return {node_id: rows.get(node_id, ()) for node_id in missing}

    fetched = await asyncio.to_thread(_read)
    cached_at = time.monotonic()
    for node_id, neighbors in fetched.items():
        bounded = neighbors[:FANOUT_CAP]
        _adjacency_cache[(uid, node_id)] = (cached_at, bounded)
        resolved[node_id] = bounded
    return resolved


async def _traverse_graph(
    uid: str,
    seeds: list[RetrievedAtom],
    query_vector: list[float],
    active_slugs: set[str],
    now: datetime,
) -> list[RetrievedAtom]:
    """Expand active graph nodes up to two hops. Seed atoms remain directly recallable."""
    results = list(seeds)
    seed_ids = [seed.node_id for seed in seeds if seed.node_id]
    seen = set(seed_ids)
    inspected = 0

    seed_nodes = await _read_graph_nodes(uid, seed_ids[:MAX_NODES])
    inspected += len(seed_ids[:MAX_NODES])
    frontier: list[str] = []
    for seed in seeds:
        data = seed_nodes.get(seed.node_id)
        if data is None:
            continue
        seed.status = str(data.get(GF.STATUS, GF.NODE_STATUS_ACTIVE))
        if seed.status not in INACTIVE_GRAPH_STATUSES:
            frontier.append(seed.node_id)

    for hops in range(1, MAX_HOPS + 1):
        if not frontier or inspected >= MAX_NODES:
            break
        adjacency = await _read_adjacency(uid, frontier)
        candidate_ids: list[str] = []
        for node_id in frontier:
            for neighbor_id in adjacency.get(node_id, ())[:FANOUT_CAP]:
                if neighbor_id not in seen:
                    seen.add(neighbor_id)
                    candidate_ids.append(neighbor_id)
                    if inspected + len(candidate_ids) >= MAX_NODES:
                        break
            if inspected + len(candidate_ids) >= MAX_NODES:
                break
        node_data = await _read_graph_nodes(uid, candidate_ids)
        inspected += len(candidate_ids)
        next_frontier: list[str] = []
        for node_id in candidate_ids:
            data = node_data.get(node_id)
            if data is None:
                continue
            status = str(data.get(GF.STATUS, GF.NODE_STATUS_ACTIVE))
            if status in INACTIVE_GRAPH_STATUSES:
                continue
            text = str(data.get(GF.DISPLAY) or data.get(GF.ENTITY) or "").strip()
            if not text:
                continue
            score, similarity = _graph_node_score(
                data, query_vector, active_slugs, now, hops,
            )
            results.append(RetrievedAtom(
                text=text,
                atom_type=str(data.get("atom_type") or "graph_node"),
                score=score,
                similarity=similarity,
                node_id=node_id,
                status=status,
                graph_hops=hops,
            ))
            next_frontier.append(node_id)
        frontier = next_frontier

    results.sort(key=lambda atom: atom.score, reverse=True)
    return results[:MAX_NODES]


# --- circuit breaker ------------------------------------------------------
# When the embedder is failing (billing/quota outage) or persistently slow, paying the
# retrieval budget on EVERY chat turn adds dead latency to time-to-first-token for zero
# benefit. After _CB_FAILURE_THRESHOLD consecutive failures/timeouts we OPEN the circuit
# and skip retrieval entirely for _CB_COOLDOWN_S; one success closes it. State is
# process-local and best-effort (a restart resets it), which is exactly right for a
# transient-outage guard.
_CB_FAILURE_THRESHOLD = 3
_CB_COOLDOWN_S = 120.0
_cb_state = {"failures": 0, "open_until": 0.0}


def _circuit_open() -> bool:
    return time.monotonic() < _cb_state["open_until"]


def _record_outcome(ok: bool) -> None:
    if ok:
        _cb_state["failures"] = 0
        _cb_state["open_until"] = 0.0
        return
    _cb_state["failures"] += 1
    if _cb_state["failures"] >= _CB_FAILURE_THRESHOLD:
        _cb_state["open_until"] = time.monotonic() + _CB_COOLDOWN_S


def reset_circuit() -> None:
    """Test/ops hook to clear the breaker."""
    _cb_state["failures"] = 0
    _cb_state["open_until"] = 0.0
    _adjacency_cache.clear()


async def retrieve_relevant_memory(
    uid: str,
    query: str,
    *,
    k: int | None = None,
    active_slugs: list[str] | None = None,
    now: datetime | None = None,
) -> list[RetrievedAtom]:
    """Return the atoms most relevant to ``query`` for this user, best-first. Fail-open:
    returns [] on timeout, an open circuit, or any error, never raises into the chat path."""
    if not uid or not should_retrieve_for_message(query):
        return []
    if _circuit_open():
        # Embeddings recently failing -> don't pay the budget on this turn.
        logger.info("memory.retrieval: circuit open, skipping (embeddings recently failing)", {
            "user_id": uid,
        })
        return []
    k = k or settings.MEMORY_INJECT_K
    now = now or datetime.now(UTC)
    slugs = set(active_slugs or [])
    try:
        result = await asyncio.wait_for(
            _gather_and_rank(uid, query, k, slugs, now),
            timeout=settings.MEMORY_RETRIEVAL_BUDGET_S,
        )
        _record_outcome(True)
        return result
    except TimeoutError:
        _record_outcome(False)
        logger.info("memory.retrieval: budget exceeded, skipping memory this turn", {
            "user_id": uid, "budget_s": settings.MEMORY_RETRIEVAL_BUDGET_S,
        })
        return []
    except Exception as exc:
        _record_outcome(False)
        logger.warn("memory.retrieval: failed, no memory this turn", {
            "user_id": uid, "error": str(exc), "error_type": type(exc).__name__,
        })
        return []


async def retrieve_relevant_subgraph(
    uid: str,
    query: str,
    *,
    active_slugs: list[str] | None = None,
    now: datetime | None = None,
    budget_s: float | None = None,
) -> list[RetrievedAtom]:
    """Return re-ranked atom seeds plus a bounded two-hop graph expansion.

    Seed/embed failures preserve the established empty-result behavior. Once seeds
    exist, traversal is independently time-boxed so a slow graph degrades to those
    seeds instead of erasing useful memory from the turn.
    """
    if not uid or not should_retrieve_for_message(query):
        return []
    if _circuit_open():
        logger.info(
            "memory.retrieval: circuit open, skipping graph retrieval",
            {"user_id": uid},
        )
        return []

    started_at = time.monotonic()
    budget = budget_s if budget_s is not None else settings.MEMORY_RETRIEVAL_BUDGET_S
    current_time = now or datetime.now(UTC)
    slugs = set(active_slugs or [])
    try:
        query_vector, seeds = await asyncio.wait_for(
            _gather_seed_result(uid, query, SEED_K, slugs, current_time),
            timeout=max(0.001, budget),
        )
        _record_outcome(True)
    except TimeoutError:
        _record_outcome(False)
        logger.info(
            "memory.retrieval: seed budget exceeded, skipping memory this turn",
            {"user_id": uid, "budget_s": budget},
        )
        return []
    except Exception as exc:
        _record_outcome(False)
        logger.warn(
            "memory.retrieval: seed failed, no memory this turn",
            {
                "user_id": uid,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return []

    if not seeds:
        return []
    remaining = budget - (time.monotonic() - started_at)
    if remaining <= 0:
        return seeds
    try:
        return await asyncio.wait_for(
            _traverse_graph(uid, seeds, query_vector, slugs, current_time),
            timeout=remaining,
        )
    except TimeoutError:
        logger.info(
            "memory.retrieval: graph traversal timed out, using seeds",
            {"user_id": uid, "budget_s": budget, "seed_count": len(seeds)},
        )
        return seeds
    except Exception as exc:
        logger.warn(
            "memory.retrieval: graph traversal failed, using seeds",
            {
                "user_id": uid,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "seed_count": len(seeds),
            },
        )
        return seeds


def render_relevant_memory_block(
    atoms: list[RetrievedAtom],
    already_shown: set[str] | None = None,
) -> str:
    """Render retrieved atoms as the trailing, uncached <relevant_memory> system block.
    Skips any atom whose text is already in the static <interests> digest (``already_shown``,
    normalized) so the model never sees the same fact twice. Returns "" when nothing to add."""
    shown = {F.normalized_text(s) for s in (already_shown or set())}
    lines = [
        f"- {atom.text}" for atom in atoms
        if atom.text and F.normalized_text(atom.text) not in shown
    ]
    if not lines:
        return ""
    return (
        "<relevant_memory>\n"
        "Things you remember about this user that relate to what they just said. "
        "Weave in naturally only if it fits; never recite this list or say you have notes.\n"
        + "\n".join(lines)
        + "\n</relevant_memory>"
    )
