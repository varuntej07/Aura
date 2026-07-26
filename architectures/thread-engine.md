# Thread engine architecture

Curiosity threads turn a proactive question into a server-authoritative conversation that can continue from the notification shade or inside the app.

## Component and data flow

```text
+----------------------+       events/tick       +----------------------+
| reactive event bus   | ----------------------> | curiosity agent      |
+----------------------+                          +----------+-----------+
                                                             |
                                                  create thread + proposal
                                                             v
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

When a reply contains consent-allowed user facts or interests, per-turn Aura capture runs asynchronously. The thread itself does not require existing memory and works for fresh users.

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

1. The curiosity agent stores a thread and submits its question as a proactive proposal.
2. The user answers from a notification action.
3. `/threads/reply` authenticates the user, appends the answer, and marks the thread engaged.
4. The responder writes Buddy's answer and sends a follow-up push.

## Non-obvious walkthrough: follow-up push is lost

1. The server persists the user's answer before response generation.
2. It generates and persists Buddy's reply.
3. FCM delivery fails, so the shade does not update.
4. When the app later opens the thread, `GET /threads/{id}/messages` reconstructs the complete server-side exchange.

## Code anchors

- `backend/src/services/threads/thread_store.py`
- `backend/src/services/threads/thread_responder.py`
- `backend/src/services/reactive/agents/curiosity.py`
- `backend/src/handlers/threads.py`
