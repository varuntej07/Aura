"""
Single source of truth for the re-engagement notification funnel contract.

The funnel spans a server writer (the scoring loop emits NOTIFICATION_SENT) and
three client writers in the Flutter app (tap / session / action). PostHog can
only join the four steps into one funnel if both sides use byte-identical event
names, property keys, and the signal-engine origin value.

This module is mirrored by ``lib/core/analytics/funnel_events.dart``. Keep the two
files in sync — ``backend/tests/test_funnel_event_contract.py`` fails CI if either
side drifts, so a rename can never silently flatten the funnel (the exact
"zero rows looks like healthy" failure mode this project has been bitten by).
"""

from __future__ import annotations

# --- Funnel event names (the four ordered steps) ---
# NOTIFICATION_TAPPED reuses the app's existing generic tap event; 
# the funnel filters it to signal-engine taps via NOTIFICATION_ORIGIN. 
# The other three are dedicated funnel events.
EVENT_NOTIFICATION_SENT = "signal_notification_sent"
EVENT_NOTIFICATION_TAPPED = "notification_tapped"
EVENT_SESSION_FROM_NOTIFICATION = "signal_session_from_notification"
EVENT_ACTION_AFTER_NOTIFICATION = "signal_action_after_notification"

# Read-path terminal. A signal notification whose content_kind is "read" opens the
# source article in an in-app browser instead of chat, so the discuss-path action
# step never fires. EVENT_CONTENT_OPENED is the equivalent conversion for the read
# path — the user engaged with the content — so "tapped and read" is measurable,
# not invisible. Fired client-side when the in-app browser is launched.
EVENT_CONTENT_OPENED = "content_opened"

# --- Shared property keys (the join keys across server and client) ---
# These intentionally match the FCM data-payload keys set in the scoring loop,
# so a notification's analytics properties and its push payload agree.
PROP_NOTIFICATION_ID = "notification_id"
PROP_CONTENT_ID = "content_id"
PROP_CATEGORY = "category"
PROP_NOTIFICATION_ORIGIN = "notification_origin"

# Firebase uid stamped onto the client tap event. The server already keys its
# capture on this uid (as the PostHog distinct_id), so the client carries it as
# a property too — that keeps the funnel join independent of the client's
# identify() timing on a cold-launch tap.
PROP_FIREBASE_UID = "firebase_uid"

# --- Origin value identifying signal-engine notifications ---
NOTIFICATION_ORIGIN_SIGNAL_ENGINE = "signal_engine"

# --- Thread (curiosity follow-up) funnel ---
# Mirrors the signal funnel for the open-loop thread path. Step 2 reuses the
# generic EVENT_NOTIFICATION_TAPPED, filtered to thread taps via
# NOTIFICATION_ORIGIN_THREAD_ENGINE. EVENT_THREAD_REPLY (the action step) fires
# server-side for a silent shade reply and client-side for an in-chat reply, so
# both ways of answering count toward the same conversion.
EVENT_THREAD_FOLLOWUP_SENT = "thread_followup_sent"
EVENT_THREAD_SESSION_FROM_NOTIFICATION = "thread_session_from_notification"
EVENT_THREAD_REPLY = "thread_reply"

# Join key for the thread funnel (analogous to PROP_NOTIFICATION_ID / CONTENT_ID).
PROP_THREAD_ID = "thread_id"

# Origin value identifying curiosity follow-up notifications.
NOTIFICATION_ORIGIN_THREAD_ENGINE = "thread_engine"

# --- Icebreaker (life-aware opener) funnel ---
# Mirrors the signal/thread funnels for the icebreaker path. Step 2 reuses the
# generic EVENT_NOTIFICATION_TAPPED, filtered to icebreaker taps via
# NOTIFICATION_ORIGIN_ICEBREAKER. EVENT_ICEBREAKER_REPLY (the action step) fires
# client-side when the user replies in the chat the tap opened.
EVENT_ICEBREAKER_SENT = "icebreaker_sent"
EVENT_ICEBREAKER_SESSION_FROM_NOTIFICATION = "icebreaker_session_from_notification"
EVENT_ICEBREAKER_REPLY = "icebreaker_reply"

# Origin value identifying icebreaker notifications.
NOTIFICATION_ORIGIN_ICEBREAKER = "icebreaker"

# --- Daily Briefing funnel ---
EVENT_BRIEFING_SENT = "daily_briefing_sent"
EVENT_BRIEFING_OPENED = "briefing_opened"
EVENT_BRIEFING_CHAT_STARTED = "briefing_chat_started"

# Origin value identifying daily-briefing notifications.
NOTIFICATION_ORIGIN_BRIEFING = "daily_briefing"

# On-demand "Catch me up on the world" snapshot. Fired CLIENT-side when the world
# snapshot loads in the briefing screen (the empty-state button or the refresh icon)
EVENT_WORLD_BRIEFING_FETCHED = "world_briefing_fetched"

# --- Buddy Keyboard funnel ---
# Acquisition/activation funnel for the Buddy Keyboard (BUDDY_EVERYWHERE.md). Most
# steps fire CLIENT-side (the native keyboard / in-app onboarding); the draft step
# ALSO fires SERVER-side from /keyboard/draft, so a served draft is counted even if
# the client capture is dropped. The properties are breakdown dimensions only
# (which action, which host app) and NEVER carry the user's typed content.
EVENT_KEYBOARD_ENABLED = "keyboard_enabled"
EVENT_KEYBOARD_FULL_ACCESS_GRANTED = "keyboard_full_access_granted"
EVENT_KEYBOARD_DRAFT_REQUESTED = "keyboard_draft_requested"
EVENT_KEYBOARD_SUGGESTION_INSERTED = "keyboard_suggestion_inserted"
EVENT_KEYBOARD_LIMIT_HIT = "keyboard_limit_hit"

PROP_KEYBOARD_ACTION = "action"
PROP_KEYBOARD_HOST_APP = "host_app"

# Field-type breakdown dimension stamped onto EVENT_KEYBOARD_DRAFT_REQUESTED so we
# can see which field classes drive drafts (text | email | url | number | phone |
# datetime | password). A breakdown only, never the user's typed content.
PROP_KEYBOARD_FIELD_TYPE = "field_type"

# Password helper + in-keyboard voice, fired CLIENT-side from the native keyboard.
# Both are content-free: the generated password is never sent anywhere, and the
# voice-started event carries no transcript or field content.
EVENT_KEYBOARD_PASSWORD_GENERATED = "keyboard_password_generated"
EVENT_KEYBOARD_VOICE_STARTED = "keyboard_voice_started"

# --- Desktop outbound-draft funnel ---
# Voice-triggered screen drafting on the desktop (Buddy Drafts). REQUESTED fires
# SERVER-side from the voice worker per new draft, REFINED fires server-side from
# the worker's refine branch and from POST /desktop/draft-outbound/refine, and
# LIMIT_HIT fires when a free-tier user runs out of daily drafts. The desktop
# client fires its own draft_card_copied / draft_card_dismissed steps. Properties
# are breakdown dimensions only (channel, length, mode) and NEVER carry the draft
# text, the context summary, or anything read off the user's screen.
EVENT_DESKTOP_DRAFT_REQUESTED = "desktop_draft_requested"
EVENT_DESKTOP_DRAFT_REFINED = "desktop_draft_refined"
EVENT_DESKTOP_DRAFT_LIMIT_HIT = "desktop_draft_limit_hit"

PROP_DRAFT_CHANNEL = "channel"
PROP_DRAFT_LENGTH = "length"
# "new" | "refine" on REQUESTED/REFINED; the chip slug or "custom"/"voice" on the
# refine event's instruction_kind breakdown.
PROP_DRAFT_MODE = "mode"
PROP_DRAFT_INSTRUCTION_KIND = "instruction_kind"

# --- Desktop client (Aura-Desktop) product events ---
# Every PostHog event the Tauri desktop client sends, mirrored by
# src/lib/analyticsEvents.ts in that repo (a TypeScript union, so a typo there
# fails its build). Listed here so the three repos agree on the names and a
# rename cannot silently split a dashboard. All of them are CLIENT-side and
# content-free: durations, counts, enum outcomes and version strings only,
# never chat text, transcripts, meeting content or anything read off screen.
# voice_session_* / voice_first_response / voice_error / chat_e2e_latency are
# shared with the Flutter app and keep the same names on both.
# desktop_heartbeat fires every 10 minutes from the main window while the app
# runs and carries subsystem health flags; client_log carries rate-limited,
# redacted WARN/ERROR log lines (same shape as the Flutter app_logger).
EVENT_DESKTOP_CLIENT_DESKTOP_APP_LAUNCHED = "desktop_app_launched"
EVENT_DESKTOP_CLIENT_DESKTOP_INSTALL_OBSERVED = "desktop_install_observed"
EVENT_DESKTOP_CLIENT_DESKTOP_ONBOARDING_STEP_COMPLETED = "desktop_onboarding_step_completed"
EVENT_DESKTOP_CLIENT_DESKTOP_ONBOARDING_COMPLETED = "desktop_onboarding_completed"
EVENT_DESKTOP_CLIENT_DESKTOP_TELEMETRY_CONSENT_ACCEPTED = "desktop_telemetry_consent_accepted"
EVENT_DESKTOP_CLIENT_DESKTOP_ONBOARDING_AUTH_PATH_SELECTED = "desktop_onboarding_auth_path_selected"
EVENT_DESKTOP_CLIENT_DESKTOP_PRIVACY_SETUP_SAVED = "desktop_privacy_setup_saved"
EVENT_DESKTOP_CLIENT_DESKTOP_HOTKEY_TEST_PASSED = "desktop_hotkey_test_passed"
EVENT_DESKTOP_CLIENT_DESKTOP_HOTKEY_TEST_SKIPPED = "desktop_hotkey_test_skipped"
EVENT_DESKTOP_CLIENT_DESKTOP_HOTKEY_TOUR_COMPLETED = "desktop_hotkey_tour_completed"
EVENT_DESKTOP_CLIENT_DESKTOP_AGENT_DEMO_STARTED = "desktop_agent_demo_started"
EVENT_DESKTOP_CLIENT_DESKTOP_AGENT_DEMO_FINISHED = "desktop_agent_demo_finished"
EVENT_DESKTOP_CLIENT_DESKTOP_AGENT_DEMO_TIMED_OUT = "desktop_agent_demo_timed_out"
EVENT_DESKTOP_CLIENT_DESKTOP_SIGN_IN_STARTED = "desktop_sign_in_started"
EVENT_DESKTOP_CLIENT_DESKTOP_SIGN_IN_COMPLETED = "desktop_sign_in_completed"
EVENT_DESKTOP_CLIENT_DESKTOP_SIGN_IN_FAILED = "desktop_sign_in_failed"
EVENT_DESKTOP_CLIENT_DESKTOP_SIGNED_OUT = "desktop_signed_out"
EVENT_DESKTOP_CLIENT_WEB_AUTH_STARTED = "web_auth_started"
EVENT_DESKTOP_CLIENT_WEB_AUTH_COMPLETED = "web_auth_completed"
EVENT_DESKTOP_CLIENT_WEB_AUTH_FAILED = "web_auth_failed"
EVENT_DESKTOP_CLIENT_WEB_AUTH_EXPIRED = "web_auth_expired"
EVENT_DESKTOP_CLIENT_WEB_AUTH_CANCELLED = "web_auth_cancelled"
EVENT_DESKTOP_CLIENT_DESKTOP_CHECKOUT_STARTED = "desktop_checkout_started"
EVENT_DESKTOP_CLIENT_DESKTOP_CHECKOUT_UPGRADED = "desktop_checkout_upgraded"
EVENT_DESKTOP_CLIENT_DESKTOP_CHECKOUT_DEADLINE = "desktop_checkout_deadline"
EVENT_DESKTOP_CLIENT_DESKTOP_NOTIFICATION_FETCHED = "desktop_notification_fetched"
EVENT_DESKTOP_CLIENT_DESKTOP_NOTIFICATION_QUEUED = "desktop_notification_queued"
EVENT_DESKTOP_CLIENT_DESKTOP_NOTIFICATION_TOAST_SHOWN = "desktop_notification_toast_shown"
EVENT_DESKTOP_CLIENT_DESKTOP_NOTIFICATION_TOAST_DENIED = "desktop_notification_toast_denied"
EVENT_DESKTOP_CLIENT_DESKTOP_NOTIFICATION_DISMISSED = "desktop_notification_dismissed"
EVENT_DESKTOP_CLIENT_DESKTOP_NOTIFICATION_PAGE_DROPPED = "desktop_notification_page_dropped"
EVENT_DESKTOP_CLIENT_VOICE_START_FAILED = "voice_start_failed"
EVENT_DESKTOP_CLIENT_CHAT_SESSION_STARTED = "chat_session_started"
EVENT_DESKTOP_CLIENT_CHAT_MESSAGE_SENT = "chat_message_sent"
EVENT_DESKTOP_CLIENT_CHAT_TURN_FAILED = "chat_turn_failed"
EVENT_DESKTOP_CLIENT_DRAFT_CARD_COPIED = "draft_card_copied"
EVENT_DESKTOP_CLIENT_DRAFT_CARD_DISMISSED = "draft_card_dismissed"
EVENT_DESKTOP_CLIENT_CALLBACK_CARD_SHOWN = "callback_card_shown"
EVENT_DESKTOP_CLIENT_CALLBACK_CARD_ENGAGED_10S = "callback_card_engaged_10s"
EVENT_DESKTOP_CLIENT_CALLBACK_CARD_DISMISSED = "callback_card_dismissed"
EVENT_DESKTOP_CLIENT_CALLBACK_CARD_TOGGLE_OFF = "callback_card_toggle_off"
EVENT_DESKTOP_CLIENT_CALLBACK_CHIP_DELETED = "callback_chip_deleted"
EVENT_DESKTOP_CLIENT_TURN_CONTEXT_UPLOAD = "turn_context_upload"
EVENT_DESKTOP_CLIENT_SCREEN_SIGHT_TOGGLED = "screen_sight_toggled"
EVENT_DESKTOP_CLIENT_MEETING_AUTO_SUMMON = "meeting_auto_summon"
EVENT_DESKTOP_CLIENT_MEETING_TICKER_DISMISSED = "meeting_ticker_dismissed"
EVENT_DESKTOP_CLIENT_MEETING_ALERTS_TOGGLE_OFF = "meeting_alerts_toggle_off"
EVENT_DESKTOP_CLIENT_MEETING_NOTES_ARM_TOGGLED = "meeting_notes_arm_toggled"
EVENT_DESKTOP_CLIENT_MEETING_NOTES_AUTO_TOGGLED = "meeting_notes_auto_toggled"
EVENT_DESKTOP_CLIENT_MEETING_CAPTURE_STARTED = "meeting_capture_started"
EVENT_DESKTOP_CLIENT_MEETING_CAPTURE_MANUAL = "meeting_capture_manual"
EVENT_DESKTOP_CLIENT_MEETING_CAPTURE_COMPLETED = "meeting_capture_completed"
EVENT_DESKTOP_CLIENT_MEETING_CAPTURE_FAILED = "meeting_capture_failed"
EVENT_DESKTOP_CLIENT_MEETING_CAP_BLOCKED = "meeting_cap_blocked"
EVENT_DESKTOP_CLIENT_MEETING_UPLOAD_ATTEMPT = "meeting_upload_attempt"
EVENT_DESKTOP_CLIENT_MEETING_NOTE_CARD_SHOWN = "meeting_note_card_shown"
EVENT_DESKTOP_CLIENT_MEETING_NOTE_CARD_DISMISSED = "meeting_note_card_dismissed"
EVENT_DESKTOP_CLIENT_MEETING_NOTE_CARD_TOGGLE_OFF = "meeting_note_card_toggle_off"
EVENT_DESKTOP_CLIENT_GUIDE_SESSION = "guide_session"
EVENT_DESKTOP_CLIENT_GUIDE_COMPLETED = "guide_completed"
EVENT_DESKTOP_CLIENT_GUIDE_ABANDONED = "guide_abandoned"
EVENT_DESKTOP_CLIENT_GUIDE_STEPS_RECEIVED = "guide_steps_received"
EVENT_DESKTOP_CLIENT_GUIDE_AUTO_FRAMES_SENT = "guide_auto_frames_sent"
EVENT_DESKTOP_CLIENT_GUIDE_AGENT_TIMEOUTS = "guide_agent_timeouts"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_SESSION_STARTED = "interview_companion_session_started"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_SESSION_ENDED = "interview_companion_session_ended"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_TURN_LATENCY = "interview_companion_turn_latency"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_FIRST_ANSWER_TEXT = "interview_companion_first_answer_text"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_ANSWER_COMPLETED = "interview_companion_answer_completed"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_QUESTION_DECISION = "interview_companion_question_decision"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_RESUME_ATTACHED = "interview_companion_resume_attached"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_CREDENTIAL_ROTATION = "interview_companion_credential_rotation"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_SCREEN_SIGHT = "interview_companion_screen_sight"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_REFLECTION = "interview_companion_reflection"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_RECOVERY = "interview_companion_recovery"
EVENT_DESKTOP_CLIENT_INTERVIEW_COMPANION_ERROR = "interview_companion_error"
EVENT_DESKTOP_CLIENT_DESKTOP_DICTATION_SHARE_DRAIN = "desktop_dictation_share_drain"
EVENT_DESKTOP_CLIENT_DICTATION_HOLD_COMPLETED = "dictation_hold_completed"
EVENT_DESKTOP_CLIENT_DESKTOP_UPDATE_AVAILABLE = "desktop_update_available"
EVENT_DESKTOP_CLIENT_DESKTOP_UPDATE_CHECK_FAILED = "desktop_update_check_failed"
EVENT_DESKTOP_CLIENT_DESKTOP_UPDATE_INSTALL_STARTED = "desktop_update_install_started"
EVENT_DESKTOP_CLIENT_DESKTOP_UPDATE_INSTALL_RESULT = "desktop_update_install_result"
EVENT_DESKTOP_CLIENT_DESKTOP_PERMISSION_RESULT = "desktop_permission_result"
EVENT_DESKTOP_CLIENT_FEEDBACK_SUBMITTED = "feedback_submitted"
EVENT_DESKTOP_CLIENT_DESKTOP_HEARTBEAT = "desktop_heartbeat"
EVENT_DESKTOP_CLIENT_DESKTOP_STARTUP_DIAGNOSTICS_SENT = "desktop_startup_diagnostics_sent"
EVENT_DESKTOP_CLIENT_CLIENT_LOG = "client_log"

# Breakdown keys stamped by the desktop client.
PROP_DESKTOP_WINDOW_LABEL = "window_label"
PROP_DESKTOP_INSTALL_ID = "install_id"
PROP_DESKTOP_LAST_EXIT_KIND = "last_exit_kind"
PROP_DESKTOP_CONSECUTIVE_FAILED_LAUNCHES = "consecutive_failed_launches"
