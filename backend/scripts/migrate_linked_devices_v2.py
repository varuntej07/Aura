"""Normalize linked-device timestamps and remove unambiguous legacy duplicates.

Run from backend/ without arguments for a dry run. Pass --apply only after
reviewing the counts. Ambiguous records are always left untouched.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from src.services.firebase import admin_firestore
from src.services.linked_devices import (
    FIELD_INSTALL_ID,
    FIELD_LINKED_AT,
    FIELD_SCHEMA_VERSION,
    SCHEMA_VERSION,
)

MATCH_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True)
class DeviceRecord:
    ref: object
    doc_id: str
    data: dict


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _canonical_id(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _name(record: DeviceRecord) -> str:
    value = record.data.get("device_name")
    return value.strip().casefold() if isinstance(value, str) else "windows pc"


def _match_time(record: DeviceRecord) -> datetime | None:
    return _as_datetime(record.data.get("last_seen_at")) or _as_datetime(
        record.data.get(FIELD_LINKED_AT)
    )


def _is_match(legacy: DeviceRecord, canonical: DeviceRecord) -> bool:
    if legacy.data.get("platform") != "windows" or canonical.data.get("platform") != "windows":
        return False
    legacy_time = _match_time(legacy)
    canonical_time = _match_time(canonical)
    return (
        _name(legacy) == _name(canonical)
        and legacy_time is not None
        and canonical_time is not None
        and abs(legacy_time - canonical_time) <= MATCH_WINDOW
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = admin_firestore()
    batch = db.batch()
    pending_writes = 0
    users_seen = canonical_seen = normalized = duplicates = ambiguous = 0

    def flush() -> None:
        nonlocal batch, pending_writes
        if args.apply and pending_writes:
            batch.commit()
        batch = db.batch()
        pending_writes = 0

    for user in db.collection("users").stream():
        users_seen += 1
        records = [
            DeviceRecord(snapshot.reference, snapshot.id, snapshot.to_dict() or {})
            for snapshot in user.reference.collection("linked_devices").stream()
        ]
        canonical = [record for record in records if _canonical_id(record.doc_id) is not None]
        legacy = [record for record in records if _canonical_id(record.doc_id) is None]
        canonical_seen += len(canonical)

        legacy_edges = {
            record.doc_id: [candidate for candidate in canonical if _is_match(record, candidate)]
            for record in legacy
        }
        canonical_degrees = {
            record.doc_id: sum(record in candidates for candidates in legacy_edges.values())
            for record in canonical
        }

        for record in canonical:
            install_id = _canonical_id(record.doc_id)
            linked_at = _as_datetime(record.data.get(FIELD_LINKED_AT))
            updates = {
                FIELD_INSTALL_ID: install_id,
                FIELD_SCHEMA_VERSION: SCHEMA_VERSION,
            }
            if linked_at is not None:
                updates[FIELD_LINKED_AT] = linked_at
            if record.data.get("last_seen_at") is not None:
                last_seen_at = _as_datetime(record.data.get("last_seen_at"))
                if last_seen_at is not None:
                    updates["last_seen_at"] = last_seen_at
            if any(record.data.get(key) != value for key, value in updates.items()):
                batch.set(record.ref, updates, merge=True)
                pending_writes += 1
                normalized += 1

        for record in legacy:
            matches = legacy_edges[record.doc_id]
            if len(matches) == 1 and canonical_degrees[matches[0].doc_id] == 1:
                batch.delete(record.ref)
                pending_writes += 1
                duplicates += 1
            elif matches:
                ambiguous += 1

        if pending_writes >= 400:
            flush()

    flush()
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(
        f"{mode}: users={users_seen} canonical={canonical_seen} "
        f"normalized={normalized} duplicates={duplicates} ambiguous={ambiguous}"
    )


if __name__ == "__main__":
    main()
