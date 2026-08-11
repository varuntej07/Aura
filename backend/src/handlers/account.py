"""
DELETE /account permanently deletes a user's account.

Deletes in order:
1. Raw dictation and meeting audio in Cloud Storage
2. Firestore subcollections and documents
3. Firebase Auth user (last, so retries don't orphan data if an earlier step fails)

All Firestore deletions run via batch writes where possible to minimize round trips.
"""
from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse
from google.cloud import firestore as fs

from ..lib.logger import logger
from ..services.dictation import gcs_audio as dictation_audio
from ..services.firebase import admin_auth, admin_firestore
from ..services.meetings import gcs_audio as meeting_audio
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
        # Raw audio is outside Firestore. Delete it strictly before
        # removing account records so a storage failure leaves Auth intact and
        # the user can safely retry the deletion request.
        await dictation_audio.delete_user_audio(uid)
        await meeting_audio.delete_user_audio(uid)
        await meeting_audio.delete_user_transcripts(uid)
        await asyncio.to_thread(_delete_all_user_data, uid)
        await asyncio.to_thread(_delete_firebase_auth_user, uid)
        logger.info("account: deletion complete", {"user_id": uid})
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception(
            "account: deletion failed",
            {
                "user_id": uid,
                "error": str(exc),
            },
        )
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

    # Desktop chat history (users/{uid}/desktop_chat_sessions and its nested
    # desktop_chat_messages) needs no entry here: the recursive walk above follows every
    # subcollection under users/{uid}. The device's local SQLite copy is separate and is
    # cleared client-side at the sign-out boundary.
    _delete_collection_docs(
        db.collection("devices").where(filter=fs.FieldFilter("uid", "==", uid)).stream()
    )

    # Top-level pairing_codes/{CODE} docs (pairing.py) aren't under users/{uid}, so the
    # subcollection-recursive deletes above never reach them.
    _delete_collection_docs(
        db.collection("pairing_codes")
        .where(filter=fs.FieldFilter(PAIRING_FIELD_UID, "==", uid))
        .stream()
    )

    _delete_collection_docs(
        db.collection("connector_oauth_attempts")
        .where(filter=fs.FieldFilter(PAIRING_FIELD_UID, "==", uid))
        .stream()
    )


def _delete_document_and_subcollections(db, doc_ref) -> None:
    for sub_collection in doc_ref.collections():
        for sub_doc in sub_collection.stream():
            _delete_document_and_subcollections(db, sub_doc.reference)
    doc_ref.delete()


def _delete_collection_docs(docs) -> None:
    for doc in docs:
        doc.reference.delete()


def _delete_firebase_auth_user(uid: str) -> None:
    try:
        admin_auth().delete_user(uid)
    except Exception as exc:
        logger.warn(
            "account: Firebase Auth user deletion failed",
            {
                "user_id": uid,
                "error": str(exc),
            },
        )
        raise
