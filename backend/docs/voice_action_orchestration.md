# Voice action orchestration

Voice uses one semantic decision-maker: Buddy's existing system prompt and native tool calling. The action policy never inspects transcript wording. It exposes a stable tool set from runtime facts and validates model-emitted calls before LiveKit executes them.

## Action data flow

```text
+----------------------+     finalized transcript     +------------------------+
| LiveKit user turn    | ---------------------------> | existing Buddy model   |
| + recent raw dialog  |                              | one system prompt      |
+----------------------+                              +-----------+------------+
                                                                  |
                                    conversation / clarification / native tool call
                                                                  |
                                                                  v
                                                     +------------------------+
                                                     | structural tool policy |
                                                     | surface + frame + turn |
                                                     | args + one side effect |
                                                     +-----------+------------+
                                                                  |
                                                                  v
                                                     +------------------------+
                                                     | backend/MCP tool       |
                                                     | structured result      |
                                                     +-----------+------------+
                                                                  |
                                                                  v
                                                     +------------------------+
                                                     | same Buddy model       |
                                                     | result-aware response  |
                                                     +------------------------+
```

Recent raw dialogue lets Buddy understand a direct clarification answer naturally. Memory and summaries can help with context, but the prompt explicitly forbids treating them or assistant text as permission for an external action.

## Failure, retry, and recovery

```text
Required argument absent --------> model asks naturally; no tool call executes
Malformed or unknown call -------> structural gate blocks it; user gets safe retry copy
Pre-final tool call -------------> writes and presentation are not exposed
Two side effects in one output --> execute the first eligible call; block later calls
Tool returns error -------------> result returns to Buddy; prompt forbids success claim
Duplicate delivered turn -------> ToolExecutor idempotency suppresses repeat side effect
Context compaction fails -------> retain original context; record telemetry
Stale background summary -------> discard rather than overwrite newer turns
```

## Obvious walkthrough: set one reminder

1. Final STT and recent raw dialogue reach the existing Buddy model with `set_reminder` available.
2. Buddy semantically chooses the native call and supplies the required schema fields.
3. Structural policy validates the registered tool, finalized turn, and arguments.
4. The tool executes and returns structured success for Buddy's result-aware response.

## Non-obvious walkthrough: indirect clarification answer

1. The user asks for a reminder without a time, and Buddy asks one natural question.
2. The user replies with an indirect fragment such as a relative period.
3. No classifier evaluates that phrase. Buddy reads the immediately preceding exchange and decides whether it is sufficient or needs another clarification.
4. A native `set_reminder` call is validated structurally and executed at most once.
5. A failed result returns to Buddy, whose prompt requires an honest failure response.

## Code anchors

- `backend/src/agent/voice/action_policy.py`
- `backend/src/agent/voice/capabilities.py`
- `backend/src/agent/voice/tool_skills.py`
- `backend/src/agent/buddy_agent.py`
- `backend/src/agent/voice/action_telemetry.py`
- `backend/src/agent/voice/context_compaction.py`
