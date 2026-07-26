# Icebreaker architecture

Icebreakers are reactive proposals. A scheduler heartbeat is only one event source; the reactive orchestrator decides whether the icebreaker agent should act.

## Component and data flow

```text
+--------------------------- event sources ----------------------------+
| hourly tick | user behavior | session state | content developments   |
+-------------------------------+--------------------------------------+
                                |
                                v
                    +-------------------------+
                    | durable reactive outbox |
                    +------------+------------+
                                 | every-minute relay
                                 v
                    +-------------------------+
                    | user orchestrator task  |
                    | coalesce events         |
                    +------------+------------+
                                 |
                                 v
                    +-------------------------+
                    | icebreaker agent gates  |
                    | cooldown, context, fit  |
                    +------------+------------+
                                 |
                      eligible proposal only
                                 v
                    +-------------------------+
                    | notification funnel     |
                    | arbitrate and deliver   |
                    +------------+------------+
                                 v
                              Flutter
```

Returning users may supply Aura, recent sessions, or interests to improve the prompt. A fresh user generally lacks enough evidence, so the agent can abstain instead of inventing familiarity.

## Failure, retry, and recovery

```text
Event write succeeds, task enqueue fails -> outbox remains unconsumed
                                         -> next minute relay retries
Duplicate events ------------------------> coalesced into one user task
Live orchestration lease exists ---------> task retries after current worker
Context read fails ----------------------> agent uses reduced context or abstains
Draft/model fails -----------------------> no proposal; future event may try again
Proposal loses arbitration -------------> held/dropped by notification policy
Push fails ------------------------------> delivery state records failure
```

## Obvious walkthrough: timely conversation starter

1. The scheduler emits a tick for an active user.
2. The outbox relay enqueues one orchestrator task.
3. The icebreaker agent finds a relevant interest and clears its cooldown gates.
4. It submits a proactive proposal.
5. The notification orchestrator sends it if it wins policy and arbitration.

## Non-obvious walkthrough: repeated activity burst

1. Several client events land while an orchestrator task is already running.
2. The events remain durable and are coalesced rather than spawning parallel agent runs.
3. A follow-up task sees the combined state.
4. If the user is now active, presence policy suppresses the push. No notification is sent merely because events existed.

## Code anchors

- `backend/src/services/reactive/event_bus.py`
- `backend/src/services/reactive/orchestrator.py`
- `backend/src/services/reactive/agents/icebreaker.py`
- `backend/src/handlers/scheduler.py`
- `backend/src/services/notifications/orchestrator.py`
