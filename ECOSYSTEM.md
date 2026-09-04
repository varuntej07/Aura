# Aura Ecosystem

How the three live Aura codebases fit together: Aura (this repo, Flutter mobile app plus the `juno-backend` FastAPI service), Aura-Desktop (Tauri Windows and macOS companion), and Aura-Web (Next.js marketing site plus the browser auth handoff page).

Each repo's own CLAUDE.md covers that repo. This file covers only what no single repo can see: which repo calls which, over what transport, and what breaks when one side changes a contract without telling the others.

## How to keep this file current

Update it **only** when a change alters a cross-repo contract: an endpoint's path or request/response shape, a shared Firestore collection's schema, an auth or pairing handshake, a data-channel or event schema, shared config identity (Firebase project, PostHog project, analytics event names), or a deploy/version linkage between repos.

Do not update it for internal refactors, UI changes, or anything that stays inside one repo. If unsure, ask rather than guessing either way.

**Filesystem assumption:** relative pointers like `../Aura/ECOSYSTEM.md` only resolve because all three repos are checked out as siblings under `C:\Users\varun\MobileApps\` on this machine. Treat this file's content as the authority and the relative pointer as a local convenience.

## System map

| Repo | Path (this machine) | GitHub remote | Stack | Deploy mechanism | Role |
|---|---|---|---|---|---|
| **Aura** (this repo) | `MobileApps/Aura` | `varuntej07/juno` (repo renamed Aura, remote URL still says juno) | Flutter mobile client (Android release target; iOS source exists but is not distributed) + FastAPI (`backend/`) | Product is in beta. Mobile: Android through Play Store / manual `.aab`; no iOS release. Backend: `docker build` straight from local disk (`backend/deploy.sh`), no git trigger, Cloud Run `juno-2ea45`/`us-central1` | Primary client (full API surface) and the shared backend every other repo talks to |
| **Aura-Desktop** | `MobileApps/Aura-Desktop` | `AuraVoice/Aura-Desktop` | Tauri v2 (Rust) + React 19 (TypeScript) | GitHub Releases (tagged build produces `.msi`/`.exe` for Windows and a notarized universal `.dmg` for macOS, plus `latest.json` covering `windows-x86_64`, `darwin-aarch64` and `darwin-x86_64`) | Current live Windows and macOS companion client, a from-scratch rewrite of the legacy Flutter desktop overlay below |
| **Aura-Web** | `MobileApps/Aura-Web` | `varuntej07/aura-web` | Next.js (App Router) + React + Framer Motion | Git-triggered deploy to Vercel (push to main auto-builds) | Marketing site (`auravoiceapp.com`), hosts the Google sign-in browser leg, and serves as the download page for Aura-Desktop |

A legacy Flutter Windows client was deleted from this repo on 2026-07-11 (code, `windows/` tree, and its GCS-hosted installers). Aura-Desktop is the only Windows client, and since 0.13.2 the only macOS one. This repo still owns the backend contracts that client consumes: pairing, web-auth, connector OAuth, draft-outbound, voice screen-sight, and screen saves.

## Shared infrastructure

- **Firebase project `juno-2ea45`**: Auth + Firestore, used by all three repos. All three write/read the same `users/{uid}` document shape (Aura-Web's `auth/complete/route.ts` explicitly builds a doc matching what the Flutter app writes, timestamps as ISO strings, not Firestore `Timestamp`, because Flutter's `DateTime.parse()` would crash on the latter).
  - **Surface footprint on the root doc:** `platform` records the surface where the account was created (`android`, `ios`, or `web`). A desktop-first Google account is created by Aura-Web with `platform: "web"` before Aura-Desktop exchanges the custom token. `linked_platforms` is an array-union accumulation of every surface the account has touched (e.g. `["android", "windows"]`), written idempotently by each surface: the mobile app unions its own platform on every app open, and `handlers/pairing.py` / `handlers/web_auth.py` union `windows` when a desktop links. `last_desktop_active_at` (ISO string, per the rule above) is stamped on each desktop link. A user's full device inventory lives in `users/{uid}/linked_devices/{install_id}`. Those subcollection documents use schema version 2 and native Firestore timestamps for `linked_at` and `last_seen_at`; the root user document keeps its separate ISO-string contract.
- **juno-backend on Cloud Run**: the only backend. Aura mobile talks to nearly its full route surface; Aura-Desktop and Aura-Web each talk to a narrow slice (see contracts below).
- **LiveKit Cloud**: voice rooms for both mobile and desktop clients, joined by the same backend-issued token from `GET /voice/token`. The voice agent worker itself deploys separately via `lk agent deploy`, not through Cloud Run.
- **PostHog**: the app side (Aura mobile, Aura-Desktop, and `juno-backend`'s own server-side capture) confirmed share one project; funnel event names are contract-tested (`backend/src/services/analytics/funnel_events.py` vs `lib/core/analytics/funnel_events.dart`). Aura-Web initializes its own `posthog-js` client from its own env vars (`NEXT_PUBLIC_POSTHOG_KEY`); whether that resolves to the same PostHog project as the app side has not been verified, see "Known gaps."
- **GitHub**: two orgs involved, not one. Aura-Desktop's repo lives under `AuraVoice`; Aura and Aura-Web live under the personal `varuntej07` account. Aura-Desktop's releases feed and Aura-Web's download page both point at `AuraVoice/Aura-Desktop`.
- **Sentry (desktop crash reporting)**: Aura-Desktop reports native Rust panics (`sentry` crate, `src-tauri/src/sentry_setup.rs`) and webview JS errors (`@sentry/browser`, `src/lib/sentry.ts`) into ONE Sentry project (org `o4511685555519488`, project `4511685630361600`).
  The DSN is deliberately hardcoded in both files (a DSN is a public write-only ingestion key, same posture as the PostHog token); dev builds no-op via `cfg!(debug_assertions)` on the Rust side and `import.meta.env.DEV` on the JS side, and the JS init is consent-gated alongside PostHog.
  This repo's ops dashboard (`ops/providers/sentry_provider.py`) reads that same project's issue feed via the Sentry API (`SENTRY_ORG`/`SENTRY_PROJECT`/`SENTRY_AUTH_TOKEN` in `ops/.env`), so a Sentry project change on the desktop side must be mirrored there.
- **Langfuse (LLM observability)**: `juno-backend` (and the LiveKit voice worker, once redeployed) writes one generation per LLM provider attempt and one span per tool call (`backend/src/services/analytics/llm_telemetry.py`, metadata and token usage only, never prompt text).
  The ops dashboard reads aggregates via the Langfuse Metrics API (`ops/providers/langfuse_provider.py`). Both sides share `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`.

## Cross-repo contracts

### 1. Device pairing (already-signed-in phone links a desktop)

Aura (mobile, authenticated) requests a short code; Aura-Desktop (unauthenticated) redeems it.

| Step | Caller | Endpoint | Notes |
|---|---|---|---|
| Request a code | Aura mobile | `POST /devices/pair/start` (authed) | 8-char code, unambiguous alphabet, 5 min TTL, capped at 3 live codes per uid |
| Redeem the code | Aura-Desktop | `POST /devices/pair/claim` (unauthenticated by design, the code IS the credential) | Sends the durable UUID `install_id`; returns a Firebase custom token, single-use via a Firestore transaction |
| Unlink | either client | `POST /devices/unlink` (authed) | Revokes all of the user's refresh tokens as a background task, an explicit "unlinking signs out every session" semantic |

Owning code: `backend/src/handlers/pairing.py` (this repo); `SignInForm.tsx` on the desktop side.

### 2. Browser-based Google sign-in (the three-repo handshake)

This is the one contract that genuinely spans all three repos, and its non-obvious part is that Aura-Web never calls a `juno-backend` HTTP endpoint to report completion.
It completes the handshake by writing directly into the same Firestore project juno-backend uses, into the exact document juno-backend created.

1. Aura-Desktop calls `POST /devices/web-auth/start`.
2. juno-backend creates a pending `web_auth_sessions/{code}` document carrying the Desktop UUID `install_id`, then returns the code with a 600-second TTL.
3. Aura-Desktop opens Aura-Web at `/auth?session=code`, where the user selects a Google account.
4. Aura-Web verifies the token, gets or creates `users/{uid}`, and transactionally marks the session complete in Firestore. It does not call juno-backend.
5. Aura-Desktop polls `POST /devices/web-auth/status` every two seconds.
6. juno-backend transactionally reads and deletes a completed session, returns the one-time custom token, and Aura-Desktop signs in.

Key files: `backend/src/handlers/web_auth.py` (this repo), `Aura-Desktop/src/overlay/useWebAuthSignIn.ts`, `Aura-Web/src/app/api/auth/complete/route.ts`.

Non-obvious details worth preserving if either side changes:
- The `web_auth_sessions/{code}` document is the entire contract between Aura-Web and juno-backend. There is no other channel. If Aura-Web's route stops writing to that exact collection/field set, the desktop poll silently never completes (falls through to `expired` after 10 minutes) with nothing to see on the backend side.
- `/status` deletes the session doc in the same transaction as the read, so a poll response can never be replayed; a hypothetical retry-on-the-desktop-side after a successful read would just get `not_found`.
- A new-device-linked push notification fires to the phone app on completion (`send_new_device_linked_push`), the same call the pairing flow above makes on success. This is a fourth touchpoint (backend to mobile) inside what looks like a three-repo flow.

### 3. Email/password sign-in

Aura-Desktop's `SignInForm` calls Firebase `signInWithEmailAndPassword` directly, no backend hop. Same Firebase project as everything else; no cross-repo contract beyond that.

### 4. Voice session (LiveKit)

Both Aura mobile and Aura-Desktop call `GET /voice/token` and join the same kind of LiveKit room against the same backend voice agent (`backend/src/agent/voice_agent.py`). The token stamps a `surface` value into participant metadata so the agent can tell which client type joined. Full sequence (join detection, agent state, captions, watchdogs) is documented in `Aura-Desktop/README.md`; that detail is desktop/backend-specific enough it isn't duplicated here.

The response also carries `realtime_bridge_enabled: bool`, owned by the backend's `REALTIME_BRIDGE_ENABLED` setting. Aura-Desktop must read this field before calling `POST /realtime/session`. When false, the backend ignores `bridged=1`, returns an ordinary LiveKit token, and the desktop activates that room without starting any OpenAI Realtime leg. Deploy this additive backend contract before releasing the desktop consumer.

Buddy's selected TTS voice is account-wide at `users/{uid}.settings.tts_voice_id`. Mobile writes that field directly through its authenticated Firestore settings path; Aura-Desktop reads and writes it through authenticated `GET /voice/preferences` and `PUT /voice/preferences` (`{voice_id}`). The backend validates the shared catalog and paid entitlement, updates only the nested voice fields, and the voice worker resolves the stored slug again when each new session starts. Deploy these backend routes before releasing the desktop picker. An active session keeps the voice pipeline it started with.

### 5. Screen-sight (desktop-only capture, backend-shared agent)

Desktop-exclusive today (mobile has no equivalent). Frame goes desktop to LiveKit `streamBytes` to the same voice agent process, which replies over the data channel with `element.point`. Full flow lives in `Aura-Desktop/README.md`; the only cross-repo fact worth stating here is that it rides the same backend voice agent as contract 4, not a separate service.

Buddy Drafts rides this same channel: the voice agent's `draft_outbound_message` tool reads the session's screen frame in-process and pushes `draft.generating` / `draft.created` / `draft.updated` / `draft.failed` events back over the data channel to the desktop's draft card. Email replies and DMs use `channel: "email_reply" | "cold_dm"`, require a fresh frame, consume the draft quota, and persist.

General visible output uses `present_visible_artifact` and publishes one backward-compatible `draft.created` event with `channel: "snippet"`, plus optional `artifact_kind: "command" | "code" | "config" | "prompt" | "steps" | "checklist" | "note"`, `content_format: "code" | "markdown"`, `title`, `language`, and `persisted: false`. Commands, code, configuration, prompts for another agent, and multi-step guidance do not require a frame unless the requested answer itself depends on the screen. They do not use the draft quota, do not persist, and are sent reliably with a strict packet-size guard. Old snippet-aware clients render the text as code and ignore the new fields; new clients render exact code or safe GFM Markdown. A desktop client older than snippet support drops this event, so the compatible desktop release must ship BEFORE the backend emits visible artifacts.

Desktop builds that validate the structured artifact checksum and acknowledge only after the exact revision commits into a visible card send `artifact_ack=displayed-v1` on `GET /voice/token`. The API copies that value into LiveKit participant metadata. The worker then requires `artifact.displayed {artifact_id, revision}` on the reliable `client_events` topic from the first ready card before it says the card is on screen, with one idempotent resend and bounded waits. Installed clients that omit the capability keep the prior optimistic behavior, and old workers ignore the additive query parameter. Acknowledging packet receipt, a rejected stale revision, a hidden overlay, or a checksum mismatch is forbidden.
The latest version of every draft also persists to Firestore at `UserAura/{uid}/drafts/{draft_id}` (`backend/src/services/drafts/`), written by the voice worker after each create/refine event, for the web dashboard's Drafts feed (contract 7).
Rows auto-expire 7 days after their last edit via a Firestore TTL policy on `expires_at` (one-time setup: `gcloud firestore fields ttls update expires_at --collection-group=drafts --enable-ttl`).
The draft text and its screen-derived context summary are what persist; the SCREEN FRAME itself stays ephemeral (worker RAM only, never Firestore/GCS/logs), and logs/analytics still never carry draft text.
The one REST piece is `POST /desktop/draft-outbound/refine` (`backend/src/handlers/draft_outbound.py`), called by the desktop card's refine chips with a Firebase ID token; when the desktop sends the worker-minted `draft_id` (optional, so old clients keep working), a successful refine also updates the stored doc, strictly update-only-if-exists so a REST caller can never mint a doc and a dashboard-deleted draft stays deleted.
The endpoint is text-only by design and cannot mint a new draft, which is also how refines stay outside the free-tier daily draft cap (`users/{uid}/usage/daily_outbound_draft`).

Storage-tree rule for desktop user data: `drafts` and `screen_saves` live under `UserAura/{uid}/…` (the same dashboard/rules surface as memory atoms), while every other desktop feature (`dictation_traces`, `meetings` and its job/claim/deletion siblings, `guide_tasks`, the Guide Mode rollup, desktop notifications) lives under `users/{uid}/…`. New desktop features default to `users/{uid}/…`; account deletion and any per-user export must enumerate both trees.

### 5a. Interview Mode job-description transfer (desktop-only, session-memory only)

Rides the same voice worker as contract 4. Buddy hands off to a separate LiveKit agent in the same `AgentSession`; the setup step asks for the company, then whether the user has the job description to hand.

The transfer is one step of a longer flow that is otherwise entirely worker-internal: Buddy -> `InterviewSupervisorAgent` (setup, then one structured planning call) -> `InterviewerAgent` (asks the planned questions) -> the same Buddy instance. Only the paste overlay crosses the repo boundary; the plan, the questions, and the interview itself never leave the worker and are never sent to any client.

When they do, the worker publishes `interview.material.request` on the reliable `client_events` topic with `{schema_version, interview_id, revision, material_type: "job_description"}`. The desktop shows one paste overlay and MUST echo `interview.material.overlay_shown {interview_id, revision}` on the same topic; the worker waits briefly, resends once idempotently at the same id and revision, and if no acknowledgement arrives it tells the user the box did not open and asks for the role conversationally instead. Acknowledging packet receipt, a hidden overlay, or a stale revision is forbidden, exactly as for `artifact.displayed` in contract 5.

The pasted text goes back over a LiveKit byte stream on topic `interview_material`, carrying `interview_id`, `revision`, `material_type` and `schema_version` as stream attributes. The worker accepts one stream per armed `(interview_id, revision)` and rejects everything else: a mismatched pair, an unparseable or superseded revision, a `material_type` other than `job_description`, an unsupported `schema_version`, a sender that is not the session user, more than 64,000 bytes, non-UTF-8, or blank. `revision` increments per request within one interview and `interview_id` is minted per interview, which is what makes a paste answering an earlier overlay unusable against a later one.

**The job description is session memory only.** It lives in the worker's `AgentSession.userdata` and dies with the session: no Firestore, no GCS, no REST, no logs. Only its character count is ever logged.

Old desktop builds that do not implement the overlay simply never acknowledge, and the worker falls back to collecting the role and background by voice, so neither side has to ship first. Aura-Desktop now implements the overlay (`src/overlay/interview/`), so both paths are live: a current build shows the box, an older one silently takes the voice fallback, and the worker cannot tell the difference except by the ack.

The desktop ranks the paste box directly below the chat slot and above every other card, because the worker has just told the user out loud to look at it. Chat still wins, and that degradation is honest by construction: the box never renders, so it is never acknowledged, so the worker takes the voice fallback rather than claiming a box is on screen.

Worker side: `backend/src/agent/voice/interview/` (`contracts.py` holds every constant named above). Desktop side: `src/overlay/interview/interviewMaterial.ts` holds the matching constants, and `interview.material.request` is registered in `src/lib/agentData.ts`.

Mock Interview and Interview Companion share only the backend `InterviewBrief` schema and pure preparation helpers in `backend/src/services/interview_preparation.py`. Mock Interview maps its session-only intake dossier into that schema before its existing question-planning call. It does not call the Interview Companion REST answer stream, use its capture session, receive its transcript, or inherit its consent or retention rules. The mapping adds no provider call, so the LiveKit handoff keeps its existing pre-session planning latency.

### 5a-2. Interview Companion preparation and answers (desktop-only, REST, memory-only)

Interview Companion is separate from Mock Interview and does not use LiveKit. Aura-Desktop calls authenticated `POST /interview-companion/brief` with contract version 3 (the request and brief models in `backend/src/services/interview_preparation.py` are the source of truth; they use `extra="forbid"`, so an older-version client receives 422), source-labeled preparation text, source verification states, and the answer-length preference. The backend returns an `InterviewBrief` whose company, role, facts, projects, STAR fields, metrics, requirements, gaps, do-not-claim boundaries, and panel questions each retain one or more source IDs plus a verification state. The endpoint does not persist or log the submitted text, and its response is `no-store`.

During a user-started supported video call, Aura-Desktop sends authenticated `POST /interview-companion/answer` requests. The answer request itself carries no `contract_version` field; the embedded brief slice is the versioned piece and is pinned to contract version 3 (`InterviewBriefSlice`), so a client sending an older slice receives 422. The request carries a locally selected, bounded brief slice, up to 12 recent source-separated turns, one action (`automatic`, `suggest`, `shorter`, `another_example`, `more_technical`, or `screen_sight`), and the current answer only for a requested rewrite. The `screen_sight` action attaches one validated JPEG frame; the frame is accepted only with that action, is attached to that single provider request, and is never persisted or made available to later turns. The backend still rejects candidate-source or non-final triggering turns. Automatic generation is decided in-stream: the answer model's first line is a sentinel — exactly `ANSWER`, or `SKIP|<reason>` with one of `another_interviewer`, `self`, `crosstalk`, `media_playback`, `uncertain` — which the backend parses and strips before any text reaches the desktop. That sentinel is a wire contract between the system prompt and the parser in `backend/src/handlers/interview_companion.py`; parsing deliberately fails open, so a model that just starts answering loses nothing. Manual actions are explicit user requests and skip the sentinel. Answer sentences may cite only verified source IDs, while gaps and do-not-claim entries remain constraints.

The version 2 turn contract also accepts optional `remote_speaker_id`, `speaker_overlap`, and `final_word_at_ms` fields. Remote speaker labels are supporting panel context only; they never replace the physical candidate/remote source boundary or establish who a question targets. Speaker overlap fails automatic gating before answer generation. The gate also fails closed for panel-to-panel speech, uncertain targeting, rhetorical or self-answered questions, media playback, and incomplete turns. SSE decision and completion frames include content-free `gate_ms` and `answer_ms` durations, while error frames include stable content-free codes. They never include transcript text in telemetry.

`POST /interview-companion/stt-token` returns no-store short-lived Deepgram and OpenAI Realtime transcription credentials. The legacy `accessToken` field remains the Deepgram token for released Desktop clients; current clients also read `deepgramAccessToken` and `openaiAccessToken`. On a partial provider outage the failed leg's token fields are omitted (never sent as empty strings), `expiresInSeconds` covers only the tokens actually present, and the response carries a `providers: {"deepgram": bool, "openai": bool}` availability object; a 503 is returned only when both legs fail. Aura-Desktop refreshes both in memory before the earlier expiry and sends replacements to the active Rust session for future WebSocket handshakes; a healthy stream is not interrupted solely to rotate credentials. Deepgram remains primary for five bounded reconnect attempts, then the same source-separated capture switches once to OpenAI for the remaining five attempts. Stale provider sockets and session epochs are discarded, and the Desktop hard-stops Interview Companion after two hours. The Desktop computes first-answer-text latency only when provider word timing is present and sends only durations, categorical outcomes, and counts through the existing consent-gated analytics path.

The reviewed brief lives only in Aura-Desktop's Rust process memory and is cleared on sign-out or process exit. The live transcript stays in the overlay session controller and is cleared on stop or sign-out. After an explicit user stop, the current Desktop may retain a bounded final-turn snapshot in React memory long enough to offer an explicit reflection. `POST /interview-companion/reflection` accepts that one session's final turns and reviewed brief slice, returns a structured response with `Cache-Control: no-store`, and writes nothing. Dismiss, sign-out, account switch, crash, or restart drops the snapshot and reflection. Only the user's separate `Save reflection` click writes a Markdown export under Downloads; it never creates a Firestore, GCS, Meeting Notes, chat-history, or UserAura record.

Deploy the additive brief module, answer contract version 3, and reflection route before releasing the Desktop consumer. The REST runtime remains independent of Mock Interview's LiveKit worker deployment.

### 5b. Meeting Notes (desktop-only capture, REST + Cloud Tasks synthesis)

Desktop-exclusive (Windows WASAPI capture; source architecture
`Aura-Desktop/MEETING_RECORDING_V2_ARCHITECTURE.md`). Unlike screen-sight/drafts this
rides pure REST, no LiveKit leg and no data-channel schema. Rollout order is
runtime lease, immutable ingest, new workers, then publication quality enforcement.

Meeting Notes and Interview Companion may subscribe concurrently to Aura-Desktop's shared WASAPI broker, but their consent and retention boundaries are independent. Meeting Notes starts only from its own arm, claim, and capture lifecycle and consumes a lossless queue into its encrypted durable segment store. Interview Companion starts only from its explicit Start control during a supported call, consumes a bounded transcription queue, and retains no audio. Starting, pausing, stopping, deleting, or recovering one consumer never authorizes or clears the other.

The contract, all under Firebase-ID-token auth (`backend/src/handlers/meetings.py`):
`POST /meetings/claim` gates capture and charges the transactional monthly counter
(`users/{uid}/usage/meetings_{YYYYMM}`; 5/month on free AND companion, unlimited
pro). It binds ownership to `installation_id`, retains `runtime_instance_id` for
diagnostics, and returns `capture_run_id`, monotonic `capture_fence`,
`lease_expires_at`, `protocol_version: 2`, and `max_capture_minutes`. A same-installation
recovery retains the immutable run identity; it retains the fence too when the
`runtime_instance_id` is unchanged (a resume), and increments it only for a different
runtime (a genuine second writer). The fence exists to lock out that second writer, and
advancing it on a resume invalidated audio the desktop had already stamped and could not
restamp, stranding the capture permanently. A different
installation receives the existing `meeting_already_claimed` conflict. Stale-fence
mutations return 409 `stale_capture_fence`, and that response body now also carries the
server's current `capture_fence` so a client can distinguish "behind" (adopt and resume)
from "forked" (unrecoverable).
`PUT /meetings/{id}/capture-runs/{capture_run_id}/segments/{seq}` takes raw
two-channel 16 kHz FLAC with the V2 integrity headers. It creates only
`audio/v2/{uid}/{meeting_id}/{capture_run_id}/{seq:06}/{plaintext_sha256}.flac`
using generation-match zero and returns a persisted receipt bound to digest, size,
object, generation, run, fence, meeting, and sequence. There is no V1 upload or
completion surface.
The bucket plus that lifecycle rule are a VERIFIED deploy prerequisite, not a comment: `backend/deploy.sh` runs `scripts/check_meeting_storage.py --check` before shifting traffic and aborts the deploy when the bucket is missing, in the wrong region, or lacks the lifecycle rule (2026-07-14 incident: the bucket was never provisioned, so every segment upload 404'd, the handler answered 503, and a real 22-minute meeting produced no note; the desktop's durable encrypted queue held the audio and recovered on the next signed-in restart once the bucket existed).
`POST /meetings/{id}/capture-runs/{capture_run_id}/complete` verifies the canonical
ordered manifest against deterministic segment documents and real upload receipts.
The same Firestore transaction advances state and creates a durable job, outbox row,
and append-only audit event. Cloud Tasks is only delivery. Workers use attempt/token
leases, transcribe per segment, persist immutable provider and transcript artifacts,
apply `meeting-quality-v1`, and can publish `ready` only through a fenced transaction
with a passing quality report. Incomplete-segment evidence remains an auditable
warning and produces a note marked partial when recognized speech is otherwise
usable. The insight model never authors transcript text or speaker labels.
Free and Companion meeting documents carry a 7-day `expires_at` TTL. Pro notes remain
until explicit deletion or account deletion. Successful upload, completion,
transcription, and publication do not delete cloud audio. `DELETE /meetings/{id}`
runs an exact-generation, retryable, receipt-bearing deletion saga; broad prefix
deletion is forbidden. It returns `state`, stable `deletion_id`, and `completed_at`;
a retryable storage or Firestore interruption returns 503
`meeting_deletion_retry_required`. The source architecture requires a resumable
handoff: server `block_new_work`, desktop durable `local_delete` receipt, then exact
cloud deletion through `delete_complete`. Uploads and stale workers must remain
blocked throughout.
`GET /meetings/recent` returns the note without transcript turns for a bounded dashboard payload. `GET /meetings/{id}` returns the full note with transcript. Both use an explicit public note-field allowlist and project the immutable canonical pointer as `transcript_artifact`.
Capture trust model is load-bearing for the brand: user-armed only (global toggle default OFF), visible recording indicator the entire time, session-lock pause.
Duration is TEMPORARILY clamped to 60 minutes on every tier (product decision 2026-07-11): events scheduled longer than an hour are not armable, the desktop engine hard-stops capture at 60 minutes per meeting, and the backend synthesis caps mirror the clamp (design values of 4h capture / 240min Pro synthesis return when long-meeting support lands).
Join detection polls only inside the event's exact scheduled window, start to end, because detection is not link-matched in v1 and a wider armed window widens the misattribution surface.

### 5b-2. Desktop dictation transcription credential (desktop -> backend -> Deepgram)

Aura Desktop's hold-to-talk dictation chord (Ctrl+Win) no longer transcribes
on-device. It opens one Deepgram Nova-3 streaming WebSocket per hold, DIRECT
from the desktop's Rust process, and authenticates it with a short-lived token
minted by `POST /dictation/stt-token`. Firebase-authenticated; returns
`{"accessToken": str, "expiresInSeconds": int}` with `Cache-Control: no-store`.
The permanent `DEEPGRAM_API_KEY` lives only in Cloud Run secrets and is
exchanged server-side through Deepgram's `/v1/auth/grant`.

Direct rather than proxied because dictation is interactive: every relay hop
would sit between the user finishing a word and seeing it. `no-store` plus a
`DEEPGRAM_STT_TOKEN_TTL_S` of 300s is what makes that safe. The desktop holds
only a minutes-long, transcription-scoped JWT, in memory, cleared on sign-out,
and refreshes at ~70% of the TTL so a chord press never pays for a mint.

When the backend cannot mint a token (key unset, Deepgram outage) the endpoint
returns 503 and the desktop shows a HUD error rather than failing at the
socket. Deploy this additive backend contract, with the key set, BEFORE
releasing the desktop consumer: the previous desktop build carried a bundled
on-device model and this one has nothing to fall back on. Dictation now
requires a signed-in user and a network, which the on-device version did not.

### 5c. Opt-in dictation training traces (desktop REST -> backend corpus)

The `modelId` and `sherpaVersion` fields in the trace payload now carry the
cloud recognizer's identity (`deepgram-nova-3-en` / `deepgram-v1-listen`). The
`sherpaVersion` field NAME is deliberately unchanged so traces already queued
on a user's machine from the on-device build still validate against the live
schema. Per-token timings are no longer sent (`tokens` is always empty): asking
the provider for word timings would mean transmitting and storing more
speech-derived data than the transcript itself.

Aura Desktop only queues a trace after the
15-minute correction window reaches `Finalized` under explicit sharing consent.
The Firebase-authenticated backend contract is `PUT /dictation/traces/{trace_id}`
for metadata, `PUT /dictation/traces/{trace_id}/audio` for 16 kHz mono PCM16 FLAC,
idempotent `DELETE /dictation/traces/{trace_id}`, and `GET /dictation/quota`.
`trace_id` is 24 lowercase hex characters; the current desktop also includes
`traceId` in JSON and the backend requires it to match the path.

The first metadata write and `users/{uid}/usage/dictation_{YYYYMM}` counter
increment are one transaction (500 traces/user/UTC month). Identical retries are
free; changed reuse or a tombstone returns 409. Metadata lives at
`users/{uid}/dictation_traces/{trace_id}`. Create-only audio lives in the separate
`DICTATION_AUDIO_BUCKET` at
`dictation/v1/{uid}/{trace_id}/{sha256}.flac`, with a prefix-scoped 180-day GCS
lifecycle rule. The hourly backend reconciliation confirms exact-generation
absence before clearing `has_audio`. Account deletion purges these blobs before
Firestore/Auth. Admin export uses `groundTruth` only and keeps recognition edits
(`verbatim/casing/punctuation`) separate from non-transcript style edits
(`disfluency/style`). See `backend/docs/dictation-training-data.md`.

### 5d. Desktop notification outbox and preferences

Aura-Desktop registers its notification capability with authenticated `PUT /desktop/notifications/preferences`, polls `GET /desktop/notifications` with an owner-bound opaque cursor, and acknowledges lifecycle through `POST /desktop/notifications/{notification_id}/ack`. The backend stores capability and category preferences at `users/{uid}/notification_preferences/desktop`, and durable outbox rows at `users/{uid}/desktop_notifications/{notification_id}`.

Channel selection is backend-owned and evaluated at delivery time. Enabled desktop users receive compatible committed, proactive, and account notifications on both mobile and desktop under one logical notification ID and budget decision. Meeting lifecycle events remain desktop-only; device-link security alerts remain mobile-only. A missing capability or failed preference lookup fails closed to mobile-only. Desktop received, seen, and acted acknowledgements update the existing notification ledger row, so adaptive engagement counts one logical send rather than one send per surface.

Retries keep that same logical notification ID. The ledger stores one cumulative
`attempt_count` plus per-channel `channel_attempt_counts` and
`channel_accept_counts`, so mobile and Desktop transport remain independently
observable without inflating the user's logical notification or budget count.

The Firebase UID is the ownership boundary on both sides. The system does not infer that two different UIDs belong to one person and does not merge their outboxes or preferences.

### 5e. Alarm schedule sync

A reminder now carries a `tier` of `reminder` or `alarm` at
`users/{uid}/reminders/{id}`, alongside `local_time` (naive wall clock) and
`timezone` (IANA). An absent `tier` means `reminder`, so already-released clients
and every pre-existing document are unaffected.

An alarm is **not** delivered as a notification. FCM cannot wake a doze'd device,
so the schedule is distributed ahead of time and each client rings from its own
local timer:

- Authenticated `GET /reminders/alarms` returns the COMPLETE set of pending
  alarm-tier reminders within 7 days, plus `server_time` for clock-drift
  detection. Clients replace their local schedule with this answer; an alarm
  absent from it must be disarmed. The endpoint returns 503 rather than an empty
  200 on failure, because an empty list means "disarm everything". Each row also
  carries a concrete `tone` slug and a stable `clip_tag` when that tone is
  `buddy`; native schedules must accept both as optional for upgrade safety.
- Authenticated `GET /reminders/{id}/wake-clip` returns `audio/mpeg` only when the
  owned reminder is an alarm whose resolved tone is `buddy`. Clients fetch it at
  arm time, cache it under `alarm_voice_{id}_{clip_tag}.mp3` in durable app
  support storage, and pass the absolute `voice_clip_path` to native code. The
  path is device-local and never crosses the API boundary.
- Authenticated `POST /reminders/{id}/ack` takes `{action: dismiss|snooze|im_up}`
  and, for a snooze, an optional `next_trigger_at` ISO instant naming the moment
  the client already armed. A `next_trigger_at` in the past, or more than 24h
  out, settles the row as dismissed instead of re-arming it.
- Silent data-only FCM messages with `notification_type: "alarm_sync"` carry
  `op: schedule|cancel|stop`; schedule messages also carry `tone` and `clip_tag`
  for immediate arming without waiting for a poll.
  These produce no user-visible artifact and deliberately bypass the
  notification funnel; they are the fast path only, and the GET above is
  authoritative.
- At the fire time the backend still submits a `SOURCE_REMINDER` proposal, but
  for alarms it is sent **data-only** with `alarm_fallback: "1"` so the client
  decides whether to render it. A client whose local alarm already rang must
  suppress it.

Snooze keeps `status = "pending"` and moves `trigger_at` forward. No consumer may
introduce a `snoozed` status: the due-scan selects on `pending` alone, so a
snoozed row would never fire again.

Server delivery is bounded by business validity, not merely worker recovery.
Ordinary reminder banners may be attempted at most four times within one hour of
`trigger_at`; an alarm gets one data-only server fallback within five minutes
because its local device schedule is authoritative. Each atomic claim increments
`delivery_attempt_count` and records first/last attempt timestamps. A row that is
late, malformed, or out of attempts becomes terminal `status = "expired"` with
`expired_at` and `delivery_terminal_reason`; it is never returned to `pending`.
The same deadline becomes Android FCM TTL, APNs expiration, and a cap on the
Desktop outbox row's `expires_at`, preventing delayed transport from resurfacing
an otherwise-valid send after its useful moment.
Current Flutter clients hide `expired` rows rather than presenting them as
delivered or allowing an undo to re-arm them.

Alarm tone slugs are a shared contract mirrored by Flutter, Android, and the
backend: `ripple`, `dawn`, `tide`, `pulse`, `chime`, `ascent`, `buddy`, `device`,
and `""` for the phone default. The backend resolves per-alarm override ->
`users/{uid}.settings.alarm_tone` -> `""` before a schedule reaches a device.

`POST /devices/register` accepts an optional boolean `alarm_capable`, stored per
token at `users/{uid}/fcm_tokens/{token}.alarm_capable`. It reports whether that
device will actually ring, i.e. whether the OS granted exact-alarm access. Three
states, and the difference is load-bearing: `true` and `false` are reports from a
client that knows, **absent means unknown and must never be read as "cannot
ring"** (it is the state of every client predating the alarm tier). The backend
uses it to decide whether Buddy may promise a wake-up at all. A non-boolean is
rejected with 400 rather than coerced.

Aura-Desktop does not ring yet: `desktop_notifications` has no `alarm_due` type,
so an alarm currently degrades to a generic desktop notification. Adding it means
a new outbox type plus a local timer driven by `GET /reminders/alarms`, since the
outbox poll cadence cannot ring at an exact minute.

### 6. Desktop distribution and auto-update

Two independent consumers of the same Aura-Desktop GitHub release, not one shared mechanism:

- **Aura-Web's download page** (`src/lib/desktop-release.ts`) calls the public GitHub Releases API (`GET /repos/AuraVoice/Aura-Desktop/releases/latest`) at request time (cached 15 min), picks the `.msi` asset for Windows and the universal `.dmg` for macOS out of that one response, and shows each platform's version/size. The two resolve independently, so a release carrying only one of them serves that one and falls back to the waitlist for the other. No redeploy needed when a new Aura-Desktop version ships; the page just reflects whatever is tagged `latest` on GitHub.
- **Aura-Desktop's own in-app updater** (`src-tauri/src/updater.rs`, Tauri's updater plugin) checks `https://github.com/AuraVoice/Aura-Desktop/releases/latest/download/latest.json` directly at startup, independent of Aura-Web entirely. Signed with a minisign keypair (`pubkey` in `tauri.conf.json`).

So: publishing a new Aura-Desktop GitHub release is a single action that both the download page and the in-app auto-updater pick up on their own, with no manual step in Aura-Web. This replaced the older mechanism (see "Known gaps" below).

### 7. Desktop-owned product data

Aura-Web has no authenticated product dashboard. History, saved items, drafts, memories, connector status, and settings are read and changed only by Aura-Desktop or the mobile app through authenticated juno-backend endpoints. Aura-Web retains account sign-in and billing checkout/portal flows only.

### 7b. Aura-Desktop dashboard reads and first-run profile attribution

Aura-Desktop calls these endpoints directly with `Authorization: Bearer <Firebase ID token>`. Every response is scoped to the token uid. The desktop sends `X-Aura-Platform: windows` or `X-Aura-Platform: macos` (`platformTag()` in `src/lib/platformKeys.ts`) and `X-Aura-App-Version`; neither header is required. The backend recognizes `android`, `ios`, `windows`, and `macos` (voice token stamping, chat product lookups, and per-platform latency metrics); unknown values degrade to an empty platform rather than erroring.

| Endpoint | Request | Response contract |
|---|---|---|
| `GET /account/onboarding` | none | Returns canonical account completion, existing profile values, minimum age/interest counts, and the backend-owned producible interest options. Desktop caches only a confirmed `complete: true`, scoped by Firebase UID. |
| `POST /account/onboarding` | `{display_name, date_of_birth, aura_consent_granted, gender, onboarding_interests, locale, language}` | Validates the same account fields mobile collects and transactionally changes `onboarding_complete` from false to true. Users under 18 are forced to `aura_consent_granted: false`. A concurrent already-completed account is preserved rather than overwritten. |
| `POST /devices/profile` | `{where_heard, where_heard_other, role, role_other}` where every field is `string | null` | `{ok: true}`. Profile fields use last write wins on `users/{uid}`. There is no per-user desktop chat rollout gate: the desktop shows its text lane to any signed-in user, and what keeps that safe is the server-enforced `surface` tool allowlist on `POST /chat` below, not a flag. |
| `POST /chat` | Existing chat request plus optional `surface`, default `app` | Desktop and unrecognized surfaces receive read-only tools only. The existing app surface retains its current tool behavior. |
| `POST /devices/guide-usage` | `{guide_session_id (32-hex), started_at, ended_at (ISO), duration_ms, outcome (completed\|abandoned\|signed_out\|session_ended), frames_sent, steps_received, agent_timeouts}` | `{ok: true}`, always (fail-soft; the desktop treats any non-2xx as a swallowed blip). Merges into a Guide Mode rollup on `users/{uid}` (lifetime counters + one latest-session snapshot; no subcollection). Counter increments are idempotent per writer per `guide_session_id`, so a replayed POST after a timed-out-but-committed write cannot double-count. The voice worker's `GuideCoordinator` writes the fields the client cannot see (model, avg TTFT, tools used, last user turn, frames processed) onto the SAME rollup, keyed by the same `guide_session_id`; a transaction guards the snapshot fields so a stale writer does not clobber a newer session. |
| `GET /desktop/home/stats` | none | `{last_used_at, last_session_seconds, sessions_this_week}` for desktop-surface voice sessions. |
| `GET /desktop/activity?limit=8` | `limit` is capped at 50 | `{items: [{id, kind, title, subtitle?, timestamp}]}` merging desktop voice sessions, drafts, and saved memory. |
| `GET /desktop/conversations?limit=30&cursor=` | `limit` capped at 100; cursor is opaque | `{items: [{id, title, preview?, started_at, duration_seconds?}], next_cursor?}`. |
| `GET /desktop/saved?limit=50` | `limit` capped at 200 | `{items: [{id, label, value?, saved_at}]}` from `UserAura/{uid}/memory_atoms`. |
| `GET /desktop/usage` | none | `{voice_minutes_used, voice_minutes_limit, drafts_used, drafts_limit, period_start, period_end}` from the daily entitlement counters. A null limit is unlimited. |

The endpoint field names are snake_case. `Aura-Desktop/src/lib/dashboardApi.ts` maps them to its own camelCase models, so neither side may rename fields independently. Empty data is a successful empty payload, not an error.

Account onboarding and desktop activation are separate gates. A mobile-completed account skips the account form but still receives desktop-specific setup on a new Windows installation. A desktop-first account must complete the authenticated account contract before `desktop_onboarding_seen_<uid>` can open the dashboard. Deploy the additive backend routes before releasing the desktop consumer.

Guide Mode itself (the armed screen-guidance session those usage rows describe) is a substantial worker-side subsystem, not just this endpoint. Arming is native on the desktop, so the worker can only request it. Full flow in `architectures/guide-mode.md`.

### 7c. Google connector control and browser handoff

Aura-Desktop controls Google Calendar and Gmail through authenticated juno-backend endpoints. Integration documents remain the credential owners; clients only receive connector state.

| Endpoint | Semantics |
|---|---|
| `GET /connectors` | Returns the connector catalog. Google Calendar and Gmail include `enabled`, `can_reconnect`, and connector-specific status fields. |
| `POST /connectors/google-calendar/enable` | Re-enables with retained credentials, performs a full sync, and recreates the webhook. Returns HTTP 409 with `{"error":"reauthorization_required"}` when usable credentials are missing or Google has revoked them. |
| `POST /connectors/google-calendar/disable` | Stops the webhook, removes active cached Calendar data, and sets `enabled: false`. It deliberately retains the server-side OAuth credentials so a later enable normally needs no consent screen. |
| `POST /connectors/google-calendar/sync` | Performs an explicit sync only while the connector is enabled. |
| `POST /connectors/gmail/enable` | Re-enables Gmail with retained credentials after refreshing them when needed. Returns the same HTTP 409 reauthorization contract when consent is required. |
| `POST /connectors/gmail/disable` | Sets Gmail `enabled: false` while retaining its server-side OAuth credentials. |

When Desktop receives `reauthorization_required`, it calls authenticated `POST /connectors/oauth/authorize` with an allowlisted `google_calendar` or `gmail` connector. juno-backend creates an owner-bound, ten-minute `connector_oauth_attempts` row with a PKCE verifier and returns Google's authorization URL. Google calls `GET /connectors/oauth/google/callback` on juno-backend, which atomically claims the attempt, exchanges the code, and writes the existing authoritative integration document. The callback opens `aura://connectors/complete`; Desktop consumes the result and refreshes `GET /connectors` once. There is no web dashboard, browser custom-token exchange, or connector polling loop.

## Full system diagram

```text
+----------------------- clients and sites ------------------------+
| Aura mobile | Aura-Desktop | Aura-Web auth/billing/download     |
+------+---------------+----------------------+--------------------+
       |               |                      |
       | full API      | pairing/auth/voice   | auth and billing
       +---------------+----------+-----------+
                                  v
                       +----------------------+
                       | juno-backend         |
                       | FastAPI / Cloud Run  |
                       +----+-------------+---+
                            |             |
                            v             v
                 +------------------+  +------------------+
                 | Firebase Auth +  |  | LiveKit Cloud    |
                 | Firestore        |  | rooms + worker   |
                 +--------+---------+  +---------+--------+
                          ^                      ^
                          | direct auth/session | WebRTC audio/data
            +-------------+------------+         |
            |                          |         |
       Aura-Web                   mobile/desktop+

Aura-Web download page ----+
                            +--> GitHub Releases <-- Aura-Desktop updater
```

## Cross-repo failure, retry, and recovery

```text
Web-auth page fails to complete Firestore session
    -> desktop polling remains pending
    -> session expires after 600 seconds
    -> user starts a new handshake

Status response is lost after transactional delete
    -> the one-time token cannot be replayed
    -> desktop must start a new handshake

New Aura-Web origin is not added to backend CORS
    -> dashboard browser reads fail before an HTTP status is exposed
    -> align both repositories' origin allowlists

Backend voice API is down
    -> clients cannot mint new room tokens
    -> existing LiveKit room behavior is independent until it needs MCP/backend tools

GitHub release metadata is unavailable
    -> download/updater retains its own failure UI or prior cached result
    -> no effect on already installed clients or backend services
```

### The non-obvious one: browser auth completes without a backend callback

Aura-Desktop asks juno-backend to create a pending code document. Aura-Web authenticates the user and updates that exact Firestore document directly, never calling juno-backend. Aura-Desktop polls juno-backend, which transactionally reads and deletes the completed document. The custom token is single-use, so losing the successful response requires a new handshake rather than replaying the deleted session.

Everything else is conventional: mobile authenticates with Firebase and calls juno-backend over HTTP. Voice is the only transport exception, where the backend mints a token and audio then travels through LiveKit.

## Known gaps / open questions

- **Aura-Web's PostHog project identity is unconfirmed.** It initializes its own `posthog-js` client from `NEXT_PUBLIC_POSTHOG_KEY`, separate code from the app-side analytics contract (`funnel_events.py` / `.dart`). Whether that resolves to the same PostHog project as the app side was never verified; the key lives in Vercel env vars, not in the repo. Confirm before assuming shared funnels between the site and the app.
- **The GCS bucket `aura-desktop-downloads`** still exists but is empty, left over from the deleted Flutter Windows client.
- **Meeting deletion still lacks the cross-repository local-delete handoff.** Aura-Desktop has not connected local deletion to `DELETE /meetings/{id}`, and the current backend route advances from `block_new_work` into cloud deletion in the same request. The source architecture requires a resumable pause for the desktop's durable `local_delete` receipt before exact cloud deletion. Split/acknowledge the route and add the desktop retry flow before calling deletion end-to-end across both repositories.
- **Meeting deletion during capture/upload is not yet proven safe.** Backend target discovery currently keys off the verified completion `segment_count`, so a delete before completion can miss already-created segment objects. Reconcile all exact run objects and generations before committing `delete_complete`.
- **Meeting V2 operational acceptance remains partial.** Current reconciliation covers stranded finalized runs, missing jobs/outbox delivery, expired worker leases, provider/quality failures, ready rows without artifacts, and integrity conflicts. Retry-deadline upload alerts, dimensional quality metrics, local-retention timing alerts, sanitized incident-bundle export, and shadow quality rollout are still missing.
- **The canonical desktop architecture status is stale.** `Aura-Desktop/MEETING_RECORDING_V2_ARCHITECTURE.md` still marks backend Phases C through E unchecked and describes this backend as read-only. Update that sibling source-of-truth only after the remaining cross-repository deletion and rollout gates are complete.

## Where to look next

- **Aura (this repo):** `CLAUDE.md` for working rules and the subsystem index, `architectures/README.md` for the architecture atlas itself (notifications, signal engine, reactive orchestration, briefing, tracking, voice, Guide Mode), `README.md` for mobile and backend setup.
- **Aura-Desktop:** `README.md` (full IPC surface, overlay state machine, voice/screen-sight sequence diagrams), `CLAUDE.md` (avatar rendering gotchas, main-thread-blocking rule, optimistic-cache rule), `lessons-learnt.txt`.
- **Aura-Web:** `CLAUDE.md` (design system rules, landing-page performance rules, blog publishing checklist), `DESIGN.md`.
