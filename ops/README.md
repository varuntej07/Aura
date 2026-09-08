# Aura Ops Dashboard

A founder-only dashboard that pulls live data from Firestore, Cloud Monitoring, Cloud
Logging, and PostHog into one screen. Lives in this repo (never bundled into the app or the
backend image) and deploys as its own Cloud Run service, reachable from any phone or laptop
behind a passcode.

## Architecture and data flow

```text
+----------------------- provider APIs ------------------------+
| Firestore | Cloud Monitoring/Logging | PostHog               |
| Crashlytics BigQuery | GitHub Releases | GCP Billing          |
+-----------------------------+-------------------------------+
                              |
                              v
                    +---------------------+
                    | providers/          |
                    | bounded reads/cache |
                    +----------+----------+
                               |
                               v
                    +---------------------+
                    | panels.py + app.py  |
                    | passcode-gated API  |
                    +----------+----------+
                               |
                               v
                    +---------------------+
                    | static browser UI   |
                    | no embedded secrets |
                    +---------------------+

Architecture tab -> local synthetic twin data -> canvas + inspector
```

The dashboard is read-only and outside every mobile, backend, and voice request path. The Architecture tab is currently a synthetic prototype and does not read live traces.

## Failure, retry, and recovery

```text
Wrong/missing passcode -----> 401; no provider data is fetched for the user
OPS_PASSCODE unset ---------> 503 fail-closed
Provider succeeds ----------> refresh server TTL cache
Provider fails with cache --> serve labeled stale data where supported
Provider fails without cache -> only that panel is unavailable; never show zero
Browser revisits tab --------> reuse client memory until explicit Refresh
Process restarts -----------> caches start cold and refill on bounded demand
```

### Obvious walkthrough: load Overview

1. The browser loads public static HTML with no user data.
2. A passcode-gated API request composes the required providers.
3. Provider results populate server caches and the response renders in the browser.

### Non-obvious walkthrough: one provider times out

1. The requested tab starts several independent provider reads.
2. One provider times out while the others succeed.
3. A safe cached value is returned with stale state, or only that panel shows unavailable.
4. The browser does not auto-refresh into a retry storm; the founder can use the rate-limited Refresh control.

## Deploy (one command)

```bash
bash ops/deploy.sh juno-2ea45 us-central1
```

It asks you to set a passcode, deploys the Firestore indexes, builds + deploys the service,
and prints the URL. Open the URL on any device, type the passcode once (your browser
remembers it), done. Re-run the same command to ship updates.

## Security model (read this first)

This dashboard aggregates **every user's private chat text and voice transcripts** into one
page, so the gate matters:

- The HTML page is public and holds **no** user data.
- `GET /api/dashboard` depends on `require_passcode`, which constant-time-compares the
  passcode against `OPS_PASSCODE`. Wrong/missing passcode → 401, no data leaves.
- An unset passcode fails **closed** (503), never open.
- Cloud Run is `--allow-unauthenticated` only so the page can load; the **passcode** is the
  gate. The service URL is **guessable** (`juno-ops-<project-number>.<region>.run.app`,
  derivable from the public backend URL), which is exactly why an unauthenticated dashboard
  would be a breach and the passcode is required.
- The passcode is a shared secret: treat it like a password, don't share or post the link
  with the passcode. Use 8+ letters/numbers.

```
phone/laptop ─► https://juno-ops-….run.app ─► enter passcode ─► compare(OPS_PASSCODE)
                                                                  match? ─no─► 401
                                                                       │ yes
                                                                       ▼
                                                  Firestore / Monitoring / Logging / PostHog
```

## Layout (v2: dark control-room)

```
Overview:  attention strip (only what needs a decision) · metric strip
           (signins/new/opened-app/talked/total/msgs/p95/5xx, every count
           clickable to see WHO) · messages + voice feeds · recommender health ·
           recommendations sent · top screens · filterable users table ·
           per-user drawer · feedback · multi-service errors ·
           retention (DAU/WAU/MAU + cohort grid) · notification funnel ·
           revenue funnel (paywall interest capture)
Mobile:    Crashlytics crash feed (BigQuery export) · per-platform backend
           latency · client-observed chat/voice p50/p95/p99 · LiveKit worker
           first-talk and reply p50/p95/p99 · downloads (honest
           "not live yet" until the store listings ship)
Desktop:   adoption funnel (site click -> installer fetches -> signed-in
           installs -> active 7d) · signed-in install list · same latency block ·
           operational errors in Logs
Web:       auravoiceapp.com pageviews · referrers · download funnel
           (download_page_viewed -> download_clicked -> installer fetches ->
           signed-in installs)
Costs:     live LLM spend from the Firestore ledger (est. USD, tokens, cache
           hit rate, per-day chart, top spenders) · one-click links to every
           provider's own usage console · Brave query count and breakdown ·
           GCP billing export · manual subscription costs with explicit labels
Logs:      error-first merged Cloud Run/LiveKit/mobile viewer · warning toggle ·
           text/service/range filters · duplicate grouping
Architecture: synthetic static/runtime topology, concurrent sample traces,
              inspector, waterfall, fallback and cache overlays
```

Within one payload build the independent provider reads are issued
concurrently (a small thread pool in `panels.py`), because they are all blocking
network round trips to different systems and a cold Overview load makes about
twenty of them. Concurrency changes *when* those calls happen, never *how many*:
the TTL caches below remain the read-cost gate.

Refresh model: NOTHING auto-refreshes. Each tab fetches once on first view,
then serves from client memory; only the Refresh button re-fetches the active
tab, rate-limited to one hit per 60 seconds (visible countdown). Server-side,
every payload rides an in-process TTL cache (feeds 55s, users 120s, analytics
120s, the billed-per-byte BigQuery crash scan 300s), so N open devices or a
scripted curl loop cost one provider fetch per window, never one per request.

## Configuration (ops/.env, all optional except the passcode)

| Var | Feeds | Notes |
|---|---|---|
| `OPS_CRASHLYTICS_BQ_DATASET` | Mobile crash feed | Default `firebase_crashlytics`; requires the one-click BigQuery export in Firebase console |
| `GITHUB_TOKEN` | Desktop installer fetches | Optional; lifts the 60 req/hr unauthenticated limit (provider caches 15 min anyway) |
| `OPS_POSTHOG_WEB_PROJECT_ID` | Web tab | Only if aura-web uses a different PostHog project than the app (unverified, see ECOSYSTEM.md) |
| `OPS_GCP_BILLING_TABLE` | Actual GCP cost | Full BigQuery billing export table name: `project.dataset.table` |
| `OPS_BRAVE_COST_PER_QUERY_USD` | Estimated Brave cost | Optional unit rate multiplied by observed billable queries |
| `OPS_PROVIDER_MONTHLY_COSTS_JSON` | Providers with subscriptions | JSON map such as `{"livekit":50,"cartesia":20}`; values are prorated for the selected range |

## Answering "who?" (every count is a drill-down)

The strip used to render `active today: 6` with no way to learn who the six
were. Now every count whose membership is knowable is clickable and filters the
Users table to exactly those people; clicking any name opens a per-user drawer
with their messages, voice sessions, what Buddy recommended them and whether it
landed, their desktop installs, and their LLM spend. The drawer is a client-side
filter over feeds the Overview payload already contains, so it costs **zero**
extra Firestore reads.

Two activity numbers sit side by side on purpose:

- **opened app today** is `last_active_at`, written by `auth_repository.dart` on
  sign-in *and on silent session restore*. It means the app came to the
  foreground, nothing more.
- **talked to Buddy today** is distinct uids in today's message and voice feeds.

For a companion app the second is the number that matters, and the gap between
them is the thing worth seeing. When people open the app and leave without
talking, the attention strip at the top says so.

## Desktop downloads are not installs

GitHub's per-asset `download_count` is a raw HTTP counter. Crawlers and security
scanners fetch release assets, and the Tauri updater re-fetches the same `.msi`
on **every auto-update of every existing install**, so one happy user generates a
"download" per release. None of that is filterable through GitHub's API. This is
why the dashboard could show 10 downloads against 0 users with both numbers
correct.

The dashboard therefore reports two different things and never conflates them:

| Number | Source | What it means |
|---|---|---|
| installer fetches | GitHub Releases | Upper bound on interest. Bots and auto-updates included. |
| installs signed in | `users/{uid}/linked_devices/{install_id}` | One doc per installation that actually reached a signed-in state. |

`linked_devices` is written by `backend/src/services/linked_devices.py` on
pairing and web-auth, keyed by the client's own `install_id`, so a reinstall does
not double count and a download that was never opened does not count at all. The
read is one bare `collection_group` stream: no index, no field override.

## LLM cost needs no configuration

The Costs tab used to say *"usage tracking intentionally disabled"* for
Anthropic/Gemini/OpenAI. That was wrong. Since 2026-08 the backend increments
`users/{uid}/cost/{YYYY-MM-DD}` on **every** model call (chat, voice, fallbacks,
background agents) with generations, input/cached/output tokens and estimated
microUSD, on a 90-day TTL. The dashboard reads it directly.

Because the doc id *is* the date, that read needs no query: the panel builds the
exact `users/{uid}/cost/{date}` paths for the window and issues one batched
`get_all()`. No index, no collection-group scan, one round trip.

Two limits, both stated on the card rather than left to be assumed:

- It is an **estimate**. The backend prices each call from a token table
  (`estimate_microusd`), not from an invoice.
- It carries **no model field**, so it cannot be split into Claude / Gemini /
  GPT. Attributing the combined total to any one vendor would be a fabrication,
  so it sits on its own `llm` row and each vendor row links to that provider's
  own usage console for the real split.

## Error and voice log sources

The Logs tab loads `ERROR` and above automatically. Select `WARNING` to include
all warnings and errors. Backend and ops records come from Cloud Logging; release
mobile warnings/errors are redacted by `AppLogger` and read from PostHog.

The LiveKit worker emits structured first-talk and per-turn records, but they
appear only after the LiveKit Cloud project has a Google Cloud log drain pointed
at this project. **This is the one and only reason the six "voice worker" tiles on
the Mobile and Desktop tabs are empty**, and those tiles now say so instead of
rendering a bare `n/a`. Two steps, both outside this repo:

1. In the LiveKit Cloud console, add a log drain targeting Google Cloud Logging
   in project `juno-2ea45`.
2. Grant the ops service account read access:

   ```bash
   gcloud projects add-iam-policy-binding juno-2ea45      --member="serviceAccount:<ops-service-account>"      --role="roles/logging.viewer"
   ```

The provider filters on `resource.type="cloud_run_revision"` plus the
`VoiceSession:` message prefixes, so drained records land in the existing query
with no further change here.

The voice measurements intentionally distinguish:

- client voice start to first assistant transcript delta;
- worker entrypoint to first assistant audio metrics;
- user end-of-utterance to first assistant audio;
- token-mint to first talk, retained as a diagnostic and not shown as the user
  startup metric because prewarmed tokens can make it misleading.

Chat TTFT starts at send and stops only on the first non-empty visible text
delta. Connection, tool, and status events do not stop the timer.

## Per-platform backend latency (one-time GCP setup)

Cloud Run's `request_latencies` metric cannot see custom headers, so the
Mobile/Desktop latency split reads a log-based DISTRIBUTION metric fed by the
backend's `request_metric` log lines (one per client request carrying
  `X-Aura-Platform`; see `RequestLoggingMiddleware` in `backend/src/main.py`).
Create the metric ONCE (needs a config file because distribution metrics take
extractors):

```bash
cat > /tmp/req_lat_metric.yaml <<'YAML'
name: request_latency_by_platform
description: Backend request latency split by client platform header
filter: >-
  resource.type="cloud_run_revision"
  resource.labels.service_name="juno-backend"
  jsonPayload.message="request_metric"
valueExtractor: EXTRACT(jsonPayload.duration_ms)
labelExtractors:
  platform: EXTRACT(jsonPayload.platform)
metricDescriptor:
  metricKind: DELTA
  valueType: DISTRIBUTION
  labels:
    - key: platform
bucketOptions:
  exponentialBuckets:
    numFiniteBuckets: 32
    growthFactor: 1.5
    scale: 10
YAML
gcloud logging metrics create request_latency_by_platform \
  --project=juno-2ea45 --config-from-file=ops/request_latency_by_platform.yaml
```

Until the metric exists AND clients send the header (new app/desktop builds),
the panel reads n/a, which is the honest state, not zero.

## Recommendation trace (what each user is getting recommended)

Two panels answer "what is the recommender doing, and is it working":

- **Recommendations sent** — newest notifications across all users, read from the
  existing `users/{uid}/notifications` ledger (writer:
  `backend/src/services/notification_ledger.py`). Each row is the actual copy the
  user received, a plain-language reason the recommender chose it (the framer's own
  `relevance_reason`, e.g. "they follow KCR"), its match score, and whether it
  landed (opened after 8s / swiped away / no tap yet).
- **Recommender health** — the signal engine's own per-tick summary line, read
  straight from Cloud Logging (no new write). When notifications go quiet this says
  *why* (starved pool vs weak matches vs nobody bootstrapped) instead of looking
  identical to "all healthy, nothing to send".

**No new index, no new writes, on purpose.** The ledger already exists and
self-purges on a 90-day Firestore TTL, so the dashboard never grows the database.
The sent panel reads each user's `notifications` subcollection ordered by `sent_at`,
a single-field order Firestore **auto-indexes at collection scope**, so it needs no
`COLLECTION_GROUP` index (deliberately avoiding the index footgun behind past
notification outages). That per-user fan-out is cheap at beta scale (tens of users).
When this reaches hundreds of users, switch `latest_notifications` to one
`collection_group("notifications")` query and add a `COLLECTION_GROUP` override on
`notifications.sent_at`, exactly like the `messages` / `voice_sessions` feeds below.

## Files

| File | Role |
|---|---|
| `app.py` | FastAPI: the passcode gate + all `/api/*` routes, serves the page |
| `panels.py` | composes the providers into per-endpoint payloads |
| `providers/` | one module per source (firestore, monitoring, logging, posthog, crashlytics/BigQuery, github releases, cost) |
| `ranges.py` | the today/7d/30d vocabulary, defined once |
| `fields.py` | every Firestore field name in one place, mirroring the app/backend writers |
| `static/` | the UI: `index.html`, `style.css`, `app.js`, the architecture-twin module, and vendored Chart.js (no build step) |

## Run locally (optional smoke test before deploying)

```bash
cd ops
gcloud auth application-default login          # read creds for the providers
pip install -r requirements.txt
OPS_PASSCODE=test1234 uvicorn app:app --reload --port 8000   # open http://localhost:8000
```

## What deploy.sh handles for you

- **Passcode** — prompts and injects it; nothing to edit.
- **Firestore indexes** — runs `firebase deploy --only firestore:indexes`. The two
  collection-group queries (messages, voice) 400 until these finish building (a few minutes
  after first deploy), so those two panels may be briefly empty on a fresh deploy. The index
  definitions are already in the repo's `firestore.indexes.json`.
- **Service account** — uses the project default unless you set `OPS_SERVICE_ACCOUNT` (a
  least-privilege SA; creation commands are commented at the top of `deploy.sh`).

## Honest caveats (design limits, not bugs)

- **Messages are near-real-time, not instant.** They reach Firestore through the app's
  write-behind sync queue (`chat_backup_service.dart`); an offline user's latest message only
  appears once their device flushes. The lag is the user's connectivity, not the dashboard.
- **The `api p95` tile is the backend HTTP service only.** The voice worker is a LiveKit
  worker, not request/response, so it has no `request_latencies` metric. Voice latency lives
  in PostHog (`voice_first_response`) and can be added as a panel later.
- **"Top screens" ranks by view count, not true dwell time.** Real "time spent on a page"
  needs per-session windowing; view count is the honest first cut.
- **PostHog screen event name is verified** (2026-09-07): the panel returns real Flutter
  route names, confirming the app emits PostHog's mobile standard (`$screen` /
  `$screen_name`). Change `posthog_provider.SCREEN_EVENT` / `SCREEN_NAME_PROPERTY` only if
  the app moves to a custom event name.
- **"Today" is `OPS_UTC_OFFSET_HOURS`.** Set it to your day boundary (e.g. `5.5` IST, `-7` PDT).
- **PostHog is optional.** Without `POSTHOG_PERSONAL_KEY` + `POSTHOG_PROJECT_ID`, only the
  top-screens panel is empty; everything else works.
