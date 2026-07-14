"""
Screen-save document contract — the single source of truth for field names on
``UserAura/{uid}/screen_saves/{item_id}`` and
``UserAura/{uid}/screen_save_collections/{doc_id}``, plus the small pure
helpers (``collection_slug``, ``new_item_id``, ``normalized_collection_name``)
the writer and every reader both depend on.

Per the data-layer discipline in CLAUDE.md, ``store.py``/``collections.py`` and
every reader import field names from HERE, never inline a string literal, so a
rename can never silently split the writer/reader contract.

Stored shape:

    UserAura/{uid}/screen_saves/{item_id} = {
        "title":            str,            # short heading, e.g. "Nike Air Max 270"
        "description":      str,            # "" if none
        "collection_name":  str,            # canonical display name, e.g. "Shoes"
        "note":             str,            # "" if none — the user's own words
        "source_url":       str | None,     # best-effort, only if visible on screen
        "image_path":       str | None,     # GCS object path, None for a text-only save
        "created_at":       iso8601,
        "session_id":       str,
        "source_frame_id":  str | None,
    }

    UserAura/{uid}/screen_save_collections/{doc_id} = {
        "display_name":  str,          # canonical spelling, first one wins
        "embedding":     Vector(768) | None,  # gemini-embedding-001, queried by find_nearest
        "item_count":    int,
        "created_at":    iso8601,
        "last_used_at":  iso8601,
    }

``screen_save_collections`` is a backend-internal dedup index, never exposed
directly to a client — readers only ever see each item's own ``collection_name``.
"""

from __future__ import annotations

import hashlib
import uuid

# --- Firestore locations ---------------------------------------------------
# Parent collection is UserAura (co-located with memory_atoms), so both fall
# through to the same default-deny Firestore rule with no rule change needed.
ITEM_PARENT_COLLECTION = "UserAura"
ITEM_SUBCOLLECTION = "screen_saves"
COLLECTION_SUBCOLLECTION = "screen_save_collections"

# --- screen_saves item fields -----------------------------------------------
TITLE = "title"
DESCRIPTION = "description"
COLLECTION_NAME = "collection_name"
NOTE = "note"
SOURCE_URL = "source_url"
IMAGE_PATH = "image_path"
CREATED_AT = "created_at"
SESSION_ID = "session_id"
SOURCE_FRAME_ID = "source_frame_id"

# --- screen_save_collections fields -----------------------------------------
DISPLAY_NAME = "display_name"
EMBEDDING = "embedding"
ITEM_COUNT = "item_count"
LAST_USED_AT = "last_used_at"
# CREATED_AT reused from above.

# --- limits ------------------------------------------------------------------
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 1000
MAX_NOTE_LENGTH = 500
MAX_COLLECTION_NAME_LENGTH = 80
DEFAULT_COLLECTION_NAME = "Uncategorized"

# cosine similarity at/above which a newly-said collection name ("kicks") is
# treated as the SAME collection as an existing one ("Shoes") rather than
# minting a near-duplicate.
SIMILARITY_THRESHOLD = 0.90

# safety cap for GET /screen-saves, matching history.py's _LIST_LIMIT pattern.
LIST_LIMIT = 30


def normalized_collection_name(name: str) -> str:
    """Casefold + whitespace-collapse so trivial edits don't fork the slug."""
    return " ".join((name or "").split()).casefold()


def collection_slug(name: str) -> str:
    """Deterministic doc id for a NEWLY MINTED collection, derived from the
    normalized name that created it. Only used on the mint path — an existing
    collection is always addressed by the doc id ``find_nearest`` returned, so
    this never needs to reproduce another entry's id from a different-but-
    similar name. Firestore doc ids cannot contain '/', so this is a hash,
    matching memory/fields.py's ``atom_id`` technique."""
    digest = hashlib.sha1(normalized_collection_name(name).encode("utf-8")).hexdigest()
    return digest[:24]


def new_item_id() -> str:
    """Fresh id for one screen_saves item. Never merged with another save."""
    return uuid.uuid4().hex
