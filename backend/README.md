# Aura Backend

FastAPI backend for Aura, deployed on Google Cloud Run, plus a separately deployed LiveKit voice worker.

## Runtime architecture

```text
+---------------- callers -----------------+
| Flutter | Aura-Desktop | Cloud Scheduler |
| Cloud Tasks | LiveKit voice worker       |
+--------------------+---------------------+
                     |
                     v
          +-------------------------+
          | src/main.py             |
          | auth + FastAPI routes   |
          +------------+------------+
                       |
            +----------+-----------+
            v                      v
  +-------------------+   +---------------------+
  | handlers          |   | /mcp tool boundary  |
  | request contracts |   | voice/text sharing  |
  +---------+---------+   +----------+----------+
            +------------------------+
                       v
          +-------------------------+
          | services                |
          | feature orchestration   |
          +-----+-----------+-------+
                |           |
                v           v
        Firestore/Auth   providers/FCM/GCS

LiveKit room -> src/agent/voice_agent.py -> STT -> LLM/tools -> TTS
```

## Module map

- `src/main.py` owns the FastAPI application and route registration.
- `src/handlers/` owns HTTP and internal-task request contracts.
- `src/services/notifications/` is the central proposal funnel for 14 sources: reminder, tracking, calendar, thread, briefing, icebreaker, news, reengage, chat reply, follow-up, device link, trial, welcome, and billing.
- `src/services/signal_engine/` ingests shared content and performs embedding-based candidate ranking without an LLM in the scoring hot path.
- `src/services/briefing/` creates scheduled personal briefings and on-demand world snapshots.
- `src/services/tracking/` researches shared topics, reconciles stable fixtures, schedules pre/live/result moments, and gates notifications on fact transitions.
- `src/services/reactive/` stores events in a durable outbox and dispatches per-user reactive agents.
- `src/services/threads/` persists curiosity threads and notification-shade replies.
- `src/services/memory/`, `user_aura_extractor.py`, and `aura_reflection.py` implement consent-gated memory and two-tier Aura updates.
- `src/services/chat_completion/` completes abandoned chat turns through durable task handoff.
- `src/services/feedback/` stores structured feedback and sends best-effort Telegram alerts.
- `src/services/keyboard/` is the Android IME backend: AI draft generation and the consent-gated vocab endpoint.
- `src/services/meetings/` owns Meeting Recording V2 claims, immutable audio ingest, durable transcription jobs, quality-gated publication, audit evidence, and exact-generation deletion.
- `src/services/analytics/` owns shared funnel and model/tool telemetry contracts.
- `src/agents/data_fetchers/` contains stateless shared-content fetchers. They do not send notifications.
- `src/agent/voice_agent.py` and `src/agent/voice/` own the LiveKit worker, pipeline fallbacks, action policy, context, and recording.

## Failure, retry, and recovery

```text
Authenticated request fails validation -> bounded 4xx; no side effects
Optional personalization read fails ----> empty context; request continues
Cloud Task handler fails ---------------> non-2xx retry + idempotent claim
Meeting task delivery is missed --------> Firestore outbox sweep redelivers it
Meeting lease cannot be claimed --------> 409 lease_busy + INFO; Cloud Tasks retries
Meeting worker loses its lease ---------> fenced commit is rejected through the same 409 path
Meeting evidence conflicts ------------> terminal split-brain evidence
Scheduler ingest fails -----------------> 5xx makes Scheduler retry
Proactive proposal cannot send ---------> durable queue holds/drops by policy
Voice pipeline provider fails ----------> fallback adapter advances
Best-effort analytics/feedback fails ---> warn and swallow; user path continues
```

### Obvious walkthrough: API request

1. `src/main.py` routes an authenticated request to a handler.
2. The handler validates the boundary and calls feature services.
3. Services read or write durable state and return a controlled response.

### Non-obvious walkthrough: duplicate signal task

1. Scheduler retry and enqueue retry produce the same four-hour generation identity.
2. The first signal task claims the generation with a lease.
3. A concurrent duplicate sees the live lease and returns 409 for retry.
4. After completion, every later duplicate receives a 200 no-op.

### Non-obvious walkthrough: Meeting Recording V2 completion

1. Aura-Desktop finalizes its capture run and submits an ordered, digest-bearing manifest.
2. The backend verifies contiguous segment documents and their persisted GCS receipts instead of trusting client counts.
3. One Firestore transaction advances the meeting to `uploaded`, creates the authoritative job and outbox records, and appends the audit event.
4. Cloud Tasks delivers the job, but a scheduler sweep can rediscover it if dispatch is lost.
5. A concurrent delivery that cannot claim the live job lease receives retryable `409 lease_busy`; the handler records structured INFO instead of an application traceback.
6. A token-and-attempt-fenced worker transcribes each segment, writes immutable artifacts, applies `meeting-quality-v1`, and publishes `ready` only when the quality report permits it.
7. Once the job is complete, a later duplicate receives `200`; source audio remains until an explicit exact-generation deletion request or configured bucket lifecycle policy removes it.

## Run locally

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker, in a separate terminal:

```powershell
cd backend
python -m src.agent.voice_agent start
```

## Arize tracing

Set `ARIZE_API_KEY` and `ARIZE_SPACE_ID` in the API and LiveKit worker secret
stores. `ARIZE_PROJECT_NAME` defaults to `aura`, and
`ARIZE_COLLECTOR_ENDPOINT` defaults to the Arize US endpoint. Override the
endpoint for an EU or Canada Arize space. With either credential absent,
tracing is disabled and the application continues normally.

The API traces OpenAI, Anthropic, and Google GenAI provider calls plus Aura's
chat and tool spans. The voice worker exports LiveKit's native agent spans.
Before export, an allowlist removes prompts, transcripts, tool arguments and
results, screen-derived content, exception messages, and span events. Langfuse
remains active as the existing cost and tool-call analytics sink.

## Deploy

`backend/deploy.sh` is the canonical Cloud Run deployment path. It builds the API image and reconciles the repository-owned scheduler jobs. The LiveKit worker deploys separately.

## Scheduled work

| Trigger | Cadence | Work |
|---|---|---|
| `/scheduler/tick` | every minute | reminders, calendar work, due tracking moments, notification drain, reactive outbox, and minute-gated maintenance |
| `/internal/signal-engine/content-ingest` | every four hours, `0 */4 * * *` | ingest shared content and enqueue one named scoring task |
| `/internal/signal-engine/tick` | Cloud Task per generation | claim and score one generation; also supports authenticated manual recovery |
| Meeting job outbox sweep | every five minutes at minute `% 5 == 2` through `/scheduler/tick` | dispatch authoritative Firestore meeting jobs that missed Cloud Tasks delivery |
| Meeting reconciliation | hourly at minute `27` through `/scheduler/tick` | emit metrics and alerts for stranded runs, missing jobs, expired leases, provider failures, quality failures, and integrity conflicts |

Signal scoring has no recurring cron of its own. A completed content ingest enqueues one durable task for the deterministic four-hour generation.

## Architecture references

- [`../architectures/README.md`](../architectures/README.md)
- [`docs/meeting-recording-v2.md`](docs/meeting-recording-v2.md)
- [`../SIGNAL_ENGINE_ARCHITECTURE.md`](../SIGNAL_ENGINE_ARCHITECTURE.md)
- [`docs/signal_engine.md`](docs/signal_engine.md)
- [`docs/tracker_research_decomposition.md`](docs/tracker_research_decomposition.md)
- [`docs/voice_action_orchestration.md`](docs/voice_action_orchestration.md)
