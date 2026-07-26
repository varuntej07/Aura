# Signal Engine backend operations

This document focuses on the deployable backend boundaries, durable state transitions, and recovery behavior.

## Runtime data flow

```text
+-------------------+        OIDC HTTP        +-------------------------+
| Cloud Scheduler   | ----------------------> | /internal/signal/ingest |
| cron: 0 */4 * * * |                         +------------+------------+
+-------------------+                                      |
                                               source fetch + Firestore writes
                                                           |
                                                           v
                                               +-------------------------+
                                               | generation_store        |
                                               | deterministic 4h key    |
                                               +------------+------------+
                                                           |
                                                named Cloud Task enqueue
                                                           v
                                               +-------------------------+
                                               | /internal/signal-tick   |
                                               | claim -> score -> finish|
                                               +------------+------------+
                                                           |
                                              +------------+------------+
                                              v                         v
                                  +---------------------+   +----------------------+
                                  | candidates/state    |   | logs and telemetry   |
                                  | in Firestore        |   | generation outcome   |
                                  +---------------------+   +----------------------+

Client -> POST /events -> validate known event types -> detached affinity updates
```

### Handler contracts

| Boundary | Success | Retry signal |
|---|---|---|
| Content ingest | Content processed and task enqueued | 5xx when ingest or enqueue fails |
| Signal tick, complete generation | 200 no-op | None |
| Signal tick, live lease | 409 | Cloud Task retries |
| Signal tick, reclaimed generation | 200 after completion | Exception leaves retryable failed state |
| Client events | 202-style accepted response contract | Invalid batches are rejected before detached work |

## Failure, retry, and recovery

```text
Scheduler retry
    -> recompute same generation key
    -> repeat idempotent ingest writes
    -> create-or-find same task name

Cloud Task retry
    -> transaction reads generation
       +-> complete: no-op
       +-> running and lease valid: 409
       +-> failed or lease expired: acquire new lease
    -> score
       +-> success: mark complete
       +-> exception: mark failed and re-raise

Manual recovery
    -> call tick with the affected generation
    -> same claim rules apply, so operator action cannot bypass idempotency
```

## Obvious walkthrough: inspect a healthy run

1. Find an ingest log for the expected four-hour generation.
2. Confirm the handler recorded a deterministic Cloud Task name.
3. Confirm the tick claimed the same generation and marked it complete.
4. Inspect candidate writes and downstream producer metrics separately from the generation state.

## Non-obvious walkthrough: a task appears stuck

1. Read the generation record instead of immediately creating another task.
2. If the state is `running` and the 15-minute lease is live, allow the current task or retry backoff to proceed.
3. If the lease expired, replay the same generation through the normal tick boundary.
4. The transaction reclaims it, and any later duplicate exits after seeing `complete`.

## Code and configuration anchors

- `backend/src/handlers/signal_content_ingest.py`
- `backend/src/handlers/signal_tick.py`
- `backend/src/handlers/signal_events.py`
- `backend/src/services/signal_engine/generation_store.py`
- `backend/src/services/signal_engine/`
- Scheduler configuration for `0 */4 * * *`

The broader product data flow is in [`../../SIGNAL_ENGINE_ARCHITECTURE.md`](../../SIGNAL_ENGINE_ARCHITECTURE.md).
