# Daily briefing architecture

Briefings combine shared world candidates with per-user targeting, then persist one ready document for the user's local day.

## Component and data flow

```text
                 +----------------------+
                 | scheduler or user    |
                 | POST /briefing/*     |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | briefing engine      |
                 +----------+-----------+
                            |
          +-----------------+-------------------+
          |                                     |
          v                                     v
+----------------------+              +----------------------+
| shared candidates    |              | user targeting       |
| news/world content   |              | timezone, interests, |
| signal outputs       |              | Aura when available  |
+----------+-----------+              +----------+-----------+
           +-----------------+--------------------+
                             v
                  +----------------------+
                  | selector + agent     |
                  | rank and narrate     |
                  +----------+-----------+
                             v
                  +----------------------+
                  | Firestore briefing  |
                  | user + local date    |
                  +----------+-----------+
                             |
                 +-----------+-----------+
                 v                       v
        +----------------+       +------------------+
        | GET /today     |       | notification    |
        | Flutter screen |       | proposal funnel |
        +----------------+       +------------------+
```

A fresh user with little or no Aura can request the world briefing path, which uses regional defaults and prevents an empty screen. A returning user's interests improve selection but do not change storage or delivery.

## Failure, retry, and recovery

```text
Scheduled generation fails ----> log failure; next scheduler run can retry
On-demand generation fails ----> read latest prior ready briefing
No current or prior briefing --> return {briefing: null} with HTTP 200

Timezone read fails -----------> use UTC local date
Candidate source is sparse ----> world/regional candidates fill the set
Notification is suppressed ----> briefing remains readable in Firestore/UI
Duplicate generation ----------> user + local-date storage converges on one result
```

## Obvious walkthrough: morning briefing

1. The scheduler selects a user whose local delivery window is due.
2. Shared candidates are ranked against the user's targeting data.
3. The agent writes the narrative and source list to Firestore.
4. The notification orchestrator decides whether to send a proactive push.
5. The screen reads the same stored briefing.

## Non-obvious walkthrough: first-day cold start

1. A new user opens the briefing screen before scheduled generation has produced anything.
2. `GET /briefing/today` returns no briefing without treating that as an error.
3. The client requests the world snapshot.
4. Regional and world candidates generate a useful briefing without requiring memory or Aura history.
5. The result is persisted so a later read uses the normal path.

## Code anchors

- `backend/src/handlers/briefing.py`
- `backend/src/services/briefing/briefing_engine.py`
- `backend/src/services/briefing/candidate_selector.py`
- `backend/src/services/briefing/briefing_agent.py`
- `backend/src/services/briefing/briefing_store.py`
- `backend/src/services/briefing/world_briefing.py`
