"""Mark an explicit, human-confirmed list of tracking checkpoints EXPIRED.

Companion to audit_tracking_topic.py: run the audit first, eyeball which
duplicate-group checkpoint series are superseded, then pass their ids here. There is
no auto-detect / heuristic mode on purpose — this only acts on ids a human picked
after reading the audit report, never on a bare topic_key guess.

Uses the existing tracking_store.mark_checkpoint (no hard delete — an EXPIRED
checkpoint keeps its row, consistent with every other terminal transition this
engine already uses), so nothing here is a new write path.

Run from the backend directory:

    cd backend && python scripts/expire_tracking_checkpoints.py <topic_key> \\
        --ids cp_id_1,cp_id_2,cp_id_3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

_SERVICE_ACCOUNT = os.path.join(_BACKEND_DIR, "service-account.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists(_SERVICE_ACCOUNT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _SERVICE_ACCOUNT

from src.services.tracking import fields as f  # noqa: E402
from src.services.tracking import tracking_store as store  # noqa: E402


async def expire(topic_key: str, ids: list[str]) -> None:
    # Verify every id actually belongs to this topic before touching anything — a
    # copy-paste mistake (an id from the wrong topic) must not silently expire
    # someone else's live schedule.
    checkpoints = await store.list_checkpoints_for_topic(topic_key)
    by_id = {cp.id: cp for cp in checkpoints}
    # list_checkpoints_for_topic caps at MAX_CHECKPOINTS_PER_TOPIC — if any requested
    # id isn't in this capped read, fall back to a direct per-id existence check
    # rather than wrongly refusing a valid id on a large topic.
    missing = [cid for cid in ids if cid not in by_id]
    if missing:
        from src.services.firebase import admin_firestore

        db = admin_firestore()
        for cid in list(missing):
            snap = db.collection(f.COLLECTION_CHECKPOINTS).document(cid).get()
            if snap.exists and (snap.to_dict() or {}).get(f.CHECKPOINT_TOPIC_KEY) == topic_key:
                missing.remove(cid)
    if missing:
        print(f"Refusing to proceed: {len(missing)} id(s) do not belong to topic_key={topic_key!r}:")
        for cid in missing:
            print(f"  {cid}")
        return

    print(f"Expiring {len(ids)} checkpoint(s) under topic_key={topic_key!r}:")
    for cid in ids:
        await store.mark_checkpoint(cid, f.CHECKPOINT_STATUS_EXPIRED)
        print(f"  expired: {cid}")
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic_key", help="tracked_topics/{topic_key} the ids belong to")
    parser.add_argument(
        "--ids", required=True,
        help="comma-separated checkpoint ids to mark EXPIRED (copy from audit_tracking_topic.py)",
    )
    args = parser.parse_args()
    ids = [cid.strip() for cid in args.ids.split(",") if cid.strip()]
    if not ids:
        parser.error("--ids must contain at least one non-empty checkpoint id")
    asyncio.run(expire(args.topic_key, ids))


if __name__ == "__main__":
    main()
