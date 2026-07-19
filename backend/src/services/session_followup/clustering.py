"""Deterministic topic clustering that never depends on graph documents landing."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from ..memory.graph_fields import normalized_entity_key

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)
_STOP = frozenset({
    "about", "after", "again", "also", "been", "being", "could", "from",
    "have", "into", "just", "like", "really", "that", "their", "there",
    "these", "they", "this", "want", "what", "when", "where", "which",
    "with", "would", "your",
})


def _tokens(turn: dict[str, Any]) -> set[str]:
    lexical = turn.get("lexical_terms")
    if isinstance(lexical, list):
        return {
            normalized_entity_key(str(value))
            for value in lexical
            if str(value).strip()
        }
    text = str(turn.get("text") or turn.get("transcript") or "")
    return {
        token.casefold()
        for token in _TOKEN.findall(text)
        if token.casefold() not in _STOP
    }


def _entities(turn: dict[str, Any]) -> set[str]:
    values = turn.get("entity_keys")
    if not isinstance(values, list):
        return set()
    return {
        normalized_entity_key(str(value))
        for value in values
        if str(value).strip()
    }


def _embedding(turn: dict[str, Any]) -> list[float] | None:
    value = turn.get("embedding")
    if not isinstance(value, (list, tuple)):
        value = turn.get("embedding_ref")
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return [float(component) for component in value]
    except (TypeError, ValueError):
        return None


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _lexical_match(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    overlap = left & right
    union = left | right
    return len(overlap) >= 2 or len(overlap) / max(1, len(union)) >= 0.28


def _stable_topic_id(entity_keys: set[str], lexical_terms: set[str]) -> str:
    identity = sorted(entity_keys) or sorted(lexical_terms)[:6]
    raw = "|".join(identity) or "uncategorized"
    return f"topic_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _project_id(entity_keys: set[str], lexical_terms: set[str]) -> str:
    identity = sorted(entity_keys)[:2] or sorted(lexical_terms)[:2] or ["general"]
    raw = "|".join(identity)
    return f"project_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def cluster_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster user turns by provenance, then embeddings, then lexical overlap."""
    user_turns = [turn for turn in turns if str(turn.get("role") or "user") == "user"]
    if not user_turns:
        return []
    parents = list(range(len(user_turns)))

    def _find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def _union(left: int, right: int) -> None:
        left_root = _find(left)
        right_root = _find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    entities = [_entities(turn) for turn in user_turns]
    embeddings = [_embedding(turn) for turn in user_turns]
    lexical = [_tokens(turn) for turn in user_turns]
    for left in range(len(user_turns)):
        for right in range(left + 1, len(user_turns)):
            if entities[left] and entities[right] and entities[left] & entities[right]:
                _union(left, right)
                continue
            if (
                embeddings[left] is not None
                and embeddings[right] is not None
                and _cosine(embeddings[left], embeddings[right]) >= 0.78
            ):
                _union(left, right)
                continue
            if _lexical_match(lexical[left], lexical[right]):
                _union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(user_turns)):
        groups.setdefault(_find(index), []).append(index)

    topics: list[dict[str, Any]] = []
    for indices in groups.values():
        topic_entities = set().union(*(entities[index] for index in indices))
        counts = Counter(
            token for index in indices for token in lexical[index]
        )
        topic_lexical = set(counts)
        ordered_terms = [
            token for token, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        topic_turns = [user_turns[index] for index in indices]
        evidence = next(
            (
                str(turn.get("text") or turn.get("transcript") or "").strip()
                for turn in reversed(topic_turns)
                if str(turn.get("text") or turn.get("transcript") or "").strip()
            ),
            " ".join(ordered_terms[:8]),
        )
        topics.append({
            "topic_id": _stable_topic_id(topic_entities, topic_lexical),
            "project_id": _project_id(topic_entities, topic_lexical),
            "entity_keys": sorted(topic_entities),
            "lexical_terms": ordered_terms[:12],
            "turns": topic_turns,
            "user_turn_count": len(topic_turns),
            "summary": evidence[:280],
        })
    topics.sort(key=lambda topic: (topic["topic_id"], topic["summary"]))
    return topics
