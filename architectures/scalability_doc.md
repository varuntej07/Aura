# Aura scalability architecture

This document separates the current deployable topology from scale-triggered changes. Capacity and cost decisions must use measured concurrency, latency, error, quota, and unit-cost data rather than fixed user-count folklore.

## Current block-level architecture and scaling flow

```text
+---------------- clients ----------------+
| Flutter mobile | desktop | web dashboard|
+---------+-----------------------+--------+
          | HTTPS/SSE             | WebRTC
          v                       v
+----------------------+   +-----------------------+
| Cloud Run FastAPI    |   | LiveKit Cloud         |
| stateless HTTP plane |   | managed voice plane   |
| autoscaled instances |   | worker jobs per room  |
+----+--------+--------+   +-----------+-----------+
     |        |                            |
     |        +----------------------------+
     |              authenticated MCP
     v
+----------------------+       +--------------------+
| Firestore/Auth/GCS   |       | external providers|
| durable shared state |       | model/STT/TTS/APIs |
+----------+-----------+       +--------------------+
           |
           v
+----------------------+
| Cloud Tasks/Scheduler|
| durable async work   |
+----------+-----------+
           |
           v
   same Cloud Run handlers

Telemetry -> provider stores -> read-only ops dashboard
```

### Scaling boundaries

| Boundary | Scale mechanism | Primary limit to measure |
|---|---|---|
| Cloud Run API | Instance autoscaling and request concurrency | Long SSE requests, CPU, memory, cold starts |
| LiveKit worker | Managed worker/job scaling | Provider quotas, worker region, per-session cost |
| Firestore | Horizontal service with document/index constraints | Hot-document contention and query shape |
| Cloud Tasks | Queue rate, retry, and backpressure | Handler idempotency and downstream quota |
| Model/STT/TTS providers | Provider-side capacity | Rate limits, latency, fallback rate, unit cost |
| Notification delivery | Per-user arbitration and FCM | Token health, policy throughput, duplicate control |

Do not add Redis, regions, queues, or self-hosted inference solely because total registered users increased. Add them when the measured bottleneck matches the component's failure mode.

### Voice plane: verified current behavior

The checked-in worker is deployed as a LiveKit Cloud Agent (`backend/livekit.toml`) and
uses `WorkerOptions` without a custom load function or load threshold. LiveKit therefore
owns worker dispatch and managed capacity. The repository does not contain the project's
live agent-session quota or provider-account quotas; read those from the LiveKit and
provider dashboards before making a capacity claim.

The current per-session pipeline is:

| Stage | Current chain | Attempt behavior |
|---|---|---|
| STT | Deepgram nova-3 -> Deepgram nova-2 | 10s per attempt, zero internal retries; same-provider fallback |
| LLM | OpenAI when configured -> Anthropic voice model -> Gemini cheap tier | 10s per attempt, zero internal retries; cross-provider fallback |
| TTS | Cartesia sonic-3 -> Deepgram aura-2 -> Cartesia sonic-2 | zero internal retries; partial-output behavior is owned by LiveKit's adapter |

Other current ceilings are a 10s room-connect timeout, 5s Firebase MCP-token mint timeout,
8s active MCP voice-tool timeout, and 1.5s total pre-session context-gather budget. If the 1.5s
context ceiling fires, the current implementation uses defaults for the entire gather,
not the subset that happened to finish early. Semantic turn-detection construction can
degrade to VAD-based endpointing.

`settings.py` also declares `VOICE_TOOL_TIMEOUT_S = 5.0`, but the active MCP path does
not read it; `handlers/mcp.py` owns a separate `_VOICE_TOOL_TIMEOUT_S = 8.0`. Treat this
as configuration drift, not as a second runtime timeout.

Important current boundary: LiveKit's fallback adapters maintain per-process provider
availability and recovery probes, but Aura does not yet implement a global
quota-aware admission controller, a cross-session retry budget, or one propagated
end-to-end deadline covering every sequential LLM fallback. These are scale-triggered
changes, not current capabilities. MCP token mint failure also returns before
`AgentSession.start`; tool-free voice startup is not currently implemented.

## Failure, retry, and recovery under load

```text
HTTP instance saturated
    -> Cloud Run queues/starts instances within configured limits
    -> shed or rate-limit before unbounded memory growth

Durable async handler fails
    -> Cloud Task retries with backoff
    -> deterministic identity/transaction prevents duplicate effects

In-process detached task is lost on instance stop
    -> acceptable only for explicitly best-effort work
    -> move required work to Cloud Tasks before load makes loss material

Provider rate limit/outage
    -> current voice path advances through bounded LiveKit fallback adapters
    -> adapter-local health suppresses failed legs and probes for recovery
    -> global admission, quota budget, and retry-storm control are not yet implemented

Firestore hot document
    -> backoff alone does not fix contention
    -> shard/aggregate/event-log redesign after measurement

Regional/provider incident
    -> degrade optional features first
    -> preserve auth, committed user actions, and durable state
```

## Obvious walkthrough: API traffic doubles

1. Cloud Monitoring shows request rate rising while p95 latency and error rate remain inside budget.
2. Cloud Run adds instances up to its configured maximum.
3. Firestore and provider metrics confirm downstream headroom.
4. No architecture change is required merely because instance count increased.

## Non-obvious walkthrough: passive Aura extraction becomes unreliable

1. Chat latency is healthy, but instance restarts cause detached per-turn extraction tasks to disappear.
2. Product metrics show memory updates missing, not request failures.
3. Because this work has become required rather than best-effort, the handler enqueues a deterministic Cloud Task after accepting the chat turn.
4. The task retries independently and uses idempotent profile merging.
5. Queue depth and extraction age become the new operational signals; the user-facing chat path stays fast.

## Evidence-gated evolution

| Measured symptom | Candidate change |
|---|---|
| Repeated cacheable reads own a material share of p95 or cost | Bounded Redis cache with explicit consistency and TTL |
| Single-region latency or availability misses its SLO | Multi-region Cloud Run and data strategy |
| Async loss or request delay is material | Cloud Tasks with idempotent handlers |
| Provider cost dominates at steady high utilization | Evaluate committed capacity or self-hosting |
| Trace volume is unaffordable | Tail sampling that keeps errors and slow traces while sampling healthy traces |

The table above is a decision aid, not another system diagram. Every change needs a baseline, trigger threshold, rollback plan, consistency analysis, and measured post-change result.

## Current code anchors

- `backend/src/main.py`
- `backend/deploy.sh`
- `backend/Dockerfile.api`
- `backend/Dockerfile`
- `backend/src/handlers/scheduler.py`
- `backend/src/handlers/signal_content_ingest.py`
- `backend/src/handlers/signal_tick.py`
- `backend/src/agent/voice_agent.py`
- `backend/src/services/notifications/orchestrator.py`
- `ops/`

## Load-test order

1. Define latency, error, durability, and cost budgets for the user flow.
2. Test HTTP/SSE and voice planes separately before a combined soak.
3. Test the exact configured voice chains, including same-provider STT correlation,
   sequential LLM attempt deadlines, partial-output fallback, and total turn latency.
4. Record provider fallback, live quota headroom, and rate-limit behavior, not only
   average throughput.
5. Inject duplicate Cloud Tasks and confirm idempotency.
6. Restart instances during detached and durable work to verify the documented loss boundary.
7. Re-run after every concurrency, region, queue, cache, model, or provider-chain change.
