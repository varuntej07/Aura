"""Single source of truth for every collection name, field name, and enum value
the topic-tracking engine reads or writes.

Per CLAUDE.md's database-field discipline: the writer and EVERY reader reference
these constants, never a string literal, so a rename lives in one place and a
writer->reader round-trip test can break CI if either side drifts. A query that
filters on a field no document has returns zero rows SILENTLY (it does not error),
which looks identical to "no data" — so the names must never be free-typed twice.

Three Firestore collections (all top-level flat, queried by indexed fields rather
than by ancestry, so the hot due-scan is a tight range query and not a
collection_group fan-out — see the storage decision in the design):

  tracked_topics/{topic_key}   — SHARED. One public event, researched once, its
                                  schedule + live-state cache fanned to all subscribers.
  trackers/{tracker_id}        — PER-USER. One user's subscription to a topic_key.
  checkpoints/{checkpoint_id}  — the due-queue. One scheduled (pre|live|post) fire.
"""

from __future__ import annotations

# Collections
COLLECTION_TRACKED_TOPICS = "tracked_topics"
COLLECTION_TRACKERS = "trackers"
COLLECTION_CHECKPOINTS = "checkpoints"

# Client-side routing key carried on the FCM payload (notification_type).
NOTIFICATION_TYPE_TRACKER_UPDATE = "tracker_update"

# Fetch-chain tier identifiers (mirror settings.tracking_fetch_tier_order).
TIER_RSS = "rss"
TIER_NEWSDATA = "newsdata"
TIER_BRAVE = "brave"
TIER_GROUNDED = "grounded"
TIER_NONE = "none"   # no tier returned usable content this fetch


# ── tracked_topics/{topic_key} ───────────────────────────────────────────────
# Identity + research
TOPIC_KEY = "topic_key"                       # normalized canonical slug, e.g. "fifa-world-cup-2026"
TOPIC_TITLE = "title"                          # human topic, e.g. "USA at the FIFA World Cup 2026"
TOPIC_KIND = "kind"                            # see TOPIC_KIND_* below
TOPIC_RESEARCH_QUERY = "research_query"        # the query the agent searches with
TOPIC_END_CONDITION = "end_condition"          # natural-language "until …" the reconcile LLM judges
# Locale that drives the localized fetch (so a non-US/non-English topic is searched in its
# own region, not the US-English Google News edition). Set by research, refined on reconcile.
TOPIC_COUNTRY = "country"                       # ISO 3166-1 alpha-2 (e.g. "IN", "BR", "GB"); "" -> US default
TOPIC_LANGUAGE = "language"                     # ISO 639-1 (e.g. "hi", "pt", "te"); "" -> en default
TOPIC_STARTS_AT = "starts_at"                  # ISO UTC, event start (nullable)
TOPIC_ENDS_AT = "ends_at"                      # ISO UTC, expected event end (nullable)
TOPIC_EXPIRES_AT = "expires_at"                # ISO UTC HARD backstop — auto-complete past this, always set
TOPIC_TIMEZONE = "timezone"                    # event-anchor tz when relevant
# Shared live-state cache (one fetch fanned to all subscribers)
TOPIC_LIVE_SUMMARY = "live_summary"            # last composed factual state, e.g. "USA 2-1 AUS, full time"
TOPIC_LIVE_FETCHED_AT = "live_fetched_at"      # ISO UTC of that fetch
TOPIC_LIVE_SOURCE_TIER = "live_source_tier"    # which fetch tier served it (TIER_*)
# Reconcile / scheduling bookkeeping
TOPIC_NEXT_RECONCILE_AT = "next_reconcile_at"  # ISO UTC — due-query key for the reconcile scan
TOPIC_LAST_RECONCILED_AT = "last_reconciled_at"
TOPIC_RECONCILE_COUNT = "reconcile_count"
TOPIC_SUBSCRIBER_COUNT = "subscriber_count"
# Adaptive pulse cadence (seconds) for the recurring heartbeat checkpoint. Tightens
# when a pulse finds genuinely-new state, loosens when it does not — so an "ongoing"
# topic (open_interest, no dated events) is still polled, faster when it is hot and
# slower when it is quiet. 0 / missing means "use the engine's initial interval".
TOPIC_PULSE_INTERVAL_SECONDS = "pulse_interval_seconds"
# Observability / health ("what is working and what is not")
TOPIC_STATUS = "status"                         # see TOPIC_STATUS_* below
TOPIC_HEALTH = "health"                         # derived: healthy | degraded | stalled
TOPIC_RESEARCH_CONFIDENCE = "research_confidence"     # 0..1, agent's self-rated schedule confidence
TOPIC_LAST_RESEARCH_TIER = "last_research_tier"       # which tier the last research used
TOPIC_LAST_RECONCILE_STATUS = "last_reconcile_status" # ok | partial | failed
TOPIC_LAST_RECONCILE_ERROR = "last_reconcile_error"   # str | null
TOPIC_CONSECUTIVE_RECONCILE_FAILURES = "consecutive_reconcile_failures"  # -> auto-fail after N
TOPIC_CHECKPOINTS_TOTAL = "checkpoints_total"
TOPIC_CHECKPOINTS_FIRED = "checkpoints_fired"
TOPIC_CHECKPOINTS_FAILED = "checkpoints_failed"
TOPIC_CREATED_AT = "created_at"
TOPIC_UPDATED_AT = "updated_at"

TOPIC_KIND_BOUNDED_EVENT = "bounded_event"      # tournament, election, launch — has an end
TOPIC_KIND_RECURRING_SEASON = "recurring_season"  # a league/season with many sub-events
TOPIC_KIND_OPEN_INTEREST = "open_interest"      # a team/person, no natural end (uses the hard backstop)

TOPIC_STATUS_ACTIVE = "active"
TOPIC_STATUS_COMPLETED = "completed"            # end_condition met / past expires_at
TOPIC_STATUS_FAILED = "failed"                  # research kept failing — stop burning calls
TOPIC_STATUS_STALE = "stale"                    # no subscribers left

TOPIC_HEALTH_HEALTHY = "healthy"
TOPIC_HEALTH_DEGRADED = "degraded"              # last reconcile partial, or fell to a low tier
TOPIC_HEALTH_STALLED = "stalled"                # repeated failures / no successful fetch in a while


# ── trackers/{tracker_id} ────────────────────────────────────────────────────
TRACKER_ID = "id"
TRACKER_USER_ID = "user_id"
TRACKER_TOPIC_KEY = "topic_key"                # -> tracked_topics/{topic_key}
TRACKER_STATUS = "status"                       # see TRACKER_STATUS_* below
TRACKER_CREATED_VIA = "created_via"             # "text" | "voice"
TRACKER_CREATED_AT = "created_at"
TRACKER_UPDATED_AT = "updated_at"
TRACKER_MUTE_UNTIL = "mute_until"               # ISO UTC; user said "pause"
# Per-user delivery observability
TRACKER_UPDATES_SENT = "updates_sent"
TRACKER_LAST_UPDATE_AT = "last_update_at"
# Per-user dedup: the last summary actually delivered to THIS user, so two users on
# the same topic don't have to share a send cursor.
TRACKER_LAST_SENT_SUMMARY = "last_sent_summary"

TRACKER_STATUS_ACTIVE = "active"
TRACKER_STATUS_PAUSED = "paused"
TRACKER_STATUS_COMPLETED = "completed"
TRACKER_STATUS_CANCELLED = "cancelled"


# ── checkpoints/{checkpoint_id} ──────────────────────────────────────────────
CHECKPOINT_ID = "id"
CHECKPOINT_TOPIC_KEY = "topic_key"             # -> tracked_topics/{topic_key} (shared, fans out)
CHECKPOINT_EVENT_LABEL = "event_label"         # "USA vs Australia"
CHECKPOINT_PHASE = "phase"                      # see CHECKPOINT_PHASE_* below
CHECKPOINT_FIRE_AT = "fire_at"                  # ISO UTC — the due-queue key (status,fire_at index)
CHECKPOINT_STATUS = "status"                    # see CHECKPOINT_STATUS_* below
CHECKPOINT_ATTEMPTS = "attempts"
CHECKPOINT_CLAIMED_AT = "claimed_at"
CHECKPOINT_FIRED_AT = "fired_at"
CHECKPOINT_LAST_SUMMARY = "last_summary"        # composed state at this checkpoint (topic-level dedup)
CHECKPOINT_LAST_FETCH_TIER = "last_fetch_tier"
CHECKPOINT_LAST_FETCH_AT = "last_fetch_at"
CHECKPOINT_LAST_ERROR = "last_error"
CHECKPOINT_CREATED_AT = "created_at"

CHECKPOINT_PHASE_PRE = "pre"                    # before the event ("kicks off in 2h")
CHECKPOINT_PHASE_LIVE = "live"                  # during ("underway, 0-0")
CHECKPOINT_PHASE_POST = "post"                  # after ("USA win 2-1")
CHECKPOINT_PHASE_MILESTONE = "milestone"        # the single moment a POINT event happens (verdict, launch, release)
CHECKPOINT_PHASE_PULSE = "pulse"                # recurring heartbeat for an ongoing topic with no dated events

# Event shape — drives which phases an event materializes (see models.ScheduledEvent).
# A span has duration (a match: pre/live/post); a point is instantaneous (a verdict,
# a launch, a release: a heads-up pre + a single milestone at the moment).
EVENT_KIND_SPAN = "span"
EVENT_KIND_POINT = "point"

CHECKPOINT_STATUS_PENDING = "pending"
CHECKPOINT_STATUS_CLAIMED = "claimed"           # a tick claimed it (in-flight) — prevents double fire
CHECKPOINT_STATUS_FIRED = "fired"
CHECKPOINT_STATUS_SKIPPED = "skipped"           # fetched but nothing new to say (dedup)
CHECKPOINT_STATUS_FAILED = "failed"
CHECKPOINT_STATUS_EXPIRED = "expired"           # topic ended before this checkpoint fired


# Client-side funnel join key carried on the tracker FCM payload as notification_origin
# (the app filters tap events on this). Kept after the gatekeeper removal because the
# value, not the old decision audit, is what the client routing depends on.
DECISION_ORIGIN_TRACKER = "tracker"
