"""Meeting V2 Firestore reference builders - the documented internal API.

``store.py``, ``tasks.py``, and ``deletion.py`` all address the same durable
documents; they import the builders from HERE (store aliases them under its
historical underscore names). This surface is deliberately small and stable
so ``store.py`` can refactor its internals without silently breaking the two
sibling modules that used to reach into its private symbols.
"""

from __future__ import annotations

from .. import usage_counter
from ..firebase import admin_firestore
from . import fields as F


def meetings_ref(uid: str):
    return (
        admin_firestore().collection(F.PARENT_COLLECTION).document(uid).collection(F.SUBCOLLECTION)
    )


def claim_ref(uid: str, event_key: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.CLAIMS_SUBCOLLECTION)
        .document(event_key)
    )


def usage_ref(uid: str, month_key: str):
    return usage_counter.usage_doc_ref(admin_firestore(), uid, f"meetings_{month_key}")


def capture_run_ref(uid: str, meeting_id: str, capture_run_id: str):
    return (
        meetings_ref(uid)
        .document(meeting_id)
        .collection(F.CAPTURE_RUNS_SUBCOLLECTION)
        .document(capture_run_id)
    )


def segments_ref(uid: str, meeting_id: str, capture_run_id: str):
    return capture_run_ref(uid, meeting_id, capture_run_id).collection(F.SEGMENTS_SUBCOLLECTION)


def jobs_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.JOBS_SUBCOLLECTION)
    )


def outbox_ref(uid: str):
    return (
        admin_firestore()
        .collection(F.PARENT_COLLECTION)
        .document(uid)
        .collection(F.JOB_OUTBOX_SUBCOLLECTION)
    )


def audit_ref(uid: str, meeting_id: str):
    return meetings_ref(uid).document(meeting_id).collection(F.AUDIT_SUBCOLLECTION)
