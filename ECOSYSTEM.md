# Aura Ecosystem

This is the map of how the three live Aura codebases fit together: Aura (this repo, Flutter mobile app + the `juno-backend` FastAPI service), Aura-Desktop (Tauri Windows companion), and Aura-Web (Next.js marketing site + browser auth handoff page).

Each repo's own README/CLAUDE.md explains that repo in depth.
This file exists for the parts no single repo's docs can see: which repo calls which, over what transport, and what breaks if one side changes a contract without the others knowing.
Read this first when a change touches more than one repo, then follow the pointers at the bottom into the repo that actually owns the code.

## How to keep this file current

Update this file only when a change alters a **cross-repo contract**: an HTTP endpoint's path/request/response shape, a shared Firestore collection's schema, an auth/pairing handshake, a data-channel or event schema, shared config identity (the Firebase project, the PostHog project, analytics event names), or a deploy/version linkage between repos (like the Windows release feed below).

Do **not** update it for internal refactors, UI changes, or anything that stays inside one repo. That kind of detail belongs in that repo's own CLAUDE.md/README, not here. If you're unsure whether a change qualifies, ask rather than guessing either way.

**Filesystem assumption:** the pointer sections added to each repo's CLAUDE.md reference this file by relative path (e.g. `../Aura/ECOSYSTEM.md`), which only resolves because all three repos happen to be checked out as siblings under `C:\Users\varun\MobileApps\` on this machine. A clone of any one repo elsewhere, without that sibling layout, won't be able to follow that relative path. Treat this file's content as the authority, the pointer as a convenience that only works here.

## System map

| Repo | Path (this machine) | GitHub remote | Stack | Deploy mechanism | Role |
|---|---|---|---|---|---|
| **Aura** (this repo) | `MobileApps/Aura` | `varuntej07/juno` (repo renamed Aura, remote URL still says juno) | Flutter (mobile, Android/iOS) + FastAPI (`backend/`) | Mobile: Play Store / manual `.aab`. Backend: `docker build` straight from local disk (`backend/deploy.sh`), no git trigger, Cloud Run `juno-2ea45`/`us-central1` | Primary client (full API surface) and the shared backend every other repo talks to |
| **Aura-Desktop** | `MobileApps/Aura-Desktop` | `AuraVoice/Aura-Desktop` | Tauri v2 (Rust) + React 19 (TypeScript) | GitHub Releases (tagged build produces `.msi`/`.exe` + `latest.json`) | Current live Windows companion client, a from-scratch rewrite of the legacy Flutter desktop overlay below |
| **Aura-Web** | `MobileApps/Aura-Web` | `varuntej07/aura-web` | Next.js (App Router) + React + Framer Motion | Git-triggered deploy to Vercel (push to main auto-builds) | Marketing site (`auravoiceapp.com`), hosts the Google sign-in browser leg, and serves as the download page for Aura-Desktop |
| Legacy Flutter desktop (was `lib/main_desktop.dart`, inside this repo) | (deleted) | same as Aura | Flutter (Windows target) | Built `.exe` was pushed to a GCS bucket (`gs://aura-desktop-downloads`) | **Deleted 2026-07-11** (code, `windows/` platform tree, and the GCS-hosted installers). Aura-Desktop (Tauri) is the only Windows client. This repo keeps the backend contracts it consumes (pairing, web-auth, dashboard-link, draft-outbound, voice screen-sight, screen saves). |

`MobileApps/Juno` (no `.git`, last touched 2026-05-20) is a stale leftover checkout, not a live repo. It is not part of this system; if it keeps causing confusion it's a candidate to delete, but nothing currently reads or writes it.

## Shared infrastructure

- **Firebase project `juno-2ea45`**: Auth + Firestore, used by all three repos. All three write/read the same `users/{uid}` document shape (Aura-Web's `auth/complete/route.ts` explicitly builds a doc matching what the Flutter app writes, timestamps as ISO strings, not Firestore `Timestamp`, because Flutter's `DateTime.parse()` would crash on the latter).
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
| Redeem the code | Aura-Desktop | `POST /devices/pair/claim` (unauthenticated by design, the code IS the credential) | Returns a Firebase custom token, single-use via a Firestore transaction |
| Unlink | either client | `POST /devices/unlink` (authed) | Revokes all of the user's refresh tokens as a background task, an explicit "unlinking signs out every session" semantic |

Owning code: `backend/src/handlers/pairing.py` (this repo); `SignInForm.tsx` on the desktop side.

### 2. Browser-based Google sign-in (the three-repo handshake)

This is the one contract that genuinely spans all three repos, and its non-obvious part is that Aura-Web never calls a `juno-backend` HTTP endpoint to report completion.
It completes the handshake by writing directly into the same Firestore project juno-backend uses, into the exact document juno-backend created.

1. Aura-Desktop calls `POST /devices/web-auth/start`.
2. juno-backend creates a pending `web_auth_sessions/{code}` document and returns the code with a 600-second TTL.
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

### 5. Screen-sight (desktop-only capture, backend-shared agent)

Desktop-exclusive today (mobile has no equivalent). Frame goes desktop to LiveKit `streamBytes` to the same voice agent process, which replies over the data channel with `element.point`. Full flow lives in `Aura-Desktop/README.md`; the only cross-repo fact worth stating here is that it rides the same backend voice agent as contract 4, not a separate service.

Buddy Drafts rides this same channel: the voice agent's `draft_outbound_message` tool reads the session's screen frame in-process and pushes `draft.generating` / `draft.created` / `draft.updated` / `draft.failed` events back over the data channel to the desktop's draft card. Email replies and DMs use `channel: "email_reply" | "cold_dm"`, require a fresh frame, consume the draft quota, and persist.

General visible output uses `present_visible_artifact` and publishes one backward-compatible `draft.created` event with `channel: "snippet"`, plus optional `artifact_kind: "command" | "code" | "config" | "prompt" | "steps" | "checklist" | "note"`, `content_format: "code" | "markdown"`, `title`, `language`, and `persisted: false`. Commands, code, configuration, prompts for another agent, and multi-step guidance do not require a frame unless the requested answer itself depends on the screen. They do not use the draft quota, do not persist, and are sent reliably with a strict packet-size guard. Old snippet-aware clients render the text as code and ignore the new fields; new clients render exact code or safe GFM Markdown. A desktop client older than snippet support drops this event, so the compatible desktop release must ship BEFORE the backend emits visible artifacts.
The latest version of every draft also persists to Firestore at `UserAura/{uid}/drafts/{draft_id}` (`backend/src/services/drafts/`), written by the voice worker after each create/refine event, for the web dashboard's Drafts feed (contract 7).
Rows auto-expire 7 days after their last edit via a Firestore TTL policy on `expires_at` (one-time setup: `gcloud firestore fields ttls update expires_at --collection-group=drafts --enable-ttl`).
The draft text and its screen-derived context summary are what persist; the SCREEN FRAME itself stays ephemeral (worker RAM only, never Firestore/GCS/logs), and logs/analytics still never carry draft text.
The one REST piece is `POST /desktop/draft-outbound/refine` (`backend/src/handlers/draft_outbound.py`), called by the desktop card's refine chips with a Firebase ID token; when the desktop sends the worker-minted `draft_id` (optional, so old clients keep working), a successful refine also updates the stored doc, strictly update-only-if-exists so a REST caller can never mint a doc and a dashboard-deleted draft stays deleted.
The endpoint is text-only by design and cannot mint a new draft, which is also how refines stay outside the free-tier daily draft cap (`users/{uid}/usage/daily_outbound_draft`).

### 5b. Meeting Notes (desktop-only capture, REST + Cloud Tasks synthesis)

Desktop-exclusive (Windows WASAPI capture; design doc `Aura-Desktop/MEETING_NOTES_PLAN.md`, implemented 2026-07-11).
Unlike screen-sight/drafts this rides pure REST, no LiveKit leg and no data-channel schema, so there is no forced client/backend release order: the desktop fails soft (silent 404) against a backend without the routes, and the routes ignore clients that never call them.

The contract, all under Firebase-ID-token auth (`backend/src/handlers/meetings.py`):
`POST /meetings/claim` gates capture and charges the transactional monthly counter (`users/{uid}/usage/meetings_{YYYYMM}`; 5/month on free AND companion, unlimited pro; 402 body mirrors the `/voice/token` cap shape `{"detail": {"code": "meeting_cap_reached", "seconds_until_reset"}}`; 409 `meeting_already_claimed` for a cross-device conflict; same-device re-claim is idempotent via `users/{uid}/meeting_claims/{sha1(event_id)}` locks that self-expire at event end + 30 min).
`POST /meetings/{id}/segments/{seq}` takes raw 2-channel 16 kHz FLAC bodies (ch0 = device owner's mic, ch1 = system loopback) with `X-Segment-Start-Ms`/`X-Segment-Duration-Ms` headers into GCS `gs://juno-2ea45-meeting-audio/meetings/{uid}/{meeting_id}/` (bucket has a 7-day lifecycle rule as backstop; the worker deletes audio immediately after synthesis).
The bucket plus that lifecycle rule are a VERIFIED deploy prerequisite, not a comment: `backend/deploy.sh` runs `scripts/check_meeting_storage.py --check` before shifting traffic and aborts the deploy when the bucket is missing, in the wrong region, or lacks the lifecycle rule (2026-07-14 incident: the bucket was never provisioned, so every segment upload 404'd, the handler answered 503, and a real 22-minute meeting produced no note; the desktop's durable encrypted queue held the audio and recovered on the next signed-in restart once the bucket existed).
`POST /meetings/{id}/complete` enqueues one Cloud Tasks job (existing `juno-engagement` queue, deterministic task name) to `/internal/meetings/synthesize`, which transcribes per-channel (Deepgram nova-3 multichannel, the same `DEEPGRAM_API_KEY` the voice worker mounts), checks the user's exclude-keyword list (`users/{uid}/settings/meeting_notes`) BEFORE any STT, synthesizes `{summary, decisions, action_items, open_questions, language, one_sided}` and persists to `users/{uid}/meetings/{meeting_id}` with a 7-day `expires_at` TTL for non-pro tiers (TTL policy setup: `gcloud firestore fields ttls update expires_at --collection-group=meetings --enable-ttl`).
`GET /meetings/recent` + `GET /meetings/{id}` are the desktop's delivery poll (and a future dashboard feed).
Capture trust model is load-bearing for the brand: user-armed only (global toggle default OFF), visible recording indicator the entire time, session-lock pause.
Duration is TEMPORARILY clamped to 60 minutes on every tier (product decision 2026-07-11): events scheduled longer than an hour are not armable, the desktop engine hard-stops capture at 60 minutes per meeting, and the backend synthesis caps mirror the clamp (design values of 4h capture / 240min Pro synthesis return when long-meeting support lands).
Join detection polls only inside the event's exact scheduled window, start to end, because detection is not link-matched in v1 and a wider armed window widens the misattribution surface.

### 6. Windows desktop distribution and auto-update

Two independent consumers of the same Aura-Desktop GitHub release, not one shared mechanism:

- **Aura-Web's download page** (`src/lib/windows-release.ts`) calls the public GitHub Releases API (`GET /repos/AuraVoice/Aura-Desktop/releases/latest`) at request time (cached 15 min), picks the `.msi` asset, and shows its version/size. No redeploy needed when a new Aura-Desktop version ships; the page just reflects whatever is tagged `latest` on GitHub.
- **Aura-Desktop's own in-app updater** (`src-tauri/src/updater.rs`, Tauri's updater plugin) checks `https://github.com/AuraVoice/Aura-Desktop/releases/latest/download/latest.json` directly at startup, independent of Aura-Web entirely. Signed with a minisign keypair (`pubkey` in `tauri.conf.json`).

So: publishing a new Aura-Desktop GitHub release is a single action that both the download page and the in-app auto-updater pick up on their own, with no manual step in Aura-Web. This replaced the older mechanism (see "Known gaps" below).

### 7. Web dashboard data reads (browser calls juno-backend directly, CORS-gated)

Unlike every other Aura-Web <-> juno-backend contract above, `/dashboard`'s data endpoints — `GET /history/sessions` + `DELETE /history/sessions/{id}`, `GET /screen-saves` + `DELETE /screen-saves/{id}`, and `GET /drafts` + `DELETE /drafts/{id}` (the Buddy Drafts feed from contract 5) — are called **directly from the browser** with a Firebase ID token (`Authorization: Bearer <token>`), not proxied through an Aura-Web API route. Only the sign-in handoff (`POST /devices/dashboard-link/start` on the desktop side, `POST /devices/dashboard-link/claim` proxied through Aura-Web's `/api/dashboard-link/claim`) goes through Aura-Web's own API, for a reason unrelated to CORS: that claim endpoint is unauthenticated by design (the token is the credential), so `isAllowedOrigin` there is real defense-in-depth against browser-based abuse, something CORS can't provide against a non-browser caller anyway.

Because this is genuine browser-JS-to-juno-backend traffic (mobile's native HTTP client and Aura-Desktop's Tauri `plugin-http` are never subject to CORS), juno-backend now runs `CORSMiddleware` (`backend/src/main.py`, added as the outermost layer) gated on an explicit origin allowlist: `settings.cors_allowed_origins`, sourced from the `CORS_ALLOWED_ORIGINS` env var (`backend/src/config/settings.py`, default `https://auravoiceapp.com`). `allow_credentials` is always `False` — auth here is a Bearer header, not a cookie, so there is nothing for CORS credentials mode to protect.

**This origin allowlist is a cross-repo contract with Aura-Web's own origin allowlist** (`Aura-Web/src/lib/origin.ts`'s `SITE_URL` + `VOICE_ALLOWED_ORIGINS`, which gates `isAllowedOrigin` on Aura-Web's mutating API routes). The two lists serve different directions of the same relationship — Aura-Web's decides who may POST into it; juno-backend's decides which origins' browser-JS may read its responses — but they should name the same domain set. If Aura-Web ever adds a new deploy domain (a custom preview domain, a domain migration), both `VOICE_ALLOWED_ORIGINS` on Aura-Web and `CORS_ALLOWED_ORIGINS` on juno-backend need updating together, or the newer domain's dashboard silently can't read its own data (fails as a client-side `TypeError: Failed to fetch` with no status code, not a clean error — see Aura-Web's `juno-backend.client.ts`, which collapses that failure into its generic `history_failed` retry state).

The base URL Aura-Web's browser code targets (`NEXT_PUBLIC_JUNO_BACKEND_URL`, `Aura-Web/src/lib/juno-backend-url.ts`) defaults to the same production Cloud Run URL every other client uses (`https://juno-backend-620715294422.us-central1.run.app`) — no separate URL exists for this contract.

### 7b. Aura-Desktop dashboard reads and first-run profile attribution

Aura-Desktop calls these endpoints directly with `Authorization: Bearer <Firebase ID token>`. Every response is scoped to the token uid. The desktop sends `X-Aura-Platform: windows` and `X-Aura-App-Version` for observability; neither header is required.

| Endpoint | Request | Response contract |
|---|---|---|
| `POST /devices/profile` | `{where_heard, where_heard_other, role, role_other}` where every field is `string | null` | `{ok: true}`. Last write wins on `users/{uid}`. |
| `GET /desktop/home/stats` | none | `{last_used_at, last_session_seconds, sessions_this_week}` for desktop-surface voice sessions. |
| `GET /desktop/activity?limit=8` | `limit` is capped at 50 | `{items: [{id, kind, title, subtitle?, timestamp}]}` merging desktop voice sessions, drafts, and saved memory. |
| `GET /desktop/conversations?limit=30&cursor=` | `limit` capped at 100; cursor is opaque | `{items: [{id, title, preview?, started_at, duration_seconds?}], next_cursor?}`. |
| `GET /desktop/saved?limit=50` | `limit` capped at 200 | `{items: [{id, label, value?, saved_at}]}` from `UserAura/{uid}/memory_atoms`. |
| `GET /desktop/usage` | none | `{voice_minutes_used, voice_minutes_limit, drafts_used, drafts_limit, period_start, period_end}` from the daily entitlement counters. A null limit is unlimited. |

The endpoint field names are snake_case. `Aura-Desktop/src/lib/dashboardApi.ts` maps them to its own camelCase models, so neither side may rename fields independently. Empty data is a successful empty payload, not an error.

## Full system diagram

```text
+----------------------- clients and sites ------------------------+
| Aura mobile | Aura-Desktop | Aura-Web auth/dashboard/download   |
+------+---------------+----------------------+--------------------+
       |               |                      |
       | full API      | pairing/auth/voice   | browser APIs/auth handoff
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

### Obvious walkthrough: mobile calls the shared backend

1. Aura mobile authenticates with Firebase and sends a request to juno-backend.
2. The backend validates the token, reads or writes shared Firestore state, and returns the feature response.
3. Voice is the exception in transport: the backend mints a token, then audio and data travel through LiveKit.

### Non-obvious walkthrough: browser auth completes without a backend callback

1. Aura-Desktop asks juno-backend to create a pending code document.
2. Aura-Web authenticates the user and updates that exact Firestore document directly.
3. Aura-Desktop polls juno-backend, which transactionally reads and deletes the completed document.
4. The custom token is single-use. Losing the successful response requires a new handshake rather than replaying the deleted session.

## Known gaps / open questions

- **Legacy Flutter desktop fully retired (2026-07-11):** the code, the `windows/` platform tree, the CLAUDE.md shipping section, and the GCS-hosted `AuraSetup*.exe` installers were all deleted. No users remained on the old build. The GCS bucket `aura-desktop-downloads` itself still exists but is empty.
- **Aura-Web's PostHog project identity is unconfirmed.** It initializes its own `posthog-js` client from `NEXT_PUBLIC_POSTHOG_KEY`, separate code from the app-side analytics contract (`funnel_events.py`/`.dart`). Whether it's the same PostHog project as the app side, or a distinct marketing-site project, wasn't verified while writing this (the key value lives in Vercel env vars, not in the repo). Confirm before assuming shared funnels between the site and the app.
- **`MobileApps/Juno`** is an untracked, non-git leftover folder, not a live repo. Not part of this system; flagged here so it isn't mistaken for one.

## Where to look next

- **Aura (this repo):** `README.md` (mobile + backend architecture), `CLAUDE.md` (working rules, the many backend subsystems: notifications, signal engine, reactive orchestration, briefing, tracking, keyboard IME).
- **Aura-Desktop:** `README.md` (full IPC surface, overlay state machine, voice/screen-sight sequence diagrams), `CLAUDE.md` (avatar rendering gotchas, main-thread-blocking rule, optimistic-cache rule), `lessons-learnt.txt`.
- **Aura-Web:** `CLAUDE.md` (design system rules, landing-page performance rules, blog publishing checklist), `DESIGN.md`.
