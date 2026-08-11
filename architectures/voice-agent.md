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

Desktop finalized speech -> narrow capture grammar -> retained session frame
                         -> JPEG upload -> Firestore screen-save item -> confirmation
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
Capture phrase has no frame ----> ask for a retry; never create a text-only save
Capture upload/write fails -----> report failure; clean up an uploaded orphan
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

## Non-obvious walkthrough: deterministic screen capture

1. The desktop sends screen frames to the worker's session-only memory store.
2. A frame may be consumed for model context, but its bytes remain retained until a newer frame arrives or the session closes.
3. Only the authenticated user's finalized transcript is checked against the narrow capture grammar. Interim STT, OCR, model output, quoted phrases, negations, and capability questions cannot authorize a save.
4. A command-only capture bypasses the LLM. `save_screen_item` is absent from the model's prompt and tool catalog.
5. The worker uploads the retained JPEG, then writes the Firestore item with a stable retry-safe id. It speaks success only after both operations succeed.
6. The recorder stores the deterministic action receipt through the same durable receipt path used by write tools.

## Code anchors

- `backend/src/agent/voice_agent.py`
- `backend/src/agent/buddy_agent.py`
- `backend/src/agent/voice/context.py`
- `backend/src/agent/voice/pipelines.py`
- `backend/src/agent/voice/action_policy.py`
- `backend/src/agent/voice/screen_capture_command.py`
- `backend/src/agent/voice/screen_frames.py`
- `backend/src/agent/voice/screen_saves.py`
- `backend/src/agent/voice/tool_skills.py`
- `backend/src/agent/voice/recorder.py`

See also [../backend/docs/voice_action_orchestration.md](../backend/docs/voice_action_orchestration.md)
and [guide-mode.md](guide-mode.md) for armed screen guidance on desktop.

## Prompt engineering rules

Both system prompts (`BUDDY_CHAT_SYSTEM_PROMPT` in `settings.py`, `VOICE_PROMPT`
in `agent/voice_prompt.py`) follow Anthropic and OpenAI house rules: XML-tagged
sections, motivation stated inline, affirmative framing, few-shot `<example>`
blocks, and for long prompts the few hard rules restated at the very end.

**Signal-to-noise beats word count.** There is no magic length threshold.
Adherence tracks structure and placement, not raw size. Attention is highest at
the START and END of context and decays in the middle. A tight 500-token prompt
out-follows a rambling 5k one. The real failure mode is competing, buried, or
duplicated instructions, fixed by structuring and de-duping, never by cutting
words that carry signal.

**Teach a CATEGORY plus a test plus diverse examples, never a fixed enumerated
list.** A fixed list overfits: writing "live scores" without "fixtures and
schedules" let Buddy fabricate a World Cup fixture list.

### The grounding decision

Stated in both prompts. Before asserting any fact, ask: could this have changed
since training, or does it need a lookup? If yes or unsure, `web_surf` FIRST and
answer only from the result. If no (a settled fact, the user's own data, an
opinion) answer directly. Never state a specific live detail you did not fetch.

The non-obvious part: "how many countries are in the EU now" and "is that cafe
still open" are changeable and need a lookup. "Capital of France" is settled,
but "mayor of Paris" is not.

### Cross-surface action capability

Buddy's action tools work identically on every surface. Chat offers all of
`tools.py`, voice and desktop expose the same set via `/mcp`, and
`voice/capabilities.py` allows them on `ALL_SURFACES`. So a "Buddy can't create
events" report is almost never a missing tool. There are two real causes, both
prompt-level:

1. An integration is a per-USER OAuth link, not a per-platform capability, so a
   calendar or email write returns `{"configured": False}` when the user has not
   linked it in Connectors. That is a soft-fail which
   `tool_output_succeeded()` counts as success.
2. The prompt never told Buddy it can CREATE (only read), nor how to handle a
   not-connected result, so the base model degrades to "here's how to do it
   yourself" plus a flat "I couldn't."

The fix is prompt-only, in both prompts, stated as a category plus a test plus
examples and never as a keyword list: Buddy uses the tool to DO the action and
never hands over manual steps for something a tool covers. A not-connected
result means telling the user warmly that it isn't linked, pointing at
Settings > Connectors, and offering to do it once linked. Never a bare refusal.

### Free-tier "1 minute left" warning

The LLM must NOT track time. The server and client own the countdown. At T-60s
inject a one-shot instruction via `generate_reply` so the model weaves it in at
the next turn boundary in Buddy's own voice (the same mechanism
`voice/recorder.py` uses for the away-nudge), fired ONCE behind a guard flag. At
T-0 queue ONE graceful wind-down line, then end the session. Never hard-cut to
silence, and never `say()` a canned line over the user mid-sentence.
