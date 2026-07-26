# Voice agent architecture

The LiveKit worker runs a cascading speech pipeline, gathers personal context before the greeting, and gives the existing Buddy model stable native tools constrained by structural runtime policy.

## Component and data flow

```text
+-------------------+       WebRTC       +----------------------+
| Flutter voice UI  | <----------------> | LiveKit room         |
+-------------------+                    +----------+-----------+
                                                    |
                                                    v
                                         +----------------------+
                                         | Aura voice worker    |
                                         | session context      |
                                         +----------+-----------+
                                                    |
                      +-----------------------------+--------------------------+
                      | profile + memory + Aura + last session + archive       |
                      +-----------------------------+--------------------------+
                                                    |
                                                    v
               audio -> +-----------+ -> text -> +-----------+ -> text -> +-----------+
                        | STT       |            | LLM       |            | TTS       |
                        | fallback  |            | fallback  |            | fallback  |
                        +-----------+            +-----+-----+            +-----------+
                                                       |
                                          native tool call + structural gate
                                                       v
                                            +----------------------+
                                            | MCP/backend services |
                                            | read/write actions   |
                                            +----------------------+

Session events -> recorder -> meeting/session persistence and summaries
```

The context gather has a hard timeout and per-source fallbacks. A fresh user gets explicit first-conversation defaults; missing memory never delays the greeting indefinitely.

## Failure, retry, and recovery

```text
Context source fails/timeouts ---> substitute empty value; start session
STT primary fails --------------> Deepgram fallback model
LLM adapter fails --------------> next configured model adapter
TTS primary fails --------------> fallback TTS with speech markup stripped
All pipeline adapters fail -----> publish friendly session.error to Flutter
Write lacks required field -----> tool schema prevents execution; Buddy clarifies
Multiple side effects emitted --> structural gate keeps only the first eligible call
Long context grows -------------> summarize in background; apply at later user boundary
Recorder/post-session fails ----> live conversation can still close; log recovery state
```

## Obvious walkthrough: spoken question

1. Flutter joins a LiveKit room and sends microphone audio.
2. The worker has already gathered available profile, memory, Aura, and session summaries.
3. STT produces text, the LLM generates a response, and TTS returns audio.
4. Captions and recorder events follow the same sanitized response stream.

## Non-obvious walkthrough: natural reminder continuation

1. The user requests a reminder without enough timing detail.
2. The existing Buddy model sees the tool schema and asks one natural clarification.
3. The next finalized transcript and recent raw dialogue return to the same model with the same stable tools.
4. Buddy interprets the continuation and either asks again or emits a complete native tool call.
5. The structural gate validates the call and allows at most one side effect. The tool result then returns to Buddy for an honest response.

## Code anchors

- `backend/src/agent/voice_agent.py`
- `backend/src/agent/buddy_agent.py`
- `backend/src/agent/voice/context.py`
- `backend/src/agent/voice/pipelines.py`
- `backend/src/agent/voice/action_policy.py`
- `backend/src/agent/voice/tool_skills.py`
- `backend/src/agent/voice/recorder.py`

See also [../backend/docs/voice_action_orchestration.md](../backend/docs/voice_action_orchestration.md).
