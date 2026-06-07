# Aura

Aura is a voice-first personal AI companion. The assistant persona is **Buddy**. The product is a Flutter app backed by a Python FastAPI service, and it covers text chat, real-time voice, reminders, memory, smart notifications, scheduled agents, live web search, and Google Calendar and Gmail tools.

The guiding principle is one clear working path over broad architecture. Every external dependency is treated as optional at development time, and every failure is made visible rather than hidden.

## Table of contents

- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [System architecture](#system-architecture)
- [Backend API surface](#backend-api-surface)
- [Voice architecture](#voice-architecture)
- [Notification and signal engine](#notification-and-signal-engine)
- [Data and storage](#data-and-storage)
- [Scheduled work](#scheduled-work)
- [Auth, onboarding, and paywall](#auth-onboarding-and-paywall)
- [Reliability principles](#reliability-principles)
- [Running locally](#running-locally)
- [Deploying](#deploying)

## Tech stack

| Layer | Technology |
| --- | --- |
| Mobile app | Flutter (Dart), MVVM with Provider, go_router, drift (local SQLite) |
| Backend API | Python FastAPI on Cloud Run |
| Voice worker | LiveKit Agents, a separate Cloud Run worker |
| Chat LLM | Anthropic Claude (Haiku 4.5) |
| Voice LLM | OpenAI GPT-4.1 mini, falling back to Anthropic Claude, then Gemini Flash |
| Cheap-tier LLM | Gemini Flash for copy framing and passive profiling |
| Embeddings | Gemini `gemini-embedding-001`, 768 dimensions |
| Voice pipeline | Deepgram STT, Cartesia TTS, Silero VAD, LiveKit turn detector |
| Identity and data | Firebase Auth, Cloud Firestore, Firebase Cloud Messaging |
| Scheduling | Cloud Scheduler, Cloud Tasks |
| Analytics | PostHog (product events), Langfuse (LLM cost and latency) |
| Integrations | Google Calendar, Gmail |

## Repository layout

```
Aura/
  lib/                    Flutter app source
    core/                 Theme, network, config, constants, shared utilities
    data/                 Models, repositories, services, local drift database
    presentation/         Screens, ViewModels, presentational widgets
    di/                   Provider wiring (providers.dart)
  backend/
    src/
      main.py             FastAPI app and route registration
      handlers/           HTTP request handlers, one per endpoint group
      services/           Domain services (chat, signal engine, connectors, ...)
      agents/             Scheduled domain agents (data fetch only)
      agent/              LiveKit voice worker and its voice/ package
      config/             Settings and environment loading
    docs/                 Architecture notes (signal_engine.md and more)
    tests/                Backend test suite and contract guards
  android/ ios/ web/      Platform build targets
  firestore.indexes.json  Firestore composite and collection-group indexes
```

The Flutter side follows MVVM. Screens read from Provider, ViewModels hold state and call repositories, repositories call services, and services talk to the backend or platform SDKs. Widgets in `lib/presentation/widgets/` are purely presentational and receive everything through constructor parameters.

## System architecture

Three runtime pieces cooperate: the Flutter client, the FastAPI backend, and the LiveKit voice worker. Firebase provides identity, storage, and push. Cloud Scheduler drives all periodic work.

```
        +-------------------------+
        |     Flutter app         |
        |  chat, voice, feed,     |
        |  reminders, settings    |
        +------------+------------+
                     |
       HTTPS / SSE   |   WebRTC (voice audio)
                     |
        +------------v------------+         +-------------------------+
        |    FastAPI backend      |  tools  |   LiveKit voice worker  |
        |    (Cloud Run)          |<--MCP-->|   (Cloud Run)           |
        |  chat, signal engine,   |         |  STT -> LLM -> TTS      |
        |  agents, connectors     |         +-----------+-------------+
        +------+-----------+------+                     |
               |           |                            |
        Firestore /      external LLMs            LiveKit Cloud (audio)
        FCM / Auth       OpenAI, Claude, Gemini,
                         Deepgram, Cartesia
                              ^
                              |
                    Cloud Scheduler + Cloud Tasks
                    (ticks for notifications,
                     agents, content ingest)
```

The text chat path streams over Server-Sent Events. The Flutter client opens `POST /chat`, the backend runs a Claude conversation with tool access, and tokens stream back as they are produced. Voice runs over WebRTC through LiveKit, which is described below.

All client HTTP goes through a single `ApiClient` in `lib/core/network/` with per-call timeouts and exponential-backoff retries. The chat stream is the one exception: once the server accepts it, it never retries, which avoids duplicate tool calls and replayed text.

## Backend API surface

Public and user-authenticated endpoints (Firebase ID token):

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Liveness probe |
| `GET /voice/token` | Mint a LiveKit room token for the signed-in user |
| `POST /chat` | Text conversation with Claude, streamed over SSE |
| `POST /notification-reply` | Reply to a notification, routed into chat |
| `DELETE /account` | Delete the user account and data |
| `POST /devices/register` | Register an FCM device token |
| `POST /events` | Report user events for the signal engine |
| `GET /feed/recommend` | Ranked in-app content feed |
| `POST /connectors/google-calendar/...` | Connect, disconnect, sync calendar |
| `POST /connectors/gmail/...` | Connect or disconnect Gmail |
| `POST /mcp` | MCP server exposing tools to the voice worker |

Internal endpoints, callable only by Cloud Scheduler or Cloud Tasks and verified by a signed OIDC token from the scheduler service account:

| Method and path | Cadence | Purpose |
| --- | --- | --- |
| `POST /scheduler/tick` | every minute | Deliver due reminders and piggyback periodic work |
| `POST /internal/signal-engine/tick` | every 15 min | Score candidates and send notifications |
| `POST /internal/signal-engine/content-ingest` | hourly | Pull fresh content into the pool |
| `POST /internal/agents/tick` | scheduled | Fan out scheduled domain agents |
| `POST /internal/daily-notify/send` | on demand | Send a calendar meeting reminder |

The `/scheduler/tick` job runs once a minute. Periodic work at wider intervals is gated inside the handler with a simple `minute % N == 0` check, so new background jobs do not need new scheduler entries.

## Voice architecture

Voice is a separate LiveKit Agents worker, not part of the FastAPI request path. It runs as its own Cloud Run service so a slow or failing voice session never blocks the API.

**Pipeline.** The worker uses a cascading architecture: speech to text, then language model, then text to speech.

- **STT**: Deepgram Nova, with `nova-3` falling back to `nova-2`
- **LLM**: OpenAI GPT-4.1 mini, falling back to Anthropic Claude, then Gemini Flash (`build_llm_pipeline`)
- **TTS**: Cartesia, with `sonic-3` falling back to `sonic-2`
- **Turn taking**: Silero VAD plus the LiveKit multilingual turn detector

**Connection flow.** The Flutter client calls `GET /voice/token` to get a LiveKit room token, then joins room `voice-{uid}`. The worker is waiting on LiveKit Cloud, sees the participant join, and starts a session. Audio flows over WebRTC the whole time.

**Tools.** The agent does not embed its own tools. It pulls them from the backend over MCP (`POST /mcp`) using `livekit.agents.mcp.MCPServerHTTP`. The worker authenticates to that endpoint with a short-lived Firebase ID token it mints per session from the user's uid. This means voice and text chat share one tool implementation.

**Module layout.** `voice_agent.py` is a thin orchestrator. Its pieces live in `backend/src/agent/voice/`:

- `telemetry.py` structured session logging
- `errors.py` pipeline error classification
- `fetchers.py` Firestore reads for session context
- `prompt_context.py` and `context.py` prompt assembly
- `pipelines.py` STT, LLM, TTS, MCP, and turn-detector builders
- `voice_controls.py` per-user voice conditioning
- `recorder.py` session event recording
- `auth.py` per-session Firebase token minting

**Failure handling.** Two watchdogs cover the dangerous case where the agent connects but never speaks, for example when LLM credit runs out.

- The Flutter client arms a 15 second silence watchdog when the agent joins and after each user turn. Any sign of life (agent state, audio, text, or data) resets it. On timeout it emits a coded `session.error` that maps to friendly copy, and the mic orb becomes the retry button.
- The backend publishes a `session.error` down the LiveKit data channel on pipeline failure, so the client does not have to wait on its own timer. `classify_pipeline_error` separates provider-exhausted or quota errors from generic failures.

Voice telemetry fires PostHog `voice_first_response` on success and `voice_error` with a code on failure. The backend also logs structured `VoiceSession` lines to Cloud Logging.

## Notification and signal engine

The signal engine is the notification and feed ranking layer in `backend/src/services/signal_engine/`. Full design is in `backend/docs/signal_engine.md`.

The core idea: do not ask an LLM to decide what to send. Scoring is fixed math, fast and cheap, and learns from outcomes. Only the single winning notification gets one LLM call to write its copy.

```
  Content ingest (scheduled)
    data fetchers pull HN, arXiv, sports, news
    each item is embedded (768-dim Gemini) into content_candidates/

  Flutter events -> POST /events -> event_ingester
    taps, dismissals, opens, skips nudge the user vector via EMA
    stored at users/{uid}/signal_store/state

  Every 15 min -> POST /internal/signal-engine/tick -> scoring_loop.run_tick()
    for each active user:
      read signal state
      find nearest candidates to the user vector
      score with pure math (no LLM)
      if best score > threshold and the user is not fatigued:
        frame copy with one Gemini Flash call
        send via FCM
        record the outcome as pending

  Flutter session open -> GET /feed/recommend
    same scoring plus a diversity penalty, returned as a ranked feed
```

**Signal state.** Each user has a `signal_store/state` document holding a 768-dimension user vector, per-slot open rates across the day, category affinities, and fatigue counters. A hard cap of three notifications per user per day acts as the fatigue floor.

**Cold start.** A brand new user vector is bootstrapped from their `UserAura` passive profile rather than a random vector, so the first notification is already relevant. If there is no profile (consent not granted), it falls back to a zero vector.

**Outcome attribution.** Every send writes a pending outcome. A tap flips it to opened and rewards the embedding, a dismiss penalizes it, and after six hours of silence the next tick marks it as a timeout with a small negative weight.

**What stays off this engine.** Calendar meeting reminders and the post-nutrition-scan engagement chain are interaction-triggered and time-critical, not discovery problems, so they remain on their existing LLM paths.

**Re-engagement funnel.** Signal notifications are instrumented as a four-step PostHog funnel so the question "which notification earns a tap and a reply" is measured, not guessed:

```
signal_notification_sent  ->  notification_tapped
   (server, after send)        (client, origin == signal_engine)
        ->  signal_session_from_notification  ->  signal_action_after_notification
              (chat opens from the tap)             (first reply in that thread)
```

The event names live in exactly one place per side: `backend/src/services/analytics/funnel_events.py` and `lib/core/analytics/funnel_events.dart`. A contract test reads the Dart file and fails CI if the two ever drift, so a rename breaks the build instead of silently flattening the funnel.

## Data and storage

**Firestore** is the source of truth on the server. Key collections:

- `users/{uid}` profile, onboarding state, consent flags
- `users/{uid}/signal_store/state` the per-user signal vector and counters
- `content_candidates/{id}` the embedded content pool
- `UserAura/{uid}` a passive behavioral profile, written fire-and-forget after each message by `user_aura_extractor.py`, gated on explicit consent

**Local drift database** on the device caches chat sessions and messages for offline reading and fast load. Real-time data is exposed as cold `Stream<T>` via `async*` generators. Broadcast stream controllers are reserved for true multi-subscriber sources such as FCM notifications and LiveKit room events, and each one is closed in `dispose()`.

**Index discipline.** Any query that uses a collection group, an inequality, an order-by, or multiple filtered fields must have a matching entry in `firestore.indexes.json`. A missing index fails the query at runtime with a 400, which can look identical to "no data" if the error is swallowed. Field-name contracts are defended with a single shared constant, a writer-to-reader round-trip test, and a loud log when a query returns nothing against data that is clearly non-empty.

## Scheduled work

All periodic work is driven by Cloud Scheduler and Cloud Tasks, authenticated with signed OIDC tokens from the scheduler service account.

| Job | Interval | Effect |
| --- | --- | --- |
| Reminder tick | every minute | Deliver due reminders, host minute-gated periodic work |
| Signal engine tick | every 15 min | Score and send notifications |
| Content ingest | hourly | Refresh the content pool |
| Sports ingest | every 30 min | Gated inside the reminder tick, no separate job |

Scheduled domain agents in `backend/src/agents/` only fetch data. They never send notifications directly. Delivery is owned by the signal engine, which keeps one budget and one fatigue model across every source.

## Auth, onboarding, and paywall

**Auth.** `AuthViewModel` subscribes to a Firebase auth state stream, so sign-in and sign-out update the UI reactively with no polling, and the router redirects automatically. Sign-in supports Google and email or password. Account creation is an explicit flow, not an implicit side effect of signing in. Error copy collapses wrong-email and wrong-password into one message (required by Firebase email enumeration protection) and always distinguishes a real offline state from a credential error.

**Onboarding.** New accounts are stamped incomplete and routed through a five-slide intro followed by an age gate and consent screen. Consent, date of birth, and completion are written atomically. Behavioral profiling only runs once consent is granted, which is the GDPR gate.

**Paywall.** Three tiers exist (Free, Companion, Pro) with a 45-day Companion trial (extended for beta). During beta, real in-app purchase is disabled and the call to action captures interest instead: it fires a PostHog event and records the intent in Firestore, then acknowledges. The purchase methods are wired and ready to switch on when payments go live.

## Reliability principles

The audience is 18 to 30, so error copy is casual, blames the technology rather than the user, and always points at the next action. No user-facing wait is ever unbounded. Every wait has a timeout that ends in a visible message.

- Verify which Firestore fields actually exist before writing a query against them. A filter on a field no document has returns zero rows silently.
- Keep every voice plugin imported in the worker matched by a `livekit-agents` extra in `pyproject.toml`. A missing extra passes local checks but crashes the Docker image at startup.
- External HTTP that may redirect must opt into following redirects, because the async client does not follow them by default.
- Before deploying, confirm the backend imports cleanly so a broken import is caught before it ships in a crashing container.

See `CLAUDE.md` and `lessons-learnt.text` for the full list and the incidents behind each rule.

## Running locally

Backend API:

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker (run `download-files` once before first use to fetch the Silero VAD and turn-detector models):

```powershell
python -m backend.src.agent.voice_agent download-files
cd backend
python -m src.agent.voice_agent start
```

Flutter app (run `flutter analyze` first to catch compile errors before the full Gradle build):

```powershell
flutter run
```

### Wireless ADB (Android)

Enable Wireless debugging in Developer Options, note the IP and port on that screen, then connect:

```powershell
adb connect [IP]:[Port]
adb devices
```

If `adb` is not recognized, add the platform-tools folder to PATH once and restart the terminal:

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:LOCALAPPDATA\Android\Sdk\platform-tools", "User")
```

If a connect fails, toggle Wireless debugging off and on to generate a new port, then reconnect with the new port.

## Deploying

Production backend URL:

```text
https://juno-backend-620715294422.us-central1.run.app
```

Deploy the backend and voice worker to Cloud Run (from the repo root, requires Git Bash):

```powershell
& "C:\Program Files\Git\bin\bash.exe" backend/deploy.sh juno-2ea45 us-central1
```

`deploy.sh` shifts all traffic to the new revision immediately. To test backend changes on your own phone first, deploy a dark candidate revision with `--no-traffic --tag=candidate` and point a debug build at the tagged URL, then promote with a traffic update once it looks good. Full steps are in `CLAUDE.md`.

Legal pages are hosted on the portfolio site:

```text
https://varuntej.dev/aura
https://varuntej.dev/aura/privacy-policy
https://varuntej.dev/aura/terms-of-service
```
