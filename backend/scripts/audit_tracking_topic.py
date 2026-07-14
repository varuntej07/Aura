"""Read-only audit of one tracking topic under the fixture/moment engine.

Prints the topic's health header, its FIXTURES (the stable-identity docs whose fact
state gates every push), the moment/pulse checkpoint census, and — with ``--fires`` —
each fixture's per-fire audit trail (every sent AND abstained decision, with the
prior/seen facts), which is where "why did/didn't I get a notification" lives.

Strictly read-only: no writes, no Firestore mutation. Legacy poll-grid remnants show
up in the census as pending live/post/milestone docs; sweep those with
``migrate_tracking_to_fixtures.py``.

Run from the backend directory:

    python scripts/audit_tracking_topic.py <topic_key>
    python scripts/audit_tracking_topic.py <topic_key> --fires

Needs Firestore credentials. If GOOGLE_APPLICATION_CREDENTIALS is unset and
backend/service-account.json exists, it's used automatically.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

_SERVICE_ACCOUNT = os.path.join(_BACKEND_DIR, "service-account.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and os.path.exists(_SERVICE_ACCOUNT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _SERVICE_ACCOUNT

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from src.services import firebase  # noqa: E402
from src.services.tracking import fields as f  # noqa: E402
from src.services.tracking.models import Checkpoint, Fixture, TrackedTopic  # noqa: E402
from src.services.tracking.moments import is_legacy_poll_phase  # noqa: E402


def _topic_ref(topic_key: str):
    return firebase.admin_firestore().collection(f.COLLECTION_TRACKED_TOPICS).document(topic_key)


def _print_topic_header(topic: TrackedTopic) -> None:
    print(f"=== {topic.topic_key} ===")
    print(f"  title:      {topic.title}")
    print(f"  status:     {topic.status}  health: {topic.health}  subscribers: {topic.subscriber_count}")
    print(f"  reconcile:  last={topic.last_reconciled_at} ({topic.last_reconcile_status or '-'})  "
          f"next={topic.next_reconcile_at}")
    print(f"  lifespan:   ends={topic.ends_at}  expires={topic.expires_at}  awaiting_date={topic.awaiting_date}")
    print(f"  pulse:      every {topic.pulse_interval_seconds}s  "
          f"recent developments: {len(topic.recent_development_keys)}")
    print(f"  live_summary: {topic.live_summary or '(none)'}\n")


def _facts_of(fx: Fixture) -> str:
    bits = [bit for bit in (
        fx.fact_winner and f"winner={fx.fact_winner}",
        fx.fact_score and f"score={fx.fact_score}",
        fx.fact_note and f"note={fx.fact_note}",
        fx.last_transition and f"({fx.last_transition})",
    ) if bit]
    return " ".join(bits) or "-"


def _print_fixtures(topic_key: str, fixtures: list[Fixture], *, show_fires: bool) -> None:
    print(f"--- fixtures: {len(fixtures)} ---")
    for fx in sorted(fixtures, key=lambda fx: fx.start_at):
        print(f"  {fx.id} | {fx.start_at:%Y-%m-%d %H:%M} | {fx.status:>9} | "
              f"{_facts_of(fx)} | {fx.label}")
        if not show_fires:
            continue
        fires = (
            _topic_ref(topic_key)
            .collection(f.COLLECTION_FIXTURES).document(fx.id)
            .collection(f.COLLECTION_FIXTURE_FIRES)
            .stream()
        )
        rows = sorted((s.to_dict() or {} for s in fires), key=lambda r: str(r.get(f.AUDIT_FIRED_AT)))
        for row in rows:
            seen = row.get(f.AUDIT_SEEN_FACTS) or {}
            seen_line = ", ".join(f"{k}={v}" for k, v in seen.items() if v) or "-"
            print(f"      {row.get(f.AUDIT_FIRED_AT)} | {row.get(f.AUDIT_MOMENT):>7} | "
                  f"{row.get(f.AUDIT_DECISION):<24} | tier={row.get(f.AUDIT_FETCH_TIER) or '-':<8} | "
                  f"sent={row.get(f.AUDIT_SENT_COUNT, 0)} | seen: {seen_line}")
    print()


def _print_checkpoint_census(topic_key: str) -> None:
    snaps = (
        firebase.admin_firestore()
        .collection(f.COLLECTION_CHECKPOINTS)
        .where(filter=FieldFilter(f.CHECKPOINT_TOPIC_KEY, "==", topic_key))
        .stream()
    )
    census: Counter = Counter()
    pending: list[Checkpoint] = []
    legacy_pending = 0
    for snap in snaps:
        cp = Checkpoint.from_dict(snap.to_dict() or {})
        census[(cp.status, cp.phase)] += 1
        if cp.status == f.CHECKPOINT_STATUS_PENDING:
            pending.append(cp)
            # Same discriminator the fire path + migration script use, so this count
            # always matches what the migration would sweep.
            if is_legacy_poll_phase(cp.phase, cp.fixture_id):
                legacy_pending += 1

    print(f"--- checkpoint census (status, phase): {dict(census) or '(none)'} ---")
    if legacy_pending:
        print(f"  !! {legacy_pending} pending LEGACY poll-grid doc(s) remain - "
              f"run migrate_tracking_to_fixtures.py to sweep them.")
    print("--- pending queue ---")
    for cp in sorted(pending, key=lambda cp: cp.fire_at):
        checks = f" checks={cp.result_checks}" if cp.phase == f.CHECKPOINT_PHASE_RESULT else ""
        print(f"  {cp.fire_at:%Y-%m-%d %H:%M} | {cp.phase:>7}{checks} | {cp.id}")
    print()


def audit(topic_key: str, *, show_fires: bool) -> None:
    snap = _topic_ref(topic_key).get()
    if not snap.exists:
        print(f"Topic {topic_key!r} does not exist.")
        return
    topic = TrackedTopic.from_dict(snap.to_dict() or {})
    _print_topic_header(topic)

    fixtures = [
        Fixture.from_dict(s.to_dict() or {})
        for s in _topic_ref(topic_key).collection(f.COLLECTION_FIXTURES).stream()
    ]
    _print_fixtures(topic_key, fixtures, show_fires=show_fires)
    _print_checkpoint_census(topic_key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic_key", help="tracked_topics/{topic_key} to audit")
    parser.add_argument("--fires", action="store_true",
                        help="also print each fixture's per-fire audit rows (sent AND abstained)")
    args = parser.parse_args()
    audit(args.topic_key, show_fires=args.fires)


if __name__ == "__main__":
    main()
