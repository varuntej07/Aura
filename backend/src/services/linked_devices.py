"""Canonical linked-device identity and Firestore persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from google.cloud import firestore as gcloud_firestore

LINKED_DEVICES_SUBCOLLECTION = "linked_devices"
FIELD_DEVICE_NAME = "device_name"
FIELD_PLATFORM = "platform"
FIELD_LINKED_AT = "linked_at"
FIELD_LAST_SEEN_AT = "last_seen_at"
FIELD_INSTALL_ID = "install_id"
FIELD_SCHEMA_VERSION = "schema_version"
SCHEMA_VERSION = 2
PLATFORM_WINDOWS = "windows"


def normalize_install_id(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return str(UUID(raw.strip()))
    except (ValueError, AttributeError):
        return None


def upsert_linked_device(
    db,
    user_id: str,
    install_id: object,
    device_name: str,
    *,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Upsert one installation without ever changing its original link time."""
    canonical_id = normalize_install_id(install_id)
    if canonical_id is None:
        return None

    observed_at = now or datetime.now(UTC)
    device_ref = (
        db.collection("users")
        .document(user_id)
        .collection(LINKED_DEVICES_SUBCOLLECTION)
        .document(canonical_id)
    )
    transaction = db.transaction()

    @gcloud_firestore.transactional
    def _execute(txn) -> None:
        snapshot = device_ref.get(transaction=txn)
        payload: dict[str, Any] = {
            FIELD_INSTALL_ID: canonical_id,
            FIELD_DEVICE_NAME: device_name,
            FIELD_PLATFORM: PLATFORM_WINDOWS,
            FIELD_LAST_SEEN_AT: observed_at,
            FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
        }
        if metadata:
            payload.update({key: value for key, value in metadata.items() if value is not None})
        if not snapshot.exists:
            payload[FIELD_LINKED_AT] = observed_at
        txn.set(device_ref, payload, merge=True)

    _execute(transaction)
    return canonical_id
