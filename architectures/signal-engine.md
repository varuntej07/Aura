# Signal engine architecture

The signal engine ingests shared content every four hours and schedules exactly one durable scoring task for that generation. User interaction events adjust affinity independently.

## Component and data flow

```text
+----------------------+   every 4 hours   +-------------------------+
| Cloud Scheduler      | ----------------> | signal content ingest   |
+----------------------+                   +------------+------------+
                                                    fetch + normalize
                                                       |
                                                       v
                                            +-------------------------+
                                            | shared content store    |
                                            | generation = 4h bucket  |
                                            +------------+------------+
                                                         |
                                           deterministic Cloud Task
                                                         v
                                            +-------------------------+
                                            | /internal/signal-tick   |
                                            | claim generation lease  |
                                            +------------+------------+
                                                         |
                                                         v
                                            +-------------------------+
                                            | score users/content     |
                                            | write ranked candidates |
                                            +------------+------------+
                                                         |
                         +-------------------------------+----------------+
                         v                                                v
              +----------------------+                       +----------------------+
              | briefing/news paths  |                       | notification funnel  |
              +----------------------+                       +----------------------+

Flutter interaction events -> POST /events -> affinity/event state -> later scores
```

Fresh users have little affinity history, so ranking relies more on shared quality, freshness, and defaults. Returning users add learned event-weighted affinity.

## Failure, retry, and recovery

```text
Ingest fails ---------------------> return 5xx; Cloud Scheduler retries
Ingest succeeds, zero new writes -> still enqueue scoring for the generation
Task enqueue fails --------------> return 5xx; retry uses deterministic task name
Generation already complete -----> HTTP 200 no-op
Generation has live lease -------> HTTP 409; Cloud Task retries later
Scoring fails -------------------> mark generation failed; retry can reclaim it
Worker dies with running lease ---> 15-minute lease expires; retry recovers
Duplicate interaction event -----> event ingestion remains isolated from generation claim
```

## Obvious walkthrough: normal content generation

1. The four-hour scheduler calls content ingest.
2. Sources are fetched and normalized into the shared content store.
3. Ingest records the deterministic generation and enqueues its named Cloud Task before returning.
4. The task claims the generation, scores users, writes candidates, and marks it complete.

## Non-obvious walkthrough: worker dies during scoring

1. The task marks the generation `running` with a 15-minute lease.
2. The worker stops before completion.
3. A retry during the live lease receives 409 and backs off.
4. After lease expiry, a retry reclaims the same generation and completes it.
5. A later duplicate task sees `complete` and exits without rescoring.

## Code anchors

- `backend/src/handlers/signal_content_ingest.py`
- `backend/src/handlers/signal_tick.py`
- `backend/src/services/signal_engine/generation_store.py`
- `backend/src/services/signal_engine/`
- `backend/src/handlers/signal_events.py`

See also [../SIGNAL_ENGINE_ARCHITECTURE.md](../SIGNAL_ENGINE_ARCHITECTURE.md) and [../backend/docs/signal_engine.md](../backend/docs/signal_engine.md).
