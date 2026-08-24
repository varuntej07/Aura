# Notification delivery architecture

All notification intent is normalized into `NotificationProposal`. A producer may request a lane, but the orchestrator is the delivery authority: only allowlisted time-exact, transactional, or explicitly awaited outcomes remain committed. Generated, recurrent, and unknown sources enter the proactive queue and compete under one policy funnel.

## Component and data flow

```text
+-------------------------- proposal sources ------------------------------+
| reminder, tracking, calendar, thread, briefing, icebreaker, news,         |
| reengage, chat reply, follow-up, device link, account, meeting, graph     |
+----------------------------------+---------------------------------------+
                                   |
                                   v
                        +-------------------------+
                        | NotificationProposal    |
                        | requested kind, TTL, key|
                        +------------+------------+
                                     |
                         central lane resolution
                                     |
                       +-------------+-------------+
                       |                           |
                       v                           v
            +---------------------+     +-----------------------+
            | COMMITTED inline    |     | PROACTIVE queue       |
            | exact/awaited outcome|    | scheduler drains/min  |
            +----------+----------+     +-----------+-----------+
                       |                            |
                       |                 +----------v-----------+
                       |                 | stale/dedup/presence |
                       |                 | quiet/budget/timing  |
                       |                 | arbitration/tap value|
                       |                 +----------+-----------+
                       +----------------------------+
                                                    v
                                         +---------------------+
                                         | channel policy      |
                                         | user + behavior prefs|
                                         +----------+----------+
                                                    |
                              +---------------------+--------------------+
                              |                                          |
                              v                                          v
                     +------------------+                      +------------------+
                     | Mobile FCM       |                      | Desktop outbox   |
                     | acceptance      |                      | durable queue    |
                     +--------+---------+                      +--------+---------+
                              |                                         |
                              +-------------------+---------------------+
                                                  v
                                      +-------------------------+
                                      | one logical ledger row  |
                                      | per-channel lifecycle   |
                                      +-------------------------+
```

The orchestrator owns delivery policy, so producers do not independently grant themselves committed delivery or implement quiet hours, caps, channel selection, or cross-feature priority. Explicit reminders, alarms, security/account transitions, and user-awaited task completions may deliver without needing a tap. A tracking subscription authorizes Aura to evaluate updates; it does not commit every generated update. Briefings, daily nudges, welcome, tracking, curiosity, follow-up, re-engagement, news, memory-graph, and unknown future sources must pass the proactive funnel.

Every proactive interruption must satisfy two distinct value checks: the push must contain a material user-serving development, not merely a technically distinct fact or a question that primarily helps Aura learn; and its tap destination must add an artifact, source, action, or context beyond repeating the push. Missing/repeat-only destinations, malformed judgments, tap-gate outages, budget-store outages, and atomic-dedup outages fail closed for proactive delivery. Committed delivery retains availability-oriented behavior because silence can violate an explicit reminder or awaited-result contract.

Every orchestrated send writes `policy_version`, requested and effective lane, lane authority, local send context, and the passed interruption checks into the notification ledger's `decision` map. Pre-send holds and drops remain reason-coded in orchestrator logs. Together these answer why a send was authorized without equating transport acceptance with device receipt or human attention.

Transport acceptance, device receipt, and human engagement are distinct. FCM success means `accepted`; an outbox write means `queued`; desktop acknowledgements advance the same logical row through `received`, `seen`, and `acted`. Initial sends never infer device receipt from FCM acceptance or a durable desktop row. Deduplication, cooldowns, and budgets close on transport acceptance so a queued desktop item is not resent, while delivery analytics use only confirmed receipt and engagement transitions. Retries retain one logical ID and cumulative attempt count, with separate mobile/Desktop attempt and acceptance counts; surfaces never inflate the logical notification or budget count. Legacy `sent`/`delivered` rows remain readable.

Desktop capability is stored at `users/{uid}/notification_preferences/desktop`. Aura-Desktop refreshes it after owner binding and every five minutes while signed in. A missing or failed preference read fails closed to mobile-only. The desktop outbox remains owner-scoped under the same Firebase UID, and account IDs are never guessed or merged.

Unsupported mobile routing types are mapped to the versioned desktop `generic` contract with the allowlisted `open_notifications` action. Sensitive personal sources are marked sensitive so operating-system toast rendering can use privacy-safe copy while the authenticated inbox retains the full message.

Curiosity threads have a fail-closed semantic sensitivity gate at creation and immediately before delivery. Structured category and memory-graph signals are persisted as sensitivity provenance. The delivery recheck includes current thread context, generated question copy, and suggested replies, so a subject that changes after creation cannot bypass the shared mobile/desktop decision. User-initiated chat and replies remain available.

## Failure, retry, and recovery

```text
Timezone alias -----------------> canonical IANA name before local-day decisions
Timezone unavailable -----------> proactive batch held as timezone_unresolved
Producer repeats proposal ------> dedup key prevents duplicate logical delivery
Producer requests bypass -------> central allowlist keeps or downgrades the lane
Proactive item is too early ----> held for a later minute drain
Item exceeds freshness TTL -----> dropped as stale
Committed item exceeds validity -> dropped as stale before transport
Mobile accepts before deadline -> Android TTL and APNs expiration cap delivery
Desktop queues before deadline -> outbox expiry is capped to the same deadline
Tap has no incremental payoff --> dropped as low_tap_value
Tap/budget/dedup gate unavailable -> proactive candidate held or dropped fail-closed
Desktop not registered ---------> mobile-only delivery
Desktop preference read fails --> mobile-only delivery, warning emitted
Outbox row already exists ------> queued as idempotent transport acceptance
Desktop received/seen/acted ----> same ledger row advances per-channel state
Invalid outbound payload ------> rejected before FCM; no token is pruned
FCM unregistered/mismatch -----> only the affected token is pruned; caches invalidated
Drain crashes ------------------> queued proposals remain durable for next drain
Committed delivery fails -------> caller receives and records the inline failure
Reminder recovery -------------> retries only inside validity and attempt bounds
Reminder exhausts either bound -> terminal expired; never requeued to pending
```

## Obvious walkthrough: reminder fires

1. The scheduler derives validity from `trigger_at` and tier, terminalizing stale or malformed rows before copy generation.
2. Its atomic claim increments the durable attempt count and refuses exhausted work.
3. It submits a committed proposal because the user explicitly requested delivery, carrying the same absolute validity deadline into the central funnel.
4. The orchestrator resolves the user's currently eligible surfaces and delivers inline under one logical notification ID.
5. The reminder is marked fired on transport acceptance; scheduler telemetry reports one logical acceptance plus separate mobile-accepted and Desktop-queued counts.

## Non-obvious walkthrough: briefing competes with an icebreaker

1. Both producers enqueue proactive proposals for the same user.
2. The minute drain removes stale and duplicate entries, then checks presence, quiet hours, timing, and budget.
3. Arbitration selects at most one proposal based on priority and policy.
4. The loser is held or dropped according to its remaining freshness, preventing two near-simultaneous pushes.

## Code anchors

- `backend/src/services/notifications/proposal.py`
- `backend/src/services/notifications/orchestrator.py`
- `backend/src/services/notifications/channel_policy.py`
- `backend/src/services/notifications/desktop_preferences.py`
- `backend/src/services/notifications/delivery_router.py`
- `backend/src/services/notifications/desktop_outbox.py`
- `backend/src/services/notification_ledger.py`
- `backend/src/services/fcm_token_registry.py`
- `backend/src/services/threads/sensitivity.py`
- `backend/src/services/timezone_utils.py`
- `backend/src/handlers/scheduler.py`
