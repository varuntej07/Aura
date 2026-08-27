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
| Alarms (the loud reminder tier) | [alarm-tier.md](alarm-tier.md) |
| Turn completion after a disconnect | [background-chat-completion.md](background-chat-completion.md) |
| Daily briefing | [daily-briefing.md](daily-briefing.md) |
| Feedback relay | [feedback-relay.md](feedback-relay.md) |
| Armed screen guidance (desktop) | [guide-mode.md](guide-mode.md) |
| Reactive icebreakers | [icebreaker.md](icebreaker.md) |
| Notification funnel | [notification-delivery.md](notification-delivery.md) |
| Event-driven agent dispatch | [reactive-orchestration.md](reactive-orchestration.md) |
| Post-session follow-ups | [session-followup.md](session-followup.md) |
| News signal generation | [signal-engine.md](signal-engine.md) |
| Curiosity threads | [thread-engine.md](thread-engine.md) |
| Tracked topics | [tracking-agent.md](tracking-agent.md) |
| User memory and Aura | [user-aura.md](user-aura.md) |
| Live voice and prompt rules | [voice-agent.md](voice-agent.md) |
| Desktop meeting recording and notes | [meeting-recording-v2.md](meeting-recording-v2.md) |
| Buddy keyboard | [buddy-keyboard.md](buddy-keyboard.md) (pointer; canonical doc is `backend/docs/BUDDY_KEYBOARD_ARCHITECTURE.md`) |
| Buddy Everywhere overview | [buddy-everywhere.md](buddy-everywhere.md) |
| Dictation training data | [dictation-training-data.md](dictation-training-data.md) |
| Research Phase 3 infrastructure | [research-phase3-infrastructure.md](research-phase3-infrastructure.md) |
| Architecture digital twin | [architecture-digital-twin.md](architecture-digital-twin.md) |

## Plans and supporting references

- [Buddy Everywhere architecture](buddy-everywhere-architecture.md)
- [Buddy Everywhere engineering plan](buddy-everywhere-engineering-plan.md)
- [Android keyboard privacy-first personalization](android-keyboard-privacy-first-personalization.md)
- [Voice session integrity remediation](voice-session-integrity-remediation.md)
- [VoiceOS competitive implementation plan](voiceos-competitive-implementation-plan.md)
- [Architecture Studio model](architecture-studio-model.md)
- [Architecture Studio decision workspace](architecture-studio-decision-workspace.md)
- [Architecture Studio simulation model](architecture-studio-simulation-model.md)
- [Architecture Studio source evidence](architecture-studio-source-evidence.md)

Deep technical references are linked from each overview. Architecture documents now live in this directory so one index covers current behavior, proposed work, and evidence.
