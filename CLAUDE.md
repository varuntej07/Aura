# Project Overview

Aura is an AI companion/assistant app (testing with ~15 users - doesn't mean app is built for only 15 users, has production access). The assistant persona is Buddy. Covers text chat, LiveKit voice, reminders, memory, notifications, scheduled agents, Google Calendar and Gmail tools, live web search, topic tracking, and an Android keyboard IME.

Keep it production-grade: scalable, maintainable, robust, future-proof, and flexible for incoming features.

**Product soul: Buddy is obsessed with the user, in the best way.** Every feature should feel like a true companion always trying to help and learn more about this person: warm, curious, proactive, remembering what matters. When a product choice is ambiguous, pick the one that makes Buddy feel more like a close friend, never a neutral tool, form, or content feed.

## Cross-repo ecosystem map

This repo's backend and mobile app are one of three codebases in the Aura system, alongside Aura-Desktop (Tauri Windows client) and Aura-Web (marketing site + browser auth handoff).
See [`ECOSYSTEM.md`](./ECOSYSTEM.md) for how they fit together: the shared Firebase project, the pairing and web-auth handshakes, and the Windows release/update pipeline.
Update that file when a change here alters a cross-repo contract (an endpoint's shape, a shared Firestore collection, an auth handshake, shared config identity, or a deploy/version linkage), not for internal-only changes.

## Architecture

Flutter app uses MVVM with Provider: screens in `lib/presentation/screens`, ViewModels in `lib/presentation/viewmodels`, repositories in `lib/data/repositories`, services in `lib/data/services`, shared code in `lib/core`, Provider wiring in `lib/di/providers.dart`.

Backend is a FastAPI app in `backend/src/main.py`; handlers in `backend/src/handlers`, services in `backend/src/services`. `backend/src/agents` holds only stateless content fetchers in `data_fetchers/` (Google News, NewsData, Brave) that feed the signal-engine pool; they never send notifications (that routes through the orchestrator).

Voice runs through `backend/src/agent/voice_agent.py` as a separate LiveKit worker. `voice_agent.py` is the thin orchestrator; its pieces (telemetry, error mapping, Firestore fetchers, prompt context, pipeline builders, voice conditioning, session event recorder) live in `backend/src/agent/voice/`. Voice exposes the `track_topic` tool (minimal-topic path only — no inline research; reconcile + pulse enrich on the next scheduler tick). Do NOT make Buddy a global dialogue state machine; keep ONE `BuddyAgent` and fix drift by de-duping the prompt. Rapport loss is LiveKit's number-one handoff failure mode and Aura's core differentiator.

**Per-turn voice expressiveness:** on top of the static per-session baseline emotion (`voice/voice_controls.py`, derived once from the aura profile), Buddy can color individual replies by emitting an allowlisted bracket cue (`[excited]`, `[whisper]`, `[hyped]`, `[laughter]`, ...) in the same grammar as the older `[laughter]`-only cue. `voice/emotion_tags.py` converts these into Cartesia sonic-3 inline `<emotion/speed/volume>` markup inside `tts_node`; any cue outside the allowlist is stripped rather than reaching TTS (kills dead-air from a hallucinated cue). Only sonic-3 understands this markup, so the Deepgram and sonic-2 fallbacks in `pipelines.py` are wrapped in `voice/fallback_tts_wrapper.py::SpeechMarkupStrippingTTS`, which strips it before those engines would otherwise read it aloud literally. The caption/transcript path and the session recorder already strip every bracket cue, so nothing downstream of the audio needs to know this exists. Cartesia's `generation_config.emotion` and inline `<emotion value="...">` both take **lowercase** names only — a past bug shipped Capitalized values from the plugin's stale `TTSVoiceEmotion` literal, which sonic-3 likely silently ignored.

`backend/src/services/user_aura_extractor.py` builds a passive behavioral profile per user, fired fire-and-forget from the chat handler after every message. Profiles live in `UserAura/{uid}`. It passes the user's previous query (`prev_user_query`) alongside the current message to Gemini Flash, which decides when prior context is needed (no hardcoded heuristics). Failed extractions are swallowed silently so the chat stream is never affected.

**Two-tier Aura reflection:** per-turn capture writes raw signals; a per-session reflection tier (runs async at session close) compacts captures into storylines and traits with kind-aware decay, growth-aware idempotency (fixes "reflect-once-then-freeze"), and prunes another-person interests. `user_aura_extractor.py` owns both tiers.

Interests are a **closed taxonomy, not free text**. `backend/src/services/user_aura_schema.py` is the single source of truth: ~30 broad categories (+ `other`) the extraction prompt constrains Gemini to (off-list coerces to `other`), each holding specific subjects (e.g. `politics_governance` -> `KCR`) with kind-aware time-decayed weights (90-day durable, 7-day event-driven, 45-day goal-instrumental) and a per-category cap. The writer (`apply_interest_signal`) and every reader (chat suffix, voice prompt, notification framer, signal-engine `user_vector` embedding) go through the schema's accessors, which fall back to legacy `deep_interest_frequencies` until a profile rebuilds. This replaced a free-text design that fragmented into 100+ near-duplicate buckets.

`backend/src/services/chat_completion/` handles background chat turn completion (Cloud Tasks handoff when the client disconnects mid-turn). `prompt_builder.py` was extracted from `chat.py` and lives here. See **Background Chat Completion** below.

`backend/src/services/tracking/` is the topic tracking engine: generic "keep me posted on X", backed by LLM-driven checkpoint scheduling. See **Topic Tracking** below.

The Android keyboard IME is a new Buddy Everywhere surface. See **Buddy Everywhere / Android IME** below.

The Windows client is Aura-Desktop (Tauri, separate repo); the legacy Flutter desktop app was deleted from this repo 2026-07-11. This repo still owns everything the desktop client consumes: the pairing/web-auth/dashboard-link/draft-outbound handlers, the shared voice path (`BuddyAgent` + screen-sight in `agent/voice/screen_frames.py`), and the `screen_saves` service. The mobile side of pairing (`link_device_screen.dart`, the phone mints the code) also lives here.

`backend/src/services/model_provider.py` is the single import for all LLM tiers: `cheap()`, `balanced()`, `expert()`, `reason_turn()`, `grounded()`. All backend LLM calls go through it. Content ingestion is RSS-first; never use `grounded()` in background ingest. Grounding is on-demand (chat and voice) only.

The deprecated FastAPI `on_event` in `mcp.py` / `main.py` is intentional. Do not migrate to `lifespan` as a cleanup — only at the next FastAPI version bump with a `/mcp` boot test.

## Notification Orchestrator

`backend/src/services/notifications/` is the funnel all notification producers route through. The original 7 producers (reminder, tracking, calendar, briefing, thread, icebreaker, news/signal) build a `NotificationProposal` and call `orchestrator.submit()`, the primary caller of `notification_service.send_notification`. Four more sources have joined since (`notifications/proposal.py`'s `SOURCE_*` constants): device-link confirmation (`handlers/pairing.py`, on a new desktop pairing), a background chat-reply push (`chat_completion/`), trial-expiry, and a follow-up producer (see **Reactive Orchestration Engine** below). Re-engagement now has two coexisting paths: the legacy dormancy handler in `backend/src/handlers/engagement.py` (`/internal/engage/*`) still calls `send_notification` directly, while a newer `services/reengagement/reengagement_engine.py` submits through the orchestrator like everything else. The orchestrator centralizes freshness, cross-agent dedup, quiet hours, daily budget, and priority arbitration. Detail in `SIGNAL_ENGINE_ARCHITECTURE.md` §0.

- **Two lanes (`ProposalKind`):** COMMITTED (reminder/tracking/calendar/briefing) sends INLINE on submit (freshness + dedup only, never held/arbitrated) and records to the budget. PROACTIVE (thread/icebreaker/news) is enqueued to `users/{uid}/notification_queue`; the per-minute drain on `/scheduler/tick` arbitrates everything pending and sends at most the highest-priority one, HOLDING losers for a later window (never dropped).
- **Priority ladder:** reminder 96 · device_link 95 · tracking 95 · chat_reply 94 · calendar 90 · trial 88 · reengage 80 · followup 75 · thread 70 · briefing 60 · icebreaker 20 · news 10. (`SOURCE_*` constants + `PRIORITY` dict in `notifications/proposal.py`.)
- **Hard freshness gate:** content older than its per-source window is DROPPED before firing (news 18h; tracking 6h default; personal openers untimed).
- **Cross-agent dedup:** reads recent DELIVERED `dedup_key`s from the ledger; a failed send stays retryable (only a delivered row dedups).
- **Loud, never silent:** every send | hold | drop logs its reason.
- **Notifications are ungated for beta.** The LLM gatekeeper was permanently deleted. PROACTIVE caps are effectively unlimited (budget cap 1000, spacing 0, hard cap 1000). Gating decisions happen after phone testing, not before.

## Reactive Orchestration Engine

`backend/src/services/reactive/` is a per-user event-driven dispatcher that replaced the old direct per-producer cron fan-outs for icebreaker and thread follow-up. This is the design referenced in Design Docs below, and it's real, working code on disk (28 files across `reactive/` + `reengagement/`) — but both directories are currently 100% untracked in git (`git log` shows zero history for either). Given this repo's deploys build from local disk, not git (see Deploy ignores git state), that does NOT rule out it already being live in prod, but it isn't confirmed either — verify before treating it as settled, and commit it once confirmed. `run_icebreaker_tick` and `thread_reflector.run_reflection_tick` are both gone from the code either way; `handlers/scheduler.py` now emits an hourly `EVENT_TICK` per active user (`minute == 0`) onto `event_bus.py`, and `orchestrator.py` + `registry.py` route each event to the agents registered for it, each wrapped in a SENSE -> PLAN -> ACT -> VERIFY -> REPAIR self-heal envelope (`agent.py`) so a failed action retries/repairs instead of silently no-op'ing.

- **Registered agents:** `agents/icebreaker.py` (`IcebreakerOpenerAgent`, replaces `icebreaker_engine.run_icebreaker_tick`), `agents/curiosity.py` (`CuriosityThreadFollowUpAgent`, replaces `thread_reflector.run_reflection_tick`), `agents/followup.py` (the new `SOURCE_FOLLOWUP` producer above, priority 75).
- Both icebreaker and curiosity agents still build a `NotificationProposal` and call `orchestrator.submit()` exactly like every other producer — this changed WHO decides to send, not the delivery funnel itself; freshness/dedup/priority behavior in Notification Orchestrator above is unaffected.
- `icebreaker_engine.py` and `thread_reflector.py` still exist but are now thin: only post-send bookkeeping (`on_icebreaker_delivered`, `on_thread_delivered`, `select_thread_to_follow_up`) remains there; the SENSE/PLAN/ACT loop itself lives in `reactive/agents/`.

## Signal Engine

`backend/src/services/signal_engine/` is the notification and feed ranking layer; design in `backend/docs/signal_engine.md`, full push architecture in `SIGNAL_ENGINE_ARCHITECTURE.md`. It is now just ONE producer (lowest-priority "news"); its send routes through `orchestrator.submit`.

**How it works:** a content pool (`content_candidates/`) holds 768-dim embeddings (`gemini-embedding-001`). A per-user signal store (`users/{uid}/signal_store/state`) tracks a user vector, time-slot open rates, category affinities, and fatigue. Scoring is ingest-triggered (2026-07-09, replacing the 15→30 min recurring cron): each completed 4-hour content ingest enqueues one durable, generation-named Cloud Task that runs `scoring_loop.run_tick()` exactly once per generation (six per UTC day; idempotency via `signal_engine/generation_store.py`'s atomic claim + lease). Scoring is pure math; only the top candidate above threshold gets one Gemini Flash call to frame copy before FCM send.

**Starvation guards (all three required):**
- Expired tombstones swept on `/scheduler/tick` at `minute % 15 == 10`; native Firestore TTL on `content_candidates.expires_at` handles long-tail cleanup.
- `embed_texts` chunks input at ≤100 texts per Gemini batch (hard API cap); sequential chunks, not parallel.
- Google News feeds trimmed to 14 by default (6 region-sensitive topics x 2 locales + 2 region-agnostic, `SIGNAL_NEWS_LOCALES` env-configurable); ingest cadence is every 4h (was hourly). Never add back paid Gemini grounding to ingest.

**Endpoints:**
- `POST /events`, Flutter reports user events (taps, dismissals, app opens); updates user vector via EMA.
- `POST /internal/signal-engine/tick`, scoring + send. No recurring cron: invoked by the durable Cloud Task each completed content ingest enqueues (also the manual-recovery path; body optionally carries `generation_id`).
- `POST /internal/signal-engine/content-ingest`, Cloud Scheduler every 4h. Pulls from `agents/data_fetchers/`: Google News RSS, NewsData, and Brave News (`content_ingest.py`); on success records the generation and enqueues the scoring task.
- Calendar fallback sync for all users runs inside `/scheduler/tick` every 30 min via `minute % 30 == 0`.

**`/scheduler/tick` runs every minute** (`juno-reminder-tick` job). Use `minute % N == 0` gating in `handlers/scheduler.py` to piggyback periodic work without new scheduler jobs.

**Funnel (PostHog):** signal notifications are a 4-step funnel (`signal_notification_sent` -> `notification_tapped` -> `signal_session_from_notification` -> `signal_action_after_notification`). Event/property names live in ONE place per side: `backend/src/services/analytics/funnel_events.py` and `lib/core/analytics/funnel_events.dart`; `test_funnel_event_contract.py` fails CI if they drift. Server capture goes through `analytics/posthog_client.py` (fire-and-forget, no-op when `POSTHOG_API_KEY` unset, never raises into a tick).

**Other producers:**
- **Curiosity thread engine** (`backend/src/services/threads/`): a *thread* is a hole in what Buddy knows (v1 source: reminders). Dispatch now runs through the Reactive Orchestration Engine above (`reactive.agents.curiosity.CuriosityThreadFollowUpAgent`, off the hourly per-user tick), asking ONE warm question per open loop; the answer enriches `UserAura`. Framing lives in `thread_framer.py`. Android sends `data_only=True` so the app builds local RemoteInput chips (`thread_notification_handler.dart`); WhatsApp-style UX: optimistic echo first, Buddy reply as follow-up push (Cloud Run freeze guard). A body tap routes via `threadBodyTapStream` + `handleThreadNotificationColdLaunch()`. iOS uses in-chat pills. Tap routing keys off `notification_type == "thread_followup"`. `POST /threads/reply` persists to `users/{uid}/threads/{id}/messages` and returns Buddy's reply for the shade.
- **Unified notification budget** (`notification_budget.py`): one per-user daily ceiling + spacing every PROACTIVE decider claims from; committed sends are never blocked but recorded for spacing. Fail-open.

## Firestore Read Discipline (cron/tick cost control)

535K reads/week on ~15 beta users (2026-07) traced to cron CADENCE multiplied by per-user unconditional reads, not per-request cost. Individually every query was indexed and `.limit()`-bounded; the leak was structural. Fixed in `fcm_token_registry.py`, `feature_store.py`, `scoring_loop.py`, `queue_store.py`, `scheduler.py`, `google_calendar_connector.py`. Apply these rules to any NEW per-tick or per-user-loop code:

**2026-07-06 follow-up audit** (755K reads/week, still on a handful of beta users): confirmed all of the above fixes were still correctly in place, no regression. The remaining volume was NOT a single bug — it's the aggregate "sticker price" of running ~10-15 separate legitimately-engineered per-minute/per-15-min collection_group discovery sweeps, each touching a handful of real documents because there are real active users (verified 5, not the assumed 2, via `list_active_user_ids`) generating real checkpoints/notifications/events. Two concrete actions taken: (1) `ops/` founder dashboard was found calling `_load_user_directory()` 4x redundantly per single `/api/dashboard` load (~350-400 reads/load, auto-refreshing every 30s) — deduped to one shared load per request, plus the frontend now pauses polling when the tab is backgrounded (Page Visibility API); (2) `signal_engine` scoring cadence widened 15min→30min (halves the entire per-user tick pipeline, the single biggest lever, since `find_nearest_for_user` alone was ~9,600-24,000 reads/day). Never reason about a cost finding like this by citing today's tiny dollar amount — reason about the scaling factor toward a real production user count.

- **A query that returns 0 docs still bills a minimum 1 read.** "Cheap when empty" is not a defense for a query that fires every single minute (1440x/day) or every 15 min (96x/day) — the cadence itself is the cost, independent of how often it finds real work. Before adding an unconditional per-tick query, ask whether it can be gated behind a cheaper signal, or whether its cadence can be widened (only if nothing downstream depends on sub-cadence freshness — see the `process_pending_sync_jobs` vs `renew_expiring_channels` split below).
- **Never loop "all active users" and issue one query per user to discover who needs work.** That's O(ticks x users) reads for a question one `collection_group` query answers in O(1): "which users have X pending/due right now." Discover first via a single collection_group query, THEN do per-user follow-up work only for users the discovery actually flagged. Precedent: `queue_store.list_user_ids_with_pending()` (was: `list_active_user_ids()` + one `list_pending()` call per active user, every minute); `intent_supervisor`/`event_bus` already did this correctly (one global `collection_group` query) — use them as the reference pattern, not the thing to copy from the old proactive-drain.
- **A per-user loop-body read that doesn't actually vary by user is loop-invariant — hoist it out, fetch once per tick, share the result.** Precedent: `scoring_loop.list_recent_breaking_candidates` (global freshest-40, no user filter) moved from once-per-user to once-per-tick in `run_tick()`.
- **`list_active_user_ids` is cached in-process** (`fcm_token_registry.py`, ~3 min TTL, fail-open to the last good value with a loud ERROR log on a query failure — never silently swallow to `[]`, since several callers already did that pre-cache and it looks identical to "everyone inactive"). Any new consumer of "who's active" should call this cached function, not run a fresh `collection_group("fcm_tokens")` scan. Bypass with `force_refresh=True` only where correctness genuinely outweighs the read cost (e.g. a once-a-day fan-out).
- **A dark-test allowlist gate (`PROACTIVE_NOTIFICATION_UID_ALLOWLIST`) must be re-applied at every discovery path that can lead to a send**, not just the original one. Refactoring "how we find affected users" can silently reintroduce sending beyond the allowlist if the new path doesn't call `feature_store.apply_proactive_allowlist()` too.
- **Any process-level cache needs an autouse test fixture that clears it between tests** (see `conftest.py::clear_active_users_cache`) or one test's fake data leaks into the next test hitting the same cache key — this manifests as order-dependent flakiness, not a clean failure.
- **A new `collection_group(...)` query means a new index in `firestore.indexes.json` in the SAME change**, not a follow-up (see the existing Firestore index maintenance rule below) — a missing index 400s at runtime, not at deploy time, and adding a `fieldOverride` for a field that already had an implicit default index must re-list every scope still needed (COLLECTION *and* COLLECTION_GROUP) or it silently breaks the existing query on that field.
- **Before widening ANY cron cadence, check what actually depends on it.** `renew_expiring_channels` was safe to move from every-minute to every-5-minutes because its lead time is 6 hours. `process_pending_sync_jobs` was NOT touched because it's confirmed the real delivery path for webhook-triggered calendar syncs (`reason=f"webhook_{{resource_state}}"`) — widening it would add real user-facing latency, not just save reads. Don't assume every "every minute" job is interchangeable busywork; several are every-minute because the product genuinely needs near-real-time firing (reminders, tracking checkpoints).

## Daily Briefing

`backend/src/services/briefing/` is an **evening digest** plus an on-demand world snapshot. Endpoints `GET /briefing/today`, `POST /briefing/world`.

- **Daily briefing:** end-of-day digest at 20:00 local time. Woven in ONE LLM call from top-ranked pool items (scans up to `BRIEFING_POOL_SCAN_LIMIT`=60, weaves 7 to 10 scannable items across 3-4 categories via `BRIEFING_ITEMS_MIN`/`BRIEFING_ITEMS_MAX`). Uses a vector-independent candidate selector so cold-start users always get a digest. The fan-out (`run_briefing_tick`) piggybacks `/scheduler/tick` at `minute % 15 == 5` (offset from thread reflector minute 0 and icebreaker minute 15). Self-gates to users whose local time is `BRIEFING_LOCAL_HOUR` (default 20:00) and claims once per local date. Always sends, via the committed lane.
- **World briefing:** on-demand "catch me up" snapshot via `model_provider.grounded()` (`TIER_GROUNDED` = `gemini-2.5-flash` + Google Search). Fills the empty state for cold-start users with no ranked pool yet. Cached **per region** (`WORLD_BRIEFING_CACHE_TTL_SECONDS`, 30 min); forced refresh rate-limited per user (`WORLD_BRIEFING_REFRESH_COOLDOWN_SECONDS`, 5 min).
- **Flutter:** `briefing_screen.dart` + `briefing_viewmodel.dart` render `daily_briefing.dart`.

## Background Chat Completion

When a client disconnects mid-turn (app backgrounded, network drop), the backend hands the turn off to Cloud Tasks for completion rather than dropping it.

- `backend/src/services/chat_completion/` owns this path. `prompt_builder.py` was extracted from `chat.py` and lives here.
- Stable message ID `<cmid>::reply` lets the client hydrate the reply on reconnect without duplicates.
- Per-turn `tool_idempotency` map prevents double-firing side-effecting tools on task retry. `send_email` is intentionally excluded from regen (a sent email can't be un-sent).
- On completion, the backend fires a "Buddy replied" push notification so the user knows to return.
- Client picks up the reply via the stable ID when it reconnects; no polling needed.

## Topic Tracking

`backend/src/services/tracking/` is the generic "keep me posted on X" engine, rebuilt 2026-07-10 around **fixtures + moments + fact transitions** after the poll-grid design flooded a user with 19 same-topic pushes in one day (unstable label-derived identity forked parallel polling series; text-hash dedup passed every reworded composition).

- Users create trackers via the `track_topic` tool in chat or voice (voice stays minimal-topic, no inline research).
- **Fixtures** (`tracked_topics/{key}/fixtures/{fixture_id}`): one doc per real-world fixture; id minted ONCE from the start slot (`fixture_matcher.py`), NEVER from the label. Reconcile injects stored fixtures into the research prompt so the LLM echoes ids it recognizes; a reworded label updates the SAME doc. Fixtures carry structured fact state (`status/score/winner/note`).
- **Moments** (`moments.py`): at most pre (T-30) + kickoff + result per fixture, deterministic doc ids `{key}__{fixture}__{moment}` in the `checkpoints` due-queue (same `(status, fire_at)` index). Pre/kickoff are fetchless with hard too-late abstain guards. Result fetches (temporally filtered via `not_before`), extracts facts at temp 0, and re-arms +12min max 5 when the outcome isn't determinable yet, the only polling left.
- **Fact gate** (`fact_gate.py`): a push requires a forward STATUS TRANSITION on the fixture, committed via transactional CAS (`commit_fact_transition`, which also replaced the fire lease). Dedup keys derive from `(topic, fixture, destination-state)`, wording-independent, so a reworded same-fact composition can never re-send.
- **Caps**: 8 tracker pushes/day/topic/user (`try_claim_tracker_daily_slot`); `wake_override` results bypass but still count. Still COMMITTED lane priority 95.
- **Audit**: every moment fire writes a decision row (sent or abstained, with prior/seen facts) under `fixtures/{id}/fires/`.
- **Pulse** survives for developments between fixtures, gated by `development_key` slugs against the topic's last 20 (not string equality).
- The news lane (signal engine) suppresses candidates matching an active tracker's topic for 24h after a tracker send (`scoring_loop._matched_tracked_topic`).
- Fetch tier: rss -> newsdata -> brave -> grounded, unchanged (`topic_fetcher.py`).
- Legacy poll-grid checkpoints are expired on sight by the fire path; `scripts/migrate_tracking_to_fixtures.py` (manual, dry-run default) sweeps the backlog and re-reconciles topics into fixtures. The old `schedule_builder.py` is deleted; the legacy phase constants (`live`/`post`/`milestone`) survive only inside `moments.is_legacy_poll_phase` + the migration script, permanently, as the stray-doc guard. `scripts/audit_tracking_topic.py --fires` shows a topic's fixtures + per-fire audit decisions.

## Buddy Everywhere / Android IME

A custom Android keyboard IME that brings Buddy into any app on the device.

**Composing pipeline (M1-M8):**
- `WordComposer` gated on `FieldProfile.predictionsAllowed` / `autocorrectAllowed` / `learningAllowed`.
- Word prediction: `PrefixIndex` + `BaseDictionary` (en_50k MIT asset) + tiered `SuggestionRanker` + adaptive suggestion bar (tappable orb).
- Personal dictionary: hand-rolled SQLite + `SystemUserDictionary` + learn-on-commit.
- Spell-check squiggle + autocorrect-on-separator + undo (`SpellChecker` / `Autocorrector`).
- Shift FSM: NONE / SHIFTED / CAPS_LOCK + sentence auto-cap (`ShiftState` / `SentenceCapitalizer`).
- Long-press accents + Gboard-style key-preview popup (`KeyPopupOptions` / `KeyTouchHandler`).
- Backspace hold-repeat + swipe-word-delete (`BackspaceRepeat` / `BackspaceTouchHandler`).
- Consent-gated `GET /keyboard/vocab` endpoint (interest subjects + storyline entities → known-word tokens); client `VocabHintsCache`.

**Field-type-aware layouts (`FieldProfile`):** numeric / phone / PIN / email / URL layouts; prediction bar suppressed in non-text and secure fields.

**Voice handoff:** "Talk to Buddy" chip fires a safe `aura://voice` deep link. The app handles the link, sends `screen_context` over the data channel, and opens the voice session. In-keyboard LiveKit duplex is deferred (device attestation + WebRTC-pin gated).

**Password chip:** `StrongPassword` chip generates a strong password; OS autofill saves it.

**Backend endpoints:** `POST /keyboard/draft` (AI draft, uses `prompt_builder` + `field_type`), `GET /keyboard/vocab` (consent-gated interest tokens). Prompt-injection risk from `context_before`: always pass it inside a delimited block, never interpolated raw.

## LLM Fallback Hardening

Every backend LLM call has an explicit fallback chain so a provider outage or quota exhaustion degrades gracefully rather than silently failing.

- **Anthropic chains:** `balanced()` -> `expert()` -> `reason_turn()` each chains down on failure.
- **Cross-provider chat fallback:** Sonnet -> Haiku -> Gemini Flash via `gemini_chat_fallback.py` (streaming + tool-call adapter).
- **Notification rewriter** uses `cheap()`.
- **Embedder** has no fallback by design: a failed embed returns `[]` and logs loudly. The pool re-ingests on the next cycle.
- **Voice** is the gold standard and already had multi-tier fallback before this pass.

## UI System

Warm **cream / light** design system (glass-style surfaces over cream) in `lib/core/theme/`. The single theme is `AppTheme.dark` in `app_theme.dart` (getter name kept for its one call site; returns light cream `ThemeData`, `Brightness.light`). Dark status-bar icons set globally in `app.dart`.

- **`app_colors.dart`:** `accentBase` teal (`0xFF1EC8B0`); `background`/`deepBackground` (`0xFFF4EEE2`) warm cream; `textPrimary` warm charcoal (`0xFF272622`). Glass tokens (`glassWhiteFill`/`glassBorderLight`/`glassBorderDim`) are warm-charcoal low-alpha tints (names retained from the old dark theme).
- **`glass_card.dart`:** `GlassCard` (real `BackdropFilter` σ=12, static non-scrolling only, always in `RepaintBoundary`); `FauxGlassCard` (gradient+border, no blur, use in all scroll lists/bubbles/tiles/pills); `GlassIconButton`; `AmbientBackground`.
- **Performance rule: never put `BackdropFilter` inside a `ListView`/`GridView`. Use `FauxGlassCard` there.**

**Chat rendering performance.** Streamed tokens must NOT flow through `notifyListeners()` (that rebuilds the whole `Consumer<ChatViewModel>` screen and re-parses every visible Markdown bubble per token). Live streaming publishes through `ChatViewModel.streamingOutput` (a `ValueNotifier<StreamingSnapshot>`); the streaming bubble in `chat_message_list.dart` is a `ValueListenableBuilder` so a token repaints only that one bubble. `isStreaming` still notifies once at start/end to insert/remove the slot; auto-scroll lives in the streaming builder. Selection is list-level: ONE `SelectionArea` wraps the list, never `selectable: true` per bubble (the main scroll-jank source).

**AppShell** (`app_shell.dart`) is the persistent shell around the single Home surface: a `Scaffold` whose body wraps the child in `AmbientBackground`. There is no bottom navigation (Home is the only tab), so there is no `extendBody` and no floating nav bar to pad around.

## Auth

`AuthViewModel` subscribes to `authRepository.userModelStream` (Firebase `authStateChanges()`); the router's `refreshListenable: authViewModel` handles redirects. Sign-in supports Google + Email/Password; account creation is an explicit "Create account" flow (sign-in does **not** auto-create).

**Error mapping** lives in `FirebaseAuthService._mapSignInError` / `_mapSignUpError` (VM/UI only render `AppException.message`):
- `user-not-found` / `wrong-password` / `invalid-credential` collapse into one "Wrong email or password" (required by Firebase Email Enumeration Protection, which returns `invalid-credential`). Do not split them.
- `network-request-failed` maps to an offline message in both maps and the Google path. Never tell an offline user their password is wrong.
- Google sign-in **cancellation** is swallowed in `AuthViewModel.signInWithGoogle` (returns to idle, no banner).

## Onboarding

New accounts are stamped `onboarding_complete: false`; accounts without the field default to `true`. The router redirects authenticated routes to `/onboarding` when `AuthViewModel.needsOnboarding` is true.

**Flow:** `/onboarding` (`OnboardingScreen`, 4-slide PageView) -> pushes `AuraConsentScreen` (age gate + Aura consent). `AuraConsentScreen` writes atomically via `OnboardingRepository.saveOnboardingResult`: `onboarding_complete: true`, `date_of_birth`, `aura_consent_granted` (forced false under 18), `aura_consent_timestamp`. After the write it calls `AuthViewModel.markOnboardingComplete()` then `context.go('/home')` explicitly (the screen was pushed via `Navigator.push`, so explicit navigation is needed).

`user_aura_extractor.py` reads `users/{uid}.aura_consent_granted` before every extraction and returns early if not granted. This is the GDPR gate. Explicit `false` blocks extraction; absent field (legacy accounts) does NOT block it.

## Paywall

`/paywall` renders `PaywallScreen` with Free, Companion ($19.99/mo, $191/yr, `aura_companion_monthly`/`aura_companion_annual`), Pro ($34.99/mo, $335/yr, `aura_pro_monthly`/`aura_pro_annual`). 45-day free Companion trial (`kTrialDurationDays` in `subscription_plan.dart`).

**Beta interest-capture mode:** real IAP disabled. Tier CTAs call `SubscriptionViewModel.captureInterest(tier, annual)` (fires PostHog `paywall_intent`, writes `users/{uid}/payment_intent/{tier}_{period}`, shows an ack dialog). `purchaseCompanion`/`purchasePro` are wired but unused; switch CTAs back when payments go live.

## Run

Backend API:
```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker (run `download-files` once first):
```powershell
python -m backend.src.agent.voice_agent download-files   # once, first time
cd backend && python -m src.agent.voice_agent start
```

Flutter app (analyze first to catch compile errors before the Gradle build):
```powershell
flutter analyze
# Release App Bundle. --obfuscate strips Dart symbols into build/symbols/ (keep to deobfuscate crashes).
flutter build appbundle --release --obfuscate --split-debug-info=build/symbols
```
The `.aab` on disk (~90MB) is NOT the user download: Play strips the R8 mapping + native symbols and delivers one ABI per device. Check the real number in Play Console -> App bundle explorer -> Download size.

Production backend URL: `https://juno-backend-620715294422.us-central1.run.app`

Deploy backend (from repo root, requires Git Bash). The voice worker is no longer deployed here: it runs on LiveKit Cloud Agents and ships separately via `lk agent deploy` (see `backend/deploy.sh` header):
```powershell
& "C:\Program Files\Git\bin\bash.exe" backend/deploy.sh juno-2ea45 us-central1
```

Legal pages: `https://auravoiceapp.com`, `/privacy-policy`, `/terms-of-service`.

### Dark deploy (test a backend change on your phone first)

`deploy.sh` shifts 100% of traffic immediately. To test on your phone first (same prod Firestore, only your `users/{uid}` docs), deploy a dark candidate at 0% traffic:
```powershell
# 1. Build & push
docker build -f backend/Dockerfile.api -t gcr.io/juno-2ea45/juno-backend:latest backend
docker push gcr.io/juno-2ea45/juno-backend:latest

# 2. Deploy dark, 0% traffic, tagged URL. Inherits live env/secrets (add --set-secrets only for a NEW one).
gcloud run deploy juno-backend --image=gcr.io/juno-2ea45/juno-backend:latest `
  --region=us-central1 --project=juno-2ea45 --no-traffic --tag=candidate

# 3. Point the phone build at the candidate.
flutter run --dart-define=API_BASE_URL=https://candidate---juno-backend-620715294422.us-central1.run.app `
            --dart-define=WS_BASE_URL=wss://candidate---juno-backend-620715294422.us-central1.run.app

# 4. Good -> promote: gcloud run services update-traffic juno-backend --region=us-central1 --project=juno-2ea45 --to-tags=candidate=100
#    Bad  -> do nothing; users never saw it.
```
`API_BASE_URL`/`WS_BASE_URL` override the dev backend in `lib/core/config/environment.dart` (empty -> prod). A tagged URL always routes to its revision even at 0%. **Caveat:** any all-users write (collection-group batch / migration / backfill) can't be dark-tested on shared prod Firestore; gate those behind an explicit trigger flag.

### Error handling and user-facing copy

Audience is 18-35. Copy is casual, blames the tech not the user, always points at the next action. Never leave a user-facing wait unbounded; every wait needs a timeout ending in a visible message.
- **Flutter HTTP** centralized in `ApiClient` (`lib/core/network/`) with per-call timeouts + exponential-backoff retries (constants in `core/constants/app_constants.dart`). The SSE chat stream never retries once the server accepts it.
- **Never blame the network for non-network failures.** `ApiClient` error mapping must distinguish timeout / server error / auth failure from real connectivity loss. Only show "check your connection" when the root cause is provably network-side.
- **Voice silence watchdog** (`voice_session_service.dart`, `_replyWatchdogTimeout` = 15s) covers "agent connected but never speaks". Arms on agent join and after each user turn, resets on any sign of life, emits a coded `session.error` -> friendly copy in `HomeViewModel._toVoiceErrorMessage`.
- **Backend voice** publishes `session.error` down the LiveKit data channel on pipeline failure; `classify_pipeline_error` (`voice/errors.py`) splits `provider_unavailable` from generic.
- **Voice telemetry:** PostHog `voice_first_response` / `voice_error {code}` from the client; backend logs structured `VoiceSession: failure` lines.

### Pre-deploy checklist

Before deploying the backend, verify it starts cleanly (catches broken imports before Docker):
```powershell
cd backend && python -c "import src.main; print('OK')"
```

### Database field verification

Whenever you change DB logic (a query, a field read/write, a backup/restore path), FIRST verify which fields actually exist on the target documents: read the writer (or inspect a live doc), confirm the exact names, then proceed stating your justification. Do not assume a field exists.

A query filtering on a field no document has does not error, it returns zero rows silently, identical to "no data." That exact mistake (an FCM active-user query filtering `last_seen` while the writer only wrote `registered_at`) caused a 4-day notification outage.

Defend every field-name contract three ways: a single shared constant/accessor (writer and all readers reference it), a writer->reader round-trip test that breaks CI on a rename, and a loud WARNING/ERROR when a query returns nothing while the data is clearly non-empty. Never let "zero rows" and "healthy" look the same.

### Firestore index maintenance

Whenever you add/change a query using `collection_group(...)`, an inequality, an `order_by`, or filters on multiple fields, you MUST also declare the matching index in `firestore.indexes.json` and deploy with `firebase deploy --only firestore:indexes --project juno-2ea45`. A `collection_group` query ordered/filtered by a field needs an explicit `COLLECTION_GROUP` field override; it is never auto-created.

A missing index throws a 400 at runtime, not at deploy/import. If swallowed (caught -> `[]`) it looks identical to "no data" (this is what stopped notifications on 2026-06-01). Declaring a field override **disables automatic single-field indexing for that path**, so list every scope you still need (`COLLECTION` asc/desc plus the `COLLECTION_GROUP` entry).

### httpx redirect behavior

`httpx.AsyncClient` does NOT follow redirects by default. Any external call that may redirect (http->https, domain change) must use `follow_redirects=True` or it silently fails with a 3xx.

### Dependency upgrade discipline

When bumping a `>=X.Y` bound in `pyproject.toml`, check the changelog for breaking changes first.

Every plugin imported from `livekit.plugins` anywhere in the voice worker (`voice_agent.py` silero prewarm + the `voice/` package) must have a matching `livekit-agents[...]` extra in `pyproject.toml`. A missing extra passes all local checks but crashes the worker Docker image at startup (`ImportError: cannot import name '<plugin>'`), failing the Cloud Run deploy after the full build. `test_voice_worker_deps.py` guards this.

livekit_client uses `SCREAMING_CASE` enum values (`ParticipantKind.AGENT`). Run `flutter analyze` after any livekit upgrade.

Adding/upgrading an Android plugin can fail Gradle with `Inconsistent JVM Target Compatibility Between Java and Kotlin Tasks`. Handled centrally in `android/build.gradle.kts`: a `subprojects { afterEvaluate { ... } }` block forces both Java and Kotlin jvmTarget to 17 on every module. It must stay registered **before** the `evaluationDependsOn(":app")` block. It forces everything down to 17; a future plugin needing JVM 21 fails with a *different* error (bump the app and this block together).

### Optimistic "applied" caches

A cache that represents "this side effect already happened" must be written **after** the side effect succeeds, never before. Writing it optimistically means one failed attempt permanently desyncs the cache from reality, and every later trigger trusts the stale cache and silently no-ops instead of retrying. This froze the (since-deleted) Flutter desktop overlay from ever showing a window after a first-boot failure (2026-07-03) — see `lessons-learnt.text`.

## Prompt & Voice Context Engineering

System prompts (`BUDDY_CHAT_SYSTEM_PROMPT` in `settings.py`, `VOICE_PROMPT` in `agent/voice_prompt.py`) follow Anthropic + OpenAI house rules: XML-tagged sections, motivation stated inline, affirmative framing, few-shot `<example>` blocks, and for long prompts the few hard rules restated at the very end.

**Length: signal-to-noise beats word count.** No magic threshold; adherence tracks structure and placement, not raw length. Attention is highest at the START and END of context; the middle decays. A tight 500-token prompt out-follows a rambling 5k one. Real failure mode is competing/buried/duplicated instructions, fixed by structuring and de-duping, never by cutting words that carry signal. Teach a CATEGORY + a test + diverse examples, never a fixed enumerated list (a fixed list overfits: "live scores" without "fixtures/schedule" let Buddy fabricate a World Cup fixture list).

**Grounding decision (in both prompts):** before stating any fact, ask "could this have changed since training, or need a lookup?" If yes/unsure, `web_surf` FIRST and answer ONLY from the result; if no (settled fact, user's own data, opinion) answer directly. Never state a specific live detail you didn't fetch. Non-obvious: "how many countries in the EU now" and "is that cafe still open" are changeable (web_surf); "capital of France" is settled but "mayor of Paris" is not.

**Voice context budget: lean live prompt + async digest + on-demand recall.** Voice `instructions` are built ONCE per session in `BuddyAgent.__init__`, so whatever you bake in rides every turn. A 10k-token blob decays instruction-following (lost-in-the-middle). So: live instructions = persona + rules + a COMPACT digest (target <= ~1-1.5k tokens; `archive_context`/`last_session`/`memory_summary` are digests). Build/refresh the digest ASYNC, off the user's turn. The full transcript persists in the archive (nothing is lost); fetch old details via a tool on demand instead of carrying them hot.

**Voice does not do inline research.** `track_topic` on voice takes only a minimal topic (name + rough event) and returns immediately; reconcile + pulse enrich happens on the next scheduler tick. The 8s voice turn cap makes full grounded research too risky (cancellation mid-stream leaves partial state).

**Free-tier "1 minute left" warning:** the LLM must NOT track time. The server/client owns the countdown; at T-60s inject a one-shot instruction via `generate_reply` so the model weaves it in at the next turn boundary in Buddy's voice (same mechanism `voice/recorder.py:148` uses for the away-nudge). Fire it ONCE behind a guard flag. At T-0 queue ONE graceful wind-down line, then end the session (never hard-cut to silence; never `say()` a canned line over the user mid-sentence).

## Stream Contract

Real-time data is exposed as `Stream<T>` via `async*` generators, cold, recreated per subscription. `StreamController.broadcast()` is allowed only for multiple independent subscribers (FCM in `NotificationService`, LiveKit room events in `VoiceSessionService`) and must be closed in `dispose()`. Never use a `StreamController` for single-subscriber UI streams; use `async*`.

## Service State Contract

Services may hold **lifecycle state** (connection handles, auth user ID, stream subscriptions, init flags) but **not** per-request transient state as instance fields. Request context lives in the call frame (locals + the `Future`/`Stream` chain), never on the instance.

## Service Interface Pattern

Chat/AI streaming goes through `abstract class ChatServiceProvider`; `BackendApiService` (prod) and `StubChatServiceProvider` (dev) implement it, selected at DI time in `lib/di/providers.dart`. Only `ChatViewModel` and subclasses depend on it; non-chat calls (`deleteAccount`, `analyzeNutrition`) stay on `BackendApiService` directly.

## Widget Purity

Widgets in `lib/presentation/widgets/` are purely presentational: no `context.read/watch/select` or `Provider.of`. All data and callbacks come via constructor params. Only screens in `lib/presentation/screens/` read from Provider.

## Component Presets

Use `FauxGlassCard` named constructors for standard configs: `.pill`, `.navTile`, `.section`, `.toggleTile`, `.destructiveButton`. Custom gradient or dynamic border color may use the default constructor.

## Naming Conventions

Names describe what something is or does in plain terms. Constants state full context (`EXCLUDED_TOOLS_FOR_GENERAL_CHAT` not `_CHAT_EXCLUDED_TOOLS`); functions name action + subject (`_get_user_local_datetime` not `_formatted_now`). Avoid abbreviations. If a name needs a comment to explain it, rename it.

## Working Style

This is a production app serving real users. Every change defaults to scalability, maintainability, and robustness, with a bird's-eye view of downstream impact: data integrity, other users, deploy safety, and backward compatibility with already-released app clients. A change that works on one device but breaks shared Firestore, the live backend revision, or older installed app versions is a regression. When in doubt about blast radius, treat it as production-impacting and say so.

**No feature flags.** Features ship unconditionally on. Never add a `*_ENABLED` setting or boolean gate. If a feature isn't ready for everyone, don't merge it.

- Start every response with the actual answer after acknowledging the question.
- Show options before acting. If uncertain about any fact, date, or quote, say so explicitly. Never fill knowledge gaps with plausible-sounding information.
- Simple questions get short answers; complex tasks get detailed ones. Never pad with restatements of the question or closing recaps.
- Before significantly altering content I've already created (rewriting sections, removing paragraphs, restructuring, changing tone), stop, describe exactly what you'll change and why, and wait for my confirmation. "I think this would be better" is not permission.
- Only change what I asked you to change. Don't refactor, rename, reorganize, or reformat anything else. If you notice something to improve elsewhere, mention it at the end; don't touch it.
- Before deleting any file, overwriting code, dropping records, or removing dependencies, stop, list exactly what will be affected, and ask for explicit confirmation.
- Never commit, send, post, publish, or schedule anything on my behalf without my explicit confirmation in the current message.
- After an editing/writing task, end with a brief status update: what changed, what was left untouched, what needs my attention. Keep it short.
- Never use em-dashes anywhere; use plain human phrasing, not AI-sounding wording.

## Design Docs

Live in `~/.gstack/projects/varuntej07-juno/` (timestamped markdown). Latest approved: `varun-main-design-20260525-175702.md` ("Aura Beta Launch, Voice Accountability for ADHD Adults", 26-day plan to ship to 10-20 testers; gate: do 3+/10 say voice matters AND they'd pay?). Reactive orchestration design (full event-driven replacement for the 7 isolated producers): `varun-main-design-20260629-133832-reactive-orchestration.md`, phased P0-P6 (tasks #5-12). Icebreaker and thread follow-up dispatch are built on it (`backend/src/services/reactive/`, see Reactive Orchestration Engine above) but uncommitted, deploy status unconfirmed; remaining phases not yet confirmed built.

## Skill routing

When a request matches an available skill, invoke it via the Skill tool as your FIRST action (skills have specialized workflows). Applies to EVERY installed skill; each skill's description states when it fires, invoke on your own initiative, don't ask "should I run X?".

**Boundaries (Working Style always wins):** anything that commits/pushes/deploys/sends/publishes/schedules needs my explicit confirmation, even via a skill (`/ship`, `/land-and-deploy`); run up to the outward action then stop. Anything destructive stops for confirmation first. If two skills match, name the choice in one line and proceed with the best fit.

Routing: product ideas/brainstorming -> office-hours · strategy/scope -> plan-ceo-review · bugs/errors -> investigate · ship/PR -> ship · QA -> qa · code review -> review · update docs -> document-release · retro -> retro · design system -> design-consultation · visual audit -> design-review · architecture -> plan-eng-review · checkpoint -> checkpoint · health -> health.

## Specialist agents

Project-local subagents in `.claude/agents/`. Delegate via the Agent tool when a task matches; otherwise handle inline.
- **python-backend-engineer** -> FastAPI/Python work in `backend/` (the closest fit to this repo's stack).
- **senior-code-reviewer** -> comprehensive review before deploy (security, architecture, performance). Stack-agnostic.
- **backend-typescript-architect** -> TS/Bun backend. Not this repo's stack.
- **ui-engineer** -> JS/TS web frontend. Note the app's UI is Flutter/Dart, not web.
- **react-coder** / **ts-coder** -> Giselle-origin React 19 / TypeScript style agents; ignore their Giselle-specific import rules.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
