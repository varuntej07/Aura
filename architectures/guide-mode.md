# Guide Mode

Bounded, natural screen guidance for an explicitly armed desktop session. A
specialized Guide supervisor watches the live screen stream and talks the user
through a task step by step.

Desktop-only. There is no mobile equivalent. It rides the same LiveKit voice
session as Buddy. Arming performs an in-session handoff to
`GuideSupervisorAgent`; disarming hands the conversation back to the same
`BuddyAgent` instance. It is not a second room, connection, or process.

## Component data flow

```text
desktop (Rust pins monitor, checks signed-in session)
   |  arms natively -- the worker can only REQUEST, never force
   v
JPEG frame every ~2s over LiveKit, stamped change:"1" or change:"0"
   |
   v
+---------------------- voice worker -----------------------+
| guide_mode.py                                             |
|   reserve Guide ownership, hand Buddy -> Guide supervisor |
|   ack EVERY frame immediately with guide.frame_ack        |
|   on change:"1" -> ONE terse nudge at a quiet boundary    |
+-----------------------+-----------------------------------+
                        |
                        v
              guide_supervisor.py
              isolated prompt + two bounded tools
                 |                 |
          quick screen turn   multi-step outcome
          one direct call          |
                                   v
                       guide_planning_task.py
                       typed AgentTask; then returns
                        |
                        v
              guide_task_runtime.py
              durable task state, planning, acceptance
                        |
              +---------+---------+
              v                   v
        guide_kernel.py     guide_provider_adapter.py
        pure contracts      model/transport binding
```

## The two decoupled loops

**Acking is decoupled from replying.** Every received frame is acked instantly
with a protocol-v2 `guide.frame_ack` message so the desktop's per-frame
handshake never stalls and the next frame keeps flowing. Replying is a separate
decision. Protocol v1 is not accepted and has no compatibility branch.

A `change:"1"` frame (the desktop's own change filter saw a real visible change)
fires at most ONE terse proactive nudge, debounced so it never stacks on a
spoken reply or on a burst of rapid changes. A `change:"0"` frame is a forced
static-screen refresh and never triggers a nudge.

## Arming is native, not agent-owned

`guide_control.py` publishes a `guide.request` event over the data channel.
`useGuideMode` on the desktop validates it and routes it to the native
`arm_guide` / `disarm_guide` command. The armed -> publish -> activate loop
lights the status dot only once capture is truly live.

The spoken lines are honest by construction: they never claim Guide Mode is
already on, only that it is starting. Same fail-soft contract as
`visible_artifacts` and `draft_outbound`: a lost packet degrades to a spoken
line that does NOT claim success, never a raised tool error mid-turn.

## Same-session handoff and ownership

Guide follows the same LiveKit supervisor boundary as Interview Mode:

```text
BuddyAgent
  -> native arm reserves GuideStartClaim
  -> session.update_agent(GuideSupervisorAgent)
  -> GuideSupervisorAgent.on_enter commits PLANNING -> ACTIVE
  -> quick visible question: one grounded model call
  -> multi-step outcome: await GuidePlanningTask[GuidePlanningTaskResult]
       -> fast typed planner call
       -> grounded visual decision
       -> task returns to the same Guide supervisor
  -> native disarm commits RETURN_PENDING
  -> session.update_agent(the same BuddyAgent)
  -> BuddyAgent.on_enter commits IDLE
```

`VoiceSessionState.guide` is the authority across handoffs. A coordinator flag
or agent-local persona boolean is not ownership. Entry is acknowledged only
after the supervisor's `on_enter` commits the matching generation and ownership
epoch. Exit is acknowledged only after the same Buddy has entered and committed
idle. A stale or timed-out activation is rolled back without consuming the
Guide generation.

The supervisor sets `mcp_servers=None`, so it cannot inherit Buddy's reminders,
calendar, memory, web, or other broad tools. Its local tools are native Guide
disarm and the bounded planning task. The model decides which described tool to
call; no phrase list or regex over user speech controls routing.

Normal Guide turns deliberately skip imageless speculative generation. The
fresh frame is attached at finalization and the supervisor makes one direct
model call, preserving the old Guide turn latency shape. Frames never create
new agents, and the handoff itself makes no model call.

The planning task is entered only for a multi-step outcome, clarification, or
replanning. Its structured planning call uses the cheap tier first and the
balanced tier as fallback, with Pydantic validation on the response. Visual
grounding remains on the balanced vision tier with the expert tier as fallback.
While the bounded work runs, `RunContext.with_filler` waits 450 ms before saying
the supervisor model's short, turn-specific acknowledgement. Two later bounded
phrases may play at three-second intervals. If work finishes first, the pending
filler is cancelled, so a fast result does not gain artificial speech latency.

## Layering rule

`guide_kernel.py` holds provider-neutral contracts and deterministic helpers. It
**must not** import an application profile, model provider, agent SDK,
transport, database client, or vendor response type. Those integrations adapt to
these contracts at the composition boundary
(`guide_provider_adapter.py`, `guide_default_profile.py`).

Task state is typed in `guide_models.py`: `GuideTaskStatus` moves through
`clarifying` -> `active` -> `waiting_user`, and `GuideTask` is the durable
record.

## Failure and recovery

```text
Frame arrives while a reply is in flight
    -> still acked immediately
    -> nudge suppressed by the debounce, not queued

Rapid burst of change frames
    -> one nudge, remainder debounced away

guide.request packet lost
    -> desktop never arms
    -> spoken line already avoided claiming success, so no false state

Desktop disarms mid-task
    -> capture stops, task record persists at its current status
    -> Guide requests return, same Buddy commits idle in on_enter

Guide activation times out or loses its ownership epoch
    -> reservation is cancelled or return is requested
    -> same Buddy is restored
    -> active acknowledgement is not published
```

## Client-side telemetry

The desktop reports a completed session through `POST /devices/guide-usage`,
which merges into a Guide Mode rollup on `users/{uid}`. The worker's
`GuideCoordinator` writes the fields the client cannot see (model, average TTFT,
tools used, last user turn, frames processed) onto the SAME rollup keyed by
`guide_session_id`, with a transaction guarding the snapshot so a stale writer
never clobbers a newer session.

## Code anchors

- `backend/src/agent/voice/guide_mode.py` (frame stream, ack/nudge loops)
- `backend/src/agent/voice/guide_supervisor.py` (specialized LiveKit agent)
- `backend/src/agent/voice/guide_planning_task.py` (bounded typed AgentTask)
- `backend/src/agent/voice/guide_session_state.py` (handoff ownership state)
- `backend/src/agent/voice/guide_task_runtime.py` (durable orchestration)
- `backend/src/agent/voice/guide_kernel.py` (provider-neutral contracts)
- `backend/src/agent/voice/guide_models.py` (typed task state)
- `backend/src/agent/voice/guide_control.py` (arm/disarm request)
- `backend/src/agent/voice/guide_provider_adapter.py`
- `backend/src/agent/voice/guide_prompt.py`
- `backend/src/agent/voice/guide_default_profile.py`
- `backend/src/agent/voice/guide_template.py` + `guide_capcut_example.py`
  (one bounded CapCut podcast-short template used to seed planning)

## LiveKit references

- [Supervisor pattern](https://docs.livekit.io/agents/logic/supervisor-pattern/)
- [Tasks](https://docs.livekit.io/agents/logic/tasks/)
- [Workflows](https://docs.livekit.io/agents/logic/workflows/)
