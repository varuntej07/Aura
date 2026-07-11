"""
DELETE /account permanently deletes a user's account.

Deletes in order:
1. Firestore subcollections and documents (data first)
2. Firebase Auth user (last, so retries don't orphan data if step 1 partially fails)

All Firestore deletions run via batch writes where possible to minimize round trips.
"""
from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services.firebase import admin_auth, admin_firestore
from ..services.request_auth import decode_firebase_claims
from .pairing import FIELD_UID as PAIRING_FIELD_UID


async def handle_delete_account(request: Request) -> JSONResponse:
    claims = decode_firebase_claims(request.headers)
    if not claims:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    uid: str = claims.get("uid") or claims.get("sub") or ""
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    logger.info("account: delete requested", {"user_id": uid})

    try:
        await asyncio.to_thread(_delete_all_user_data, uid)
        await asyncio.to_thread(_delete_firebase_auth_user, uid)
        logger.info("account: deletion complete", {"user_id": uid})
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("account: deletion failed", {
            "user_id": uid,
            "error": str(exc),
        })
        return JSONResponse({"error": "Deletion failed. Please try again."}, status_code=500)


def _delete_all_user_data(uid: str) -> None:
    db = admin_firestore()

    # Collections to fully delete for this user
    top_level_collections = [
        ("UserAura", uid),
        ("UserSignals", uid),
    ]
    for collection, doc_id in top_level_collections:
        _delete_document_and_subcollections(db, db.collection(collection).document(doc_id))

    user_ref = db.collection("users").document(uid)
    _delete_document_and_subcollections(db, user_ref)

    # Chat sessions are stored per-user in local SQLite on device, nothing to delete server-side
    _delete_collection_docs(db.collection("devices").where("uid", "==", uid).stream())

    # Top-level pairing_codes/{CODE} docs (pairing.py) aren't under users/{uid}, so the
    # subcollection-recursive deletes above never reach them.
    _delete_collection_docs(
        db.collection("pairing_codes").where(PAIRING_FIELD_UID, "==", uid).stream()
    )

    # Same shape: dashboard_link_codes/{TOKEN} (dashboard_link.py) is also top-level.
    _delete_collection_docs(
        db.collection("dashboard_link_codes").where(PAIRING_FIELD_UID, "==", uid).stream()
    )


def _delete_document_and_subcollections(db, doc_ref) -> None:
    for sub_collection in doc_ref.collections():
        for sub_doc in sub_collection.stream():
            sub_doc.reference.delete()
    doc_ref.delete()


def _delete_collection_docs(docs) -> None:
    for doc in docs:
        doc.reference.delete()


def _delete_firebase_auth_user(uid: str) -> None:
    try:
        admin_auth().delete_user(uid)
    except Exception as exc:
        logger.warn("account: Firebase Auth user deletion failed", {
            "user_id": uid,
            "error": str(exc),
        })
        raise
