# Aura

Aura is a voice-first personal AI companion. The assistant persona is **Buddy**, built to feel like a close friend who is genuinely curious about you: warm, proactive, and always learning what matters to you.

## What Aura can do

- **Talk in real-time voice or text.** Hold a natural spoken conversation with Buddy, or type, with the same persona and the same tools behind both paths.
- **Remember you.** Buddy builds a passive profile of your interests and what you care about, and carries that context across conversations so it feels like one ongoing relationship.
- **Keep you on track.** Set reminders and ask Buddy to keep you posted on any topic you care about.
- **Reach out first.** Proactive notifications, curiosity-driven follow-up questions, a daily evening briefing, and life-aware openers, all timed to be useful rather than noisy.
- **Search the live web.** When something could have changed since training, Buddy looks it up and answers from the result instead of guessing.
- **Handle your calendar and email.** Buddy works with Google Calendar and Gmail on your behalf.
- **Live everywhere.** A custom Android keyboard brings Buddy into any app on your phone, for drafting replies and quick answers without switching apps. On Windows, the Aura-Desktop companion (Tauri, separate repo) pairs to the same account.

## Architecture

```text
+-------------------- Flutter client ---------------------+
| text chat | LiveKit voice | briefing | memory | pushes |
+---------------------------+-----------------------------+
                            |
             HTTPS/SSE      |       WebRTC
                 +----------+-----------+
                 v                      v
      +----------------------+  +-----------------------+
      | FastAPI on Cloud Run |  | LiveKit Cloud         |
      | handlers + services  |  | rooms + voice worker  |
      +----+------------+----+  +-----------+-----------+
           |            ^                   |
           |            +-------------------+
           |             authenticated MCP tools
           v
      +----------------------+       +---------------------+
      | Firebase/Firestore   |       | external providers  |
      | user + durable state |       | models, STT/TTS, APIs|
      +----------+-----------+       +---------------------+
                 ^
                 |
      +----------+-----------+
      | Scheduler/Cloud Tasks|
      | durable background   |
      +----------------------+
```

Flutter uses MVVM layers under `lib/`. The backend is a FastAPI application under `backend/src/`. Voice is a separately deployed LiveKit Agents worker, so a slow voice session does not occupy the FastAPI request path. Text and voice share backend tools through the authenticated MCP boundary.

Fresh users start with neutral context. Returning users can contribute consent-allowed memory and Aura summaries to chat and voice. A context read failure degrades to an empty value instead of blocking the conversation.

## Failure, retry, and recovery

```text
Chat provider fails before output -> Anthropic/Gemini/OpenAI fallback contract
Voice STT/LLM/TTS stage fails ----> next pipeline adapter
All voice adapters fail ----------> friendly session.error to Flutter
Durable Cloud Task fails ---------> retry with idempotent claim/dedup identity
Optional context read fails ------> continue with empty context
Proactive notification conflicts -> central policy holds/drops/arbitrates
Committed user action fails ------> surface controlled failure; never claim success
```

### Obvious walkthrough: text chat

1. Flutter sends an authenticated chat turn and receives SSE events.
2. The backend builds context, runs the model/tool loop, and streams deltas.
3. Tool writes go through shared services, and Flutter finishes on the `done` event.

### Non-obvious walkthrough: returning user starts voice during a memory outage

1. Flutter obtains a LiveKit token and joins the room.
2. The worker gathers profile, memory, Aura, archive, and last-session inputs in parallel.
3. Memory times out, so only that input becomes empty.
4. The session starts normally with the remaining context. Later pipeline failures still use STT, LLM, or TTS fallbacks independently.

## Documentation

- Architecture index: [`architectures/README.md`](architectures/README.md)
- Backend operations: [`backend/README.md`](backend/README.md)
- Cross-repository contracts: [`ECOSYSTEM.md`](ECOSYSTEM.md)
- Monitoring: [`MONITORING.md`](MONITORING.md)
- Scalability: [`scalability_doc.md`](scalability_doc.md)
- Repository working rules: [`CLAUDE.md`](CLAUDE.md)
