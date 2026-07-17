# Reminder observability and audit plan

Status: design for review.
Author context: written after the 2026-07-16 incident where Buddy spoke "Locked in for 10 PM tonight" over voice, no reminder was written to Firestore, and no single log line explained why.
The guard and gate bugs from that incident are already fixed separately (see `spoken_action_guard.py`, `action_policy.py`, and the parity tests in `tests/test_voice_action_orchestration.py`).
This document is only about making the system auditable so the next incident is one query, not an archaeology dig.

## Problem statement

A reminder crosses four independently deployed or hosted components, and no correlation id threads through them:

1. Desktop app (`Aura-Desktop`, `useVoiceBar.ts`) joins a LiveKit room `voice-{uid}`.
2. The voice worker (`buddy_agent.py`) runs on LiveKit Cloud, decides whether to call `set_reminder`, and speaks the reply.
3. The worker reaches the backend over HTTP at `POST /mcp/` (`voice/pipelines.py`, authed via `voice/auth.py`).
4. `handlers/mcp.py` calls `tool_executor._set_reminder`, which writes `users/{uid}/reminders/{id}` and later fires via `handlers/scheduler.py` and `services/notifications/delivery_router.py` to FCM and the desktop outbox.

Two failures made the incident unauditable:

- **Split log sinks.** The worker's decision logs (`VoiceAction: turn policy`, `execution deferred`, `spoken action blocked` from `action_telemetry.py` and `buddy_agent.py`) go wherever the worker runs. GCP Cloud Run only ever sees the worker when it makes an outbound `/mcp/` call. The user searched Cloud Run and correctly found nothing, because the decision never produced an `/mcp/` call.
- **No correlation id.** `job_id` and `room_id` exist at the LiveKit layer but never propagate to `/mcp`, to the Firestore write, or to the delivery events. There is no key that ties "what Buddy said" to "what was written" to "what was delivered".

There is also a live contradiction about where the worker runs.
`deploy.sh:3` says the worker runs on LiveKit Cloud Agents.
`start.sh` still launches `python -m src.agent.voice_agent start` in the Cloud Run container.
Until that is resolved, "where do the worker logs go" has no single answer.

## Goals

- One correlation id, present on every event across all four hops, so a reminder's full life is one query.
- Structured domain events, not free-text logs, so invariants can be asserted, not eyeballed.
- One queryable sink, so there is a single place to look.
- An automated audit that flags the exact class of bug that happened: a spoken success with no committed receipt.
- A design that scales to production volume and migrates cleanly onto OpenTelemetry without rework.

## Non-goals

- Not rebuilding the guard or the gate. Those fixes shipped.
- Not a full APM rollout for every backend route. Scope is the reminder lifecycle, built so other flows can adopt the same pattern.
- Not exposing raw audit data to end users in v1.

## Prerequisite (step zero): one log sink

Nothing below is worth building on two sinks.
Resolve `start.sh` vs `deploy.sh` first, then make the worker's structured logs land in the same store as the backend.

- If the worker runs on LiveKit Cloud: configure its logger to ship structured JSON to Cloud Logging (log router or the LiveKit Cloud log drain), and delete the dead agent launch in `start.sh`.
- If the worker runs in-container on Cloud Run: its stdout already reaches Cloud Logging; then `deploy.sh:3`'s comment is wrong and should be corrected.

Acceptance: a `VoiceAction: turn policy` line and a `POST /mcp/` line for the same session are both queryable in one place.

## The correlation model

Root id: `trace_id`, owned by the worker, stable for a voice session.
Derive it from `job_id` (present at `entrypoint` in `voice_agent.py`), which already scopes one LiveKit session.
Session granularity is enough to answer "what happened in this call". Turn granularity (`job_id:turn_index`) layers on later where useful; `action_telemetry.py` already carries `turn_index`.

Why not invent a new UUID per turn on the desktop: the desktop's only role is joining the room, and the room name `voice-{uid}` plus `job_id` is already the natural correlation root. The desktop should keep logging its room name (it already does in `useVoiceBar`) so a user-reported issue maps to a `job_id`.

## Propagation across each hop (concrete touchpoints)

1. Worker owns `trace_id = job_id`.
   Every `action_telemetry.py` event already includes `session_id` and `turn_index`; add `trace_id`.

2. Worker to `/mcp`.
   The `/mcp` call is made by LiveKit's MCP client, not directly by our code, so a per-call dynamic header is awkward.
   Pragmatic path: set a static session-level header when the MCP server client is constructed in `voice/pipelines.py` (alongside the existing auth token), for example `X-Aura-Trace-Id: {job_id}`.
   This gives session-granularity correlation, which covers the incident's need.
   Risk to verify: confirm `livekit.agents.mcp.MCPServerHTTP` accepts custom static headers. If not, fall back to deriving `trace_id` server-side from the authenticated worker session on `/mcp`.

3. Backend `/mcp` handler.
   `handlers/mcp.py` reads `X-Aura-Trace-Id` (or derives it) and passes it into `tool_executor`.
   `ToolExecutor` gains a `trace_id` field, set once per request, same as `_user_id` and `_created_via` today.

4. Firestore write.
   `_set_reminder` stamps `trace_id` into the reminder doc next to the existing `created_via`.
   This makes every reminder self-describing: which session created it.

5. Firing and delivery.
   `handle_scheduler_tick` reads `trace_id` off the reminder doc and puts it on the `NotificationProposal` (`scheduler.py`), and `delivery_router` includes it on the per-channel delivery event.

## Event schema

Emit these domain events, each as one structured log line and one audit row (see sink), all keyed by `trace_id`:

| event | emitted where | key fields |
|---|---|---|
| `reminder_intent` | worker, on REMINDER_WRITE classification | trace_id, uid, turn_index, transcript_hash |
| `reminder_tool_withheld` | worker, `evaluate_execution` deny | trace_id, reason_code, exposed_tools |
| `reminder_committed` | backend, `_set_reminder` success | trace_id, uid, reminder_id, trigger_at |
| `reminder_commit_failed` | backend, `_set_reminder` raise | trace_id, uid, error_type |
| `reminder_success_spoken` | worker, guard sees a success claim | trace_id, committed_at_speak: bool |
| `reminder_success_blocked` | worker, `spoken_action_guard` blocks | trace_id, reason |
| `reminder_fired` | scheduler | trace_id, reminder_id |
| `reminder_delivered` | delivery_router, per channel | trace_id, reminder_id, channel |

`transcript_hash` not raw transcript, to keep the audit store free of message content.

## Data model and sink

Tier 1 sink: a Firestore audit collection, `reminder_audit/{trace_id}/events/{event_id}`, plus the same events to structured stdout.

- Per-user, per-session audit trail readable by `trace_id` in one read. This is the "single trace" the incident needed.
- Bounded: a handful of events per reminder, pruned on the same 7-day horizon as the reminder itself.
- The duplicate to structured stdout means a future BigQuery log sink captures the same events with no code change.

Tier 2 sink (scale): a Cloud Logging structured-log router to BigQuery for ad-hoc audit queries and retention, and OpenTelemetry spans exported to Cloud Trace for cross-service latency and causality.

Why not Firestore alone at scale: it is right for per-user trails and the invariant check, but poor for cross-cutting analytics ("how many success-without-commit events this week across all users"). BigQuery is the scalable answer for that, and Cloud Trace for span visualization.

## The audit invariant (the point of all this)

A checker that asserts, over the events for a `trace_id`:

- **No `reminder_success_spoken` without a preceding `reminder_committed`.** This is exactly the 2026-07-16 bug. If the guard ever leaks a new phrasing, this catches it in monitoring instead of relying on the regex being exhaustive. The guard becomes a fast in-line backstop, and this invariant becomes the real guarantee.
- No `reminder_committed` without a `reminder_fired` by `trigger_at + slack`.
- No `reminder_fired` without at least one `reminder_delivered`.

Home: a bounded pass inside the existing `/scheduler/tick` (`handle_scheduler_tick`), which already runs on a cron and already fans out isolated sub-tasks.
It queries recent audit events and emits an alert-severity log on any violation.
Scalable because it is event-count bounded and, at Tier 2, runs as a windowed query on the columnar store rather than scanning Firestore.

## Scalability justification

- Correlation via W3C `traceparent` at Tier 2 is the vendor-neutral industry standard, supports head and tail sampling, and integrates with Cloud Trace and Cloud Logging. No bespoke id scheme to maintain.
- Structured events on a columnar sink (BigQuery) scale to millions of rows and are queryable by `trace_id` or `uid` in seconds.
- The invariant checker is O(events), not O(users), and windowed, so cost tracks reminder volume, not user count.
- Firestore audit subcollection at Tier 1 is per-reminder bounded and auto-pruned, so it cannot grow unbounded.

## Rollout order

1. Resolve the worker deployment contradiction and unify the log sink (prerequisite).
2. Thread `trace_id` worker to `/mcp` to `_set_reminder` to the reminder doc. Verify the MCP header path.
3. Emit the eight events to structured stdout and the Firestore audit subcollection.
4. Add the success-without-commit invariant to `/scheduler/tick` with alerting.
5. Tier 2: OTel `traceparent` end to end, BigQuery log sink, Cloud Trace export.

Tiers 2 through 4 give immediate reminder auditability with no infra change.
Step 5 is the scale upgrade and reuses the same event schema, so no rework.

## Open decisions for review

1. Session-granularity `trace_id` (via static MCP header) versus turn-granularity (needs a dynamic header or a server-side derivation). Recommendation: ship session granularity, layer turn granularity only where a turn-level question actually arises.
2. Tier 1 Firestore audit subcollection versus going straight to Cloud Logging plus BigQuery. Recommendation: Tier 1 Firestore for the fast user-facing trail, dual-write to stdout so BigQuery is a later no-code-change add.
3. Whether the desktop should mint and display a support-facing `trace_id` so a user can quote it in a bug report. Low cost, high support value, but a desktop change.
