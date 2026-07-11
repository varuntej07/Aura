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
