"""POST /aura/consolidate-session — the per-session reflection trigger.

The chat transcript is client-owned (the local drift DB), so when a session ends the
client ships its turns here (fired from the app-background rail, a new-session boundary,
or a resume that finds a stale un-consolidated session). We kick off the reflection tier
fire-and-forget and return immediately, so the client is never blocked. Reflection is
idempotent per session_id, GDPR-gated on consent, and swallows its own errors, so a
retry or duplicate send is safe.

Authenticated as the end user via Firebase ID token (same as /events, /threads/reply).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.aura_reflection import consolidate_session
from ..services.firebase import admin_firestore
from ..services.memory.atom_store import delete_atom, list_atoms
from ..services.memory.fields import (
    ATOM_TYPE_FACT,
    ATOM_TYPE_INTEREST_SUBJECT,
    ATOM_TYPE_STORYLINE,
    ATOM_TYPE_TRAIT,
)
from ..services.memory.graph_store import wipe_graph
from ..services.request_auth import resolve_user_id_from_request

# The client owns the chat; this is digest input, not storage. Keep the most recent
# turns and let reflection compress further if needed.
MAX_TURNS = 400


async def handle_consolidate_session(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object."}, status_code=400)

    session_id = str(body.get("session_id", "")).strip() or None
    raw_turns = body.get("turns")
    if not isinstance(raw_turns, list):
        return JSONResponse({"error": "Field 'turns' must be a list."}, status_code=400)
    modality = str(body.get("modality", "text")).strip() or "text"

    turns = [t for t in raw_turns if isinstance(t, dict)][-MAX_TURNS:]

    # Fire-and-forget: reflection runs after the response returns. It gates on consent,
    # is idempotent per session_id, and never raises, so detaching it is safe.
    asyncio.create_task(consolidate_session(user_id, session_id, turns, modality))

    logger.info("AuraConsolidate: accepted", {
        "user_id": user_id,
        "session_id": session_id,
        "turns": len(turns),
        "modality": modality,
    })
    return JSONResponse({"status": "accepted"}, status_code=202)


# Map each atom type to a user-facing group for the "what Buddy remembers" screen.
_ATOM_TYPE_GROUP = {
    ATOM_TYPE_FACT: "facts",
    ATOM_TYPE_STORYLINE: "storylines",
    ATOM_TYPE_INTEREST_SUBJECT: "interests",
    ATOM_TYPE_TRAIT: "traits",
}


async def handle_get_memory(request: Request) -> JSONResponse:
    """GET /aura/memory — what Buddy remembers about the user, grouped by type.

    NOT consent-gated on purpose: a user can always SEE (and delete) their stored data,
    even after revoking processing consent. Showing memory is a GDPR access right; only
    the chat-time USE of it is gated (in handlers/chat.py). Read-only, never writes.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    atoms = await list_atoms(user_id)
    groups: dict[str, list] = {"facts": [], "storylines": [], "interests": [], "traits": []}
    for atom in atoms:
        bucket = _ATOM_TYPE_GROUP.get(atom["atom_type"], "facts")
        groups[bucket].append({
            "id": atom["id"],
            "text": atom["text"],
            "last_seen": atom["last_seen"],
        })

    logger.info("AuraMemory: listed", {"user_id": user_id, "total": len(atoms)})
    return JSONResponse({"memory": groups, "total": len(atoms)})


async def handle_delete_memory(request: Request, atom_id: str) -> JSONResponse:
    """DELETE /aura/memory/{atom_id} — forget one memory. Always allowed for the owner
    (a data-subject erasure right). v1 is a hard delete with no tombstone: if the user
    later re-states the fact, the extractor may recreate it, which is a reasonable signal
    to remember it again."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    atom_id = (atom_id or "").strip()
    if not atom_id:
        return JSONResponse({"error": "Missing atom id."}, status_code=400)

    ok = await delete_atom(user_id, atom_id)
    logger.info("AuraMemory: deleted", {"user_id": user_id, "atom_id": atom_id, "ok": ok})
    return JSONResponse({"ok": ok})


def _delete_doc_tree(doc_ref) -> int:
    removed = 0
    for subcollection in doc_ref.collections():
        for child in subcollection.stream():
            removed += _delete_doc_tree(child.reference)
    doc_ref.delete()
    return removed + 1


def _revoke_and_delete_aura_memory(uid: str) -> int:
    """Fail-closed consent revoke: stop processing, then erase all memory data."""
    db = admin_firestore()
    user_ref = db.collection("users").document(uid)
    user_ref.set({
        "aura_consent_granted": False,
        "aura_consent_timestamp": datetime.now(UTC).isoformat(),
    }, merge=True)

    aura_ref = db.collection("UserAura").document(uid)
    removed = 0
    for name in (
        "memory_atoms",
        "sessions",
    ):
        for child in aura_ref.collection(name).stream():
            removed += _delete_doc_tree(child.reference)
    # The root is the compact learned profile. Deleting it does not delete the
    # independent drafts/screen-save subcollections that the consent does not own.
    aura_ref.delete()
    return removed + 1


async def handle_wipe_memory(request: Request) -> JSONResponse:
    """DELETE /aura/memory: withdraw consent and erase learned memory."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        removed = await asyncio.to_thread(_revoke_and_delete_aura_memory, user_id)
        try:
            removed += await wipe_graph(user_id)
        except Exception as exc:
            logger.warn("AuraMemory: graph wipe failed open", {
                "user_id": user_id,
                "error": str(exc),
            })
        logger.info("AuraMemory: consent revoked and memory wiped", {
            "user_id": user_id,
            "removed": removed,
        })
        return JSONResponse({"ok": True, "removed": removed})
    except Exception as exc:
        logger.exception("AuraMemory: consent revoke wipe failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return JSONResponse({"error": "Memory deletion failed. Please try again."}, status_code=500)
