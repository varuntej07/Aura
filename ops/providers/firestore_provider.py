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

# Mirrors static/time.js FUTURE_TOLERANCE_SECONDS. A shared convention, stated in
# both places rather than assumed: a timestamp more than this far ahead of now is
# a broken client clock, not activity.
FUTURE_TOLERANCE_SECONDS = 300

from firebase_admin import firestore
from google.cloud.firestore_v1 import Query
from google.cloud.firestore_v1.base_query import FieldFilter

from fields import (
    COST,
    COST_EST_MICROUSD,
    COST_LLM_CACHED_INPUT_TOKENS,
    COST_LLM_GENERATIONS,
    COST_LLM_INPUT_TOKENS,
    COST_LLM_OUTPUT_TOKENS,
    LD_DEVICE_NAME,
    LD_INSTALL_ID,
    LD_LAST_SEEN_AT,
    LD_LINKED_AT,
    LD_PLATFORM,
    LINKED_DEVICES,
    USER_LAST_DESKTOP_ACTIVE_AT,
    USER_LINKED_PLATFORMS,
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


def _owner_uid(ref) -> str:
    """The users/{uid} doc id that owns a collection-group hit.

    None of the subcollection docs store uid as a field: it is always an
    ancestor doc id, so it is recovered by walking the reference up to the
    `users` collection. Walking by NAME rather than by a fixed number of
    .parent hops means one helper serves every depth the dashboard reads —
    voice_sessions and notifications sit two levels up, chat messages four —
    and a doc that moves deeper later does not silently start reporting a
    session id as a uid.
    """
    node = ref
    while node is not None:
        parent_collection = node.parent
        if parent_collection is not None and parent_collection.id == USERS:
            return node.id
        node = parent_collection.parent if parent_collection is not None else None
    return ""


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
        uid = _owner_uid(doc.reference)
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
        uid = _owner_uid(doc.reference)
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
    app refreshes on every silent session restore, not just explicit logins, so it
    means "opened the app", NOT "talked to Buddy".

    Every count is bounded on BOTH sides of today: a device with a wrong clock
    writes a future last_active_at and would otherwise count as active forever.
    Those users are reported in the `future_stamped` cohort instead of being
    silently discarded.

    Alongside the counts this returns the uid SET behind each one, because a count
    nobody can expand is a count nobody can act on.
    """
    start = _start_of_today(now, utc_offset_hours)
    # A timestamp cannot legitimately be in the future. The client writes
    # last_active_at from its OWN clock (auth_repository.dart), so a device with
    # a wrong date stamps a date days ahead and then counts as "active today"
    # forever after. The message feeds already discard future timestamps using
    # this same 300s tolerance (static/time.js FUTURE_TOLERANCE_SECONDS); the
    # counts did not, which silently inflated "active today". They are excluded
    # from the counts and surfaced separately, never dropped without a trace.
    horizon = now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS)

    def _within_today(value: datetime | None) -> bool:
        return value is not None and start <= value <= horizon

    new_today = signins_today = active_today = 0
    table: list[dict] = []
    for uid, data in users.items():
        created = _to_datetime(data.get(USER_CREATED_AT))
        last_login = _to_datetime(data.get(USER_LAST_LOGIN_AT))
        last_active = _to_datetime(data.get(USER_LAST_ACTIVE_AT))

        future_stamped = last_active is not None and last_active > horizon
        is_new = _within_today(created)
        signed_in = _within_today(last_login) and not is_new
        is_active = _within_today(last_active)
        new_today += is_new
        signins_today += signed_in
        active_today += is_active

        platforms = data.get(USER_LINKED_PLATFORMS)
        table.append({
            "uid": uid,
            "name": data.get(USER_DISPLAY_NAME) or data.get(USER_EMAIL) or uid[:6],
            "email": data.get(USER_EMAIL, ""),
            "created_at": _iso(created),
            "last_login": _iso(last_login),
            "last_active": _iso(last_active),
            "login_count": data.get(USER_LOGIN_COUNT, 0),
            "is_active": bool(data.get(USER_IS_ACTIVE, False)),
            "platform": data.get(USER_SIGN_IN_METHOD, "") or data.get(USER_PLATFORM, ""),
            "linked_platforms": [str(p) for p in platforms] if isinstance(platforms, list) else [],
            "last_desktop_active": _iso(_to_datetime(data.get(USER_LAST_DESKTOP_ACTIVE_AT))),
            "aura_consent": bool(data.get(USER_AURA_CONSENT, False)),
            # Which of the top-strip counts this row is BEHIND. The dashboard used
            # to render only the counts, so "6 active today" named nobody and the
            # founder could not tell who they were. Every count now ships the set
            # it counted, from the same single users read.
            "new_today": bool(is_new),
            "signed_in_today": bool(signed_in),
            "active_today": bool(is_active),
            "future_stamped": bool(future_stamped),
        })

    table.sort(key=lambda row: row["last_active"], reverse=True)
    return {
        "metrics": {
            "total_users": len(users),
            "new_today": new_today,
            "signins_today": signins_today,
            "active_today": active_today,
        },
        # The membership lists behind the counts, so the UI can answer "who?"
        # without a second read. active_today means last_active_at moved today,
        # and its writer (auth_repository.dart) fires on silent session restore
        # as well as explicit sign-in: it means "opened the app", NOT "talked to
        # Buddy". The UI derives the stronger "talked today" set from the message
        # and voice feeds it already has.
        "cohorts": {
            "active_today": [r["uid"] for r in table if r["active_today"]],
            "future_stamped": [r["uid"] for r in table if r["future_stamped"]],
            "new_today": [r["uid"] for r in table if r["new_today"]],
            "signins_today": [r["uid"] for r in table if r["signed_in_today"]],
            "total_users": [r["uid"] for r in table],
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
            _notification_row(_owner_uid(doc.reference), users, doc.to_dict() or {})
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


def payment_intents(users: dict[str, dict], limit: int = 500) -> list[dict]:
    """Every captured paywall interest across all users (beta interest-capture
    writes, see fields.py PAYMENT_INTENT block). One BARE collection-group
    stream: no field filter or order, so it needs NO composite index or field
    override (Read Discipline: one query, never a per-user loop). The
    collection is tiny by construction — at most one doc per tier+period per
    user — and sorting happens in memory. The `limit` is a backstop, not a
    product rule: the bound is structural, but an unbounded stream in code is
    a promise nothing enforces."""
    try:
        docs = list(_db().collection_group(PAYMENT_INTENT).limit(limit).stream())
    except Exception as exc:
        logger.error("payment_intents read failed: %s", exc)
        return []

    rows: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        uid = _owner_uid(doc.reference)
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


def desktop_installs(users: dict[str, dict], now: datetime, limit: int = 1000) -> dict:
    """Real desktop installs: one row per installation that reached a signed-in state.

    This exists because GitHub's per-asset `download_count` is not an install
    count and cannot be made into one. It is a raw HTTP counter incremented by
    crawlers, security scanners, and, decisively, the Tauri updater re-fetching
    the same .msi on every auto-update of every EXISTING install. That is how the
    dashboard could show 10 "downloads" against 0 users with both numbers correct.

    `users/{uid}/linked_devices/{install_id}` is the honest denominator: the
    backend writes exactly one doc per installation that completed pairing or
    web-auth (backend/src/services/linked_devices.py), keyed by the client's own
    install_id, so re-running the same install does not double count and an
    installer that was downloaded but never opened does not count at all.

    ONE bare collection_group stream: no field filter, no order, therefore no
    composite index and no COLLECTION_GROUP override (the same discipline
    payment_intents follows). Sorting and the 7-day window are applied in memory
    over a bounded row set.
    """
    empty = {
        "available": False, "installs": 0, "active_7d": 0,
        "users_with_desktop": 0, "by_platform": {}, "devices": [],
    }
    try:
        docs = list(_db().collection_group(LINKED_DEVICES).limit(limit).stream())
    except Exception as exc:
        logger.error("desktop_installs read failed: %s", exc)
        return empty

    week_ago = now - timedelta(days=7)
    by_platform: dict[str, int] = {}
    rows: list[dict] = []
    active_7d = 0
    for doc in docs:
        data = doc.to_dict() or {}
        uid = _owner_uid(doc.reference)
        platform = str(data.get(LD_PLATFORM) or "unknown")
        by_platform[platform] = by_platform.get(platform, 0) + 1
        last_seen = _to_datetime(data.get(LD_LAST_SEEN_AT))
        if last_seen is not None and last_seen >= week_ago:
            active_7d += 1
        rows.append({
            "uid": uid,
            "name": _display_name(uid, users),
            "install_id": str(data.get(LD_INSTALL_ID) or doc.id),
            "device_name": str(data.get(LD_DEVICE_NAME) or ""),
            "platform": platform,
            "linked_at": _iso(_to_datetime(data.get(LD_LINKED_AT))),
            "last_seen": _iso(last_seen),
            "_sort": last_seen or datetime.min.replace(tzinfo=timezone.utc),
        })

    rows.sort(key=lambda row: row["_sort"], reverse=True)
    for row in rows:
        row.pop("_sort", None)

    # Cross-check against the root-doc footprint. linked_platforms is written by
    # a DIFFERENT path (the array-union in desktop_profile / pairing / web_auth),
    # so a mismatch between these two numbers means one writer is failing rather
    # than being a rounding difference. Both are shown, never reconciled silently.
    mobile_only = {"android", "ios", "web"}
    users_with_desktop = sum(
        1 for data in users.values()
        if isinstance(data.get(USER_LINKED_PLATFORMS), list)
        and any(str(platform) not in mobile_only for platform in data[USER_LINKED_PLATFORMS])
    )

    return {
        "available": True,
        "installs": len(rows),
        "active_7d": active_7d,
        "users_with_desktop": users_with_desktop,
        "by_platform": by_platform,
        "devices": rows[:200],
    }


def llm_spend(users: dict[str, dict], now: datetime, days: int = 7) -> dict:
    """Real LLM spend from the per-user daily ledger the backend already writes.

    The Costs tab used to claim "usage tracking intentionally disabled" for
    anthropic/gemini/openai. That has been false since 2026-08: every backend LLM
    call merge-increments `users/{uid}/cost/{YYYY-MM-DD}` with generations,
    input/cached/output tokens and estimated microUSD
    (backend/src/services/analytics/llm_cost_ledger.py, schema in cost_doc.py),
    whether or not Langfuse is configured.

    Read shape: the doc id IS the UTC date, so this needs no query at all. It
    builds the exact document paths for (every user x every day in the window)
    and issues ONE batched get_all(). No index, no collection-group scan, one
    round trip, and a day with no activity simply comes back non-existent rather
    than costing anything. At beta scale that is 16 users x 7 days = 112 refs in
    a single RPC. The window is capped at 30 days so the ref count stays bounded;
    at thousands of users this should read a rollup the backend writes instead.

    HONEST LIMIT: the ledger stores no model field (estimate_microusd folds the
    model into the dollar amount), so this gives real TOTAL and PER-USER spend but
    cannot give a per-model claude/gemini/gpt split. The Costs tab links out to
    each provider's own console for that.
    """
    window_days = max(1, min(int(days), 30))
    empty = {
        "available": False, "days": window_days, "est_usd": None,
        "generations": 0, "input_tokens": 0, "cached_input_tokens": 0,
        "output_tokens": 0, "docs_found": 0, "daily": [], "by_user": [],
    }
    if not users:
        return empty

    dates = [(now - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(window_days)]
    db = _db()
    refs = [
        db.collection(USERS).document(uid).collection(COST).document(date)
        for uid in users
        for date in dates
    ]
    try:
        snapshots = list(db.get_all(refs))
    except Exception as exc:
        logger.error("llm_spend batch read failed (%d refs): %s", len(refs), exc)
        return empty

    microusd_total = generations_total = 0
    input_total = cached_total = output_total = 0
    per_day: dict[str, float] = {date: 0.0 for date in dates}
    per_user: dict[str, dict] = {}
    present = 0

    for snapshot in snapshots:
        if not getattr(snapshot, "exists", False):
            continue
        present += 1
        data = snapshot.to_dict() or {}
        uid = _owner_uid(snapshot.reference)
        microusd = int(data.get(COST_EST_MICROUSD) or 0)
        generations = int(data.get(COST_LLM_GENERATIONS) or 0)
        input_tokens = int(data.get(COST_LLM_INPUT_TOKENS) or 0)
        cached_tokens = int(data.get(COST_LLM_CACHED_INPUT_TOKENS) or 0)
        output_tokens = int(data.get(COST_LLM_OUTPUT_TOKENS) or 0)

        microusd_total += microusd
        generations_total += generations
        input_total += input_tokens
        cached_total += cached_tokens
        output_total += output_tokens
        per_day[snapshot.id] = round(per_day.get(snapshot.id, 0.0) + microusd / 1e6, 6)

        row = per_user.setdefault(uid, {
            "uid": uid, "name": _display_name(uid, users),
            "est_usd": 0.0, "generations": 0, "tokens": 0,
        })
        row["est_usd"] = round(row["est_usd"] + microusd / 1e6, 6)
        row["generations"] += generations
        # Cached prompt tokens are a separate field, not a subset of input_tokens,
        # so all three are summed for a true per-user token total.
        row["tokens"] += input_tokens + cached_tokens + output_tokens

    # A ledger that exists but recorded nothing this window is a real, useful
    # zero. A ledger with NO documents at all means the backend is not writing it.
    # Those two must never render identically, so `available` carries the
    # distinction and `docs_found` shows the evidence.
    by_user = sorted(per_user.values(), key=lambda row: row["est_usd"], reverse=True)
    return {
        "available": present > 0,
        "days": window_days,
        "est_usd": round(microusd_total / 1e6, 4),
        "generations": generations_total,
        "input_tokens": input_total,
        "cached_input_tokens": cached_total,
        "output_tokens": output_total,
        "docs_found": present,
        "daily": [{"day": date, "est_usd": per_day.get(date, 0.0)} for date in sorted(per_day)],
        "by_user": by_user[:25],
    }
