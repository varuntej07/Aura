"""Best-effort Firestore writer for the additive memory graph.

This module has no read-path consumers. Every public operation catches Firestore
failures so callers can run it only after their established atom/profile write.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.vector import Vector

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..user_aura_schema import DEFAULT_INTEREST_KIND, INTEREST_KINDS
from . import graph_fields as F

STRONG_EDGE_WEIGHT_FLOOR = 0.75
STRONG_EDGE_EVIDENCE_TYPES = frozenset({"relates_to", "part_of"})


@dataclass(frozen=True)
class GraphNodeInput:
    node_id: str
    entity: str
    display: str
    aliases: tuple[str, ...] = ()
    emb768: list[float] | None = None
    weight: float = 1.0
    status: str = F.NODE_STATUS_ACTIVE
    project_id: str | None = None
    decay_kind: str = DEFAULT_INTEREST_KIND
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdgeInput:
    src: str
    dst: str
    edge_type: str = "relates_to"
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


def entity_node(
    key: str,
    *,
    label: str | None = None,
    entity_type: str = "entity",
    aliases: tuple[str, ...] = (),
    emb768: list[float] | None = None,
    project_id: str | None = None,
    decay_kind: str = DEFAULT_INTEREST_KIND,
    weight: float = 1.0,
) -> GraphNodeInput:
    clean = str(key or "").strip()
    return GraphNodeInput(
        node_id=F.entity_id(entity_type, clean),
        entity=F.normalized_entity_key(clean),
        display=(label or clean).strip(),
        aliases=aliases,
        emb768=emb768,
        project_id=project_id,
        decay_kind=decay_kind,
        weight=weight,
    )


def atom_node(
    atom_type: str,
    text: str,
    *,
    project_id: str | None = None,
    decay_kind: str = DEFAULT_INTEREST_KIND,
    weight: float = 1.0,
) -> GraphNodeInput:
    clean = str(text or "").strip()
    return GraphNodeInput(
        node_id=F.atom_id(atom_type, clean),
        entity=F.normalized_entity_key(clean),
        display=clean,
        project_id=project_id,
        decay_kind=decay_kind,
        weight=weight,
        metadata={"atom_type": atom_type},
    )


def _user_ref(uid: str):
    return admin_firestore().collection(F.PARENT_COLLECTION).document(uid)


def _collection(uid: str, name: str):
    return _user_ref(uid).collection(name)


def _safe_weight(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.0


def _safe_decay_kind(value: Any) -> str:
    return value if isinstance(value, str) and value in INTEREST_KINDS else DEFAULT_INTEREST_KIND


def _clean_aliases(values: Any) -> list[str]:
    aliases: dict[str, str] = {}
    if isinstance(values, (list, tuple, set)):
        for value in values:
            clean = str(value).strip()
            if clean:
                aliases.setdefault(F.normalized_entity_key(clean), clean)
    return list(aliases.values())


def _safe_embedding(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 768:
        return None
    try:
        return [float(component) for component in value]
    except (TypeError, ValueError):
        return None


def _canonical_edge(edge: GraphEdgeInput) -> tuple[str, str, str, str]:
    src = str(edge.src).strip()
    dst = str(edge.dst).strip()
    edge_type = F.normalized_entity_key(edge.edge_type)
    return F.edge_id(src, edge_type, dst), src, dst, edge_type


def _adj_data(data: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_neighbors = (data or {}).get(F.NEIGHBORS, [])
    if isinstance(raw_neighbors, dict):
        raw_neighbors = raw_neighbors.keys()
    neighbors = sorted({str(value) for value in raw_neighbors if value})
    return {F.NEIGHBORS: neighbors[: F.MAX_NEIGHBORS]}


def _add_adj_neighbor(data: dict[str, Any], neighbor_id: str) -> dict[str, Any]:
    updated = _adj_data(data)
    updated[F.NEIGHBORS] = sorted({*updated[F.NEIGHBORS], neighbor_id})[: F.MAX_NEIGHBORS]
    return updated


def _remove_adj_neighbor(data: dict[str, Any], neighbor_id: str) -> dict[str, Any]:
    updated = _adj_data(data)
    updated[F.NEIGHBORS] = [value for value in updated[F.NEIGHBORS] if value != neighbor_id]
    return updated


async def upsert_graph(
    uid: str,
    entities: list[GraphNodeInput],
    edges: list[GraphEdgeInput],
    *,
    source: str = "memory_graph",
    now: datetime | None = None,
) -> dict[str, int]:
    """Idempotently upsert nodes/edges and maintain adjacency. Never raises."""
    if not uid or (not entities and not edges):
        return {"nodes": 0, "edges": 0}
    now = now or datetime.now(UTC)

    clean_nodes = {
        node.node_id: node
        for node in entities
        if node.node_id
        and node.entity
        and node.display
        and node.status in F.NODE_STATUSES
    }
    clean_edges: dict[str, tuple[GraphEdgeInput, str, str, str]] = {}
    for edge in edges:
        if not edge.src or not edge.dst or edge.src == edge.dst:
            continue
        eid, src, dst, edge_type = _canonical_edge(edge)
        if edge_type in F.EDGE_TYPES:
            clean_edges[eid] = (edge, src, dst, edge_type)

    try:
        def _write() -> dict[str, int]:
            db = admin_firestore()
            node_coll = _collection(uid, F.NODE_SUBCOLLECTION)
            edge_coll = _collection(uid, F.EDGE_SUBCOLLECTION)
            adj_coll = _collection(uid, F.ADJ_SUBCOLLECTION)

            endpoint_ids = {
                endpoint
                for _, src, dst, _ in clean_edges.values()
                for endpoint in (src, dst)
            }
            requested_node_ids = set(clean_nodes) | endpoint_ids
            node_snaps = {
                snap.id: (snap.to_dict() or {})
                for snap in db.get_all([node_coll.document(nid) for nid in requested_node_ids])
                if snap.exists
            }
            known_node_ids = set(clean_nodes) | set(node_snaps)
            valid_edges = {
                eid: value
                for eid, value in clean_edges.items()
                if value[1] in known_node_ids and value[2] in known_node_ids
            }
            edge_snaps = {
                snap.id: (snap.to_dict() or {})
                for snap in db.get_all([edge_coll.document(eid) for eid in valid_edges])
                if snap.exists
            }
            new_edges = {
                eid: value for eid, value in valid_edges.items() if eid not in edge_snaps
            }
            edge_evidence_by_node: dict[str, dict[str, Any]] = {}
            if settings.NOTIF_GRAPH:
                display_by_node = {
                    nid: node.display for nid, node in clean_nodes.items()
                }
                display_by_node.update({
                    nid: str(data.get(F.DISPLAY) or data.get(F.ENTITY) or nid)
                    for nid, data in node_snaps.items()
                    if nid not in display_by_node
                })
                for edge, src, dst, edge_type in new_edges.values():
                    edge_weight = _safe_weight(edge.weight)
                    if (
                        edge_type not in STRONG_EDGE_EVIDENCE_TYPES
                        or edge_weight < STRONG_EDGE_WEIGHT_FLOOR
                    ):
                        continue
                    for node_id, connected_node_id in ((src, dst), (dst, src)):
                        evidence = {
                            "event": F.EVENT_MEMORY_EDGE,
                            "edge_type": edge_type,
                            "edge_weight": edge_weight,
                            "connected_node_id": connected_node_id,
                            "connected_display": display_by_node.get(
                                connected_node_id, connected_node_id
                            ),
                        }
                        current = edge_evidence_by_node.get(node_id)
                        if current is None or edge_weight > current["edge_weight"]:
                            edge_evidence_by_node[node_id] = evidence
            touched_adj_ids = {
                endpoint
                for _, src, dst, _ in new_edges.values()
                for endpoint in (src, dst)
            }
            adj_snaps = {
                snap.id: (snap.to_dict() or {})
                for snap in db.get_all([adj_coll.document(nid) for nid in touched_adj_ids])
                if snap.exists
            }
            adj_updates = {nid: _adj_data(adj_snaps.get(nid)) for nid in touched_adj_ids}
            for _, src, dst, _ in new_edges.values():
                adj_updates[src] = _add_adj_neighbor(adj_updates[src], dst)
                adj_updates[dst] = _add_adj_neighbor(adj_updates[dst], src)
            degree_increments = Counter(
                endpoint
                for _, src, dst, _ in new_edges.values()
                for endpoint in (src, dst)
            )

            batch = db.batch()
            for nid, node in clean_nodes.items():
                prior = node_snaps.get(nid, {})
                degree = max(0, int(prior.get(F.DEGREE, 0))) + degree_increments[nid]
                aliases = _clean_aliases([
                    *_clean_aliases(prior.get(F.ALIASES, [])),
                    *node.aliases,
                ])
                payload = {
                    F.ENTITY: node.entity,
                    F.DISPLAY: node.display,
                    F.ALIASES: aliases,
                    F.STATUS: prior.get(F.STATUS, node.status),
                    F.PROJECT_ID: (
                        node.project_id
                        if node.project_id is not None
                        else prior.get(F.PROJECT_ID)
                    ),
                    F.WEIGHT: max(
                        _safe_weight(prior.get(F.WEIGHT, 0.0)),
                        _safe_weight(node.weight),
                    ),
                    F.DEGREE: degree,
                    F.DECAY_KIND: _safe_decay_kind(node.decay_kind),
                    F.FIRST_SEEN: prior.get(F.FIRST_SEEN, now.isoformat()),
                    F.LAST_SEEN: now.isoformat(),
                    F.SOURCE: source,
                    **node.metadata,
                }
                if nid in edge_evidence_by_node:
                    payload.update({
                        F.NEW_STRONG_EDGE_AT: now.isoformat(),
                        F.NEW_STRONG_EDGE_EVIDENCE: edge_evidence_by_node[nid],
                    })
                embedding = _safe_embedding(node.emb768)
                if embedding is not None:
                    payload[F.EMB768] = Vector(embedding)
                batch.set(node_coll.document(nid), payload, merge=True)
            for eid, (edge, src, dst, edge_type) in valid_edges.items():
                prior = edge_snaps.get(eid, {})
                batch.set(edge_coll.document(eid), {
                    F.EDGE_ID: eid,
                    F.SRC: src,
                    F.DST: dst,
                    F.EDGE_TYPE: edge_type,
                    F.WEIGHT: max(
                        _safe_weight(prior.get(F.WEIGHT, 0.0)),
                        _safe_weight(edge.weight),
                    ),
                    F.FIRST_SEEN: prior.get(F.FIRST_SEEN, now.isoformat()),
                    F.LAST_SEEN: now.isoformat(),
                    F.SOURCE: source,
                    **edge.metadata,
                }, merge=True)
            for nid, data in adj_updates.items():
                batch.set(adj_coll.document(nid), data)
                if nid not in clean_nodes:
                    prior_degree = max(0, int(node_snaps.get(nid, {}).get(F.DEGREE, 0)))
                    update = {F.DEGREE: prior_degree + degree_increments[nid]}
                    if nid in edge_evidence_by_node:
                        update.update({
                            F.NEW_STRONG_EDGE_AT: now.isoformat(),
                            F.NEW_STRONG_EDGE_EVIDENCE: edge_evidence_by_node[nid],
                        })
                    batch.set(
                        node_coll.document(nid),
                        update,
                        merge=True,
                    )
            batch.commit()
            return {"nodes": len(clean_nodes), "edges": len(valid_edges)}

        result = await asyncio.to_thread(_write)
        logger.info("memory.graph_store: graph upserted", {
            "user_id": uid,
            "nodes": result["nodes"],
            "edges": result["edges"],
            "source": source,
        })
        return result
    except Exception as exc:
        logger.warn("memory.graph_store: upsert failed", {
            "user_id": uid,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return {"nodes": 0, "edges": 0}


async def set_node_status(uid: str, node_id: str, status: str) -> bool:
    """Transition status without deleting the node, edges, or history."""
    if not uid or not node_id or status not in F.NODE_STATUSES:
        return False
    try:
        await asyncio.to_thread(
            _collection(uid, F.NODE_SUBCOLLECTION).document(node_id).update,
            {F.STATUS: status},
        )
        return True
    except Exception as exc:
        logger.warn("memory.graph_store: status update failed", {
            "user_id": uid,
            "node_id": node_id,
            "error": str(exc),
        })
        return False


async def delete_node(uid: str, node_id: str) -> bool:
    """Hard-delete a node and incident graph data for explicit erasure only."""
    if not uid or not node_id:
        return False
    try:
        def _delete() -> None:
            db = admin_firestore()
            node_coll = _collection(uid, F.NODE_SUBCOLLECTION)
            edge_coll = _collection(uid, F.EDGE_SUBCOLLECTION)
            adj_coll = _collection(uid, F.ADJ_SUBCOLLECTION)
            incident: dict[str, dict[str, Any]] = {}
            for field_name in (F.SRC, F.DST):
                query = edge_coll.where(filter=FieldFilter(field_name, "==", node_id))
                for snap in query.stream():
                    incident[snap.id] = snap.to_dict() or {}

            neighbor_incident_counts = Counter(
                str(edge.get(F.DST) if edge.get(F.SRC) == node_id else edge.get(F.SRC))
                for edge in incident.values()
                if edge.get(F.SRC) and edge.get(F.DST)
            )
            neighbor_ids = set(neighbor_incident_counts)
            neighbor_adj = {
                snap.id: (snap.to_dict() or {})
                for snap in db.get_all([adj_coll.document(nid) for nid in neighbor_ids])
                if snap.exists
            }
            neighbor_nodes = {
                snap.id: (snap.to_dict() or {})
                for snap in db.get_all([node_coll.document(nid) for nid in neighbor_ids])
                if snap.exists
            }

            batch = db.batch()
            for eid in incident:
                batch.delete(edge_coll.document(eid))
            batch.delete(adj_coll.document(node_id))
            batch.delete(node_coll.document(node_id))
            for neighbor, removed_edges in neighbor_incident_counts.items():
                prior_degree = max(0, int(neighbor_nodes.get(neighbor, {}).get(F.DEGREE, 0)))
                degree = max(0, prior_degree - removed_edges)
                data = _remove_adj_neighbor(neighbor_adj.get(neighbor, {}), node_id)
                if degree == 0:
                    batch.delete(adj_coll.document(neighbor))
                    batch.delete(node_coll.document(neighbor))
                else:
                    batch.set(adj_coll.document(neighbor), data)
                    batch.set(
                        node_coll.document(neighbor),
                        {F.DEGREE: degree},
                        merge=True,
                    )
            batch.commit()

        await asyncio.to_thread(_delete)
        return True
    except Exception as exc:
        logger.warn("memory.graph_store: node delete failed", {
            "user_id": uid,
            "node_id": node_id,
            "error": str(exc),
        })
        return False


def _delete_collection(collection) -> int:
    removed = 0
    batch = admin_firestore().batch()
    for snap in collection.stream():
        batch.delete(snap.reference)
        removed += 1
        if removed % 400 == 0:
            batch.commit()
            batch = admin_firestore().batch()
    if removed % 400:
        batch.commit()
    return removed


async def wipe_graph(uid: str) -> int:
    """Delete graph nodes, edges, and adjacency docs. Never raises."""
    if not uid:
        return 0
    try:
        def _wipe() -> int:
            return sum(
                _delete_collection(_collection(uid, name))
                for name in (
                    F.EDGE_SUBCOLLECTION,
                    F.ADJ_SUBCOLLECTION,
                    F.NODE_SUBCOLLECTION,
                )
            )

        removed = await asyncio.to_thread(_wipe)
        logger.info("memory.graph_store: graph wiped", {
            "user_id": uid,
            "removed": removed,
        })
        return removed
    except Exception as exc:
        logger.warn("memory.graph_store: graph wipe failed", {
            "user_id": uid,
            "error": str(exc),
        })
        return 0


