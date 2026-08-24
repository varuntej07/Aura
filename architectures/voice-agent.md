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

## Session start: wait for the human, then speak first

Two rules, both learned from one production session that ran 300993ms and captured
zero turns while its user talked into it.

**Nothing may read participant metadata before the participant exists.**
`ctx.connect()` connects the AGENT. Every launch parameter (surface, conversation_id,
bridged, output_mode, voice_request_id) lives in the USER's token metadata, and the
`_resolve_*` helpers all read `ctx.room.remote_participants`, which is empty until they
join. `_wait_for_user_participant` now gates every one of them, and a participant that
never arrives ends the run (`voice_run_participant_never_joined`) instead of holding an
empty room. The old code asserted in a comment that connect implied the participant; a
comment is not a mechanism, and the cost was every session recording
`surface: "unknown", conversation_id: ""`.

**Buddy opens the call.** `on_enter` speaks the memory-seeded opener
(`voice/greeting.py`) when it resolves inside `VOICE_GREETING_SEED_BUDGET_S`, else a
static `CASUAL_GREETINGS` line. Bridge mode is the sole exception, because the desktop's
Realtime leg is already talking.

This replaced a deliberate "silence until the user's first finalized turn" policy. That
policy assumed the user could be heard. When they could not, three things lined up to
guarantee total silence: Buddy never spoke first, no audio meant no STT and so no turn,
and the 45s away nudge could not fire because LiveKit skips arming the away timer while
`room_io.subscribed_fut` is pending. The user got five minutes of nothing and filed "why
don't you respond when I use voice chat?". An opening line is the cheapest proof that a
call is live.

**The inbound path is watched.** `voice/input_liveness.py` distinguishes "participant
published no audio track" from "audio arrived, STT produced nothing", raises a
`session.error` on the data channel, and has Buddy say so in his own words via
`generate_reply`. Never a canned line, and never "check your mic": the microphone is
usually working and is how they were talking in the first place.

**Zero turns is a defect, loudly.** A run past `VOICE_ZERO_TURN_ALERT_MS` with no turns
logs `voice_session_zero_turns` at error and persists `health` on the session doc, so it
is countable in Firestore rather than only greppable for a fortnight.

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
User participant never joins ---> log at error and end the run; never hold an empty room
User publishes no audio track --> session.error + Buddy says so; never sit mute
Run ends with zero turns -------> error log + health field on the session doc
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

## Prompt rule: say whether it rings

`set_reminder` takes a required `tier` of `reminder` or `alarm`, and voice is the
surface where getting this wrong is worst. "Set an alarm at 3am" was once
answered with a silent reminder, and a user who only hears "done, I'll remind
you" has no way to tell that nothing will actually wake them.

- Choose `alarm` only for a request to be woken or physically interrupted ("set
  an alarm", "wake me at 6", "make sure I'm up"). Everything else is `reminder`,
  including timed tasks. Time of day alone never makes something an alarm, and
  ambiguity resolves to `reminder`.
- The spoken confirmation must carry the tier: "I'll wake you up" for an alarm,
  "I'll remind you" for a reminder. `handlers/mcp.py` sets this from the tool
  RESULT, not the argument, because the server decides the stored tier.
- Never promise a wake-up the device cannot deliver. If the exact-alarm
  permission is missing, the honest answer is a nudge.

See `architectures/alarm-tier.md`.

## Non-obvious walkthrough: deterministic screen capture

1. The desktop sends screen frames to the worker's session-only memory store.
2. A frame may be consumed for model context, but its bytes remain retained until a newer frame arrives or the session closes.
3. Only the authenticated user's finalized transcript is checked against the narrow capture grammar. Interim STT, OCR, model output, quoted phrases, negations, and capability questions cannot authorize a save.
4. A command-only capture bypasses the LLM. `save_screen_item` is absent from the model's prompt and tool catalog.
5. The worker uploads the retained JPEG, then writes the Firestore item with a stable retry-safe id. It speaks success only after both operations succeed.
6. The recorder stores the deterministic action receipt through the same durable receipt path used by write tools.

## Specialized agent handoffs

Interview Mode and Guide Mode replace Buddy with specialized agents inside the
same `AgentSession`. Neither mutates Buddy's prompt or tools in place. Both keep
one room and one speech pipeline, carry state in `AgentSession.userdata`, isolate
their tool surfaces with `mcp_servers=None`, and return the same `BuddyAgent`
instance.

```text
BuddyAgent --native Guide arm--> GuideSupervisorAgent
                                      | quick screen turn: direct grounded reply
                                      | multi-step: await GuidePlanningTask[result]
                                      |             then resume same supervisor
BuddyAgent <--native disarm----- GuideSupervisorAgent
```

```text
one AgentSession (STT/LLM/TTS/VAD, one voice connection, one room)
  BuddyAgent --start_mock_interview--> InterviewSupervisorAgent
                                         await InterviewIntakeTask[InterviewDossier]
                                           company
                                           has JD?  no  -> role + experience, spoken
                                                    yes -> revisioned overlay + byte stream
                                           -> typed dossier
                                         validate, one retry if incomplete
                                         report setup captured
                                         -> InterviewerAgent
                                            fixed role-aware plan
                                            30m heads-up; 34m final warning
                                            35m: debrief, then hand back
  BuddyAgent <--return---------------- InterviewSupervisorAgent / InterviewerAgent
```

The split follows LiveKit's own distinction, and it is the load-bearing decision
here. A **handoff** is for when conversational identity and responsibility change:
Buddy stops being responsible and the supervisor takes over. An **`AgentTask`** is
for bounded work that returns a result and gives control back: Interview intake
returns a dossier, and Guide planning returns one typed task result to the same
Guide supervisor. Neither is a second personality. Getting this backwards
produces either a supervisor that never owns its mode or a bounded operation that
is handed conversational identity it does not want.

Where the task may be awaited is fixed by the SDK: `AgentTask` raises unless it is
awaited inside a tool function or an `on_enter`/`on_exit` hook. Interview awaits
intake in `on_enter`; Guide awaits planning from the supervisor's model-selected
tool. The quick Guide lane never enters an `AgentTask`.

Five things make the boundary work, and each is load-bearing:

1. **The trigger is ordinary tool reasoning.** `start_mock_interview` is a normal
   `@function_tool` on `BuddyAgent`, registered in `VOICE_TOOL_REGISTRY` like any
   other. No phrase list, no UI affordance, no second voice session. The same is
   true one level down: nothing keyword-matches whether the user has a JD, the
   model picks `request_job_description` or `record_role_and_experience`.
2. **The handoff is the tool's return value.** Returning `(Agent, str)` makes
   LiveKit call `session.update_agent()`. The same session, pipelines and room are
   reused throughout, so the handoff costs no connection latency.
3. **Isolation comes from `mcp_servers=None`, not from the prompt.**
   `AgentActivity` resolves MCP servers as "the agent's own if given, otherwise the
   session's", and the session carries the entire production MCP surface. Without
   that explicit `None` the supervisor and the intake task would silently inherit
   reminders, calendar, memory and web.
4. **State lives in `AgentSession.userdata`, never on an agent.** Agent instances do
   not survive a handoff. `VoiceSessionState` does, and nests feature state so the
   next feature to need session state does not churn every `RunContext` annotation.
   Intake commits its draft to that state on every tool, not only at completion, so
   a cancelled or timed-out setup still keeps what the user answered.
5. **The return trip hands back the SAME `BuddyAgent` instance**, through a
   `buddy_factory` wired in `voice_agent.py` before `session.start`. Every
   coordinator (recorder, guide, artifact tracker, bridge, screen context) holds a
   reference to that instance; returning a fresh Buddy would orphan all of them.

### Job description transfer

A bounded LiveKit byte stream, not a database upload. The JD never touches
Firestore, GCS or a REST endpoint: the decoded text lands in `session.userdata` and
dies with the session.

The overlay request rides the reliable `client_events` topic and is revisioned and
acknowledged, reusing `artifact_delivery.py` wholesale, including its lesson:
publishing a packet is not evidence the user can see a paste box, so the waiter is
armed before publishing and a missing ack means "no overlay", never "probably
fine". Without the ack the intake says the box did not open and falls back to
asking, rather than telling the user to paste into nothing.

The text arrives on the `interview_material` topic carrying `interview_id`,
`revision`, `material_type` and `schema_version` as stream attributes. The receiver
matches `(interview_id, revision)` against what is armed, both before reading and
again after assembly, because a newer revision can be armed while bytes are in
flight. That check is the important one: a paste answering an earlier request would
otherwise be accepted as the answer to this one, which is a bug that looks like
nothing at all. Size, UTF-8, blankness and sender identity are all enforced on
receipt regardless of what the client claims.

The payload is UNTRUSTED, being text pasted out of someone else's web page, and is
treated the same way `screen_context_stream.render_for_model` treats its own.

### Known boundaries

- The supervisor and the intake task are plain agents, so they have none of
  `BuddyAgent.llm_node`: no action policy, no speculative generation, no artifact or
  `[POINT]` handling, no Action Truth envelopes, no `speak_only` constraint.
- Their tool calls are not observed. `VoiceSessionRecorder`'s tool observer is bound
  to Buddy, and its action receipts key off `ToolEffect.WRITE` registry entries;
  neither agent's tools are in either. Transcript still records, since that is
  session-level.
- Ambient screen/OCR context, early graph-memory injection, and input-liveness
  speech are gated by the session owner. They do not target inactive Buddy while
  either specialized mode owns the floor. Typed user messages remain routed to
  the current agent so a muted user can still ask to leave the mode.
- `evaluate_execution` refuses any non-READ tool in a turn where `web_surf`,
  `query_memory` or `get_user_context` already returned, and the BM25 core floor
  always exposes `query_memory`. A turn that reads memory first and then calls
  `start_mock_interview` drops the handoff silently. A selected handoff is not a
  guaranteed handoff.
- Aura-Desktop implements the paste overlay contract. Its source wiring is
  present, but this repository has not yet proven the cross-repo exchange in a
  live desktop session.
- Answer records and the debrief exist only in `AgentSession.userdata`. This
  phase adds no interview database write, history, score, or hiring prediction.
- The interviewer owns a monotonic, session-only wall clock from its first
  question: it warns at 30 minutes, reminds the candidate at 34 that no more
  questions will be asked after 35, then waits for the in-flight utterance to
  finish, gives one bounded debrief, and returns to Buddy. It does not close the
  LiveKit room or voice window. A disconnect or the independent five-minute
  no-transcript safety watchdog can still end a session earlier.

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
- `backend/src/agent/voice/interview/`

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
`voice/capabilities.py` allows them on `ALL_SURFACES`.

One caveat this section used to omit, and it cost us a user. `ToolCatalog.select`
(`voice/tool_discovery.py`) narrows the exposed set per turn by BM25 score, and it used
to be able to return NOTHING (`no_semantic_match`, `higher_scoring_tool_ineligible`,
`active_intent_cancelled`). A model handed an empty tool list says so out loud, which is
how "I don't have a set_reminder tool exposed to me right now" reached a user who had
used that tool an hour earlier.

`CORE_TOOLS` (`shared/tools.py`) is now a floor the selector may never drop: it is unioned
in after `max_results` is spent, so the semantic bundle is unchanged, and it replaces
every previously-empty return. A cancelled intent gets the read-only subset, never empty
and never the write just cancelled. The floor intersects the structurally eligible set, so
a non-finalized turn still exposes no writes. Reason code: `core_floor_applied`.

Wording decides which tools are SUGGESTED. It never decides which ones EXIST. This is the
same lesson as "arming is session state, not wording" below, applied to the tool list.

### A strict tool contract ships twice, and the worker's copy is the one that counts

The schema the model actually sees for an MCP tool is produced in two independent
places, and each deploys on its own pipeline:

- the backend advertises it (`handlers/mcp.py` `_enforce_canonical_tool_contract`
  replaces the FastMCP signature-derived schema with the canonical one), shipped by
  `backend/deploy.sh`;
- the worker then OVERRIDES it (`voice/pipelines.py` `AuraMCPServerHTTP._make_function_tool`
  swaps in `openai_function_definition(name)` for any `strict: True` tool), shipped by
  `lk agent deploy`.

The override wins, so a correct backend cannot rescue a worker carrying an older
`shared/tools.py`. Changing a strict contract means deploying BOTH, worker included.
A strict schema whose `required` omits a property is a 400 on every turn that carries
the tool list, not just the turn that wanted the tool; `assert_strict_tool_schema` and
the `TOOL_DEFINITIONS` sweep in `shared/tools.py` turn that into an import-time failure,
but only for the revision that actually runs.

So a "Buddy can't create events" report is almost never a missing tool. There are two real
causes, both prompt-level:

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

### Copyable content: cards, not speech

Buddy must never read a draft, command, prompt or snippet aloud. Three layers,
in order of authority:

1. **`ArtifactSession`** (`voice/artifact_session.py`) is the card on screen and
   the authority for whether a turn is about it. It opens when a card renders
   and stays open until the turn commits to a WRITE/PRESENT capability outside
   the artifact pair, or ages out after `MAX_IDLE_TURNS`. Read-only capabilities
   and the speech channel never close it: a lookup ("check what he posted") is
   not the user moving on.
2. **`tool_choice="required"`** on armed turns, set as a local `ModelSettings`
   inside `llm_node`. Plain prose is not a representable answer, so the strict
   tool schema carries every word.
3. **`_card_narrated_artifact`** holds the stream and diverts a narrated body to
   a card. Last resort, for unarmed turns and for a leg that ignores tool_choice.

**Arming is session state, not wording.** This is the non-obvious part, and it
was learned the hard way. Lexical arming matched 3 of 8 turns in a real failing
session, and every one of those 3 carded correctly while 4 of the other 5
recited the draft aloud. Two things defeat any lexicon here: revision turns do
not restate the noun ("where is the hook?"), and endpointing splits one spoken
thought across several finalized messages, so a keyword that matched can be
discarded before the generation runs. `spoken_action_guard` now only recognizes
the turn that OPENS a card, where the user does say the noun.

**A constrained turn still needs a way to talk.** `speak_only` exists because
forcing a tool without it would make a clarifying question impossible, and Buddy
would render its own question to a card. It is exposed ONLY on armed turns, so
ordinary turns keep streaming text into TTS from the first token.

Structured output here is tool calls, never `response_format`:
`lk_llm.FallbackAdapter.chat()` has no `response_format` parameter, so a JSON
schema constraint would silently stop applying on failover to Anthropic or
Google. `tool_choice` is on the FallbackAdapter and maps on every leg.

### Free-tier "1 minute left" warning

The LLM must NOT track time. The server and client own the countdown. At T-60s
inject a one-shot instruction via `generate_reply` so the model weaves it in at
the next turn boundary in Buddy's own voice (the same mechanism
`voice/recorder.py` uses for the away-nudge), fired ONCE behind a guard flag. At
T-0 queue ONE graceful wind-down line, then end the session. Never hard-cut to
silence, and never `say()` a canned line over the user mid-sentence.
