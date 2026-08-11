# Thread engine architecture

Curiosity threads turn a proactive question into a server-authoritative conversation that can continue from the notification shade or inside the app.

## Component and data flow

```text
   reminder tool ------+                    +------ session finalization
   (tool_executor)     |                    |       (session_followup)
                       v                    v
              +------------------------------------+
              | thread_writer                      |
              | worthiness judge + subject dedup   |
              | the ONLY creator of threads        |
              +------------------+-----------------+
                                 |
+----------------------+       events/tick       +----------------------+
| reactive event bus   | ----------------------> | curiosity agent      |
+----------------------+                         | FOLLOWS UP only,     |
                                 |               | never creates        |
                                 |               +----------+-----------+
                                 |                          |
                                 v                     proposal
                                                  +----------------------+
                                                  | thread store         |
                                                  | thread + messages    |
                                                  +----------+-----------+
                                                             |
                                                  +----------v-----------+
                                                  | notification funnel  |
                                                  +----------+-----------+
                                                             v
                                                  +----------------------+
                                                  | Flutter shade/app    |
                                                  +----------+-----------+
                                                             | user reply
                                                             v
                                                  +----------------------+
                                                  | POST /threads/reply  |
                                                  | persist -> respond   |
                                                  +----+------------+----+
                                                       |            |
                                                       v            v
                                                Aura capture   reply push/UI
```

When a reply contains consent-allowed user facts or interests, per-turn Aura capture runs asynchronously. A thread requires no existing memory, so a user's very first conversation can open one.

Threads have exactly two origins, both in `thread_writer`, and the curiosity agent is not one of them: it only selects among already-open threads. A reminder opens a thread 1:1 with itself. A finalized session opens up to three, one per clustered topic, judged against a stricter worthiness bar than a reminder because a conversation topic is not an explicit commitment. Both paths share the same subject dedup, so a re-mentioned subject reuses its thread rather than forking a parallel one and re-arming a spent follow-up budget.

Conversation-derived threads are gated on Aura consent, matching the curiosity agent's own gate: a follow-up enriches UserAura.

## Failure, retry, and recovery

```text
Question proposal suppressed ----> thread may exist but no push is delivered
Duplicate trigger ---------------> cooldown/thread state prevents parallel prompts
Reply references missing thread -> create tolerant fallback thread shell
User reply accepted -------------> persist user message before generating answer
Responder fails -----------------> user message remains durable for recovery
Buddy reply push fails ----------> stored reply remains available via GET messages
Aura extraction fails -----------> swallow/log; thread reply still succeeds
```

## Obvious walkthrough: reply in the shade

1. `thread_writer` stored the thread earlier (from a reminder or a finalized session); the curiosity agent selects it and submits its question as a proactive proposal.
2. The user answers from a notification action.
3. `/threads/reply` authenticates the user, appends the answer, and marks the thread engaged.
4. The responder writes Buddy's answer and sends a follow-up push.

## Non-obvious walkthrough: follow-up push is lost

1. The server persists the user's answer before response generation.
2. It generates and persists Buddy's reply.
3. FCM delivery fails, so the shade does not update.
4. When the app later opens the thread, `GET /threads/{id}/messages` reconstructs the complete server-side exchange.

## Code anchors

- `backend/src/services/threads/thread_writer.py`
- `backend/src/services/threads/thread_store.py`
- `backend/src/services/threads/thread_responder.py`
- `backend/src/services/reactive/agents/curiosity.py`
- `backend/src/handlers/threads.py`
