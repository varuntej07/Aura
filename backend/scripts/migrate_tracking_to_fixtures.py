"""One-shot migration: retire the legacy poll-grid checkpoints and hand every active
topic to the fixture/moment engine (the 2026-07-10 tracker redesign).

What it does per active topic:
  1. Marks every PENDING legacy checkpoint EXPIRED — a legacy doc is one with a
     poll-grid phase (live/post/milestone) or a non-pulse doc with no fixture_id
     binding (the old pre docs). The pulse and any already-settled docs are left
     alone. (The deployed engine also expires legacy docs on sight when they fire;
     this sweep just clears the backlog in one pass instead of one-per-minute.)
  2. Sets next_reconcile_at = now, so the deployed engine's OWN reconcile re-researches
     the topic into fixtures and lays the sparse moments — one write path, no bespoke
     research code here.

Trackers, subscriber counts, and topic fact history are untouched.

Deliberately a MANUAL script (not an endpoint): it is an all-users prod write that
cannot be dark-tested on shared Firestore, so it runs only when a human runs it.

Run from the backend directory (dry-run is the default; nothing is written without
--apply):

    python scripts/migrate_tracking_to_fixtures.py                       # dry-run, all active topics
    python scripts/migrate_tracking_to_fixtures.py --topic-key fifa-world-cup-2026 --apply   # pilot
    python scripts/migrate_tracking_to_fixtures.py --apply               # everything
    python scripts/migrate_tracking_to_fixtures.py --verify fifa-world-cup-2026   # post-reconcile check
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import UTC, datetime

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

_SERVICE_ACCOUNT = os.path.join(_BACKEND_DIR, "service-account.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists(_SERVICE_ACCOUNT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _SERVICE_ACCOUNT

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from src.services.firebase import admin_firestore  # noqa: E402
from src.services.tracking import fields as f  # noqa: E402
from src.services.tracking.moments import is_legacy_poll_phase  # noqa: E402

_EXPIRE_BATCH_SIZE = 400  # Firestore batch limit is 500; leave headroom.


def _list_active_topic_keys(only_topic_key: str | None) -> list[str]:
    db = admin_firestore()
    if only_topic_key:
        snap = db.collection(f.COLLECTION_TRACKED_TOPICS).document(only_topic_key).get()
        if not snap.exists:
            print(f"Topic {only_topic_key!r} does not exist; nothing to do.")
            return []
        return [only_topic_key]
    snaps = (
        db.collection(f.COLLECTION_TRACKED_TOPICS)
        .where(filter=FieldFilter(f.TOPIC_STATUS, "==", f.TOPIC_STATUS_ACTIVE))
        .stream()
    )
    return [snap.id for snap in snaps]


def _legacy_pending_checkpoint_refs(topic_key: str) -> tuple[list, Counter]:
    """(refs to expire, per-(status,phase) census of everything seen)."""
    db = admin_firestore()
    snaps = (
        db.collection(f.COLLECTION_CHECKPOINTS)
        .where(filter=FieldFilter(f.CHECKPOINT_TOPIC_KEY, "==", topic_key))
        .stream()
    )
    census: Counter = Counter()
    refs = []
    for snap in snaps:
        data = snap.to_dict() or {}
        status = str(data.get(f.CHECKPOINT_STATUS, ""))
        phase = str(data.get(f.CHECKPOINT_PHASE, ""))
        fixture_id = str(data.get(f.CHECKPOINT_FIXTURE_ID, "") or "")
        census[(status, phase)] += 1
        if status == f.CHECKPOINT_STATUS_PENDING and is_legacy_poll_phase(phase, fixture_id):
            refs.append(snap.reference)
    return refs, census


def _expire_refs(refs: list) -> int:
    db = admin_firestore()
    expired = 0
    for chunk_start in range(0, len(refs), _EXPIRE_BATCH_SIZE):
        batch = db.batch()
        for ref in refs[chunk_start : chunk_start + _EXPIRE_BATCH_SIZE]:
            batch.update(ref, {f.CHECKPOINT_STATUS: f.CHECKPOINT_STATUS_EXPIRED})
        batch.commit()
        expired += len(refs[chunk_start : chunk_start + _EXPIRE_BATCH_SIZE])
        print(f"    ... expired {expired}/{len(refs)}")
    return expired


async def migrate(only_topic_key: str | None, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN (pass --apply to write)"
    print(f"Tracking fixture migration - {mode}\n")
    topic_keys = _list_active_topic_keys(only_topic_key)
    if not topic_keys:
        print("No active topics found.")
        return

    now = datetime.now(UTC)
    total_legacy = 0
    for topic_key in topic_keys:
        refs, census = _legacy_pending_checkpoint_refs(topic_key)
        total_legacy += len(refs)
        print(f"{topic_key}:")
        print(f"  checkpoint census (status, phase): {dict(census) or '(none)'}")
        print(f"  pending legacy poll-grid docs to expire: {len(refs)}")
        if not apply:
            continue
        if refs:
            _expire_refs(refs)
        admin_firestore().collection(f.COLLECTION_TRACKED_TOPICS).document(topic_key).update({
            f.TOPIC_NEXT_RECONCILE_AT: now,
            f.TOPIC_UPDATED_AT: now,
        })
        print("  next_reconcile_at set to now - the deployed engine's next reconcile "
              "pass (within 15 min) lays fixtures + moments.")

    print(f"\n{'Expired' if apply else 'Would expire'} {total_legacy} legacy checkpoint(s) "
          f"across {len(topic_keys)} topic(s).")
    if apply:
        print("Verify after the next reconcile pass with: "
              "python scripts/migrate_tracking_to_fixtures.py --verify <topic_key>")


def verify(topic_key: str) -> None:
    """Post-migration read-back: fixtures laid, moments pending, legacy drained."""
    db = admin_firestore()
    topic_snap = db.collection(f.COLLECTION_TRACKED_TOPICS).document(topic_key).get()
    if not topic_snap.exists:
        print(f"Topic {topic_key!r} does not exist.")
        return
    topic = topic_snap.to_dict() or {}
    print(f"{topic_key}: status={topic.get(f.TOPIC_STATUS)} "
          f"last_reconcile={topic.get(f.TOPIC_LAST_RECONCILED_AT)} "
          f"({topic.get(f.TOPIC_LAST_RECONCILE_STATUS)})")

    fixtures = list(
        db.collection(f.COLLECTION_TRACKED_TOPICS).document(topic_key)
        .collection(f.COLLECTION_FIXTURES).stream()
    )
    print(f"  fixtures: {len(fixtures)}")
    for snap in sorted(fixtures, key=lambda s: str((s.to_dict() or {}).get(f.FIXTURE_START_AT))):
        fx = snap.to_dict() or {}
        print(f"    {snap.id} | {fx.get(f.FIXTURE_START_AT)} | {fx.get(f.FIXTURE_STATUS):>9} | "
              f"{fx.get(f.FIXTURE_LABEL)}")

    census: Counter = Counter()
    for snap in (
        db.collection(f.COLLECTION_CHECKPOINTS)
        .where(filter=FieldFilter(f.CHECKPOINT_TOPIC_KEY, "==", topic_key))
        .stream()
    ):
        data = snap.to_dict() or {}
        census[(str(data.get(f.CHECKPOINT_STATUS)), str(data.get(f.CHECKPOINT_PHASE)))] += 1
    print(f"  checkpoint census (status, phase): {dict(census)}")
    legacy_pending = sum(
        count for (status, phase), count in census.items()
        if status == f.CHECKPOINT_STATUS_PENDING
        and phase in (f.CHECKPOINT_PHASE_LIVE, f.CHECKPOINT_PHASE_POST, f.CHECKPOINT_PHASE_MILESTONE)
    )
    print(f"  pending legacy docs remaining: {legacy_pending} "
          f"({'OK' if legacy_pending == 0 else 'RUN THE MIGRATION AGAIN'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic-key", help="migrate only this topic (pilot mode)")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; default is a read-only dry-run")
    parser.add_argument("--verify", metavar="TOPIC_KEY",
                        help="read-back check for a migrated topic (no writes)")
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
        return
    asyncio.run(migrate(args.topic_key, args.apply))


if __name__ == "__main__":
    main()
