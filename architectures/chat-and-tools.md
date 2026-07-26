# Chat and tools architecture

The text chat path streams one response contract across three model providers and one shared tool executor.

## Component and data flow

```text
+------------------+     HTTPS/SSE      +----------------------+
| Flutter chat UI  | -----------------> | POST /chat handler   |
+------------------+                    +----------+-----------+
                                                   |
                        +--------------------------+-------------------+
                        | authenticate, load conversation, build prompt|
                        +--------------------------+-------------------+
                                                   |
                  +--------------------------------+------------------+
                  | optional context: profile + Aura + memory + files |
                  +--------------------------------+------------------+
                                                   |
                                                   v
                                      +-------------------------+
                                      | Anthropic tool loop     |
                                      +------------+------------+
                                                   |
                              tool call            | text/events
                    +------------------------------+-------------+
                    v                                            v
          +----------------------+                    +----------------+
          | shared ToolExecutor  |                    | SSE to Flutter |
          | reminders, memory,   |                    | delta/tool/done|
          | search, connectors   |                    +----------------+
          +----------+-----------+
                     |
                     v
          +----------------------+
          | Firestore/external   |
          | services/connectors  |
          +----------------------+
```

Fresh users supply no Aura or memory summary. The prompt uses empty first-conversation defaults and the same tool path remains available. Returning users contribute only consent-allowed context.

## Failure, retry, and recovery

```text
Context read fails --------------------> use empty context; keep turn alive

Anthropic fails before first token ----> Gemini tool loop
Gemini fails before first token -------> OpenAI tool loop
OpenAI fails --------------------------> one friendly SSE error; turn ends

Provider fails after streaming starts -> do not replay on another provider
                                      -> finish with error to avoid duplicate text/tools

Tool fails ----------------------------> return structured tool error to model
                                      -> model explains or asks for clarification
```

## Obvious walkthrough: answer a question

1. Flutter sends the signed-in user's message.
2. The handler loads conversation and available personal context.
3. The model streams text deltas.
4. Flutter renders deltas until the `done` event.

## Non-obvious walkthrough: set an underspecified reminder

1. The model calls the reminder tool without all required fields.
2. The shared executor returns the clarification sentinel instead of writing partial data.
3. The stream emits clarification UI and preserves the turn context.
4. The user's next answer completes the tool call. A provider handoff before the first token can continue the same tool loop without changing the client event contract.

## Code anchors

- `backend/src/handlers/chat.py`
- `backend/src/services/claude_client.py`
- `backend/src/services/gemini_chat_fallback.py`
- `backend/src/services/openai_chat_fallback.py`
- `backend/src/services/tool_executor.py`
