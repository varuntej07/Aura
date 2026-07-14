"""Read-only audit of memory-row display quality for the v0.1.7 callback card.

Pulls the raw ``users/{uid}/memories`` rows for the most recently active users
and prints them verbatim so a human can answer the design gate from the v0.1.7
visible-memory plan: how many users have at least one specific, correct, warm
fact you would be comfortable putting on screen unedited? If fewer than half,
the callback-line generation needs a curation pass before any UI ships.

Strictly read-only: no writes, no Firestore mutation.

Run from the backend directory:

    cd backend && python scripts/audit_memory_quality.py [--users 10]

Needs Firestore credentials. If GOOGLE_APPLICATION_CREDENTIALS is unset and
backend/service-account.json exists, it's used automatically (same file the app
already has for local dev; see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import os
import sys

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

_SERVICE_ACCOUNT = os.path.join(_BACKEND_DIR, "service-account.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists(_SERVICE_ACCOUNT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _SERVICE_ACCOUNT

from src.services import firebase  # noqa: E402


def _recent_memory_owner_uids(limit_users: int) -> list[str]:
    """Uids of the users with the most recently updated memory rows.

    Uses a collection-group scan over ``memories`` ordered by ``updated_at``;
    falls back to an unordered scan if the composite index for the ordered
    collection-group query doesn't exist (this is an audit script, ordering is
    a nicety, coverage is the point).
    """
    db = firebase.admin_firestore()
    uids: list[str] = []

    # .stream() is lazy: the query RPC (and any missing-index error) only fires
    # when the generator is first consumed, so materialize to a list INSIDE the
    # try - catching around the .stream() call alone never sees the error. The
    # ordered scan needs a COLLECTION_GROUP_DESC index on memories.updated_at;
    # without it we fall back to an unordered scan (ordering is a nicety here,
    # coverage is the point).
    try:
        snaps = list(
            db.collection_group("memories")
            .order_by("updated_at", direction="DESCENDING")
            .limit(500)
            .stream()
        )
    except Exception as exc:  # missing index, permissions, etc.
        print(f"(ordered collection-group query failed: {type(exc).__name__}; falling back to unordered scan)")
        snaps = list(db.collection_group("memories").limit(500).stream())

    for snap in snaps:
        # Path: users/{uid}/memories/{doc}
        parent = snap.reference.parent.parent
        if parent is None:
            continue
        uid = parent.id
        if uid not in uids:
            uids.append(uid)
        if len(uids) >= limit_users:
            break
    return uids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=10, help="how many users to sample")
    args = parser.parse_args()

    db = firebase.admin_firestore()
    uids = _recent_memory_owner_uids(args.users)
    if not uids:
        print("No memory rows found anywhere. The gate question is moot: nothing can render.")
        return

    print(f"Sampled {len(uids)} users with the most recent memory activity.\n")
    users_with_publishable_candidate = 0
    for i, uid in enumerate(uids, 1):
        rows = list(
            db.collection("users").document(uid).collection("memories")
            .order_by("updated_at", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        print(f"--- user {i} ({uid[:8]}…) : {len(rows)} rows (up to 10 shown) ---")
        for snap in rows:
            row = snap.to_dict() or {}
            key = str(row.get("key", "")).strip()
            value = str(row.get("value", "")).strip()
            updated = row.get("updated_at", "?")
            print(f"  [{updated}] {key}: {value}")
        print()
        # Counting is left to the human eyeball; the script only lays out the evidence.

    print("GATE QUESTION: for how many of these users does at least one row read as a")
    print("specific, correct, warm fact you would put on screen unedited?")
    print(f"If fewer than {len(uids) // 2 + 1} of {len(uids)}, add a curation pass before shipping the card.")
    _ = users_with_publishable_candidate  # human judgment, not code judgment


if __name__ == "__main__":
    main()
