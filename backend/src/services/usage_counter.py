"""``users/{uid}/usage/*`` period counters - one owner for the doc path, the
UTC month window, and the write discipline.

Four features write into this one subcollection today, with four schemas:
entitlement's daily docs (``{date, count}`` / ``{date, seconds}``), research's
``research_{YYYY-MM-DD}`` (``{credits, runs, updated_at}``, merge+Increment),
dictation's ``dictation_{YYYYMM}``, and meetings' ``meetings_{YYYYMM}``. This
module owns the shared monthly pieces (path builder + month window) used by
dictation and meetings; migrating the daily writers (entitlement, research)
is deliberate follow-up work - entitlement is the mobile-shared hot path.

Write discipline for every usage writer: merge-set or field-level update,
NEVER a bare document set. A bare set silently clobbers whatever fields a
sibling writer or a future schema adds to the same doc.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

PARENT_COLLECTION = "users"
USAGE_SUBCOLLECTION = "usage"


def usage_doc_ref(db: Any, uid: str, doc_id: str) -> Any:
    return (
        db.collection(PARENT_COLLECTION)
        .document(uid)
        .collection(USAGE_SUBCOLLECTION)
        .document(doc_id)
    )


def month_window(now: datetime) -> tuple[str, int, int]:
    """``(month_key "YYYYMM", reset_epoch_ms, seconds_until_reset)`` for the
    UTC month containing ``now``. One implementation of the "first of next
    month, UTC, December wraps" rule for every monthly cap surface."""
    utc_now = (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(UTC)
    month_key = utc_now.strftime("%Y%m")
    if utc_now.month == 12:
        reset = datetime(utc_now.year + 1, 1, 1, tzinfo=UTC)
    else:
        reset = datetime(utc_now.year, utc_now.month + 1, 1, tzinfo=UTC)
    return (
        month_key,
        int(reset.timestamp() * 1_000),
        max(0, int((reset - utc_now).total_seconds())),
    )
