# Tracking research decomposition

The implemented tracking design separates semantic research from deterministic fixture identity and fact-transition delivery. It replaces label-derived poll grids with stable fixtures and a few meaningful moments.

## Decomposed data flow

```text
+------------------+      natural-language request      +------------------+
| track_topic tool | ---------------------------------> | topic_agent      |
+------------------+                                    | structured output|
                                                        +--------+---------+
                                                                 |
                                                   TopicResearch + fixtures
                                                                 v
                                                        +------------------+
                                                        | fixture_matcher  |
                                                        | echo ID first    |
                                                        | time/label backup|
                                                        +--------+---------+
                                                                 |
                                                 stable creates/updates/cancels
                                                                 v
                                                        +------------------+
                                                        | moments          |
                                                        | pre/live/result  |
                                                        +--------+---------+
                                                                 |
                                                           due checkpoint
                                                                 v
                                                        +------------------+
                                                        | topic_fetcher    |
                                                        | evidence fetch   |
                                                        +--------+---------+
                                                                 v
                                                        +------------------+
                                                        | fact_gate        |
                                                        | transition only  |
                                                        +--------+---------+
                                                                 v
                                                        tracking proposal
```

### Responsibility boundaries

| Component | Owns | Must not own |
|---|---|---|
| Topic agent | Interpreting request and researching real fixtures | Persistent fixture identity |
| Fixture matcher | Stable ID reuse, schedule refresh, cautious cancellation | Live fact claims |
| Moments | Pre, live, and result checkpoint timing | Repeated polling grids |
| Topic fetcher | Current evidence and source fallback | Notification policy |
| Fact gate | Verified state transitions and dedup | User delivery arbitration |
| Notification orchestrator | Timing, budget, priority, FCM | Topic research |

## Failure, retry, and recovery

```text
Model echoes stored fixture ID ----------> update that fixture in place
No echoed ID ----------------------------> match by time proximity and label evidence
No safe match ---------------------------> mint ID from UTC start slot
Substantial research drops far fixture --> mark cancelled
Sparse/empty research drops fixture -----> keep it; absence is not cancellation proof

Due checkpoint claimed twice -----------> atomic claim permits one worker
Evidence fetch fails --------------------> record failure and retry via later work
Evidence has no new verified fact ------> emit no proposal
Verified transition repeats ------------> development/fact key suppresses duplicate
```

## Obvious walkthrough: schedule a result update

1. Research returns a match start and expected end.
2. The matcher mints a stable start-slot fixture ID.
3. Moment generation creates pre, live, and result checkpoints.
4. At result time, evidence fetch finds a verified winner.
5. The fact gate records a transition and permits one notification proposal.

## Non-obvious walkthrough: bracket placeholder resolves

1. Research first stores `Portugal/Spain Winner vs USA/Belgium Winner`.
2. A later pass returns `Spain vs Belgium`, possibly with a shifted estimated time.
3. The agent's echoed ID wins when present. Otherwise the matcher uses widened placeholder timing rules.
4. The fixture ID and fact state remain stable while its label and times update.
5. Existing moments are reconciled instead of forking a second series.

## Code anchors

- `backend/src/services/tracking/topic_agent.py`
- `backend/src/services/tracking/fixture_matcher.py`
- `backend/src/services/tracking/moments.py`
- `backend/src/services/tracking/topic_fetcher.py`
- `backend/src/services/tracking/fact_gate.py`
- `backend/src/services/tracking/tracking_engine.py`
- `backend/src/services/tracking/tracking_store.py`
