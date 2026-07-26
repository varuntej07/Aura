# Monitoring architecture

Aura currently observes the mobile/API/voice system through several provider-specific signals. The ops dashboard is a read-only aggregation surface; it is not in the user request path.

## Monitoring data flow

```text
+---------------------- runtime producers -----------------------+
| Flutter analytics | Cloud Run logs/metrics | voice worker logs |
| LLM/tool telemetry | Firestore state       | desktop Sentry    |
+-------------------------------+-------------------------------+
                                |
             +------------------+-------------------+
             |                  |                   |
             v                  v                   v
       +-----------+      +-----------+      +-------------+
       | PostHog   |      | GCP       |      | Langfuse    |
       | funnels   |      | log/metric|      | LLM/tools   |
       +-----+-----+      +-----+-----+      +------+------+
             |                  |                   |
             +------------------+-------------------+
                                |
                         +------v-------+
                         | ops providers|
                         | TTL caches   |
                         +------+-------+
                                |
                         +------v-------+
                         | FastAPI ops  |
                         | dashboard    |
                         +--------------+

Desktop Sentry issues -> Sentry provider -> same ops dashboard
Firestore operational reads -----------> same ops dashboard
```

### Signal ownership

| Question | Primary source |
|---|---|
| Product conversion and notification engagement | PostHog |
| Cloud Run request rate, latency, and failures | Cloud Monitoring and Logging |
| LLM attempts, tokens, and tool spans | Langfuse |
| Persisted product state | Firestore, queried narrowly |
| Desktop crashes | Sentry |
| Voice stage timing | structured worker telemetry, subject to deployment/log access |

Do not treat an absent provider response as a zero. Dashboards must distinguish unavailable, stale cache, empty result, and a measured zero.

## Failure, retry, and recovery

```text
Provider succeeds --------> refresh cache -> return fresh data
Provider times out --------> return stale cache when safe + mark stale
No cached value -----------> return unavailable/partial panel, not zero
One provider fails --------> other panels continue independently
Rate limit encountered ----> obey provider backoff/cache window
Ops process restarts ------> in-memory cache is cold; providers refill on demand
User runtime telemetry fails -> fail open; never block chat, voice, or notifications
Sensitive field appears ----> redact/drop before logs or analytics export
```

## Obvious walkthrough: inspect API health

1. The dashboard requests Cloud Monitoring request count, latency, and 5xx data.
2. The provider uses its TTL cache or performs a bounded read.
3. The API returns data with freshness metadata.
4. The UI renders measured values and the selected time range.

## Non-obvious walkthrough: Langfuse is temporarily unavailable

1. The LLM panel requests fresh aggregates.
2. The provider call fails or times out.
3. If a safe cached result exists, the dashboard serves it and labels it stale.
4. If no cache exists, only that panel becomes unavailable. Cloud Run and product panels continue to render.
5. User-facing model calls are unaffected because monitoring is outside their execution path.

## Operational checks

- Alert on user-impacting symptoms: sustained error rate, latency budget breach, provider fallback spike, FCM invalid-token spike, and failed durable tasks.
- Prefer rate/error/duration metrics for dashboards and structured logs for diagnosis.
- Correlate with bounded request, session, generation, and tool identifiers. Do not log prompt or transcript content.
- Keep provider credentials in deployment secrets and never expose them to the browser.

## Code anchors

- `ops/app.py`
- `ops/providers/`
- `ops/static/`
- `backend/src/services/analytics/`
- `backend/src/agent/voice/telemetry.py`
- `backend/src/lib/logger.py`
