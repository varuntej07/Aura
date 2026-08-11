# Aura architecture digital twin

Status: the repository contains a synthetic dashboard prototype. Live cross-runtime tracing, the collector, Cloud Trace export, and the BigQuery projection described here are proposed, not deployed.

## Current and proposed data flow

```text
CURRENT RUNTIME

+------------+   token/API   +-------------+   room media   +-------------+
| Flutter    | ------------> | Cloud Run   | <------------> | LiveKit     |
| client     |               | FastAPI     |                | Cloud       |
+-----+------+               +------+------+                +------+------+
      |                             ^                              |
      | WebRTC                      | authenticated MCP            |
      +-----------------------------|------------------------------+
                                    |
                             +------v------+
                             | voice worker|
                             | STT/LLM/TTS |
                             +------+------+
                                    |
                   +----------------+----------------+
                   v                v                v
              Firestore       model providers   current telemetry
                                                logs/PostHog/Langfuse

SYNTHETIC PROTOTYPE TODAY

ops dashboard -> Architecture tab -> local synthetic traces -> canvas/inspector
                                     no production API reads

PROPOSED LIVE TWIN

client/API/worker spans -> authenticated OTel collector
                         +-> Cloud Trace for trace inspection
                         +-> Cloud Monitoring for RED metrics
                         +-> 24h BigQuery projection for dashboard queries
                                      |
                                      v
                           authenticated ops SSE/API
                                      |
                                      v
                        architecture/runtime visualization
```

### Verified current boundaries

- Flutter obtains a room-scoped token from Cloud Run and joins LiveKit.
- A separately deployed LiveKit worker runs context gathering, STT, model/tool, and TTS stages.
- Worker tools call the Cloud Run MCP boundary with a Firebase token.
- Context gathering reads profile, memory, recent session, archive, Aura, and entitlement inputs in parallel with fail-soft defaults.
- Current telemetry is split across worker logs, Cloud Run monitoring/logging, PostHog, Langfuse, and persisted session records.
- `ops/static/architecture-twin.js` is synthetic and makes no production telemetry request.

### Proposed privacy contract

Only allowlisted metadata may enter the live twin: pseudonymous IDs, component/stage names, models, durations, status, bounded error categories, token counts, cost estimates, retry/fallback fields, and low-cardinality infrastructure tags.

Prompts, transcripts, audio, generated text, names, message bodies, raw tool arguments/results, document paths, and arbitrary exceptions are forbidden. Unknown attributes are dropped.

## Failure, retry, and recovery

```text
Voice or API path
    -> telemetry export is bounded and nonblocking
    -> exporter failure never changes user-visible execution

Collector unavailable
    -> bounded SDK queue applies documented drop policy
    -> operational drop counter rises
    -> application does not retry on the turn thread

Trace sampled out or stage missing
    -> dashboard shows unknown/coverage warning
    -> never converts missing data to zero

Ops stream disconnects
    -> browser reconnects from a bounded cursor/window
    -> static architecture remains available

Unauthorized viewer
    -> server rejects live data and UID lookup
    -> synthetic topology can remain separately gated
```

## Obvious walkthrough: inspect one slow voice turn

1. A session root links the client, worker, and MCP traces with pseudonymous correlation.
2. Child spans identify endpointing, STT, LLM attempts, tools, TTS, and output milestones.
3. The dashboard selects the trace and renders its ordered waterfall.
4. The inspector attributes latency and cost to the measured stage instead of inferring from logs.

## Non-obvious walkthrough: primary model fails, fallback succeeds

1. The logical LLM stage has two sibling provider-attempt spans.
2. The first records a bounded failure category and fallback reason without exception text or prompt content.
3. The second succeeds and supplies the user response.
4. Aggregate success remains true, while retry rate, extra latency, and billable attempts remain visible.
5. If either span is missing, the UI reports partial coverage rather than inventing a duration.

## Implementation boundary

Implemented prototype:

- `ops/static/architecture-twin.js`
- `ops/static/architecture-twin.css`
- `ops/static/index.html`
- `ops/static/app.js`

Verified runtime sources:

- `lib/data/services/voice_session_service.dart`
- `backend/src/agent/voice_agent.py`
- `backend/src/agent/voice/context.py`
- `backend/src/agent/voice/pipelines.py`
- `backend/src/handlers/mcp.py`
- `backend/src/services/tool_executor.py`
- `backend/src/services/analytics/llm_telemetry.py`

Proposed work must be implemented and security-reviewed before this document can call the twin live: OTel instrumentation, collector gateway, trace/metric exporters, short-retention query projection, authenticated live endpoints, and server-side authorization/audit for identity lookup.
