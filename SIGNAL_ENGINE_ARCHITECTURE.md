# Signal Engine architecture

This is the repository-level specification for Aura's shared content ingest, per-user ranking, and behavior feedback loop. It reflects the current generation-based implementation.

## Block-level architecture and data flow

```text
+-------------------- shared content plane --------------------+
| Cloud Scheduler: every 4 hours                              |
+-----------------------------+-------------------------------+
                              v
                 +--------------------------+
                 | signal_content_ingest    |
                 | fetch, normalize, store  |
                 +------------+-------------+
                              v
                 +--------------------------+
                 | content + generation     |
                 | generation = 4h UTC slot |
                 +------------+-------------+
                              |
                    deterministic Cloud Task
                              v
                 +--------------------------+
                 | signal_tick              |
                 | atomic generation claim  |
                 +------------+-------------+
                              v
                 +--------------------------+
                 | candidate scoring        |
                 | quality, freshness,      |
                 | affinity, diversity      |
                 +------------+-------------+
                              v
                 +--------------------------+
                 | per-user candidates      |
                 +------+-------------------+
                        |
            +-----------+------------------+
            v                              v
  +--------------------+         +-----------------------+
  | briefing selectors |         | news notification     |
  | and screen content |         | proposal              |
  +--------------------+         +-----------+-----------+
                                            v
                                  notification orchestrator

+-------------------- feedback plane --------------------------+
| Flutter events -> POST /events -> event weights/affinity     |
|                                -> later scoring generations   |
+--------------------------------------------------------------+
```

### Main records

| Record | Purpose |
|---|---|
| Shared content | Normalized content fetched once for all users |
| Generation | Durable state for one four-hour ingest/scoring cycle |
| User affinity | Learned preference state from client behavior |
| User candidates | Ranked content consumed by briefing and news paths |

The generation state is `pending`, `running`, `complete`, or `failed`. A running claim carries a 15-minute lease. The deterministic generation and task name make scheduler and Cloud Task retries converge on the same work.

### Fresh and returning users

Fresh users have little affinity, so shared quality, freshness, and defaults dominate. Returning users add event history and learned affinity. Both use the same shared content generation and notification policy.

Aura memory is not the durable control plane for signal scoring. Feature consumers may combine candidates with their own targeting, but failure to load personal context must not corrupt the shared generation.

## Failure, retry, and recovery

```text
Content fetch/normalize fails
    -> ingest returns 5xx
    -> Cloud Scheduler retries the same four-hour generation

Ingest commits but task enqueue fails
    -> ingest returns 5xx
    -> retry uses the deterministic Cloud Task name

Scoring task arrives
    +-> generation complete: return 200, no-op
    +-> live running lease: return 409, task retries later
    +-> pending/failed/expired lease: claim and run

Worker dies after claim
    -> lease expires after 15 minutes
    -> retry reclaims generation

Scoring raises
    -> generation becomes failed
    -> later retry is allowed
```

## Obvious walkthrough: one normal generation

1. At a four-hour UTC boundary, Cloud Scheduler calls the ingest handler.
2. The handler fetches and normalizes shared content, then records the generation.
3. It synchronously enqueues the generation's named scoring task before returning.
4. The task claims the generation, scores eligible content for users, writes candidates, and marks the generation complete.
5. Briefing and news producers consume candidates through their own delivery rules.

## Non-obvious walkthrough: zero new content

1. Ingest runs successfully but source deduplication produces zero new content writes.
2. The handler still records and enqueues the current generation.
3. Scoring can account for existing content, changed user affinity, expiry, or delivery state.
4. The generation reaches `complete`, preventing repeated work from duplicate tasks.

## Ownership and code anchors

- HTTP scheduling boundary: `backend/src/handlers/signal_content_ingest.py`
- Durable scoring boundary: `backend/src/handlers/signal_tick.py`
- Generation state: `backend/src/services/signal_engine/generation_store.py`
- Signal services: `backend/src/services/signal_engine/`
- Client feedback ingest: `backend/src/handlers/signal_events.py`
- Downstream notification policy: `backend/src/services/notifications/orchestrator.py`

For the shorter navigation view, see [`architectures/signal-engine.md`](architectures/signal-engine.md). For backend operations, see [`backend/docs/signal_engine.md`](backend/docs/signal_engine.md).
