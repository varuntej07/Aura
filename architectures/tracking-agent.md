# Tracking agent architecture

Tracked topics are researched once per shared topic, reconciled into stable fixtures, and scheduled as a small set of meaningful moments. The current design does not create a recurring poll grid.

## Component and data flow

```text
+----------------------+    track_topic tool    +----------------------+
| text or voice user   | ---------------------> | topic agent          |
+----------------------+                        | research + structure |
                                                +----------+-----------+
                                                           |
                                                           v
                                                +----------------------+
                                                | tracked topic        |
                                                | shared subscribers   |
                                                +----------+-----------+
                                                           |
                                          research/reconcile fixtures
                                                           v
                                                +----------------------+
                                                | fixture matcher      |
                                                | stable slot identity |
                                                +----------+-----------+
                                                           |
                                                  create/update moments
                                                           v
                                                +----------------------+
                                                | flat due checkpoints |
                                                | pre/live/result      |
                                                +----------+-----------+
                                                           | every-minute due scan
                                                           v
                                                +----------------------+
                                                | fetch live evidence  |
                                                | fact transition gate |
                                                +----------+-----------+
                                                           |
                                                meaningful change only
                                                           v
                                                notification proposal
```

One `TrackedTopic` is shared across subscribers, while each `Tracker` holds a user's subscription. Tracking is request-driven, so fresh-user versus returning-user memory does not change the scheduling architecture.

## Failure, retry, and recovery

```text
Research fails --------------------> setup reports retryable failure; no fake schedule
Reconcile rewords a fixture -------> echoed ID, then time/token matching keeps identity
Weak/empty reconcile result -------> do not cancel the existing future schedule
Duplicate due scan ----------------> atomic checkpoint claim prevents double processing
Live fetch fails ------------------> checkpoint records failure; later reconcile/moment retries
No verified fact transition -------> suppress notification, preserve last fact state
Notification loses arbitration ----> proposal can be held within freshness policy
Topic ends/no subscribers ---------> stop or expire future work
```

## Obvious walkthrough: follow a tournament match

1. The user asks Buddy to keep them posted.
2. The topic agent researches the tournament and stores shared fixtures.
3. Each fixture gets pre, live, and result moments rather than dozens of polling documents.
4. A due moment fetches evidence, verifies a new fact, and submits a tracking proposal.
5. All subscribed users can reuse the shared research result.

## Non-obvious walkthrough: fixture name changes

1. Initial research stores `Quarterfinal 3` with an ID based on its UTC start slot.
2. Reconcile later calls it `Spain vs Belgium`.
3. The matcher first accepts an echoed fixture ID, then falls back to start-time and label rules.
4. The existing fixture is updated in place, including its fact history.
5. No parallel moment series is created, avoiding duplicate pushes.

## Code anchors

- `backend/src/services/tracking/topic_agent.py`
- `backend/src/services/tracking/fixture_matcher.py`
- `backend/src/services/tracking/moments.py`
- `backend/src/services/tracking/fact_gate.py`
- `backend/src/services/tracking/tracking_engine.py`
- `backend/src/services/tracking/tracking_store.py`
- `backend/src/handlers/scheduler.py`

See also [../backend/docs/tracker_research_decomposition.md](../backend/docs/tracker_research_decomposition.md).
