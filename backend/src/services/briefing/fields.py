"""Daily Briefing Firestore field names — the single source of truth.

Per the data-layer discipline in CLAUDE.md, every writer and reader of a briefing
document references these constants instead of hard-coding the string, so a rename
can never silently break the generate / claim / read contract (the "zero rows
looks healthy" trap). The store round-trips a write through a read in
``backend/tests/test_briefing_store.py``.

Firestore layout (one document per user per local date; read by document id, no
collection-group query, no inequality, so NO declared index is needed):

    users/{uid}/daily_briefing/{YYYY-MM-DD}

The per-date document IS the idempotency lock: a concurrent fan-out tick that finds
the document already present (status ``generating`` / ``ready``) stands down.
"""

from __future__ import annotations

# Per-user briefing subcollection (mirrors signal_store, icebreaker_state, threads).
DAILY_BRIEFING_SUBCOLLECTION = "daily_briefing"

# ── daily_briefing/{date} document fields ───────────────────────────────────
# Lifecycle status. The claim writes ``generating``; a successful generation flips
# it to ``ready``; ``skipped`` means nothing was worth sending today (no push);
# ``failed`` means the LLM/store errored and a later tick MAY re-claim and retry.
FIELD_STATUS = "status"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# The user-local date ("YYYY-MM-DD") this briefing is for. Mirrors the document id;
# stored as a field too so a reader never has to parse the id.
FIELD_LOCAL_DATE = "local_date"

# The synthesized, on-screen briefing narrative (a few short paragraphs).
FIELD_NARRATIVE = "narrative"

# The short Buddy opener that seeds the "Chat about this" FAB chat. Names the items
# concretely so the seeded bubble carries enough hooks for Buddy to continue.
FIELD_CHAT_SEED_MESSAGE = "chat_seed_message"

# The source items the narrative actually wove in: a list of
# {title, url, source, category} maps, shown as the on-screen sources footer and
# used to verify the narrative was grounded on real pool items.
FIELD_SOURCES = "sources"

# When the claim document was first created (UTC datetime) — audit only.
FIELD_CREATED_AT = "created_at"

# When generation completed and the doc went ``ready`` (UTC datetime) — audit only.
FIELD_GENERATED_AT = "generated_at"

# Client routing key in the FCM data payload. The Flutter app switches on this to
# open the briefing screen (mirrors signal_engine / icebreaker / thread_followup).
# Intentionally equal to the funnel origin value so one string drives both routing
# and the PostHog funnel join (see analytics/funnel_events.NOTIFICATION_ORIGIN_BRIEFING).
NOTIFICATION_TYPE_BRIEFING = "daily_briefing"
