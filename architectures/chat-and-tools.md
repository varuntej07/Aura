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

## Tool exposure invariant

**Nothing about the user's wording removes a tool.** The model sees the same tools on a
turn that says "remind me at 7" and a turn that says "ugh". The only things that subtract
are structural: which surface this is (`resolve_chat_surface_allowed_tools`, a denylist of
`send_email` plus a client-rendering carve-out) and regeneration safety
(`_REGEN_EXCLUDED_TOOLS`).

This was learned the expensive way. `excluded_tools_for_text_turn` used to delete
`set_reminder` whenever the turn did not match a create-command regex, and Buddy, seeing
no such tool, told a user "I don't have a set_reminder tool exposed to me right now" 50
minutes after the same account used it. The same gate broke the clarification walkthrough
below whenever the user's answer arrived in a new request.

Authorization now happens where it can be explained:

- **Turn contradiction.** `action_intent_policy.blocked_write_reasons_for_text_turn`
  denies a reminder write only on a clear negative (a status question, an explicit
  negation), and `ToolExecutor.execute` returns an Action Truth envelope
  (`not_authorized_this_turn`) so Buddy says something true instead of going quiet.
- **Tier.** `shared.tools.TIER_GATED_TOOLS` is enforced in the executor too
  (`upgrade_required`), not by hiding the tool. A free user asking about their calendar
  gets an honest upgrade sentence rather than "Aura has no calendar".
- **Core floor.** `shared.tools.CORE_TOOLS` names the capabilities no surface may appear
  to lack. `shared/tool_exposure.py` checks every surface at process start and logs
  `core_tool_exposure_regression` if one is unreachable.

Whatever Buddy says is then checked against the tools it actually held:
`shared/capability_claims.py` logs `buddy_invented_aura_limitation` when a reply denies an
exposed tool or asserts a cross-device limitation. It is log-only by design; the verbatim
sentence is in the log because a count cannot tell a real confabulation from a regex
artefact.

The facts Buddy is allowed to state about Aura live in `_AURA_PRODUCT_TRUTH`
(`prompts.py`), inside the cached prefix on every surface. Do not add a line there that is
not true today.

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

## Reminder tiers: a reminder is quiet, an alarm makes noise

`set_reminder` takes a required `tier` of `reminder` or `alarm`. There is no
settings toggle: Buddy picks it from the user's own words, and the field is
required rather than optional so the model states which one it means on every
call instead of defaulting into one by omission.

- `alarm` only when the user is asking to be woken or physically pulled out of
  what they are doing ("set an alarm", "wake me at 6", "make sure I'm up").
- `reminder` for everything else, including timed tasks like "remind me to take
  my meds at 8am". Time of day alone never makes something an alarm.
- Ambiguity resolves to `reminder`. A wrong 3 AM ring costs far more than a
  missed banner, and `normalize_tier` collapses anything unrecognised down to the
  quiet tier rather than up.

Two consequences worth knowing before changing this:

- **Buddy must say which tier it chose.** The tool result carries an
  `instruction` for the alarm tier, and the voice path varies its spoken
  confirmation ("I'll wake you up" vs "I'll remind you"). The tier is inferred
  from language, so the confirmation is the user's only chance to catch a misread
  before the alarm is due.
- **Dedup upgrades, never downgrades.** "Remind me at 3am" followed by "actually
  make that an alarm" is the same occasion by every similarity measure, so
  without the explicit upgrade in `_set_reminder` the second request would
  collapse into the first and leave the user on the tier they asked to leave.

An alarm does not ring from a push. See `architectures/alarm-tier.md`.

## Code anchors

- `backend/src/handlers/chat.py`
- `backend/src/services/claude_client.py`
- `backend/src/services/gemini_chat_fallback.py`
- `backend/src/services/openai_chat_fallback.py`
- `backend/src/services/tool_executor.py`
- `backend/src/services/action_intent_policy.py`
- `backend/src/shared/tool_exposure.py`
- `backend/src/shared/capability_claims.py`
