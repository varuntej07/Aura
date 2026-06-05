# Project Overview

Aura is AI companion app (currently beta testing with 15 users). The assistant persona is Buddy. The app covers text chat, LiveKit voice, reminders, memory, notifications, scheduled agents, Google Calendar and Gmail tools, and live web search.

Keep the project production-grade. Prefer clear working path with scalabiity, maintainabiity, future-proof and robust, most importantly flexible with additional incoming features in the long run. 

## Architecture

The Flutter app uses MVVM with Provider.

Screens live in `lib/presentation/screens`.

ViewModels live in `lib/presentation/viewmodels`.

Repositories live in `lib/data/repositories`.

Services live in `lib/data/services`.

Shared app code lives in `lib/core`.

Provider wiring lives in `lib/di/providers.dart`.

The backend is a FastAPI app in `backend/src/main.py`.

Handlers live in `backend/src/handlers`.

Backend services live in `backend/src/services`.

Scheduled domain agents live in `backend/src/agents`. These agents only fetch data (`fetch_data`). Notification sending is handled by the signal engine, not the agents.

Voice runs through `backend/src/agent/voice_agent.py` as a separate LiveKit worker.
`voice_agent.py` is the thin orchestrator; its pieces — telemetry, error mapping, Firestore fetchers, prompt context, pipeline builders, voice conditioning, and the session event recorder — live in the `backend/src/agent/voice/` package.

`backend/src/services/user_aura_extractor.py` builds a passive behavioral profile per user.
It fires as a fire-and-forget `asyncio.create_task` from the chat handler after every message.
Profile documents live in the `UserAura/{uid}` Firestore collection.
The extractor always passes the user's previous query (`prev_user_query` field) alongside the
current message to Gemini Flash, which decides when prior context is needed — no hardcoded
heuristics. Failed extractions are swallowed silently so the chat stream is never affected.

Interests are stored as a **closed taxonomy, not free text**. `backend/src/services/user_aura_schema.py`
is the single source of truth: ~30 broad categories (+ `other`) that the extraction prompt constrains
Gemini to (off-list values coerce to `other`), each holding the specific subjects named in the message
(e.g. `politics_governance` → `KCR`) with time-decayed weights (30-day half-life) and a per-category cap.
The writer (`apply_interest_signal`) and every reader — chat prompt suffix, voice prompt, notification
framer, and the signal-engine `user_vector` embedding (which embeds subjects, never raw slugs) — go
through the schema's accessors, which fall back to the legacy `deep_interest_frequencies` map until a
profile rebuilds. This replaced an earlier design where free-text interest strings were Firestore map keys
and fragmented into 100+ near-duplicate buckets (see `lessons-learnt.text`, 2026-06-04). `deep_interest_frequencies`
is kept on the doc (frozen) for old app clients; the two dead maps (`surface_topic_frequencies`,
`named_entities_seen`) are dropped once a profile reaches 5 categories.

## Signal Engine

`backend/src/services/signal_engine/` is the notification and feed ranking layer.
Full architecture in `backend/docs/signal_engine.md`.

**How it works:** A content pool (`content_candidates/`) holds embeddings (768-dim, `gemini-embedding-001`) fetched by data fetchers. A per-user signal store (`users/{uid}/signal_store/state`) tracks a user vector, time-slot open rates, category affinities, and fatigue. Every 15 min, `scoring_loop.run_tick()` scores candidates with pure math, and only the top candidate above threshold gets one Gemini Flash call to frame the copy before FCM send.

**Endpoints:**
- `POST /events` — Flutter reports user events (taps, dismissals, skips, app opens). Updates user vector via EMA.
- `GET /feed/recommend` — ranked content feed for in-app display.
- `POST /internal/signal-engine/tick` — Cloud Scheduler every 15 min. Runs scoring and sends notifications.
- `POST /internal/signal-engine/content-ingest` — Cloud Scheduler hourly. Pulls HN, arXiv, ESPN Cricinfo RSS, and global Google News RSS into the pool.
- Sports ingest (cricbuzz live match scores only) runs inside `/scheduler/tick` every 30 min via a `minute % 30 == 0` gate — no separate scheduler job needed. Broader sports headlines now arrive via Google News RSS in the hourly content-ingest; the old Gemini-grounded web search for leagues was removed.

**`/scheduler/tick` runs every minute** (`juno-reminder-tick` Cloud Scheduler job). Use `minute % N == 0` gating inside `handlers/scheduler.py` to piggyback any periodic work at N-minute intervals without creating new scheduler jobs.

**Out of scope for the signal engine:** calendar meeting reminders and the post-nutrition-scan engagement chain. These stay on their existing LLM paths.

`backend/src/services/daily_notification/orchestrator.py`  only runs the calendar reminder pipeline 

### Notification re-engagement funnel (PostHog)

Signal-engine notifications are instrumented as a 4-step PostHog funnel so "which notification earns a tap and a reply" is measurable, not guessed:
`signal_notification_sent` (server, `scoring_loop` after delivery) → `notification_tapped` (client, filter `notification_origin == signal_engine`) → `signal_session_from_notification` (chat opens from the tap) → `signal_action_after_notification` (user's first reply in that thread).

The event names and join-key property names live in ONE place per side: `backend/src/services/analytics/funnel_events.py` and `lib/core/analytics/funnel_events.dart`. `backend/tests/test_funnel_event_contract.py` reads the Dart file and fails CI if the two drift — a rename on either side breaks the build instead of silently flattening the funnel.

Server capture goes through `analytics/posthog_client.py` — fire-and-forget, a no-op when `POSTHOG_API_KEY` is unset, and never raises into a scoring tick. It reuses the public `phc_` project key the app already embeds (`POSTHOG_API_KEY` / `POSTHOG_HOST` in `settings.py`, wired in `deploy.sh`). `run_tick` logs a loud WARNING if it sends notifications while the key is unset (funnel-blind).

Tapping a `signal_engine` notification opens chat seeded with `opening_chat_message`, routed via `dispatchNotificationTap` → `signalNotificationTapStream` → `/chat/new`. `ChatViewModel.loadSignalNotificationContext` fires the session event and arms the first reply to fire the action event once.

## UI System

The app uses a glass morphism design system defined in `lib/core/theme/`.

**`app_colors.dart`** — All color constants including glass-specific ones:
- `deepBackground` (`0xFF080812`) — base dark background
- `glassWhiteFill`, `glassBorderLight`, `glassBorderDim`, `glassHighlight` — glass surface layers
- `glassOrb1`, `glassOrb2` — ambient background gradient orb colors

**`glass_card.dart`** — Four UI primitives:
- `GlassCard` — real `BackdropFilter` blur (σ=12). Use only on static, non-scrolling elements. Always wrapped in `RepaintBoundary`.
- `FauxGlassCard` — gradient + border only, no blur. Use everywhere inside scroll lists, message bubbles, tiles, pills.
- `GlassIconButton` — circular glass button with real blur. Use for icon buttons in app bars.
- `AmbientBackground` — `Stack` with two radial gradient orbs over `deepBackground`. Wrap entire screens that need the glass effect to have something to blur.

Performance rule: never put `BackdropFilter` inside a `ListView` or `GridView`. Use `FauxGlassCard` there instead.

**AppShell** (`lib/presentation/screens/app_shell.dart`) — wraps child in `AmbientBackground`, uses `extendBody: true` so content flows under the floating nav bar. The floating glass nav bar is ~58px tall. Screens that scroll to the bottom must add `SizedBox(height: MediaQuery.of(context).viewPadding.bottom + 96)` at the bottom to avoid content being hidden.

## Auth

`AuthViewModel` uses a stream subscription to `authRepository.userModelStream` (backed by Firebase `authStateChanges()`). Auth state updates reactively — no polling. The router's `refreshListenable: authViewModel` handles redirects automatically.

Sign-in supports Google and Email/Password. Account creation is an explicit "Create account" flow — sign-in does **not** auto-create (and couldn't reliably anyway, see below).

The home screen drawer checks `authVm.user != null` and shows a sign-in button when unauthenticated, hiding the session list.

**Error mapping** lives in `FirebaseAuthService._mapSignInError` / `_mapSignUpError` (data layer; the VM/UI only render `AppException.message`):
- `user-not-found` / `wrong-password` / `invalid-credential` collapse into one "Wrong email or password" message — required by Firebase **Email Enumeration Protection** (on by default), which returns `invalid-credential` instead of the granular codes. Do not split them.
- `network-request-failed` is mapped explicitly to an offline message in both maps and the Google credential path — never tell a user their password is wrong when they're actually offline.
- Google sign-in **cancellation** is swallowed in `AuthViewModel.signInWithGoogle` (returns to idle, no red banner) — backing out of the account picker is a normal action, not an error.

## Onboarding

New accounts are stamped `onboarding_complete: false` in Firestore at creation. Existing accounts without the field default to `true` so they are never shown the flow.

The router redirect enforces onboarding before any authenticated screen: if `AuthViewModel.needsOnboarding` is true, any route redirects to `/onboarding`.

**Flow:** `/onboarding` (`OnboardingScreen` — 5-slide PageView) → pushes `AuraConsentScreen` (age gate + Aura consent toggle).

`AuraConsentScreen` writes all three fields atomically via `OnboardingRepository.saveOnboardingResult`:
- `onboarding_complete: true`
- `date_of_birth: ISO date`
- `aura_consent_granted: bool` (forced false for users under 18)
- `aura_consent_timestamp: ISO datetime`

After a successful write, it calls `AuthViewModel.markOnboardingComplete()` (updates in-memory model) and then `context.go('/home')` explicitly. The screen was pushed via `Navigator.push`, not GoRouter, so explicit navigation is required to clear the mixed stack correctly.

`backend/src/services/user_aura_extractor.py` reads `users/{uid}.aura_consent_granted` before every extraction and returns early if not granted. This is the GDPR gate — behavioural profiling only runs with explicit opt-in.

## Paywall

`/paywall` route renders `PaywallScreen` with three tiers: Free, Companion ($19.99/mo, $191/yr, IDs `aura_companion_monthly` / `aura_companion_annual`), and Pro ($34.99/mo, $335/yr, IDs `aura_pro_monthly` / `aura_pro_annual`). 45-day free Companion trial (extended for beta) via `kTrialDurationDays` in `subscription_plan.dart`.

**Beta interest-capture mode:** Real IAP is disabled. The tier CTAs call `SubscriptionViewModel.captureInterest(tier, annual)` which fires a PostHog `paywall_intent` event and writes `users/{uid}/payment_intent/{tier}_{period}` to Firestore, then shows an acknowledgement `AlertDialog`. The `purchaseCompanion` / `purchasePro` methods on the VM are wired but unused while beta is on — switch the paywall CTAs back to those when payments go live.

## Run

Backend API:

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker (run `download-files` once before first use to fetch Silero VAD + MultilingualModel ONNX files):

```powershell
python -m backend.src.agent.voice_agent download-files
```

```powershell
cd backend
python -m src.agent.voice_agent start
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

`deploy.sh` shifts 100% of traffic to the new revision immediately. To test new
backend code on your own phone first — same single Firestore, signed in as
yourself so only your own `users/{uid}` docs are touched — deploy a **dark
candidate** revision and point only your debug build at it:

```powershell
# 1. Build & push the new image
docker build -t gcr.io/juno-2ea45/juno-backend:latest backend
docker push gcr.io/juno-2ea45/juno-backend:latest

# 2. Deploy dark — 0% live traffic, gets a tagged URL. Inherits all
#    env vars/secrets from the live revision (only add --set-secrets if the
#    code introduces a NEW one).
gcloud run deploy juno-backend `
  --image=gcr.io/juno-2ea45/juno-backend:latest `
  --region=us-central1 --project=juno-2ea45 `
  --no-traffic --tag=candidate

# 3. Run the phone build against the candidate URL (released app is unaffected —
#    it has the bare prod URL compiled in and can't reach the tagged URL).
flutter run --dart-define=API_BASE_URL=https://candidate---juno-backend-620715294422.us-central1.run.app `
            --dart-define=WS_BASE_URL=wss://candidate---juno-backend-620715294422.us-central1.run.app

# 4a. Good → promote to everyone (bare prod URL now serves candidate code,
#     no app update needed):
gcloud run services update-traffic juno-backend `
  --region=us-central1 --project=juno-2ea45 --to-tags=candidate=100

# 4b. Bad → do nothing. Users never saw it.
```

The `API_BASE_URL` / `WS_BASE_URL` dart-defines override the dev backend in
`lib/core/config/environment.dart` (empty by default → prod URL). The `candidate`
in the URL is the `--tag` value; change the tag and the prefix changes to match.
Bare URL routes to whatever revision holds live traffic; a tagged URL always
routes to its revision even at 0% traffic. **Caveat:** any code path that writes
across *all* users (collection-group batch / migration / backfill) can't be
safely dark-tested against the shared prod Firestore — gate those behind an
explicit trigger flag.

Aura app legal pages (hosted on varuntej.dev portfolio):

```text
https://varuntej.dev/aura
https://varuntej.dev/aura/privacy-policy
https://varuntej.dev/aura/terms-of-service
```

## Reliability Notes

This is useful as a personal project, but reliability still depends on clean local configuration and external services.

Keep `.env`, service account JSON, OAuth client JSON, and platform Google service files out of commits. `.env` is intentionally not ignored so variable names stay visible locally.

The backend depends on several external services: Firebase, Anthropic, OpenAI, Gemini, Brave Search, LiveKit, Deepgram, Cartesia, Google Calendar, Gmail, Cloud Scheduler, Cloud Tasks, and FCM. Treat every integration as optional at development time and make failures explicit.

### Error handling and user-facing copy

Audience is 18-30. Error copy is casual, blames the tech not the user, and always points at the next action ("try again", "check your connection"). Never leave a user-facing wait unbounded — every wait needs a timeout that ends in a visible message.

- **Flutter HTTP** is centralized in `ApiClient` (`lib/core/network/`) with per-call timeouts + exponential-backoff retries; timeout constants live in `core/constants/app_constants.dart`. The SSE chat stream never retries once the server accepts it (avoids duplicate tool calls / replayed text).
- **Voice silence watchdog** (`voice_session_service.dart`, `_replyWatchdogTimeout` = 15s) covers the "agent connected but never speaks" hang (e.g. zero LLM credit). It arms when the agent joins (greeting) and after each user turn (reply), resets on any sign of life (agent state, audio, text, data), and emits a coded `session.error`. Codes → friendly copy in `HomeViewModel._toVoiceErrorMessage` (the mic orb is the retry button).
- **Backend voice** (the `voice/` package) publishes a `session.error` down the LiveKit data channel on pipeline failure so the client doesn't wait on its own watchdog; `classify_pipeline_error` (in `voice/errors.py`) splits provider-exhausted/quota (`provider_unavailable`) from generic failures.
- **Voice telemetry:** PostHog `voice_first_response` (success) and `voice_error` `{code}` (failure) fire from the Flutter client; the backend also logs structured `VoiceSession: failure` lines to Cloud Logging.

The Flutter and Dart analyzer commands timed out in this environment during review. Recheck locally before relying on the current app state.

### Pre-deploy checklist

Before deploying the backend, verify it starts cleanly:

```powershell
cd backend && python -c "import src.main; print('OK')"
```

This catches broken imports before Docker builds them into a crashing container.

### Database field verification

Whenever you change any database logic (a Firestore query, a field read/write, a backup/restore path), FIRST verify which fields actually exist on the target documents before writing the code — read the writer that produces those documents (or inspect a live document), confirm the exact field names, and only then proceed, stating the justification for the field you chose. Do not query or read a field on the assumption it exists.

This is not optional. A query that filters on a field no document has does not error — it returns zero rows silently, which looks identical to "no data." That exact mistake (an FCM active-user query filtering `last_seen` while the writer only ever wrote `registered_at`) caused a 4-day notification outage. See `lessons-learnt.text` (2026-05-31).

Defend every field-name contract three ways: a single shared constant/accessor so the name lives in one place (writer and all readers reference it), a writer→reader round-trip test that breaks CI if either side is renamed, and a loud WARNING/ERROR log when a query returns nothing while the underlying data is clearly non-empty. Never let "zero rows" and "healthy" look the same.

### Firestore index maintenance

Whenever you add or change a Firestore query that uses `collection_group(...)`, an inequality (`>`, `>=`, `<`, `<=`), an `order_by`, or filters on multiple fields, you MUST also declare the matching index in `firestore.indexes.json` (wired via `firebase.json`) and deploy it with `firebase deploy --only firestore:indexes --project juno-2ea45`. Firestore auto-creates single-field indexes only at **collection** scope — a `collection_group` query ordered/filtered by a field needs an explicit `COLLECTION_GROUP` field override, which is never created automatically.

A missing index makes the query throw a 400 at runtime, not at deploy or import time. If that error is swallowed (e.g. caught and returning `[]`), it looks identical to "no data." This is exactly what happened on 2026-06-01: the `fcm_tokens` collection-group query filtering `registered_at >= cutoff` had no `COLLECTION_GROUP_ASC` index, so `list_active_user_ids` returned zero users and notifications silently stopped.

Note that declaring a field override **disables Firestore's automatic single-field indexing for that field path** — list every scope you still need (`COLLECTION` ascending/descending plus the `COLLECTION_GROUP` entry), not just the new one.

### httpx redirect behavior

`httpx.AsyncClient` does NOT follow redirects by default. Any external HTTP call that may redirect (http → https, domain changes) must use `follow_redirects=True` or the request silently fails with a 3xx error.

### Dependency upgrade discipline

When bumping a `>=X.Y` bound in `pyproject.toml`, check the package changelog for breaking API changes before deploying.

Every plugin imported from `livekit.plugins` anywhere in the voice worker — `backend/src/agent/voice_agent.py` (the `silero` VAD prewarm) and the `backend/src/agent/voice/` package (most live in `voice/pipelines.py`) — must have a matching `livekit-agents[...]` extra in `pyproject.toml`. A missing extra passes all local checks (the plugin is in the dev venv) but crashes the worker Docker image at startup with `ImportError: cannot import name '<plugin>' from 'livekit.plugins'`, failing the Cloud Run deploy after the full build. `backend/tests/test_voice_worker_deps.py` guards this — it scans `voice_agent.py` plus the whole `voice/` package, so adding a plugin import in any new package module is still covered.

livekit_client uses `SCREAMING_CASE` enum values (e.g. `ParticipantKind.AGENT`, not `ParticipantKind.agent`). Run `flutter analyze` after any livekit upgrade to catch casing mismatches before the full Gradle build.

Adding or upgrading an Android Flutter plugin can fail the Gradle build with `Inconsistent JVM Target Compatibility Between Java and Kotlin Tasks` on that plugin's `:compileDebugKotlin` task. Cause: each plugin pins its own Java and Kotlin JVM targets and they disagree (some pin Kotlin high, some pin Java low), and the only JDK here is Android Studio's bundled JBR 21, so an unpinned Kotlin target defaults to 21. This is already handled centrally in `android/build.gradle.kts`: a `subprojects { afterEvaluate { ... } }` block forces **both** Java (`BaseExtension.compileOptions`) and Kotlin (`KotlinCompile` jvmTarget) to 17 on every module, so the pair is always consistent. That block must stay registered **before** the `evaluationDependsOn(":app")` block (evaluating `:app` eagerly evaluates every plugin module; a later registration throws "Cannot run afterEvaluate ... already evaluated"). Caveat: it forces everything **down** to 17 — a future plugin that genuinely requires JVM 21 would fail with a *different* error (a 21-only API / "source release 21" message), at which point bump the app and this block together.

## Stream Contract

Real-time data is exposed as `Stream<T>` via `async*` generators — cold and recreated per subscription.

`StreamController.broadcast()` is allowed only when events fire to multiple independent subscribers
(e.g. FCM notifications in `NotificationService`, LiveKit room events in `VoiceSessionService`).
Every broadcast `StreamController` must be closed in a `dispose()` method.

Do not use `StreamController` as a push mechanism for single-subscriber UI streams — use an `async*`
generator instead.

## Service State Contract

Services (`lib/data/services/`, `lib/data/repositories/`) may hold **lifecycle state**: connection
handles, auth user ID, platform stream subscriptions, initialization flags.

Services must **not** store per-request transient state as instance fields (e.g. saving a
`_currentRequestId` while a call is in flight). All request context lives in the call frame —
local variables and the `Future`/`Stream` chain — not on the service instance.

## Service Interface Pattern

Chat/AI streaming access goes through `abstract class ChatServiceProvider`
(`lib/data/services/chat_service_provider.dart`).

`BackendApiService` (production) and `StubChatServiceProvider` (dev) both implement it.
Selection happens at DI time in `lib/di/providers.dart` — no `_useStub` flags or conditional
branches inside production service implementations.

Non-chat backend calls (`deleteAccount`, `analyzeNutrition`) remain on `BackendApiService` directly.
Only `ChatViewModel` and its subclasses depend on `ChatServiceProvider`.

## Widget Purity

Widgets in `lib/presentation/widgets/` are purely presentational. They must not call
`context.read<T>()`, `context.watch<T>()`, `context.select()`, or `Provider.of<T>()`.

All data and callbacks are passed via constructor parameters.

Only screens in `lib/presentation/screens/` read from Provider.

## Component Presets

`FauxGlassCard` named constructors must be used for all standard visual configurations.
Do not configure raw `borderRadius`/`padding` inline at callsites when a preset matches
the semantic role of the card.

Available presets (defined in `lib/core/theme/glass_card.dart`):
- `FauxGlassCard.pill` — suggestion pills, interest/tag chips
- `FauxGlassCard.navTile` — navigation and info display tiles
- `FauxGlassCard.section` — panel/section containers with padding
- `FauxGlassCard.toggleTile` — switch/toggle wrappers
- `FauxGlassCard.destructiveButton` — sign-out / delete-account buttons

Custom gradient or dynamic border color (e.g. per-agent color, per-message state) may still
use the default constructor.

## Naming Conventions

Names must describe what something is or does in plain terms.

Constants: state the full context of what they represent. Use `EXCLUDED_TOOLS_FOR_GENERAL_CHAT` not `_CHAT_EXCLUDED_TOOLS`.

Functions: name the action and the subject together so the return value is obvious without reading the body. Use `_get_user_local_datetime` not `_formatted_now`. 

Avoid abbreviations, cryptic prefixes, and names that only make sense after reading the body. If a name needs a comment to explain it, rename it instead.


## Working Style

This is a personal project, so default to the simplest useful change.

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

Design documents live in `~/.gstack/projects/varuntej07-juno/` as timestamped markdown files.

**Latest approved:** `varun-main-design-20260525-175702.md` — "Aura Beta Launch — Voice Accountability for ADHD Adults." 26-day plan to ship the existing app to 10-20 beta testers in ADHD/accountability communities. Decision gate: do 3+ of 10 testers say voice matters AND they'd pay?

**Prior designs:**
- `varun-main-design-20260519-185952.md` — "Buddy — AI Accountability Partner with Personality" (APPROVED). Positioned Aura as accountability tool for ADHD adults/solo operators.
- `varun-main-design-20260513-211359.md` — "Notification Overhaul — Global Budget + Zomato-grade Copy" (DRAFT). Signal engine architecture for notification coordination.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming -> invoke office-hours
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