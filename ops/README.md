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
Overview:  metric strip (signins/new/active/total/msgs/p95/5xx) ·
           messages + voice feeds · recommender health · recommendations sent ·
           top screens · users table · feedback · multi-service errors ·
           retention (DAU/WAU/MAU + cohort grid) · notification funnel ·
           revenue funnel (paywall interest capture)
Mobile:    Crashlytics crash feed (BigQuery export) · per-platform backend
           latency · client-observed chat/voice p50/p95/p99 · LiveKit worker
           first-talk and reply p50/p95/p99 · downloads (honest
           "not live yet" until the store listings ship)
Desktop:   operational errors in Logs · same latency block ·
           GitHub Releases download counts
Web:       auravoiceapp.com pageviews · referrers · download funnel
           (download_page_viewed -> download_clicked -> installer downloads)
Costs:     Claude/Gemini/OpenAI traced spend · Brave query count and breakdown ·
           GCP billing export · manual subscription costs with explicit labels
Logs:      error-first merged Cloud Run/LiveKit/mobile viewer · warning toggle ·
           text/service/range filters · duplicate grouping
Architecture: synthetic static/runtime topology, concurrent sample traces,
              inspector, waterfall, fallback and cache overlays
```

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
| `GITHUB_TOKEN` | Desktop downloads | Optional; lifts the 60 req/hr unauthenticated limit (provider caches 15 min anyway) |
| `OPS_POSTHOG_WEB_PROJECT_ID` | Web tab | Only if aura-web uses a different PostHog project than the app (unverified, see ECOSYSTEM.md) |
| `OPS_GCP_BILLING_TABLE` | Actual GCP cost | Full BigQuery billing export table name: `project.dataset.table` |
| `OPS_BRAVE_COST_PER_QUERY_USD` | Estimated Brave cost | Optional unit rate multiplied by observed billable queries |
| `OPS_PROVIDER_MONTHLY_COSTS_JSON` | Providers with subscriptions | JSON map such as `{"livekit":50,"cartesia":20}`; values are prorated for the selected range |

## Error and voice log sources

The Logs tab loads `ERROR` and above automatically. Select `WARNING` to include
all warnings and errors. Backend and ops records come from Cloud Logging; release
mobile warnings/errors are redacted by `AppLogger` and read from PostHog.

The LiveKit worker emits structured first-talk and per-turn records, but they
appear only after the LiveKit Cloud project has a Google Cloud log drain pointed
at this project. Configure that drain in LiveKit Cloud, then grant the ops
service account `roles/logging.viewer`. Until then worker values remain `n/a`.

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
| `providers/` | one module per source (firestore, monitoring, logging, posthog, langfuse, crashlytics/BigQuery, sentry, github releases) |
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
- **PostHog screen event name is unverified.** `posthog_provider.SCREEN_EVENT` /
  `SCREEN_NAME_PROPERTY` default to PostHog's mobile standard (`$screen` / `$screen_name`).
  Confirm against the Flutter `AppRouteObserver` before trusting that one panel.
- **"Today" is `OPS_UTC_OFFSET_HOURS`.** Set it to your day boundary (e.g. `5.5` IST, `-7` PDT).
- **PostHog is optional.** Without `POSTHOG_PERSONAL_KEY` + `POSTHOG_PROJECT_ID`, only the
  top-screens panel is empty; everything else works.
