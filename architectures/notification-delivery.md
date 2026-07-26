# Notification delivery architecture

All notification intent is normalized into `NotificationProposal`. Committed proposals deliver inline; proactive proposals enter a queue and compete under one policy funnel.

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
                        | kind, priority, TTL, key|
                        +------------+------------+
                                     |
                       +-------------+-------------+
                       |                           |
                       v                           v
            +---------------------+     +-----------------------+
            | COMMITTED inline    |     | PROACTIVE queue       |
            | expected user action|     | scheduler drains/min  |
            +----------+----------+     +-----------+-----------+
                       |                            |
                       |                 +----------v-----------+
                       |                 | stale/dedup/presence |
                       |                 | quiet/budget/timing  |
                       |                 | arbitration          |
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
                     | token delivery   |                      | owner-scoped poll|
                     +--------+---------+                      +--------+---------+
                              |                                         |
                              +-------------------+---------------------+
                                                  v
                                      +-------------------------+
                                      | one logical ledger row  |
                                      | per-channel lifecycle   |
                                      +-------------------------+
```

The orchestrator owns delivery policy, so producers do not independently implement quiet hours, caps, channel selection, or cross-feature priority. Automatic channel selection is resolved at delivery time from the authenticated user's current desktop capability and category preferences. Explicit meeting desktop delivery and mobile-only device-link security alerts retain their source contracts.

Desktop capability is stored at `users/{uid}/notification_preferences/desktop`. Aura-Desktop refreshes it after owner binding and every five minutes while signed in. A missing or failed preference read fails closed to mobile-only. The desktop outbox remains owner-scoped under the same Firebase UID, and account IDs are never guessed or merged.

Unsupported mobile routing types are mapped to the versioned desktop `generic` contract with the allowlisted `open_notifications` action. Sensitive personal sources are marked sensitive so operating-system toast rendering can use privacy-safe copy while the authenticated inbox retains the full message.

## Failure, retry, and recovery

```text
Timezone alias -----------------> canonical IANA name before local-day decisions
Timezone unavailable -----------> proactive batch held as timezone_unresolved
Producer repeats proposal ------> dedup key prevents duplicate logical delivery
Proactive item is too early ----> held for a later minute drain
Item exceeds freshness TTL -----> dropped as stale
Desktop not registered ---------> mobile-only delivery
Desktop preference read fails --> mobile-only delivery, warning emitted
Outbox row already exists ------> accepted as an idempotent desktop delivery
Desktop received/seen/acted ----> same ledger row advances per-channel state
Drain crashes ------------------> queued proposals remain durable for next drain
Committed delivery fails -------> caller receives and records the inline failure
```

## Obvious walkthrough: reminder fires

1. The reminder scheduler atomically claims a due reminder.
2. It submits a committed proposal because the user explicitly requested delivery.
3. The orchestrator resolves the user's currently eligible surfaces and delivers inline.
4. The reminder is marked fired only through the scheduler's claim/update path.

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
- `backend/src/services/timezone_utils.py`
- `backend/src/handlers/scheduler.py`
