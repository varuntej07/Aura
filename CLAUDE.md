# Project Overview

Aura is AI companion app (currently beta testing with 15 users). The assistant persona is Buddy. The app covers text chat, LiveKit voice, reminders, memory, notifications, scheduled agents, Google Calendar and Gmail tools, and live web search.

Keep the project production-grade. Prefer clear working path with scalabiity, maintainabiity, future-proof and robust, most importantly flexible with additional incoming features in the long run. 

**Product soul, Buddy is obsessed with the user, in the best way.** Every feature should feel
like a true virtual companion who is always trying to help and always trying to learn more about
this person: warm, curious, proactive, remembering what matters and asking to know more. When a
product choice is ambiguous, pick the one that makes Buddy feel more like a close friend genuinely
into this person's life, never a neutral tool, a form, or a content feed.

## Architecture

The Flutter app uses MVVM with Provider: screens in `lib/presentation/screens`, ViewModels in `lib/presentation/viewmodels`, repositories in `lib/data/repositories`, services in `lib/data/services`, shared code in `lib/core`, Provider wiring in `lib/di/providers.dart`.

The backend is a FastAPI app in `backend/src/main.py`; handlers in `backend/src/handlers`, services in `backend/src/services`. Scheduled domain agents in `backend/src/agents` only fetch data (`fetch_data`), notification sending is the signal engine's job, not the agents'.

Voice runs through `backend/src/agent/voice_agent.py` as a separate LiveKit worker.
`voice_agent.py` is the thin orchestrator; its pieces, telemetry, error mapping, Firestore fetchers, prompt context, pipeline builders, voice conditioning, and the session event recorder, live in the `backend/src/agent/voice/` package.

`backend/src/services/user_aura_extractor.py` builds a passive behavioral profile per user.
It fires as a fire-and-forget `asyncio.create_task` from the chat handler after every message.
Profile documents live in the `UserAura/{uid}` Firestore collection.
The extractor always passes the user's previous query (`prev_user_query` field) alongside the
current message to Gemini Flash, which decides when prior context is needed, no hardcoded
heuristics. Failed extractions are swallowed silently so the chat stream is never affected.

Interests are stored as a **closed taxonomy, not free text**. `backend/src/services/user_aura_schema.py`
is the single source of truth: ~30 broad categories (+ `other`) that the extraction prompt constrains
Gemini to (off-list values coerce to `other`), each holding the specific subjects named in the message
(e.g. `politics_governance` → `KCR`) with time-decayed weights (30-day half-life) and a per-category cap.
The writer (`apply_interest_signal`) and every reader, chat prompt suffix, voice prompt, notification
framer, and the signal-engine `user_vector` embedding (which embeds subjects, never raw slugs), go
through the schema's accessors, which fall back to the legacy `deep_interest_frequencies` map until a
profile rebuilds. This replaced an earlier design where free-text interest strings were Firestore map keys
and fragmented into 100+ near-duplicate buckets (see `lessons-learnt.text`, 2026-06-04). `deep_interest_frequencies`
is kept on the doc (frozen) for old app clients; the two dead maps (`surface_topic_frequencies`,
`named_entities_seen`) are dropped once a profile reaches 5 categories.

## Signal Engine

`backend/src/services/signal_engine/` is the notification and feed ranking layer.
Signal-engine design + rationale in `backend/docs/signal_engine.md`. The full
push-notification architecture across all three send paths (signal engine, user
reminders, calendar meeting reminders) plus the shared FCM delivery core is in
`SIGNAL_ENGINE_ARCHITECTURE.md` at the repo root.

**How it works:** A content pool (`content_candidates/`) holds embeddings (768-dim, `gemini-embedding-001`) fetched by data fetchers. A per-user signal store (`users/{uid}/signal_store/state`) tracks a user vector, time-slot open rates, category affinities, and fatigue. Every 15 min, `scoring_loop.run_tick()` scores candidates with pure math, and only the top candidate above threshold gets one Gemini Flash call to frame the copy before FCM send.

**Endpoints:**
- `POST /events`, Flutter reports user events (taps, dismissals, skips, app opens). Updates user vector via EMA.
- `GET /feed/recommend`, ranked content feed for in-app display.
- `POST /internal/signal-engine/tick`, Cloud Scheduler every 15 min. Runs scoring and sends notifications.
- `POST /internal/signal-engine/content-ingest`, Cloud Scheduler hourly. Pulls HN, arXiv, ESPN Cricinfo RSS, and global Google News RSS into the pool.
- Sports ingest (cricbuzz live match scores only) runs inside `/scheduler/tick` every 30 min via a `minute % 30 == 0` gate, no separate scheduler job needed. Broader sports headlines now arrive via Google News RSS in the hourly content-ingest; the old Gemini-grounded web search for leagues was removed.

**`/scheduler/tick` runs every minute** (`juno-reminder-tick` Cloud Scheduler job). Use `minute % N == 0` gating inside `handlers/scheduler.py` to piggyback any periodic work at N-minute intervals without creating new scheduler jobs.

**Out of scope for the signal engine:** calendar meeting reminders and the post-nutrition-scan engagement chain stay on their existing LLM paths (`daily_notification/orchestrator.py` runs only the calendar reminder pipeline).

### Notification re-engagement funnel (PostHog)

Signal-engine notifications are instrumented as a 4-step PostHog funnel so "which notification earns a tap and a reply" is measurable, not guessed:
`signal_notification_sent` (server, `scoring_loop` after delivery) → `notification_tapped` (client, filter `notification_origin == signal_engine`) → `signal_session_from_notification` (chat opens from the tap) → `signal_action_after_notification` (user's first reply in that thread).

The event names and join-key property names live in ONE place per side: `backend/src/services/analytics/funnel_events.py` and `lib/core/analytics/funnel_events.dart`. `backend/tests/test_funnel_event_contract.py` reads the Dart file and fails CI if the two drift, a rename on either side breaks the build instead of silently flattening the funnel.

Server capture goes through `analytics/posthog_client.py`, fire-and-forget, a no-op when `POSTHOG_API_KEY` is unset, never raises into a scoring tick. It reuses the public `phc_` project key the app already embeds (`POSTHOG_API_KEY` / `POSTHOG_HOST` in `settings.py`, wired in `deploy.sh`); `run_tick` logs a loud WARNING if it sends while the key is unset (funnel-blind). Tapping a `signal_engine` notification opens chat seeded with `opening_chat_message` via `dispatchNotificationTap` → `signalNotificationTapStream` → `/chat/new`, and `ChatViewModel.loadSignalNotificationContext` fires the session event and arms the first reply once.

### Curiosity thread engine (open-loop notifications): flag-gated, not yet deployed

A 4th notification decider in `backend/src/services/threads/`. A *thread* is a hole in what Buddy knows about the user (v1 source: reminders; the reminder text becomes the thread with no LLM cost). An hourly reflector (`thread_reflector.run_reflection_tick`, piggybacked on `/scheduler/tick` at `minute == 0`) selects at most one open loop and asks ONE warm, curious question ("what's that about?", never "did you finish?"); the answer enriches `UserAura` through the same extractor every chat turn uses. The whole feature is gated by `settings.THREAD_ENGINE_ENABLED` (default **off**).

- **Tap routing:** `notification_type == "thread_followup"` → `dispatchNotificationTap` → `threadFollowUpTapStream` → `/chat/new` → `ChatViewModel.loadThreadFollowUpContext`. Routing keys off `notification_type`, not the (vestigial) `deep_link` field.
- **Android interactive chips:** the follow-up is sent `data_only=True` (see `notification_service.send_notification`) so the app builds the notification locally with RemoteInput suggestion chips (`thread_notification_handler.dart`). Because it's a *local* notification, a body tap is delivered to the local-notifications handler, not `onMessageOpenedApp`, it's bridged via `threadBodyTapStream` + `handleThreadNotificationColdLaunch()` (terminated launch) and relayed by `NotificationService._relayThreadBodyTap`. iOS has no dynamic chips: plain push → in-chat pills.
- **Silent shade reply:** `POST /threads/reply` persists the exchange to a server-authoritative `users/{uid}/threads/{id}/messages` subcollection (the main chat is client-owned), flips the thread to `engaged`, enriches the aura, and returns Buddy's reply for the notification shade. `GET /threads/{id}/messages` lets the client reconcile that exchange when the thread is opened.
- **Funnel:** mirrors the signal funnel via the shared `funnel_events` files, `thread_followup_sent` → `notification_tapped` (filtered `notification_origin == thread_engine`) → `thread_session_from_notification` → `thread_reply` (fired server-side for shade replies, client-side for in-chat).

### Unified notification budget: flag-gated, not yet deployed

`backend/src/services/notification_budget.py` is one per-user daily ceiling + spacing that every PROACTIVE decider (signal engine, threads, engagement) claims a slot from, so they can't each spam from their own independent cap. Committed sends (user reminders, calendar meeting reminders) are never blocked but are recorded so a proactive push is spaced away from them. Gated by `settings.UNIFIED_NOTIFICATION_BUDGET_ENABLED` (default **off**); additive on top of each decider's own sub-cap, and fail-open (a budget read error allows the send, never an outage).

## Daily Briefing: flag-gated, not yet deployed

`backend/src/services/briefing/` is a synthesized morning digest plus an on-demand world snapshot. Two endpoints: `GET /briefing/today` and `POST /briefing/world`.

- **Daily briefing:** the signed-in user's morning digest, woven in ONE LLM call from the top-ranked content-pool items (`BRIEFING_CANDIDATE_POOL`, default 8). The fan-out (`run_briefing_tick` in `briefing/briefing_engine.py`) piggybacks `/scheduler/tick` at `minute % 15 == 5`, offset from the thread reflector (minute 0) and icebreaker (minute 15) so the LLM passes never burst together. It self-gates: it only generates for users whose local time is `BRIEFING_LOCAL_HOUR` (default 06:00) and claims once per local date, so running it 4x/hour stays cheap. Gated by `settings.DAILY_BRIEFING_ENABLED` (default **off**).
- **World briefing:** an on-demand "catch me up on the world" snapshot (cold-start empty-state fill + refresh icon), built through the new `backend/src/services/model_provider.py` `grounded()` primitive (`TIER_GROUNDED` = `gemini-2.5-flash` with Google Search grounding, live web search + synthesis in one call). The result is identical for everyone in a region, so it is cached **per region** (`WORLD_BRIEFING_CACHE_TTL_SECONDS`, 30 min) rather than per user, which caps grounded calls at roughly one per region per window. A forced refresh is rate-limited per user by `WORLD_BRIEFING_REFRESH_COOLDOWN_SECONDS` (5 min).
- **Flutter:** `briefing_screen.dart` + `briefing_viewmodel.dart` render the `daily_briefing.dart` model; the chat handoff loads a briefing item as conversation context. Briefing interactions are instrumented through the shared `funnel_events` files.

## UI System

The app uses a warm **cream / light** design system (glass-style surfaces over a cream canvas) defined in `lib/core/theme/`. The single theme is `AppTheme.dark` in `app_theme.dart` — the getter name is kept for its one call site, but it now returns the light cream `ThemeData` (`Brightness.light`); dark status-bar icons are set globally in `app.dart`.

**`app_colors.dart`**, color constants: `accentBase` is teal (`0xFF1EC8B0`, kept); `background`/`deepBackground` (`0xFFF4EEE2`) is the warm cream base; text is warm charcoal (`textPrimary` `0xFF2B2A26`). The glass tokens `glassWhiteFill`/`glassBorderLight`/`glassBorderDim` now hold **warm-charcoal low-alpha tints** (names retained from the old dark theme; only `glassHighlight` stays a soft white sheen) so faux-glass surfaces read on cream; `glassOrb1`/`glassOrb2` are faint teal ambient orbs.

**`glass_card.dart`**, primitives: `GlassCard` (real `BackdropFilter` σ=12; static non-scrolling only; always in `RepaintBoundary`); `FauxGlassCard` (gradient+border, no blur, use in all scroll lists, message bubbles, tiles, pills); `GlassIconButton` (circular blurred icon button for app bars); `AmbientBackground` (`Stack` of two radial orbs over `deepBackground`, wrap screens that need something to blur).

Performance rule: never put `BackdropFilter` inside a `ListView` or `GridView`. Use `FauxGlassCard` there instead.

**Chat rendering performance.** Streamed tokens must NOT flow through
`notifyListeners()` — that rebuilds the whole `Consumer<ChatViewModel>` screen
(AppBar, input, and the entire `ListView`, re-parsing every visible Markdown
bubble) on every token. Live streaming is published through
`ChatViewModel.streamingOutput` (a `ValueNotifier<StreamingSnapshot>`); the
streaming bubble in `chat_message_list.dart` is a `ValueListenableBuilder` bound
to it, so a token repaints only that one bubble. `isStreaming` still notifies
(once at stream start, once at end) to insert/remove the slot; auto-scroll lives
in the streaming builder, not the screens. Selection is a list-level concern: the
list is wrapped in ONE `SelectionArea`, never `selectable: true` per bubble
(per-bubble selectable was the main scroll-jank source — see `lessons-learnt.text`,
2026-06-13).

**AppShell** (`lib/presentation/screens/app_shell.dart`), wraps child in `AmbientBackground`, uses `extendBody: true` so content flows under the floating nav bar. The floating glass nav bar is ~58px tall. Screens that scroll to the bottom must add `SizedBox(height: MediaQuery.of(context).viewPadding.bottom + 96)` at the bottom to avoid content being hidden.

## Auth

`AuthViewModel` subscribes to `authRepository.userModelStream` (Firebase `authStateChanges()`), so
auth state updates reactively (no polling) and the router's `refreshListenable: authViewModel`
handles redirects. Sign-in supports Google + Email/Password; account creation is an explicit
"Create account" flow (sign-in does **not** auto-create). The home drawer checks `authVm.user != null`
and shows a sign-in button when unauthenticated, hiding the session list.

**Error mapping** lives in `FirebaseAuthService._mapSignInError` / `_mapSignUpError` (data layer; the VM/UI only render `AppException.message`):
- `user-not-found` / `wrong-password` / `invalid-credential` collapse into one "Wrong email or password" message, required by Firebase **Email Enumeration Protection** (on by default), which returns `invalid-credential` instead of the granular codes. Do not split them.
- `network-request-failed` is mapped explicitly to an offline message in both maps and the Google credential path, never tell a user their password is wrong when they're actually offline.
- Google sign-in **cancellation** is swallowed in `AuthViewModel.signInWithGoogle` (returns to idle, no red banner), backing out of the account picker is a normal action, not an error.

## Onboarding

New accounts are stamped `onboarding_complete: false` at creation; accounts without the field
default to `true` (never shown the flow). The router redirect sends any authenticated route to
`/onboarding` when `AuthViewModel.needsOnboarding` is true.

**Flow:** `/onboarding` (`OnboardingScreen`, 5-slide PageView) → pushes `AuraConsentScreen` (age gate + Aura consent toggle).

`AuraConsentScreen` writes all three fields atomically via `OnboardingRepository.saveOnboardingResult`:
- `onboarding_complete: true`
- `date_of_birth: ISO date`
- `aura_consent_granted: bool` (forced false for users under 18)
- `aura_consent_timestamp: ISO datetime`

After a successful write, it calls `AuthViewModel.markOnboardingComplete()` (updates in-memory model) and then `context.go('/home')` explicitly. The screen was pushed via `Navigator.push`, not GoRouter, so explicit navigation is required to clear the mixed stack correctly.

`backend/src/services/user_aura_extractor.py` reads `users/{uid}.aura_consent_granted` before every extraction and returns early if not granted. This is the GDPR gate, behavioural profiling only runs with explicit opt-in.

## Paywall

`/paywall` route renders `PaywallScreen` with three tiers: Free, Companion ($19.99/mo, $191/yr, IDs `aura_companion_monthly` / `aura_companion_annual`), and Pro ($34.99/mo, $335/yr, IDs `aura_pro_monthly` / `aura_pro_annual`). 45-day free Companion trial (extended for beta) via `kTrialDurationDays` in `subscription_plan.dart`.

**Beta interest-capture mode:** Real IAP is disabled. The tier CTAs call `SubscriptionViewModel.captureInterest(tier, annual)` which fires a PostHog `paywall_intent` event and writes `users/{uid}/payment_intent/{tier}_{period}` to Firestore, then shows an acknowledgement `AlertDialog`. The `purchaseCompanion` / `purchasePro` methods on the VM are wired but unused while beta is on, switch the paywall CTAs back to those when payments go live.

## Run

Backend API:

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker (run `download-files` once first to fetch Silero VAD + MultilingualModel ONNX files):

```powershell
python -m backend.src.agent.voice_agent download-files   # once, first time
cd backend && python -m src.agent.voice_agent start
```

Flutter app (run analyze first to catch compile errors before the full Gradle build):

Production backend URL:

```text
https://juno-backend-620715294422.us-central1.run.app
```

Deploy backend + voice worker to Cloud Run (from repo root, requires Git Bash):

```powershell
& "C:\Program Files\Git\bin\bash.exe" backend/deploy.sh juno-2ea45 us-central1
```

### Test a backend change on your phone before all users get it (dark deploy)

`deploy.sh` shifts 100% of traffic immediately. To test on your own phone first (same prod
Firestore, only your `users/{uid}` docs touched), deploy a **dark candidate** at 0% traffic and
point only your debug build at its tagged URL:

```powershell
# 1. Build & push
docker build -t gcr.io/juno-2ea45/juno-backend:latest backend
docker push gcr.io/juno-2ea45/juno-backend:latest

# 2. Deploy dark, 0% traffic, tagged URL. Inherits live env/secrets (add --set-secrets only for a NEW one).
gcloud run deploy juno-backend --image=gcr.io/juno-2ea45/juno-backend:latest `
  --region=us-central1 --project=juno-2ea45 --no-traffic --tag=candidate

# 3. Point the phone build at the candidate (released app can't reach it).
flutter run --dart-define=API_BASE_URL=https://candidate---juno-backend-620715294422.us-central1.run.app `
            --dart-define=WS_BASE_URL=wss://candidate---juno-backend-620715294422.us-central1.run.app

# 4. Good → promote: gcloud run services update-traffic juno-backend --region=us-central1 --project=juno-2ea45 --to-tags=candidate=100
#    Bad  → do nothing; users never saw it.
```

`API_BASE_URL`/`WS_BASE_URL` override the dev backend in `lib/core/config/environment.dart`
(empty → prod). Bare URL = live-traffic revision; a tagged URL always routes to its revision even
at 0%. **Caveat:** any all-users write (collection-group batch / migration / backfill) can't be
dark-tested on shared prod Firestore, gate those behind an explicit trigger flag.

Aura app legal pages (hosted on varuntej.dev portfolio):

```text
https://varuntej.dev/aura
https://varuntej.dev/aura/privacy-policy
https://varuntej.dev/aura/terms-of-service
```

## Reliability Notes

Keep `.env`, service account JSON, OAuth client JSON, and platform Google service files out of
commits (`.env` is intentionally not ignored so variable names stay visible locally). The backend
depends on many external services (Firebase, Anthropic, OpenAI, Gemini, Brave Search, LiveKit,
Deepgram, Cartesia, Google Calendar, Gmail, Cloud Scheduler, Cloud Tasks, FCM), treat every
integration as optional in dev and make failures explicit.

### Error handling and user-facing copy

Audience is 18-30. Error copy is casual, blames the tech not the user, and always points at the next action ("try again", "check your connection"). Never leave a user-facing wait unbounded, every wait needs a timeout that ends in a visible message.

- **Flutter HTTP** is centralized in `ApiClient` (`lib/core/network/`) with per-call timeouts + exponential-backoff retries; timeout constants live in `core/constants/app_constants.dart`. The SSE chat stream never retries once the server accepts it (avoids duplicate tool calls / replayed text).
- **Voice silence watchdog** (`voice_session_service.dart`, `_replyWatchdogTimeout` = 15s) covers the "agent connected but never speaks" hang (e.g. zero LLM credit). It arms when the agent joins (greeting) and after each user turn (reply), resets on any sign of life (agent state, audio, text, data), and emits a coded `session.error`. Codes → friendly copy in `HomeViewModel._toVoiceErrorMessage` (the mic orb is the retry button).
- **Backend voice** (the `voice/` package) publishes a `session.error` down the LiveKit data channel on pipeline failure so the client doesn't wait on its own watchdog; `classify_pipeline_error` (in `voice/errors.py`) splits provider-exhausted/quota (`provider_unavailable`) from generic failures.
- **Voice telemetry:** PostHog `voice_first_response` (success) and `voice_error` `{code}` (failure) fire from the Flutter client; the backend also logs structured `VoiceSession: failure` lines to Cloud Logging.

### Pre-deploy checklist

Before deploying the backend, verify it starts cleanly:

```powershell
cd backend && python -c "import src.main; print('OK')"
```

This catches broken imports before Docker builds them into a crashing container.

### Database field verification

Whenever you change any database logic (a Firestore query, a field read/write, a backup/restore path), FIRST verify which fields actually exist on the target documents before writing the code, read the writer that produces those documents (or inspect a live document), confirm the exact field names, and only then proceed, stating the justification for the field you chose. Do not query or read a field on the assumption it exists.

This is not optional. A query that filters on a field no document has does not error, it returns zero rows silently, which looks identical to "no data." That exact mistake (an FCM active-user query filtering `last_seen` while the writer only ever wrote `registered_at`) caused a 4-day notification outage. See `lessons-learnt.text` (2026-05-31).

Defend every field-name contract three ways: a single shared constant/accessor so the name lives in one place (writer and all readers reference it), a writer→reader round-trip test that breaks CI if either side is renamed, and a loud WARNING/ERROR log when a query returns nothing while the underlying data is clearly non-empty. Never let "zero rows" and "healthy" look the same.

### Firestore index maintenance

Whenever you add or change a Firestore query that uses `collection_group(...)`, an inequality (`>`, `>=`, `<`, `<=`), an `order_by`, or filters on multiple fields, you MUST also declare the matching index in `firestore.indexes.json` (wired via `firebase.json`) and deploy it with `firebase deploy --only firestore:indexes --project juno-2ea45`. Firestore auto-creates single-field indexes only at **collection** scope, a `collection_group` query ordered/filtered by a field needs an explicit `COLLECTION_GROUP` field override, which is never created automatically.

A missing index makes the query throw a 400 at runtime, not at deploy or import time. If that error is swallowed (e.g. caught and returning `[]`), it looks identical to "no data." This is exactly what happened on 2026-06-01: the `fcm_tokens` collection-group query filtering `registered_at >= cutoff` had no `COLLECTION_GROUP_ASC` index, so `list_active_user_ids` returned zero users and notifications silently stopped.

Note that declaring a field override **disables Firestore's automatic single-field indexing for that field path**, list every scope you still need (`COLLECTION` ascending/descending plus the `COLLECTION_GROUP` entry), not just the new one.

### httpx redirect behavior

`httpx.AsyncClient` does NOT follow redirects by default. Any external HTTP call that may redirect (http → https, domain changes) must use `follow_redirects=True` or the request silently fails with a 3xx error.

### Dependency upgrade discipline

When bumping a `>=X.Y` bound in `pyproject.toml`, check the package changelog for breaking API changes before deploying.

Every plugin imported from `livekit.plugins` anywhere in the voice worker, `backend/src/agent/voice_agent.py` (the `silero` VAD prewarm) and the `backend/src/agent/voice/` package (most live in `voice/pipelines.py`), must have a matching `livekit-agents[...]` extra in `pyproject.toml`. A missing extra passes all local checks (the plugin is in the dev venv) but crashes the worker Docker image at startup with `ImportError: cannot import name '<plugin>' from 'livekit.plugins'`, failing the Cloud Run deploy after the full build. `backend/tests/test_voice_worker_deps.py` guards this, it scans `voice_agent.py` plus the whole `voice/` package, so adding a plugin import in any new package module is still covered.

livekit_client uses `SCREAMING_CASE` enum values (e.g. `ParticipantKind.AGENT`, not `ParticipantKind.agent`). Run `flutter analyze` after any livekit upgrade to catch casing mismatches before the full Gradle build.

Adding or upgrading an Android Flutter plugin can fail the Gradle build with `Inconsistent JVM Target Compatibility Between Java and Kotlin Tasks` on that plugin's `:compileDebugKotlin` task. Cause: each plugin pins its own Java and Kotlin JVM targets and they disagree (some pin Kotlin high, some pin Java low), and the only JDK here is Android Studio's bundled JBR 21, so an unpinned Kotlin target defaults to 21. This is already handled centrally in `android/build.gradle.kts`: a `subprojects { afterEvaluate { ... } }` block forces **both** Java (`BaseExtension.compileOptions`) and Kotlin (`KotlinCompile` jvmTarget) to 17 on every module, so the pair is always consistent. That block must stay registered **before** the `evaluationDependsOn(":app")` block (evaluating `:app` eagerly evaluates every plugin module; a later registration throws "Cannot run afterEvaluate ... already evaluated"). Caveat: it forces everything **down** to 17, a future plugin that genuinely requires JVM 21 would fail with a *different* error (a 21-only API / "source release 21" message), at which point bump the app and this block together.

## Stream Contract

Real-time data is exposed as `Stream<T>` via `async*` generators, cold, recreated per subscription.
`StreamController.broadcast()` is allowed only for multiple independent subscribers (e.g. FCM in
`NotificationService`, LiveKit room events in `VoiceSessionService`) and must be closed in
`dispose()`. Never use a `StreamController` for single-subscriber UI streams, use `async*`.

## Service State Contract

Services (`lib/data/services/`, `lib/data/repositories/`) may hold **lifecycle state** (connection
handles, auth user ID, stream subscriptions, init flags) but **not** per-request transient state as
instance fields (e.g. a `_currentRequestId` mid-call). Request context lives in the call frame
(locals + the `Future`/`Stream` chain), never on the instance.

## Service Interface Pattern

Chat/AI streaming goes through `abstract class ChatServiceProvider`
(`lib/data/services/chat_service_provider.dart`); `BackendApiService` (prod) and
`StubChatServiceProvider` (dev) implement it, selected at DI time in `lib/di/providers.dart` (no
`_useStub` flags in production code). Only `ChatViewModel` and subclasses depend on it; non-chat
calls (`deleteAccount`, `analyzeNutrition`) stay on `BackendApiService` directly.

## Widget Purity

Widgets in `lib/presentation/widgets/` are purely presentational: no `context.read/watch/select`
or `Provider.of`. All data and callbacks come via constructor params. Only screens in
`lib/presentation/screens/` read from Provider.

## Component Presets

Use `FauxGlassCard` named constructors for all standard visual configs (don't set raw
`borderRadius`/`padding` inline when a preset fits the card's role). Presets in
`lib/core/theme/glass_card.dart`: `.pill` (pills/chips), `.navTile` (nav/info tiles), `.section`
(padded panels), `.toggleTile` (switch wrappers), `.destructiveButton` (sign-out/delete). Custom
gradient or dynamic border color (per-agent/per-message) may still use the default constructor.

## Naming Conventions

Names describe what something is or does in plain terms. Constants state full context
(`EXCLUDED_TOOLS_FOR_GENERAL_CHAT` not `_CHAT_EXCLUDED_TOOLS`); functions name action + subject so
the return is obvious without reading the body (`_get_user_local_datetime` not `_formatted_now`).
Avoid abbreviations and cryptic prefixes. If a name needs a comment to explain it, rename it.


## Working Style

This is a production application serving real users. Every change must default to scalability,
maintainability, and robustness, with a bird's-eye view of what the change affects downstream , 
data integrity, other users, deploy safety, and backward compatibility with already-released app
clients. A change that works on one device but breaks the shared Firestore, the live backend
revision, or older installed app versions is a regression, not a fix. When in doubt about blast
radius, treat the change as production-impacting and say so.

Start every response with the actual answer.
No preamble, no acknowledgment of the question.
Just the information.

Always show options before acting. If you are uncertain about any fact, statistic, date, quote, or piece of information, say so explicitly before including it.

Never fill gaps in your knowledge with plausible-sounding information.
When in doubt, say so.

Match response length to task complexity.

Simple questions get direct, short answers. Complex tasks get full, detailed responses.

Never compress or summarize work that requires real depth.
Never pad responses with restatements of the question or closing sentences that repeat what you just said.

Before making any change that significantly alters content I've already created (rewriting sections, removing paragraphs, restructuring the flow, changing tone), stop completely.

Describe exactly what you're about to change and why.
Wait for my confirmation before proceeding.

"I think this would be better" is not permission to change it.

Only change what I specifically asked you to change.

Do not rewrite, rephrase, restructure, or "improve" anything I didn't ask about, even if you think it would be better.

If you notice something that could be improved elsewhere, mention it at the end of your response.
Do not touch it unless I explicitly ask you to.

After completing any editing or writing task, always end with a brief summary:
- What was changed: [description]
- What was left untouched: [if relevant]
- What needs my attention: [anything requiring a decision or review]

Keep it short. This is a status update, not a recap of everything you just did.

Never commit, send, post, publish, share, or schedule anything on my behalf without my explicit confirmation in the current message.

Only modify files, functions, and lines of code directly and specifically related to the current task.

Do not refactor, rename, reorganize, reformat, or "improve" anything I did not explicitly ask you to change.

If you notice something worth fixing elsewhere, mention it in a note.
Do not touch it. Ever.

Before deleting any file, overwriting existing code, dropping database records, removing dependencies, or making any change that cannot be trivially undone, stop completely. List exactly what will be affected. Ask for explicit confirmation. Only proceed after I say yes in the current message.

## Design Docs

Design docs live in `~/.gstack/projects/varuntej07-juno/` (timestamped markdown). **Latest approved:**
`varun-main-design-20260525-175702.md`, "Aura Beta Launch, Voice Accountability for ADHD Adults"
(26-day plan to ship to 10-20 ADHD/accountability testers; gate: do 3+/10 say voice matters AND
they'd pay?). Priors: `...20260519-185952` (Buddy accountability positioning, APPROVED),
`...20260513-211359` (Notification Overhaul, global budget + copy, DRAFT).

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

**Proactive invocation, don't wait to be asked.** Applies to EVERY installed skill (including new
plugin skills), not just the table below. Each skill's own description states when it fires ("use
when…"); treat that as the trigger, invoke on your own initiative, don't ask "should I run X?",
don't answer manually when a skill fits.

**Boundaries that override proactive use** (Working Style always wins):
- Anything that commits/pushes/deploys/sends/posts/publishes/schedules needs my explicit
  confirmation in the current message, even via a skill (`/ship`, `/land-and-deploy`), run up to
  the outward action, then stop and confirm.
- Anything destructive or hard to undo (deleting files, dropping records, force-push) stops for
  confirmation first, whichever skill drives it.
- If two skills both match, name the choice in one line and proceed with the best fit, unless it
  changes an outward/destructive action.

Key routing rules:
- Product ideas, "is this worth building", brainstorming -> invoke office-hours
- Think bigger, rethink scope, strategy/founder review -> invoke plan-ceo-review
- Bugs, errors, "why is this broken", 500 errors -> invoke investigate
- Ship, deploy, push, create PR -> invoke ship
- QA, test the site, find bugs -> invoke qa
- Code review, check my diff -> invoke review
- Update docs after shipping -> invoke document-release
- Weekly retro -> invoke retro
- Design system, brand -> invoke design-consultation
- Visual audit, design polish -> invoke design-review
- Architecture review -> invoke plan-eng-review
- Save progress, checkpoint, resume -> invoke checkpoint
- Code quality, health check -> invoke health