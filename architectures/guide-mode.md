# Guide Mode

Bounded, natural screen guidance for an explicitly armed desktop session. Buddy
watches a live screen stream and talks the user through a task step by step.

Desktop-only. There is no mobile equivalent. It rides the same LiveKit voice
session and the same single `BuddyAgent` as everything else in
[voice-agent.md](voice-agent.md); it is not a separate agent or a separate
process.

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
|   ack EVERY frame immediately ("Screen checked.")         |
|   on change:"1" -> ONE terse nudge at a quiet boundary    |
+-----------------------+-----------------------------------+
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
with a `guide.step` message so the desktop's per-frame handshake never stalls
and the next frame keeps flowing. Replying is a separate decision.

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
- `backend/src/agent/voice/guide_task_runtime.py` (durable orchestration)
- `backend/src/agent/voice/guide_kernel.py` (provider-neutral contracts)
- `backend/src/agent/voice/guide_models.py` (typed task state)
- `backend/src/agent/voice/guide_control.py` (arm/disarm request)
- `backend/src/agent/voice/guide_provider_adapter.py`
- `backend/src/agent/voice/guide_prompt.py`
- `backend/src/agent/voice/guide_default_profile.py`
- `backend/src/agent/voice/guide_template.py` + `guide_capcut_example.py`
  (one bounded CapCut podcast-short template used to seed planning)
