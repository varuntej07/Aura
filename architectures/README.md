# Aura architecture atlas

These documents describe the current code, not a target design. Every architecture uses plain ASCII so it is readable in GitHub, a terminal, or any basic Markdown viewer.

## System map

```text
+---------------- Flutter client ----------------+
| chat | voice | briefing | notifications | aura |
+------------------------+------------------------+
                         |
                         v
+---------------- FastAPI / Cloud Run ----------------+
| authenticated handlers | scheduler | internal tasks |
+-----------+-------------+-------------+--------------+
            |             |             |
            v             v             v
     +------------+ +------------+ +----------------+
     | feature    | | model      | | LiveKit voice  |
     | services   | | providers  | | worker         |
     +------+-----+ +------+-----+ +--------+-------+
            |              |                |
            +--------------+----------------+
                           v
              +--------------------------+
              | Firestore | FCM | APIs   |
              +--------------------------+
```

## Architecture documents

| Area | Canonical overview |
|---|---|
| Text conversation and tools | [chat-and-tools.md](chat-and-tools.md) |
| Daily briefing | [daily-briefing.md](daily-briefing.md) |
| Feedback relay | [feedback-relay.md](feedback-relay.md) |
| Reactive icebreakers | [icebreaker.md](icebreaker.md) |
| Notification funnel | [notification-delivery.md](notification-delivery.md) |
| News signal generation | [signal-engine.md](signal-engine.md) |
| Curiosity threads | [thread-engine.md](thread-engine.md) |
| Tracked topics | [tracking-agent.md](tracking-agent.md) |
| User memory and Aura | [user-aura.md](user-aura.md) |
| Live voice | [voice-agent.md](voice-agent.md) |

Deep technical references are linked from each overview. Buddy Everywhere and keyboard-specific documents are intentionally outside this atlas.
