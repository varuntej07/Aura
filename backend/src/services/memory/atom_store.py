"""
Memory-atom writer — the UNBOUNDED long-term store.

``upsert_atoms`` folds a batch of memory atoms into ``UserAura/{uid}/memory_atoms``.
It is idempotent (keyed by a stable ``atom_id`` derived from the normalized text), so
the same memory mentioned across many messages/sessions updates ONE doc instead of
fragmenting. Embedding is the expensive part, so we re-embed ONLY when the text
actually changed (``norm_text_hash`` differs); an unchanged atom just gets its
weight/last_seen bumped.

There is deliberately NO cap and NO eviction here. The capped UserAura doc is the lean
prompt digest; this subcollection is "remember forever", recalled by semantic
similarity in ``retrieval.py``. Atoms are removed only by explicit user delete
(``delete_atom``) or consent wipe (``wipe_atoms``).

All Firestore work runs in ``asyncio.to_thread`` (the SDK is sync). Callers fire this
fire-and-forget off the chat response path and swallow failures, exactly like the
extractor it hooks into.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1.vector import Vector

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..signal_engine.embedder import embed_texts
from ..user_aura_schema import (
    DEFAULT_INTEREST_KIND,
    INTEREST_KINDS,
    decayed_weight_by_kind,
)
from . import fields as F


@dataclass
class AtomInput:
    """One memory unit to persist. Built by the extractor/reflection wiring from
    plain data (never the pydantic models) so this module imports neither, keeping
    the dependency direction one-way and circular-import-free."""

    text: str
    atom_type: str
    decay_kind: str = DEFAULT_INTEREST_KIND
    importance: float = 0.5
    categories: list[str] = field(default_factory=list)
    confidence: float = 0.5
    confirmation_status: str = F.CONFIRMATION_UNKNOWN
    source_conversation_id: str = ""
    source_message_id: str = ""
    evidence_text: str = ""


def _atoms_collection(uid: str):
    return (
        admin_firestore()
        .collection(F.ATOM_PARENT_COLLECTION)
        .document(uid)
        .collection(F.ATOM_SUBCOLLECTION)
    )


def _clamp_importance(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _safe_kind(kind: Any) -> str:
    return kind if isinstance(kind, str) and kind in INTEREST_KINDS else DEFAULT_INTEREST_KIND


def _stronger_confirmation(prior: Any, incoming: str) -> str:
    rank = {
        F.CONFIRMATION_UNKNOWN: 0,
        F.CONFIRMATION_REFLECTED: 1,
        F.CONFIRMATION_EXPLICIT_USER: 2,
        F.CONFIRMATION_TOOL_VERIFIED: 3,
    }
    prior_value = str(prior or F.CONFIRMATION_UNKNOWN)
    return incoming if rank.get(incoming, 0) >= rank.get(prior_value, 0) else prior_value


def _provenance_records(
    prior: dict[str, Any], atom: AtomInput, source: str, now: datetime
) -> list[dict[str, Any]]:
    existing = prior.get(F.PROVENANCE)
    records = (
        [dict(item) for item in existing if isinstance(item, dict)]
        if isinstance(existing, list)
        else []
    )
    evidence = (atom.evidence_text or atom.text or "").strip()
    record = {
        F.PROVENANCE_SOURCE_TYPE: source,
        F.PROVENANCE_CONVERSATION_ID: atom.source_conversation_id[:128],
        F.PROVENANCE_MESSAGE_ID: atom.source_message_id[:128],
        F.PROVENANCE_EVIDENCE_HASH: hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        F.PROVENANCE_OBSERVED_AT: now.isoformat(),
        F.PROVENANCE_CONFIDENCE: _clamp_importance(atom.confidence),
        F.PROVENANCE_CONFIRMATION_STATUS: atom.confirmation_status,
    }
    identity = (
        record[F.PROVENANCE_SOURCE_TYPE],
        record[F.PROVENANCE_CONVERSATION_ID],
        record[F.PROVENANCE_MESSAGE_ID],
        record[F.PROVENANCE_EVIDENCE_HASH],
    )
    records = [
        item
        for item in records
        if (
            item.get(F.PROVENANCE_SOURCE_TYPE),
            item.get(F.PROVENANCE_CONVERSATION_ID),
            item.get(F.PROVENANCE_MESSAGE_ID),
            item.get(F.PROVENANCE_EVIDENCE_HASH),
        )
        != identity
    ]
    return [*records, record][-F.MAX_PROVENANCE_RECORDS :]


async def upsert_atoms(
    uid: str,
    atoms: list[AtomInput],
    *,
    source: str,
    now: datetime | None = None,
) -> int:
    """Insert/refresh a batch of atoms. Returns the number of atoms written. Never
    raises — logs and returns 0 on failure (this rides the fire-and-forget path)."""
    if not uid or not atoms:
        return 0
    now = now or datetime.now(UTC)

    # Collapse duplicates within the batch (same memory named twice in one message);
    # last write wins. Drop empty / over-long text here so the rest is clean.
    by_id: dict[str, tuple[AtomInput, str]] = {}
    for atom in atoms:
        text = (atom.text or "").strip()[: F.MAX_ATOM_TEXT_LENGTH]
        if not text:
            continue
        by_id[F.atom_id(atom.atom_type, text)] = (atom, text)
    if not by_id:
        return 0

    started_at = time.monotonic()
    read_ms = 0
    embed_ms = 0
    write_ms = 0
    try:
        collection = _atoms_collection(uid)

        def _read_existing() -> dict[str, dict[str, Any]]:
            refs = [collection.document(aid) for aid in by_id]
            return {
                snap.id: (snap.to_dict() or {})
                for snap in admin_firestore().get_all(refs)
                if snap.exists
            }

        stage_started = time.monotonic()
        existing = await asyncio.to_thread(_read_existing)
        read_ms = round((time.monotonic() - stage_started) * 1000)

        # Re-embed only atoms whose normalized text changed (or that are new).
        need_embed: list[tuple[str, str]] = [
            (aid, text)
            for aid, (_, text) in by_id.items()
            if existing.get(aid, {}).get(F.NORM_TEXT_HASH) != F.norm_text_hash(text)
        ]
        vectors: dict[str, list[float]] = {}
        if need_embed:
            stage_started = time.monotonic()
            embedded = await embed_texts([text for _, text in need_embed])
            embed_ms = round((time.monotonic() - stage_started) * 1000)
            vectors = {aid: vec for (aid, _), vec in zip(need_embed, embedded)}

        def _write() -> None:
            batch = admin_firestore().batch()
            for aid, (atom, text) in by_id.items():
                ref = collection.document(aid)
                prior = existing.get(aid, {})
                kind = _safe_kind(atom.decay_kind)
                confidence = _clamp_importance(atom.confidence)
                provenance = _provenance_records(prior, atom, source, now)
                confirmation_status = _stronger_confirmation(
                    prior.get(F.CONFIRMATION_STATUS), atom.confirmation_status
                )
                new_weight = decayed_weight_by_kind(
                    prior.get(F.WEIGHT, 0.0), prior.get(F.LAST_SEEN), kind, now
                ) + 1.0
                if aid in vectors:
                    # New atom, or text changed -> full (re)write incl. fresh embedding.
                    batch.set(ref, {
                        F.TEXT: text,
                        F.NORM_TEXT_HASH: F.norm_text_hash(text),
                        F.EMBEDDING: Vector(vectors[aid]),
                        F.ATOM_TYPE: atom.atom_type,
                        F.DECAY_KIND: kind,
                        F.WEIGHT: new_weight,
                        F.IMPORTANCE: _clamp_importance(atom.importance),
                        F.CATEGORIES: [c for c in atom.categories if isinstance(c, str)],
                        F.FIRST_SEEN: prior.get(F.FIRST_SEEN, now.isoformat()),
                        F.LAST_SEEN: now.isoformat(),
                        F.SOURCE: source,
                        F.CONFIDENCE: confidence,
                        F.CONFIRMATION_STATUS: confirmation_status,
                        F.PROVENANCE: provenance,
                    })
                else:
                    # Unchanged text -> cheap touch: bump weight/last_seen, keep the strongest
                    # importance seen. No embed, no embedding rewrite.
                    batch.update(ref, {
                        F.WEIGHT: new_weight,
                        F.LAST_SEEN: now.isoformat(),
                        F.IMPORTANCE: max(
                            _clamp_importance(prior.get(F.IMPORTANCE, 0.0)),
                            _clamp_importance(atom.importance),
                        ),
                        F.CONFIDENCE: max(
                            _clamp_importance(prior.get(F.CONFIDENCE, 0.0)),
                            confidence,
                        ),
                        F.CONFIRMATION_STATUS: confirmation_status,
                        F.PROVENANCE: provenance,
                    })
            batch.commit()

        stage_started = time.monotonic()
        await asyncio.to_thread(_write)
        write_ms = round((time.monotonic() - stage_started) * 1000)
        logger.info("memory.atom_store: upserted atoms", {
            "user_id": uid,
            "total": len(by_id),
            "embedded": len(need_embed),
            "source": source,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "read_ms": read_ms,
            "embed_ms": embed_ms,
            "write_ms": write_ms,
        })
        return len(by_id)
    except Exception as exc:
        logger.warn("memory.atom_store: upsert failed", {
            "user_id": uid,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "source": source,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "read_ms": read_ms,
            "embed_ms": embed_ms,
            "write_ms": write_ms,
        })
        return 0


async def delete_atom(uid: str, atom_id: str) -> bool:
    """Remove one atom (the A2 'forget this' affordance). Never raises."""
    try:
        await asyncio.to_thread(_atoms_collection(uid).document(atom_id).delete)
    except Exception as exc:
        logger.warn("memory.atom_store: delete failed", {"user_id": uid, "error": str(exc)})
        return False

    # Explicit forget cascades only after the established atom delete succeeds.
    # Detach the additive graph write so its latency/failure cannot change the
    # endpoint's established result.
    asyncio.create_task(_delete_graph_node_fail_open(uid, atom_id))
    return True


async def _delete_graph_node_fail_open(uid: str, node_id: str) -> None:
    from .graph_store import delete_node

    try:
        await delete_node(uid, node_id)
    except Exception as exc:
        logger.warn("memory.atom_store: graph delete failed open", {
            "user_id": uid,
            "node_id": node_id,
            "error": str(exc),
        })


async def list_atoms(uid: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Recent atoms for the 'what Buddy remembers' screen: most-recent first, capped.
    Returns lightweight dicts (no embedding). Never raises -- returns [] on failure.

    Orders by last_seen, which keeps its automatic single-field index: the vector
    field-override only disables auto-indexing on ``embedding``, not the whole collection."""
    if not uid:
        return []
    try:
        def _read() -> list[dict[str, Any]]:
            query = (
                _atoms_collection(uid)
                .order_by(F.LAST_SEEN, direction="DESCENDING")
                .limit(limit)
            )
            rows: list[dict[str, Any]] = []
            for snap in query.stream():
                data = snap.to_dict() or {}
                rows.append({
                    "id": snap.id,
                    "text": str(data.get(F.TEXT, "")),
                    "atom_type": str(data.get(F.ATOM_TYPE, "")),
                    "categories": list(data.get(F.CATEGORIES, []) or []),
                    "last_seen": data.get(F.LAST_SEEN),
                })
            return rows
        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("memory.atom_store: list failed", {"user_id": uid, "error": str(exc)})
        return []


async def wipe_atoms(uid: str) -> int:
    """Delete every atom for a user (consent revoke / account delete). Returns the
    count removed. Never raises."""
    try:
        def _wipe() -> int:
            collection = _atoms_collection(uid)
            removed = 0
            batch = admin_firestore().batch()
            for snap in collection.stream():
                batch.delete(snap.reference)
                removed += 1
                if removed % 400 == 0:  # Firestore batch hard cap is 500
                    batch.commit()
                    batch = admin_firestore().batch()
            if removed % 400 != 0:
                batch.commit()
            return removed

        count = await asyncio.to_thread(_wipe)
        logger.info("memory.atom_store: wiped atoms", {"user_id": uid, "removed": count})
        return count
    except Exception as exc:
        logger.warn("memory.atom_store: wipe failed", {"user_id": uid, "error": str(exc)})
        return 0
