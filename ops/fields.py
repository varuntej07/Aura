"""Firestore collection + field names the dashboard reads.

Single source of truth for the ops service, mirroring the app/backend WRITERS so a
rename on either side is caught in one place (the data-discipline rule in CLAUDE.md:
the writer and every reader reference the same string). Each block cites the writer it
mirrors. If you rename a field there, rename it here, and the dashboard panel keeps
working instead of silently returning zero rows.

These intentionally duplicate the backend constants rather than importing them: the ops
service deploys as its own Cloud Run image and must not pull in `backend/src`. The cost
of that isolation is this file; keep it honest against the cited writers.
"""

# ── users/{uid} ───────────────────────────────────────────────────────────────
# Writer: lib/data/repositories/auth_repository.dart (_loginMetadataFields,
# _getOrCreateUser) + the onboarding/consent flow.
USERS = "users"
USER_DISPLAY_NAME = "display_name"
USER_EMAIL = "email"
USER_CREATED_AT = "created_at"
USER_LAST_LOGIN_AT = "last_login_at"
USER_LAST_ACTIVE_AT = "last_active_at"
USER_LOGIN_COUNT = "login_count"
USER_IS_ACTIVE = "is_active"
USER_SIGN_IN_METHOD = "sign_in_method"
USER_PLATFORM = "platform"
USER_TIMEZONE = "timezone"
USER_AURA_CONSENT = "aura_consent_granted"

# ── users/{uid}/chat_sessions/{sid}/messages/{mid} ───────────────────────────
# Writer: lib/data/services/chat_backup_service.dart (_messageDoc). uid is NOT a
# field on the doc — it is the grandparent doc id, recovered from the reference path.
CHAT_SESSIONS = "chat_sessions"
MESSAGES = "messages"
MSG_TEXT = "text"
MSG_ROLE = "role"               # "user" | "assistant"
MSG_CHANNEL = "channel"         # "text" | "voice"
MSG_CREATED_AT = "created_at"   # Firestore Timestamp
MSG_ROLE_USER = "user"

# ── users/{uid}/voice_sessions/{sid} ─────────────────────────────────────────
# Writer: backend/src/services/voice_session_summarizer.py (_write_session_doc).
# uid recovered from the reference path (not a field).
VOICE_SESSIONS = "voice_sessions"
VOICE_STARTED_AT = "started_at"      # ISO-8601 string
VOICE_SUMMARY = "summary"
VOICE_TOTAL_DURATION = "total_duration"
VOICE_NUM_TURNS = "num_of_turns"
VOICE_ARCHIVED = "archived"

# ── observed_feedback/{id} (top-level collection) ────────────────────────────
# Writer: backend/src/services/feedback/feedback_schema.py (build_feedback_document).
OBSERVED_FEEDBACK = "observed_feedback"
FB_SUMMARY = "summary"
FB_QUOTE = "verbatim_quote"
FB_CATEGORY = "category"
FB_SEVERITY = "severity"
FB_SOURCE = "source"
FB_CREATED_AT = "created_at"
FB_USERNAME = "username"

# ── users/{uid}/notifications/{notification_id} ──────────────────────────────
# Writer: backend/src/services/notification_ledger.py (record_send / record_tap).
# One durable row per notification across every send path. This is the "what did
# this user actually get recommended, and did they tap it" trace. The `decision`
# sub-map is filled only on LLM-framed proactive sends (the signal engine): the
# score the recommender gave it, the framer's plain-language reason, and the lane.
# Reminders / calendar leave `decision` null. uid is the grandparent doc id, not a
# field. Rows self-purge on a 90-day Firestore TTL (expires_at), so this collection
# never grows unbounded — the dashboard adds no writes of its own.
NOTIFICATIONS = "notifications"
NOTIF_TYPE = "type"
NOTIF_ORIGIN = "origin"            # "signal_engine" | "reminder" | "thread_engine" | ...
NOTIF_TITLE = "title"
NOTIF_BODY = "body"
NOTIF_CATEGORY = "category"
NOTIF_SOURCE = "source"            # the content source (HN, arXiv, Google News, ...)
NOTIF_SENT_AT = "sent_at"          # Firestore Timestamp; single-field, auto-indexed
NOTIF_STATUS = "status"            # "sent" | "failed"
NOTIF_OUTCOME = "outcome"          # "pending" | "opened" | "dismissed" | "timeout"
NOTIF_TIME_TO_TAP = "time_to_tap_seconds"
NOTIF_DECISION = "decision"        # nested map, present only on framed proactive sends
# decision.* sub-keys (mirror NotificationDecision in notification_ledger.py)
DEC_SCORE = "score"
DEC_RELEVANCE_REASON = "relevance_reason"
DEC_MATCHED_SLUG = "matched_interest_slug"
DEC_LANE = "lane"                  # "" for the normal personal lane, "breaking" for lane B

# ── users/{uid}/payment_intent/{tier}_{period} ───────────────────────────────
# Writer: lib/data/services/subscription_service.dart (captureInterest, L191-200).
# Beta interest-capture: one doc per tier+period the user tapped on the paywall
# (doc id "companion_monthly", "pro_annual", ...). NOTE the real field names are
# billing_period and captured_at — NOT period/timestamp.
PAYMENT_INTENT = "payment_intent"
PI_TIER = "tier"                    # "companion" | "pro"
PI_BILLING_PERIOD = "billing_period"  # "monthly" | "annual"
PI_CAPTURED_AT = "captured_at"      # Firestore server Timestamp
