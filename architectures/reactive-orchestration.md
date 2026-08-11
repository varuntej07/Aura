# Reactive orchestration engine

The tracked, per-user, event-driven dispatcher that replaced direct per-producer
cron fan-outs for icebreakers and thread follow-up.

This changed WHO decides to send. It did not change the delivery funnel: every
agent still builds a `NotificationProposal` and calls `orchestrator.submit()`
exactly like every other producer, so freshness, dedup, and priority behaviour
in [notification-delivery.md](notification-delivery.md) is unaffected.

## Component data flow

```text
/scheduler/tick (minute == 0)
   |  one deterministic hourly Cloud Task per active user
   |  carrying a transient EVENT_TICK -- clock signals never touch Firestore
   v
+------------------- reactive/ -------------------+
| event_bus.py                                    |
|   transactional outbox, ONLY for events tied    |
|   atomically to a business-state write          |
|   inline dispatch primary, 5-min sweep backstop |
+---------------------+---------------------------+
                      v
            orchestrator.py + registry.py
                      |
      +---------------+---------------+
      v               v               v
 icebreaker.py   curiosity.py    followup.py
      |               |               |
      +---------------+---------------+
                      v
          each wrapped in agent.py envelope
       SENSE -> PLAN -> ACT -> VERIFY -> REPAIR
                      v
             orchestrator.submit()
```

## Registered agents

| Agent | File | Replaced |
|---|---|---|
| `IcebreakerOpenerAgent` | `agents/icebreaker.py` | `icebreaker_engine.run_icebreaker_tick` |
| `CuriosityThreadFollowUpAgent` | `agents/curiosity.py` | `thread_reflector.run_reflection_tick` |
| Follow-up producer | `agents/followup.py` | new; `SOURCE_FOLLOWUP` |

`icebreaker_engine.py` and `thread_reflector.py` still exist but are now thin.
Only post-send bookkeeping remains there (`on_icebreaker_delivered`,
`on_thread_delivered`, `select_thread_to_follow_up`); the SENSE/PLAN/ACT loop
itself lives in `reactive/agents/`.

## Two event sources, deliberately different

**Clock signals** are transient. The hourly per-user tick carries `EVENT_TICK`
and is never persisted, because writing 1440 rows a day per user to discover
"nothing to do" is exactly the read-discipline failure documented in
`.claude/rules/backend-firestore.md`.

**Business events** go through the transactional outbox, and only when the event
is atomically tied to a state write. Inline dispatch is the primary path; the
five-minute sweep is the durable recovery backstop, not the normal route.

## Failure, retry, and recovery

```text
Inline dispatch fails
    -> outbox row remains unclaimed
    -> 5-minute sweep re-dispatches
    -> idempotency.py prevents a double ACT

Agent raises inside ACT
    -> envelope moves to REPAIR
    -> run recorded in agent_runs.py either way

Cloud Task for a user is lost
    -> that user simply has no tick this hour
    -> next hour's task is independent, no catch-up storm

Allowlist not re-applied at a new discovery path
    -> sends escape the dark-test boundary
    -> every discovery path must call apply_proactive_allowlist()
```

## Code anchors

- `backend/src/services/reactive/orchestrator.py`, `registry.py`
- `backend/src/services/reactive/agent.py` (the envelope)
- `backend/src/services/reactive/event_bus.py`, `events.py`, `inbox.py`
- `backend/src/services/reactive/idempotency.py`, `lease.py`, `guardrails.py`
- `backend/src/services/reactive/agents/{icebreaker,curiosity,followup}.py`
- `backend/src/handlers/scheduler.py` (the `minute == 0` enqueue)
