# Voice agent architecture reference

This reference describes the current LiveKit worker and its backend boundaries. The shorter navigation version is [`../architectures/voice-agent.md`](../architectures/voice-agent.md).

## Block-level architecture and data flow

```text
+---------------- Flutter ----------------+
| token request | WebRTC | captions/errors|
+--------------------+---------------------+
                     |
        +------------+----------------------+
        |                                   |
        v                                   v
+-------------------+             +-----------------------+
| Cloud Run API     |             | LiveKit Cloud         |
| /voice/token, MCP |             | room + worker dispatch|
+---------+---------+             +-----------+-----------+
          ^                                   |
          | Firebase-authenticated tools     v
          |                         +-----------------------+
          +-------------------------| Aura voice worker    |
                                    | session orchestration|
                                    +-----------+-----------+
                                                |
                           +--------------------+-------------------+
                           | context gather: profile, memory, Aura, |
                           | last session, archive, tier/time       |
                           +--------------------+-------------------+
                                                |
                      audio -> STT -> transcript -> LLM -> text -> TTS -> audio
                                  fallback          |       fallback
                                                    v
                                            action/tool policy
                                                    |
                                         local tools or Cloud Run MCP
                                                    |
                                                    v
                                           recorder + post-session
                                           persistence/summaries
```

### Runtime responsibilities

| Component | Responsibility |
|---|---|
| Flutter voice service | Token, room lifecycle, playback, captions, client watchdogs |
| Cloud Run API | Authenticate token requests and execute remote tools |
| LiveKit | Room transport and managed worker dispatch |
| Session context | Parallel, bounded personal context gathering with source defaults |
| Pipeline builders | STT, LLM, and TTS fallback adapters |
| BuddyAgent | Prompt, turn hooks, screen context, tools, and action policy |
| Recorder | Conversation/tool/usage lifecycle events and session completion |
| Post-session services | Persist session data, summarize, and update Aura when allowed |

Fresh users receive explicit empty-memory and first-conversation prompt values. Returning users can receive profile, memory, last-session, archive, and Aura summaries. Every source independently degrades to an empty value when unavailable.

## Failure, retry, and recovery

```text
Token request/auth fails ---------> client cannot join; show controlled connection error
One context read fails -----------> use that source's default; continue startup
Context fan-out exceeds ceiling --> cancel/ignore late reads; start with defaults
STT/LLM/TTS attempt fails --------> adapter advances to fallback
All attempts fail ---------------> publish classified session.error to Flutter
Remote tool times out/fails ------> structured failure; do not speak false success
Unsafe success wording ----------> regenerate once, then neutral fail-closed speech
Long dynamic context ------------> background compaction applied at later boundary
Post-session task fails ----------> isolate/log failure; completed live session remains valid
```

## Obvious walkthrough: normal conversation turn

1. Flutter obtains a token and joins the LiveKit room.
2. The worker validates room identity, gathers context, and starts the agent session.
3. STT finalizes the user's speech.
4. The LLM produces text, and TTS streams audio back through the room.
5. The recorder observes messages, metrics, and usage for session closure.

## Non-obvious walkthrough: memory is unavailable and TTS falls back

1. Firestore memory lookup times out during the bounded context fan-out.
2. The worker substitutes an empty memory summary and still greets the user.
3. The response LLM succeeds, but the primary TTS provider fails.
4. A fallback TTS adapter receives speech with provider-specific markup stripped.
5. Audio continues with a consistent caption/transcript stream; telemetry records the fallback without making missing memory a session error.

## Code anchors

- `lib/data/services/voice_session_service.dart`
- `backend/src/agent/voice_agent.py`
- `backend/src/agent/buddy_agent.py`
- `backend/src/agent/voice/context.py`
- `backend/src/agent/voice/pipelines.py`
- `backend/src/agent/voice/errors.py`
- `backend/src/agent/voice/recorder.py`
- `backend/src/services/voice_session_summarizer.py`
