"""
Memory-atom document contract — the single source of truth for the field names
on ``UserAura/{uid}/memory_atoms/{atom_id}``, plus the small pure helpers
(``atom_id``, ``normalized_text``, ``norm_text_hash``) that the writer and reader
both depend on.

Per the data-layer discipline in CLAUDE.md, the writer (``atom_store``) and every
reader (``retrieval``, tests) import the field names from HERE, never inline a
string literal, so a rename can never silently split the contract.

Why a separate subcollection (not the UserAura doc):

  UserAura/{uid} is hard-capped (MAX_STORYLINES, MAX_SUBJECTS_PER_CATEGORY, ...)
  to stay under Firestore's 1 MiB doc limit and to keep the always-injected prompt
  digest lean. Those caps are a STORAGE/PROMPT constraint, not "forget the user".
  The atom store is the UNBOUNDED long-term memory: every meaningful unit becomes
  one atom doc, never hard-evicted, recalled purely by semantic similarity. So a
  storyline leaving the capped digest is non-destructive — its atom lives forever
  here and still surfaces when a query is close to it.

Stored shape:

    UserAura/{uid}/memory_atoms/{atom_id} = {
        "text":          str,            # the human-readable memory ("dislikes early showers")
        "norm_text_hash": str,           # hash of normalized text — skip re-embed when unchanged
        "embedding":     Vector(768),    # gemini-embedding-001, queried by find_nearest
        "atom_type":     str,            # fact | storyline | interest_subject | trait
        "decay_kind":    str,            # durable | event_driven | goal_instrumental (recency half-life)
        "weight":        float,          # reinforcement count (decay-then-increment), drives importance
        "importance":    float,          # 0..1 salience hint set at write (e.g. reflection confidence)
        "categories":    [str],          # taxonomy slugs, for the interest-affinity prior
        "first_seen":    iso8601,
        "last_seen":     iso8601,
        "source":        str,            # "extractor" | "reflection" — provenance, for debugging
    }
"""

from __future__ import annotations

import hashlib

# --- Firestore location ---------------------------------------------------
# Parent collection is UserAura (co-located with the profile the extractor and
# reflection already write); the atoms hang off it as a per-user subcollection.
ATOM_PARENT_COLLECTION = "UserAura"
ATOM_SUBCOLLECTION = "memory_atoms"

# --- Field names (the contract) ------------------------------------------
TEXT = "text"
NORM_TEXT_HASH = "norm_text_hash"
EMBEDDING = "embedding"
ATOM_TYPE = "atom_type"
DECAY_KIND = "decay_kind"
WEIGHT = "weight"
IMPORTANCE = "importance"
CATEGORIES = "categories"
FIRST_SEEN = "first_seen"
LAST_SEEN = "last_seen"
SOURCE = "source"

# --- Atom types -----------------------------------------------------------
ATOM_TYPE_FACT = "fact"
ATOM_TYPE_STORYLINE = "storyline"
ATOM_TYPE_INTEREST_SUBJECT = "interest_subject"
ATOM_TYPE_TRAIT = "trait"

# Longest atom text we will store/embed (defence against a runaway model output).
MAX_ATOM_TEXT_LENGTH = 400


def normalized_text(text: str) -> str:
    """Casefold + whitespace-collapse so trivial edits don't fork the atom."""
    return " ".join((text or "").split()).casefold()


def norm_text_hash(text: str) -> str:
    """Stable hash of the normalized text. Used to skip re-embedding an atom whose
    text did not meaningfully change (only its weight/last_seen did)."""
    return hashlib.sha1(normalized_text(text).encode("utf-8")).hexdigest()


def atom_id(atom_type: str, text: str) -> str:
    """Deterministic, collision-safe doc id from (type, normalized text) so the
    SAME memory upserts in place (idempotent) instead of fragmenting into
    near-duplicates across messages/sessions. Firestore doc ids cannot contain
    '/', so this is a hash, never the raw text."""
    digest = hashlib.sha1(f"{atom_type}:{normalized_text(text)}".encode("utf-8")).hexdigest()
    return f"{atom_type}_{digest[:24]}"
