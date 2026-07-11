"""
Outbound-draft document contract - the single source of truth for field names
on ``UserAura/{uid}/drafts/{draft_id}``.

Per the data-layer discipline in CLAUDE.md, ``store.py`` and every reader
import field names from HERE, never inline a string literal, so a rename can
never silently split the writer/reader contract.

Stored shape (latest version only - refines overwrite ``text`` in place):

    UserAura/{uid}/drafts/{draft_id} = {
        "channel":          str,        # "email_reply" | "cold_dm" | "snippet"
        "length":           str,        # "short" | "medium" | "detailed", latest target
                                        #   (always "short" for snippet, which has no ladder)
        "text":             str,        # latest draft text
        "context_summary":  str,        # model-written summary of the source screen
        "recipient_hint":   str,        # "" if none
        "revision":         int,        # 1 at create, Increment(1) on every update
        "session_id":       str,        # originating voice session, never exposed via REST
        "created_at":       iso8601,    # set once; the list endpoint sorts on this
        "updated_at":       iso8601,    # bumped on every refine
        "expires_at":       timestamp,  # native datetime, drives the Firestore TTL policy
    }

``draft_id`` is the uuid4 hex the voice worker minted for the data-channel
events, so the desktop card, the REST refine, and this doc all name the same
draft.

Retention: ``expires_at`` is set to now + ``RETENTION_DAYS`` on create and on
every refine (an actively-worked draft stays alive). A Firestore TTL policy on
the ``drafts`` collection group deletes expired docs; TTL deletion can lag up
to ~72h, so ``store.list_drafts`` also drops already-expired rows itself.
One-time infra step: ``gcloud firestore fields ttls update expires_at
--collection-group=drafts --enable-ttl``.
"""

from __future__ import annotations

# --- Firestore locations ------------------------------------------------------
# Parent collection is UserAura (co-located with screen_saves/memory_atoms), so
# the subcollection falls through to the same default-deny Firestore rule with
# no rule change needed - backend Admin SDK only, dashboard reads via REST.
ITEM_PARENT_COLLECTION = "UserAura"
ITEM_SUBCOLLECTION = "drafts"

# --- drafts item fields ---------------------------------------------------------
CHANNEL = "channel"
LENGTH = "length"
TEXT = "text"
CONTEXT_SUMMARY = "context_summary"
RECIPIENT_HINT = "recipient_hint"
REVISION = "revision"
SESSION_ID = "session_id"
CREATED_AT = "created_at"
UPDATED_AT = "updated_at"
EXPIRES_AT = "expires_at"

# --- limits / retention ---------------------------------------------------------
# safety cap for GET /drafts, matching screen_saves/fields.py's LIST_LIMIT.
LIST_LIMIT = 30

# How long a draft lives after its last create/refine before the TTL policy
# removes it. Drafts are transient artifacts; a week covers "drafted it Friday,
# need it Monday" without turning the dashboard into an archive.
RETENTION_DAYS = 7
