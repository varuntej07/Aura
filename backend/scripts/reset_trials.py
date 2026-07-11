"""One-off launch migration: give every existing user a fresh 45-day trial.

When payments go live, every account that signed up during the beta gets its
trial clock restarted from the launch date (SUBSCRIPTION_PLAN.md section 7,
step 5), so nobody's trial silently expired before they ever saw a paywall.
Coverage is the users collection itself, not just existing entitlement docs:
an account that never called GET /entitlement gets its doc created here, and a
doc stuck on status "expired" (or a churned ex-paid account) is reset to a
live free trial. Only genuinely paying accounts (paid tier whose normalized
status is still active/gracePeriod) are skipped untouched. The
trial_notified_* flags reset so the day-42 / day-45 lifecycle pushes re-arm.

This script is Phase 5 of the subscription rollout. DO NOT run it before the
launch sequence says so; it touches every user.

Dry run (default) prints every intended write and changes nothing:

    cd backend && python scripts/reset_trials.py --launch-date 2026-08-01

Apply for real (only with explicit founder approval):

    cd backend && python scripts/reset_trials.py --launch-date 2026-08-01 --apply

Limit to a single account first to sanity-check (recommended before --apply):

    cd backend && python scripts/reset_trials.py --launch-date 2026-08-01 --uid <uid> --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_SERVICE_ACCOUNT = os.path.join(_BACKEND_DIR, "service-account.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists(_SERVICE_ACCOUNT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _SERVICE_ACCOUNT

from src.services.entitlement import (  # noqa: E402
    TRIAL_DURATION_DAYS,
    has_active_paid_subscription,
)

# Firestore batched writes cap at 500 ops; stay under it.
_BATCH_SIZE = 400


def _user_refs(db, only_uid: str | None):
    """Every users/{uid} document reference. list_documents() (not stream())
    so "shadow" parents that only exist through subcollections are included."""
    if only_uid:
        yield db.collection("users").document(only_uid)
        return
    yield from db.collection("users").list_documents()


def reset_trials(db, launch_date: datetime, *, apply: bool, only_uid: str | None = None) -> dict:
    """Returns {"reset": int, "created": int, "skipped_paid": int}."""
    new_end = launch_date + timedelta(days=TRIAL_DURATION_DAYS)
    trial_doc = {
        "tier": "free",
        "status": "trialing",
        "trial_start_date": launch_date,
        "trial_end_date": new_end,
        "trial_notified_3d": False,
        "trial_notified_expired": False,
        "updated_at": datetime.now(UTC),
    }

    mode = "APPLY" if apply else "DRY RUN"
    print(f"[{mode}] trial reset: trial_start={launch_date.isoformat()} trial_end={new_end.isoformat()}")

    batch = db.batch()
    pending = 0
    counts = {"reset": 0, "created": 0, "skipped_paid": 0}

    for user_ref in _user_refs(db, only_uid):
        uid = user_ref.id
        ent_ref = user_ref.collection("entitlement").document("current")
        data = ent_ref.get().to_dict() or {}

        if not data:
            print(f"  create: {uid}  (no entitlement doc) trial_end -> {new_end.isoformat()}")
            counts["created"] += 1
        elif has_active_paid_subscription(data):
            counts["skipped_paid"] += 1
            print(f"  skipped (paid, tier={data.get('tier')}): {uid}")
            continue
        else:
            old_end = data.get("trial_end_date")
            old_end_str = old_end.isoformat() if isinstance(old_end, datetime) else str(old_end)
            print(f"  reset: {uid}  trial_end {old_end_str} -> {new_end.isoformat()}")
            counts["reset"] += 1

        if apply:
            # merge=True: dodo_customer_id and other billing fields on a
            # churned ex-paid doc survive; tier/status/dates are overwritten.
            batch.set(ent_ref, trial_doc, merge=True)
            pending += 1
            if pending >= _BATCH_SIZE:
                batch.commit()
                batch = db.batch()
                pending = 0

    if apply and pending:
        batch.commit()

    verb = "applied" if apply else "planned"
    print(
        f"[{mode}] done: {counts['reset']} reset(s) and {counts['created']} create(s) {verb}, "
        f"{counts['skipped_paid']} paying account(s) skipped."
    )
    if not apply:
        print("Nothing was written. Re-run with --apply to perform these writes.")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--launch-date", required=True,
        help="launch day as YYYY-MM-DD (UTC midnight); trial_end becomes this + 45d",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="perform the writes; without this flag the script only prints what it would do",
    )
    parser.add_argument(
        "--uid",
        help="restrict to one uid (sanity-check a single account before the full run)",
    )
    args = parser.parse_args()

    try:
        launch_date = datetime.strptime(args.launch_date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        parser.error("--launch-date must be YYYY-MM-DD")

    from src.services.firebase import admin_firestore

    reset_trials(admin_firestore(), launch_date, apply=args.apply, only_uid=args.uid)


if __name__ == "__main__":
    main()
