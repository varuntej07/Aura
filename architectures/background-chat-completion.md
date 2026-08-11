# Background chat completion

When a client disconnects mid-turn (app backgrounded, network drop), the backend
hands the turn off to Cloud Tasks for completion rather than dropping it.

This exists because the visible symptom of a dropped turn was a false "check
your internet" message, which violates the rule that we never blame the network
for a non-network failure.

## Component data flow

```text
client streaming a chat turn
   |
   +-- completes normally --> delete recovery checkpoint + cancel delayed task
   |
   x  disconnects mid-turn
   |
   v
+--------- services/chat_completion/ ---------+
| completion.py    finishes the turn          |
| prompt_builder.py  (extracted from chat.py) |
| tool_idempotency.py  per-turn tool map      |
| turn_store.py    persists under a stable ID |
+---------------------+-----------------------+
                      v
        reply saved as  <cmid>::reply
                      v
        "Buddy replied" push via orchestrator
                      v
        client reconnects, hydrates by stable ID
```

## Why the ID is stable

The reply is written under `<cmid>::reply`, derived from the client's own
message ID. On reconnect the client looks up that exact ID, so the reply appears
once whether or not the client ever saw the stream. No polling, and no duplicate
bubble from a racing reconnect.

## Tool idempotency

A Cloud Task can retry. A retried turn must not fire a side-effecting tool
twice, so `tool_idempotency.py` keeps a per-turn map of tools already executed
and their results, and a regenerated turn reuses the recorded result instead of
re-calling.

**`send_email` is deliberately excluded from regeneration.** A sent email cannot
be un-sent, so it is never replayed even when the rest of the turn is.

## Failure, retry, and recovery

```text
Cloud Task retries after a partial completion
    -> tool_idempotency returns recorded results
    -> no duplicate reminder, event, or tracker

Task fails permanently
    -> no reply row is written
    -> client reconnect finds nothing and the turn is simply absent
    -> better than a half-written reply presented as complete

Client reconnects while the task is still running
    -> stable ID not yet present
    -> client shows the pending state, hydrates when the row lands

Push fires but the user never returns
    -> reply is already durable; nothing is time-dependent

Foreground stream finishes normally
    -> delete the temporary `chat_turns/{cmid}` checkpoint transactionally
    -> cancel the delayed Cloud Task
    -> if cancellation races, the task sees no checkpoint and exits

Only exceptional background completions remain for hydration. Their copied prompt
and history fields are removed in the terminal write, and Firestore TTL deletes the
small reply record after two days.
```

## Code anchors

- `backend/src/services/chat_completion/completion.py`
- `backend/src/services/chat_completion/prompt_builder.py`
- `backend/src/services/chat_completion/tool_idempotency.py`
- `backend/src/services/chat_completion/turn_store.py`
- `backend/src/handlers/chat.py` (disconnect detection and handoff)

See also [chat-and-tools.md](chat-and-tools.md) for the normal connected path.
