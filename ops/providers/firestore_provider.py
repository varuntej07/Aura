"""Firestore-backed panels: latest messages, latest voice, user metrics + table, feedback.

All reads use the Firebase Admin SDK (full read access, bypasses security rules). uid is
recovered from each collection-group doc's reference path because the message/voice docs
do not store uid as a field, it is the grandparent doc id.

Every query is wrapped so a missing index or transient error returns an empty panel and
logs loudly (never a silent zero that looks like "no activity"). The two collection-group
queries REQUIRE explicit COLLECTION_GROUP indexes, see ops/README.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from firebase_admin import firestore
from google.cloud.firestore_v1 import Query
from google.cloud.firestore_v1.base_query import FieldFilter

from fields import (
    DEC_LANE,
    DEC_MATCHED_SLUG,
    DEC_RELEVANCE_REASON,
    DEC_SCORE,
    PAYMENT_INTENT,
    PI_BILLING_PERIOD,
    PI_CAPTURED_AT,
    PI_TIER,
    FB_CATEGORY,
    FB_CREATED_AT,
    FB_QUOTE,
    FB_SEVERITY,
    FB_SUMMARY,
    FB_USERNAME,
    MESSAGES,
    MSG_CHANNEL,
    MSG_CREATED_AT,
    MSG_ROLE,
    MSG_ROLE_USER,
    MSG_TEXT,
    NOTIF_BODY,
    NOTIF_CATEGORY,
    NOTIF_DECISION,
    NOTIF_OUTCOME,
    NOTIF_SENT_AT,
    NOTIF_SOURCE,
    NOTIF_STATUS,
    NOTIF_TIME_TO_TAP,
    NOTIF_TITLE,
    NOTIFICATIONS,
    OBSERVED_FEEDBACK,
    USER_AURA_CONSENT,
    USER_CREATED_AT,
    USER_DISPLAY_NAME,
    USER_EMAIL,
    USER_IS_ACTIVE,
    USER_LAST_ACTIVE_AT,
    USER_LAST_LOGIN_AT,
    USER_LOGIN_COUNT,
    USER_PLATFORM,
    USER_SIGN_IN_METHOD,
    USERS,
    VOICE_NUM_TURNS,
    VOICE_SESSIONS,
    VOICE_STARTED_AT,
    VOICE_SUMMARY,
    VOICE_TOTAL_DURATION,
)

logger = logging.getLogger("ops.firestore")


def _db():
    return firestore.client()


def _to_datetime(value: Any) -> datetime | None:
    """Parse a Firestore value into a tz-aware UTC datetime.

    Handles both shapes the app writes: Timestamp fields come back as tz-aware
    datetimes (message.created_at), ISO-8601 strings for the rest (voice.started_at,
    user.last_login_at). Anything unparseable returns None so a single bad doc never
    crashes a panel.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def _start_of_today(now: datetime, utc_offset_hours: float) -> datetime:
    """Midnight of 'today' in the dashboard's configured offset, expressed back in UTC.

    'Today' is ambiguous when the founder and the users span time zones; OPS_UTC_OFFSET_HOURS
    makes the day boundary explicit (default 0 = UTC). All stored timestamps are UTC.
    """
    local = now + timedelta(hours=utc_offset_hours)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight - timedelta(hours=utc_offset_hours)


def load_user_directory() -> dict[str, dict]:
    """Read every users/{uid} doc once so panels can map uid -> name without N lookups.

    Callers must fetch this ONCE per dashboard build and pass it into every panel
    function below — each panel used to call this independently (4x per request,
    ~4x the users-collection read cost for no reason). Fine at beta scale (tens of
    users). At thousands, cache this with a short TTL.
    """
    try:
        return {doc.id: (doc.to_dict() or {}) for doc in _db().collection(USERS).stream()}
    except Exception as exc:
        logger.error("user directory read failed: %s", exc)
        return {}


def _display_name(uid: str, users: dict[str, dict]) -> str:
    user = users.get(uid, {})
    return user.get(USER_DISPLAY_NAME) or user.get(USER_EMAIL) or uid[:6]


def _uid_from_message_ref(ref) -> str:
    # users/{uid}/chat_sessions/{sid}/messages/{mid} -> users/{uid}
    return ref.parent.parent.parent.parent.id


def _uid_from_voice_ref(ref) -> str:
    # users/{uid}/voice_sessions/{sid} -> users/{uid}
    return ref.parent.parent.id


def latest_text_messages(users: dict[str, dict], limit: int = 60) -> list[dict]:
    """Newest user-authored text messages across all users (left column).

    Read cost is the reason this has two paths. The PREFERRED path filters
    role == "user" server-side (exactly `limit` doc reads); it needs the
    composite COLLECTION_GROUP index on messages (role ASC, created_at DESC)
    declared in firestore.indexes.json. Until that index is deployed/built,
    the query 400s and we FALL BACK to the legacy shape: order the whole
    collection group by created_at and over-fetch 3x to fill `limit` after the
    client-side role filter (3x the reads, the old sticker price). The
    fallback self-retires the moment the index goes live.
    """
    try:
        docs = list(
            _db()
            .collection_group(MESSAGES)
            .where(filter=FieldFilter(MSG_ROLE, "==", MSG_ROLE_USER))
            .order_by(MSG_CREATED_AT, direction=Query.DESCENDING)
            .limit(limit)
            .stream()
        )
    except Exception as exc:
        logger.warning(
            "latest_text_messages composite path unavailable (deploy the "
            "messages role+created_at COLLECTION_GROUP index to cut reads 3x); "
            "falling back to over-fetch: %s", exc,
        )
        docs = None

    if docs is None:
        try:
            docs = list(
                _db()
                .collection_group(MESSAGES)
                .order_by(MSG_CREATED_AT, direction=Query.DESCENDING)
                .limit(limit * 3)
                .stream()
            )
        except Exception as exc:
            logger.error(
                "latest_text_messages failed (missing COLLECTION_GROUP index on %s.%s?): %s",
                MESSAGES, MSG_CREATED_AT, exc,
            )
            return []

    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get(MSG_ROLE) != MSG_ROLE_USER:
            continue
        uid = _uid_from_message_ref(doc.reference)
        out.append({
            "uid": uid,
            "name": _display_name(uid, users),
            "text": data.get(MSG_TEXT, ""),
            "channel": data.get(MSG_CHANNEL, "text"),
            "at": _iso(_to_datetime(data.get(MSG_CREATED_AT))),
        })
        if len(out) >= limit:
            break
    return out


def latest_voice_sessions(users: dict[str, dict], limit: int = 30) -> list[dict]:
    """Newest voice sessions across all users (right column).

    REQUIRES a COLLECTION_GROUP index on voice_sessions.started_at (DESC).
    """
    try:
        docs = list(
            _db()
            .collection_group(VOICE_SESSIONS)
            .order_by(VOICE_STARTED_AT, direction=Query.DESCENDING)
            .limit(limit)
            .stream()
        )
    except Exception as exc:
        logger.error(
            "latest_voice_sessions failed (missing COLLECTION_GROUP index on %s.%s?): %s",
            VOICE_SESSIONS, VOICE_STARTED_AT, exc,
        )
        return []

    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        uid = _uid_from_voice_ref(doc.reference)
        out.append({
            "uid": uid,
            "name": _display_name(uid, users),
            "summary": data.get(VOICE_SUMMARY, ""),
            "duration": data.get(VOICE_TOTAL_DURATION, ""),
            "turns": data.get(VOICE_NUM_TURNS, 0),
            "at": _iso(_to_datetime(data.get(VOICE_STARTED_AT))),
        })
    return out


def user_metrics_and_table(users: dict[str, dict], now: datetime, utc_offset_hours: float = 0.0) -> dict:
    """Top-strip counts + the scroll-down per-user table, from the shared users read.

    signins_today counts users whose last_login_at is today AND who were NOT created
    today (your "signins today, not new"). active_today uses last_active_at, which the
    app refreshes on every silent session restore, not just explicit logins.
    """
    start = _start_of_today(now, utc_offset_hours)

    new_today = signins_today = active_today = 0
    table: list[dict] = []
    for uid, data in users.items():
        created = _to_datetime(data.get(USER_CREATED_AT))
        last_login = _to_datetime(data.get(USER_LAST_LOGIN_AT))
        last_active = _to_datetime(data.get(USER_LAST_ACTIVE_AT))

        is_new = created is not None and created >= start
        if is_new:
            new_today += 1
        if last_login is not None and last_login >= start and not is_new:
            signins_today += 1
        if last_active is not None and last_active >= start:
            active_today += 1

        table.append({
            "uid": uid,
            "name": data.get(USER_DISPLAY_NAME) or data.get(USER_EMAIL) or uid[:6],
            "email": data.get(USER_EMAIL, ""),
            "last_login": _iso(last_login),
            "last_active": _iso(last_active),
            "login_count": data.get(USER_LOGIN_COUNT, 0),
            "is_active": bool(data.get(USER_IS_ACTIVE, False)),
            "platform": data.get(USER_SIGN_IN_METHOD, "") or data.get(USER_PLATFORM, ""),
            "aura_consent": bool(data.get(USER_AURA_CONSENT, False)),
        })

    table.sort(key=lambda row: row["last_active"], reverse=True)
    return {
        "metrics": {
            "total_users": len(users),
            "new_today": new_today,
            "signins_today": signins_today,
            "active_today": active_today,
        },
        "users": table,
    }


def messages_today_count(now: datetime, utc_offset_hours: float = 0.0) -> int | None:
    """Count of all messages created today via an aggregation query (cheap, no doc reads).

    Returns None (not 0) if the query errors, so the UI can distinguish "no index / error"
    from a real zero. Uses the same COLLECTION_GROUP index as latest_text_messages.
    """
    start = _start_of_today(now, utc_offset_hours)
    try:
        agg = (
            _db()
            .collection_group(MESSAGES)
            .where(filter=FieldFilter(MSG_CREATED_AT, ">=", start))
            .count()
        )
        return int(agg.get()[0][0].value)
    except Exception as exc:
        logger.error("messages_today_count failed: %s", exc)
        return None


def recent_feedback(limit: int = 20) -> list[dict]:
    """Newest observed_feedback docs (scroll-down panel). Top-level collection, no CG index."""
    try:
        docs = list(
            _db()
            .collection(OBSERVED_FEEDBACK)
            .order_by(FB_CREATED_AT, direction=Query.DESCENDING)
            .limit(limit)
            .stream()
        )
    except Exception as exc:
        logger.error("recent_feedback failed: %s", exc)
        return []

    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        out.append({
            "summary": data.get(FB_SUMMARY, ""),
            "quote": data.get(FB_QUOTE, ""),
            "category": data.get(FB_CATEGORY, ""),
            "severity": data.get(FB_SEVERITY, ""),
            "username": data.get(FB_USERNAME, ""),
            "at": _iso(_to_datetime(data.get(FB_CREATED_AT))),
        })
    return out


def _humanize_seconds(seconds: float | None) -> str:
    """A tap latency as a person would say it: '8s', '3m', '1h'."""
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def _recommendation_reason(decision: dict | None) -> str:
    """One plain sentence for WHY the recommender picked this, written the way a
    person would explain it, not a metric dump.

    The framer already wrote a human relevance_reason ("they follow KCR"); we lead
    with that. Falls back to the matched interest, then to a neutral line. The raw
    score rides along separately as a small tag, so this stays readable prose.
    """
    if not decision:
        return "Sent on a schedule (reminder or calendar), not a recommendation."
    if decision.get(DEC_LANE) == "breaking":
        return "A big story worth seeing even if it is not their usual thing."
    reason = str(decision.get(DEC_RELEVANCE_REASON, "") or "").strip()
    if reason:
        return f"Matched what they care about: {reason}"
    matched = str(decision.get(DEC_MATCHED_SLUG, "") or "").strip()
    if matched:
        pretty = matched.replace("_", " ")
        return f"Closest match to their interest in {pretty}."
    return "Best available match for this person right now."


def _recommendation_outcome(data: dict) -> str:
    """What the person did with it, in plain words."""
    if data.get(NOTIF_STATUS) == "failed":
        return "Delivery failed"
    outcome = str(data.get(NOTIF_OUTCOME, "") or "")
    if outcome == "opened":
        latency = _humanize_seconds(data.get(NOTIF_TIME_TO_TAP))
        return f"Opened after {latency}" if latency else "Opened"
    if outcome == "dismissed":
        return "Swiped away"
    if outcome == "timeout":
        return "No tap (gave up after 6h)"
    return "No tap yet"


def _notification_row(uid: str, users: dict[str, dict], data: dict) -> dict:
    decision = data.get(NOTIF_DECISION) or None
    score = decision.get(DEC_SCORE) if isinstance(decision, dict) else None
    sent_dt = _to_datetime(data.get(NOTIF_SENT_AT))
    return {
        "uid": uid,
        "name": _display_name(uid, users),
        "title": data.get(NOTIF_TITLE, ""),
        "body": data.get(NOTIF_BODY, ""),
        "category": data.get(NOTIF_CATEGORY, ""),
        "source": data.get(NOTIF_SOURCE, ""),
        "score": round(float(score), 2) if isinstance(score, (int, float)) else None,
        "reason": _recommendation_reason(decision if isinstance(decision, dict) else None),
        "outcome": _recommendation_outcome(data),
        "at": _iso(sent_dt),
        "_sort": sent_dt or datetime.min.replace(tzinfo=timezone.utc),
    }


def latest_notifications(users: dict[str, dict], per_user: int = 6, total: int = 50) -> list[dict]:
    """Newest notifications across all users — the recommendation trace: what each
    person was actually sent, why the recommender chose it, and whether it landed.

    PREFERRED path: ONE collection_group query ordered by sent_at (exactly
    `total` doc reads, one round trip). Needs the notifications.sent_at
    COLLECTION_GROUP field override in firestore.indexes.json. Until that
    override is deployed the query 400s and we FALL BACK to the legacy per-user
    fan-out (O(users) queries per load — the exact discovery anti-pattern the
    Read Discipline rules exist for, kept only as a bridge; it self-retires
    once the index is live). The ledger self-purges on a 90-day TTL either way,
    so the dashboard adds zero growth.
    """
    try:
        snaps = list(
            _db()
            .collection_group(NOTIFICATIONS)
            .order_by(NOTIF_SENT_AT, direction=Query.DESCENDING)
            .limit(total)
            .stream()
        )
        rows = [
            _notification_row(doc.reference.parent.parent.id, users, doc.to_dict() or {})
            for doc in snaps
        ]
        for r in rows:
            r.pop("_sort", None)
        return rows
    except Exception as exc:
        logger.warning(
            "latest_notifications collection_group path unavailable (deploy the "
            "notifications.sent_at COLLECTION_GROUP override to cut this to one "
            "query); falling back to per-user fan-out: %s", exc,
        )

    rows = []
    for uid in users:
        try:
            snaps = list(
                _db()
                .collection(USERS).document(uid)
                .collection(NOTIFICATIONS)
                .order_by(NOTIF_SENT_AT, direction=Query.DESCENDING)
                .limit(per_user)
                .stream()
            )
        except Exception as exc:
            logger.error("latest_notifications read failed for %s: %s", uid[:6], exc)
            continue
        rows.extend(_notification_row(uid, users, doc.to_dict() or {}) for doc in snaps)

    rows.sort(key=lambda r: r["_sort"], reverse=True)
    for r in rows:
        r.pop("_sort", None)
    return rows[:total]


def payment_intents(users: dict[str, dict]) -> list[dict]:
    """Every captured paywall interest across all users (beta interest-capture
    writes, see fields.py PAYMENT_INTENT block). One BARE collection-group
    stream: no field filter or order, so it needs NO composite index or field
    override (Read Discipline: one query, never a per-user loop). The
    collection is tiny by construction — at most one doc per tier+period per
    user — and sorting happens in memory."""
    try:
        docs = list(_db().collection_group(PAYMENT_INTENT).stream())
    except Exception as exc:
        logger.error("payment_intents read failed: %s", exc)
        return []

    rows: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        # users/{uid}/payment_intent/{tier}_{period} -> users/{uid}
        uid = doc.reference.parent.parent.id
        captured = _to_datetime(data.get(PI_CAPTURED_AT))
        rows.append({
            "uid": uid,
            "name": _display_name(uid, users),
            "tier": str(data.get(PI_TIER, "") or ""),
            "period": str(data.get(PI_BILLING_PERIOD, "") or ""),
            "at": _iso(captured),
            "_sort": captured or datetime.min.replace(tzinfo=timezone.utc),
        })
    rows.sort(key=lambda r: r["_sort"], reverse=True)
    for r in rows:
        r.pop("_sort", None)
    return rows
