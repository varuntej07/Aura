# Session follow-up architecture

A conversation has no end event. This subsystem infers one, decides whether the conversation was worth returning to, waits about an hour, re-checks that the answer is still true, and only then pushes. It is the only proactive producer whose trigger is the user's own last conversation.

## Component and data flow

```text
chat POST /chat ------+                +---- voice worker (LiveKit)
POST /chat/session-   |                |
     background       |                |
                      v                v
              +--------------------------------+
              | SessionLifecycleService        |
              | the ONLY writer of `finalized` |
              +---------------+----------------+
                              |
        users_aura/{uid}/sessions/{sid}      (mutable root)
        users_aura/{uid}/sessions/{sid}/turns (immutable)
                              |
              +---------------v----------------+
              | scheduler tick, every 60s      |
              | sweep_idle_sessions()          |
              |   chat idle .......... 30 min  |
              |   voice idle .......... 5 min  |
              |   voice disconnect .... 90 s   |
              |   chat backgrounded ... 2 min  |
              +---------------+----------------+
                              v
                     finalize_session()   one-shot, transactional
                              v
              +--------------------------------+
              | evaluator                      |
              | cluster -> drop -> score       |
              +---------------+----------------+
                              v
              candidate_machine.install_candidate
              state=scheduled, fire_at=+55..75 min
              expires_at=+6 h
                              v
              +--------------------------------+
              | scheduler tick, every 60s      |
              | run_due_followups()            |
              |   -> revalidate every gate     |
              +---------------+----------------+
                              v
              orchestrator.submit(PROACTIVE, priority 75)
                              v
                    the shared notification funnel
```

Session end is inferred from two independent signals and neither one finalizes on its own. Inactivity is authoritative; the client's background report only arms a shorter grace clock, because a mobile pause is not a departure. Any user turn clears the clock and returns the session to `active`.

Cross-surface sessions never merge: a voice session and a chat session are separate session ids by contract (`cross_surface_session_ids_separate`).

## What earns a notification

Hard drops run before scoring, so a disqualified topic is never rescued by a high score:

```text
inferred_sensitive ----------> health, money, relationships, grief
reminder_created_in_session -> a delivery promise already exists
terminal_status -------------> the underlying graph node is completed or abandoned
lineage_loop ----------------> this session began from a follow-up tap
cold_start ------------------> under 3 prior sessions, explicit intent required
missing_value_payload -------> nothing concrete to say
below_threshold -------------> weighted score under 0.45
```

The surviving topic must also carry one of six value payloads: `unresolved_action`, `deadline`, `next_step`, `new_information`, `cross_memory_connection`, `prepared_artifact`. The score is a deterministic weighted sum, not an LLM call; the model is used only to frame copy at fire time.

## Failure, retry, and recovery

```text
Client background hint lost ---> falls back to the 30 min idle sweep
User returns inside grace -----> note_user_turn clears the grace, session lives
Turn arrives after finalize ---> turn stored, session NOT resurrected
Duplicate finalize ------------> transactional one-shot returns False
Same topic live at fire time --> canceled, never deferred
Other topic live at fire time -> deferred 15 min
Quiet hours -------------------> deferred 30 min until 6 h max age expires it
Reservation lost --------------> deferred past the collision window
Orchestrator holds or drops ---> deferred one retry delay, intent preserved
Cloud Tasks retry after send --> fire-epoch guard rejects; delivered is terminal
Idle sweep index missing ------> fails open, logs "missing index?", finalizes none
```

## Obvious walkthrough: an unresolved task

1. Five chat turns drafting a message the user says they will send Monday.
2. Thirty idle minutes pass; the sweep finalizes the session.
3. The evaluator clusters one topic, finds `unresolved_action` plus `deadline`, scores well above threshold, and schedules a candidate roughly an hour out.
4. The drain revalidates: no live session, consent intact, not sensitive, inside waking hours.
5. One push goes through the shared funnel as a proactive proposal at priority 75.

## Non-obvious walkthrough: the conversation that must never notify

1. Eleven turns, high depth, strong entity overlap. The score is among the highest the evaluator can produce.
2. `inferred_sensitive` is set on the turns.
3. The topic is dropped before scoring is even consulted, and no candidate exists.

Depth is not permission. The same suppression is re-checked at fire time against the graph nodes, so a topic that only later becomes sensitive is still caught.

## Non-obvious walkthrough: the user comes back first

1. A candidate is scheduled for 8:47 pm.
2. At 8:30 pm the user opens the app and talks about that exact topic.
3. At fire time `same_topic_live` matches the live session's `active_topic_id` and the candidate is canceled outright rather than deferred. Deferring would only reschedule an interruption.
4. Had they been talking about something else, it would defer 15 minutes instead, since the intent is still good but the moment is not.

## Code anchors

- `backend/src/services/session_followup/lifecycle.py`
- `backend/src/services/session_followup/evaluator.py`
- `backend/src/services/session_followup/clustering.py`
- `backend/src/services/session_followup/revalidator.py`
- `backend/src/services/session_followup/fields.py`
- `backend/src/services/notifications/candidate_machine.py`
- `backend/src/handlers/scheduler.py`
- `backend/src/handlers/chat.py`
- `lib/presentation/screens/chat/embedded_chat_panel.dart`
