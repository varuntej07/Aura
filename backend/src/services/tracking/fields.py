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
# Subcollections under tracked_topics/{topic_key}: the stable-identity fixture docs and
# their per-fire audit trail. Both are parent-scoped reads only (a topic has at most a
# few dozen fixtures), so neither needs a composite index.
COLLECTION_FIXTURES = "fixtures"
COLLECTION_FIXTURE_FIRES = "fires"

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
# Local notify window (hours 0-23 in the topic's timezone). A routine beat whose fire
# lands OUTSIDE [start, end) is held (an event poll skips; the pulse re-arms to the next
# open) so a 3am match poll never buzzes — unless the checkpoint is wake_override (a
# can't-miss final/verdict), which bypasses it. The research agent picks these; the
# defaults below apply when it gives nothing usable.
TOPIC_NOTIFY_START_HOUR = "notify_start_hour"
TOPIC_NOTIFY_END_HOUR = "notify_end_hour"
DEFAULT_NOTIFY_START_HOUR = 8
DEFAULT_NOTIFY_END_HOUR = 23
# True when the topic is a real future event whose DATE is not yet announced (e.g. an IPO
# with no set date). events[] is empty and the heartbeat stays tight so the announcement
# is caught fast; the daily reconcile flips it false and lays the schedule once a date appears.
TOPIC_AWAITING_DATE = "awaiting_date"
# The pulse's fact-novelty gate: short slugs of the concrete developments already
# pushed (last ~20, newest last). A pulse compose must name a development_key; one
# already in this list is a re-worded repeat and abstains. Replaces string-equality
# on live_summary as the pulse's dedup.
TOPIC_RECENT_DEVELOPMENT_KEYS = "recent_development_keys"
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


# ── tracked_topics/{topic_key}/fixtures/{fixture_id} ────────────────────────
# One doc per REAL-WORLD fixture (a match, a hearing, a launch window). The id is
# minted ONCE from the fixture's start slot (never from its label), so a reconcile
# that rewords the label ("Quarterfinal 3" -> "Match 98" -> "Spain vs Belgium")
# updates the SAME doc instead of forking a parallel notification series — the
# 2026-07-10 19-push incident was exactly that fork, multiplied by a poll grid.
FIXTURE_ID = "id"                              # e.g. "20260710-1800" (+ "-b" on a slot collision)
FIXTURE_TOPIC_KEY = "topic_key"
FIXTURE_LABEL = "label"                        # display label — free to be reworded by reconcile
FIXTURE_START_AT = "start_at"                  # updated in place when the schedule shifts
FIXTURE_EXPECTED_END_AT = "expected_end_at"    # start + typical duration; when the result moment fires
FIXTURE_KIND = "kind"                          # EVENT_KIND_SPAN | EVENT_KIND_POINT
FIXTURE_LEAD_MINUTES = "lead_minutes"          # research-chosen pre heads-up lead; 0 -> moments default (30)
FIXTURE_WAKE_OVERRIDE = "wake_override"        # can't-miss fixture: result may push outside the notify window
# Structured FACT state — the send gate. A push happens only when these facts
# TRANSITION (e.g. status scheduled -> finished), never because a fresh LLM
# composition worded the same facts differently.
FIXTURE_STATUS = "status"                      # see FIXTURE_STATUS_* below (fact state, not queue state)
FIXTURE_FACT_SCORE = "fact_score"              # e.g. "1-0"; "" until known
FIXTURE_FACT_WINNER = "fact_winner"            # advancing team / outcome; "" until known
FIXTURE_FACT_NOTE = "fact_note"                # short extra fact (penalties, postponed-to, venue change)
FIXTURE_FACTS_UPDATED_AT = "facts_updated_at"
FIXTURE_LAST_TRANSITION = "last_transition"    # e.g. "scheduled->finished" (observability)
FIXTURE_CREATED_AT = "created_at"
FIXTURE_UPDATED_AT = "updated_at"

FIXTURE_STATUS_SCHEDULED = "scheduled"
FIXTURE_STATUS_LIVE = "live"
FIXTURE_STATUS_FINISHED = "finished"
FIXTURE_STATUS_CANCELLED = "cancelled"


# ── tracked_topics/{topic_key}/fixtures/{fixture_id}/fires/{auto_id} ────────
# One audit row per MOMENT FIRE, sent or abstained. The per-user notification ledger
# already records every delivered push; this records the abstains (why nothing was
# sent), which is where "why did/didn't I get a notification" debugging lives.
AUDIT_MOMENT = "moment"                        # which moment fired (pre|kickoff|result|…)
AUDIT_FIRED_AT = "fired_at"
AUDIT_QUERY = "query"                          # the fetch query used ("" for fetchless moments)
AUDIT_FETCH_TIER = "fetch_tier"                # which tier served it (TIER_*; "" for fetchless)
AUDIT_PRIOR_FACTS = "prior_facts"              # fact state before this fire (map)
AUDIT_SEEN_FACTS = "seen_facts"                # fact state the extraction saw (map; empty on abstain-before-extract)
AUDIT_TRANSITION = "transition"                # the committed transition ("" when none)
AUDIT_DECISION = "decision"                    # see AUDIT_DECISION_* below
AUDIT_SENT_COUNT = "sent_count"                # subscribers actually delivered to
AUDIT_TITLE = "title"                          # the push title ("" when nothing sent)

AUDIT_DECISION_SENT = "sent"
AUDIT_DECISION_ABSTAIN_NO_TRANSITION = "abstain_no_transition"
AUDIT_DECISION_ABSTAIN_WRONG_FIXTURE = "abstain_wrong_fixture"
AUDIT_DECISION_ABSTAIN_STALE_CONTENT = "abstain_stale_content"
AUDIT_DECISION_ABSTAIN_TOO_LATE = "abstain_too_late"       # pre/kickoff fired past its usefulness window
AUDIT_DECISION_ABSTAIN_RACE_LOST = "abstain_race_lost"     # another moment committed this transition first
AUDIT_DECISION_REARMED = "rearmed"                          # result not determinable yet; re-check scheduled
AUDIT_DECISION_FAILED_FETCH = "failed_fetch"


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
# Last summary actually delivered to THIS user. Display/observability only since the
# fixture fact-transition gate replaced string-equality dedup (a transition key can
# never repeat, so no per-user text cursor is needed to prevent re-sends).
TRACKER_LAST_SENT_SUMMARY = "last_sent_summary"
# Per-user-per-topic daily ceiling (founder decision 2026-07-10: 8/day). The date is a
# UTC "YYYY-MM-DD" string; a fire on a new date resets the counter in the same claim
# transaction (reset-on-read, mirroring notification_budget's day rollover).
TRACKER_SENT_TODAY = "sent_today"
TRACKER_SENT_TODAY_DATE = "sent_today_date"

TRACKER_STATUS_ACTIVE = "active"
TRACKER_STATUS_PAUSED = "paused"
TRACKER_STATUS_COMPLETED = "completed"
TRACKER_STATUS_CANCELLED = "cancelled"


# ── checkpoints/{checkpoint_id} ──────────────────────────────────────────────
CHECKPOINT_ID = "id"
CHECKPOINT_TOPIC_KEY = "topic_key"             # -> tracked_topics/{topic_key} (shared, fans out)
CHECKPOINT_EVENT_LABEL = "event_label"         # "USA vs Australia" — free-text DISPLAY label
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
CHECKPOINT_WAKE_OVERRIDE = "wake_override"     # may fire/push outside the notify window (a can't-miss moment)
# The fixture this moment belongs to. Empty on a pulse and on legacy poll-grid docs —
# the fire path uses empty+non-pulse as the "legacy, expire on sight" discriminator.
CHECKPOINT_FIXTURE_ID = "fixture_id"
# How many times the RESULT moment has re-armed waiting for the outcome to become
# determinable. Distinct from CHECKPOINT_ATTEMPTS (claim count): a re-arm is a healthy
# "not over yet", not a failure retry.
CHECKPOINT_RESULT_CHECKS = "result_checks"

CHECKPOINT_PHASE_PRE = "pre"                    # heads-up before the fixture ("kicks off in 30 min")
CHECKPOINT_PHASE_KICKOFF = "kickoff"            # the moment the fixture starts ("underway now")
CHECKPOINT_PHASE_RESULT = "result"              # outcome check at expected end (bounded re-arm loop)
CHECKPOINT_PHASE_PULSE = "pulse"                # recurring heartbeat for developments between fixtures
# LEGACY poll-grid phases. Never written anymore; kept PERMANENTLY because
# moments.is_legacy_poll_phase and the migration script discriminate on them to
# expire stray pre-redesign docs on sight.
CHECKPOINT_PHASE_LIVE = "live"
CHECKPOINT_PHASE_POST = "post"
CHECKPOINT_PHASE_MILESTONE = "milestone"

# Fixture shape (see models.ResearchedFixture / Fixture). A span has duration (a
# match: its result is checked at the expected end); a point is instantaneous (a
# verdict, a launch: its result is checked shortly after the moment).
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
