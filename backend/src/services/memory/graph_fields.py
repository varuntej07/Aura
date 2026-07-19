"""Pure field and identity contract for the additive memory graph.

All document ids are derived only from normalized semantic identity. Replaying the
same extraction therefore targets the same Firestore documents without an LLM call.
"""

from __future__ import annotations

import hashlib

from .fields import atom_id, normalized_text

PARENT_COLLECTION = "UserAura"
NODE_SUBCOLLECTION = "graph_nodes"
EDGE_SUBCOLLECTION = "graph_edges"
ADJ_SUBCOLLECTION = "graph_adj"

NODE_ID = "node_id"
ENTITY = "entity"
DISPLAY = "display"
ALIASES = "aliases"
EMB768 = "emb768"
STATUS = "status"
PROJECT_ID = "project_id"
WEIGHT = "weight"
DEGREE = "degree"
DECAY_KIND = "decay_kind"
FIRST_SEEN = "first_seen"
LAST_SEEN = "last_seen"
SOURCE = "source"
DEADLINE = "deadline"
LAST_MEANINGFUL_ENGAGEMENT = "last_meaningful_engagement"
VALUE_PAYLOAD = "value_payload"
INFERRED_SENSITIVE = "inferred_sensitive"
REMINDER_CREATED_IN_SESSION = "reminder_created_in_session"
NEW_STRONG_EDGE_AT = "new_strong_edge_at"
NEW_STRONG_EDGE_EVIDENCE = "new_strong_edge_evidence"

EDGE_ID = "edge_id"
SRC = "src"
DST = "dst"
EDGE_TYPE = "type"

EVENT_MEMORY_EDGE = "memory_edge"

NEIGHBORS = "neighbors"
EDGE_IDS = "edge_ids"

MAX_NEIGHBORS = 32
EDGE_TYPES = frozenset({
    "mentions",
    "about",
    "relates_to",
    "part_of",
    "before",
    "after",
    "same_as",
})

NODE_STATUS_ACTIVE = "active"
NODE_STATUS_DORMANT = "dormant"
NODE_STATUS_COMPLETED = "completed"
NODE_STATUS_ABANDONED = "abandoned"
NODE_STATUSES = frozenset({
    NODE_STATUS_ACTIVE,
    NODE_STATUS_DORMANT,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_ABANDONED,
})


def normalized_entity_key(value: str) -> str:
    """Canonical entity key shared by deterministic graph identities."""
    return normalized_text(value)


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def entity_id(entity_type: str, name: str | None = None) -> str:
    """Build the stable id for an entity type and case-folded display name.

    ``name=None`` keeps compatibility with the pre-Phase-0 helper callers, which
    represented every value as a generic entity.
    """
    if name is None:
        name = entity_type
        entity_type = "entity"
    normalized_type = normalized_entity_key(entity_type).replace(" ", "_")
    normalized_name = normalized_entity_key(name)
    digest = _sha1(f"{normalized_type}:{normalized_name}")[:24]
    return f"entity_{normalized_type}_{digest}"


def edge_id(src: str, edge_type: str, dst: str) -> str:
    """Build the stable id for a directed, typed graph edge."""
    source = str(src).strip()
    relation = normalized_entity_key(edge_type)
    destination = str(dst).strip()
    return f"edge_{_sha1(f'{source}|{relation}|{destination}')[:32]}"


__all__ = ["atom_id", "edge_id", "entity_id"]
