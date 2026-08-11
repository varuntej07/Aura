# Voice session integrity, storage, and write-cost remediation plan

Status: Awaiting explicit approval. No production code or production data changes are authorized by this document.

## Goal

Fix the current failure mode where the assistant can claim a reminder was set without any tool receipt, the same logical conversation is duplicated across multiple persistent records, launch surface metadata is dropped, and read/status follow-ups can trigger redundant writes. The remediation should make one logical conversation have one canonical transcript, make side-effect claims provable from successful tool receipts, preserve recovery behavior, store launch surface metadata, present readable session recaps, and materially reduce Firestore writes without weakening offline restore.

Tool execution must be runtime-enforced. The prompt can describe allowed actions and wording, but only the orchestrator, policy layer, and executor can authorize and perform a side effect. A model-generated string like `{set_reminder}` is not a tool call and cannot be treated as one.

## Evidence and root causes

This plan is based on the current code paths and a read-only Firestore audit for user `Qx12s9dXPFZJ3bYW4QGi0w4il1q2`. Architecture documents were not used.

| Finding | Verified evidence | Root cause | Classification |
|---|---|---|---|
| The voice assistant said the reminder was set, but no voice tool ran | The voice run at 2026-07-14 19:32 UTC has `tool_calls_made: []` while the transcript says both "locked in" and "reminder set" | The first turn contained a complete reminder plus a timezone correction, so the deterministic policy stored no unresolved action. The next turn, "No. Central time.", was classified as a correction without reminder capability. `set_reminder` was not exposed, but the model could still speak success language. The spoken guard does not match reverse-order phrases such as "reminder set" or "locked in" | Correctness defect |
| The first text follow-up created the missing reminder | The first `chat_turn` has `completed_tools: [set_reminder]`; the only reminder in the incident window was created through text at 19:33:49 UTC | General text chat exposes every tool. A status question, "did the reminder set?", was allowed to call the write tool | Correctness and authorization defect |
| The second text follow-up invoked the reminder tool again | Two `chat_turn` documents have the same semantic reminder arguments and two completed idempotency claims, but only one reminder document exists | Idempotency is keyed by `client_message_id`, so it prevents retries of one request, not duplicate intent across requests. The executor's reminder duplicate check prevented a second reminder document, but only after another tool execution path | Avoidable work; final data remained deduplicated |
| Desktop metadata is absent | All 23 audited voice session documents lack `surface` | `/voice/token` validates and stamps `surface`; the worker resolves it; `run_post_session_pipeline` and `_write_session_doc` then drop it | Field-contract defect |
| The summary is cluttered and falsely claims actions | The stored summary uses `OPEN LOOPS`, `DECISIONS MADE`, and other mandated headings, and says a reminder was set despite no voice tool call | The summarizer prompt requires those headings and receives transcript text, not authoritative action receipts | Product and correctness defect |
| A logical voice conversation has two unrelated IDs and two permanent transcripts | The audited account has 23 voice runs and zero same-ID overlaps with 145 chat sessions. The incident's chat session has 16 messages while its voice run has the first 12 turns under another UUID | The client generates a chat session ID; the worker independently generates a voice run ID; neither is passed to the other. Both persist the transcript | Redundant permanent storage and broken lifecycle contract |
| `chat_turns` appears to be a third session copy | Each text request stores a sanitized 12-turn history with a two-day TTL | This is a recovery snapshot for background completion, not another permanent session. Two documents exist because the user sent two text requests | Intentional temporary duplication; keep initially |
| Every backed-up message rewrites its parent session | `_processJob` always writes `chat_sessions/{id}` and then writes the child message for `messageUpsert` | Session metadata and message durability are coupled in one batch even when only the child changed | Redundant permanent writes |
| Delete and archive do not cover the logical conversation | Voice history deletion and archival operate on `voice_sessions`; the duplicated `chat_sessions/messages` transcript has no link to the voice run | No shared conversation ID and no explicit voice-run message ownership | Privacy and retention defect |

### Measured minimum write amplification

For the 12-message voice call, the current client path performs 24 Firestore writes because each message writes both its child and the parent session. The final title update adds one. The voice pipeline then adds at least the voice session document and latest session state, for a minimum of 27 writes before Aura extraction, reflection, analytics, and other variable work.

Each side-effect text follow-up currently adds at least nine bookkeeping and backup writes before any actual reminder document: four chat backup writes for the user and assistant messages, one `chat_turn` start, two idempotency claim/result writes, one `completed_tools` merge, and one terminal turn update. The two follow-ups therefore caused two independent reminder tool executions and two idempotency records even though only one reminder document survived the executor's duplicate guard.

## Target contracts

1. `conversation_id` identifies the user-visible thread and is generated once by the client.
2. `voice_run_id` identifies one voice connection/run and remains useful for telemetry, timing, and run-scoped deletion.
3. `surface` is a required enum on schema-v2 voice runs: `app`, `keyboard`, or `desktop`. Legacy records use `unknown` only during reads or backfill.
4. `chat_sessions/{conversation_id}/messages/{message_id}` is the only canonical transcript.
5. `voice_sessions/{voice_run_id}` stores run metadata and a `conversation_id` pointer. It does not retain `raw_turns` after the compatibility period.
6. Every canonical message created during voice contains `voice_run_id`, allowing run-scoped history display and deletion without deleting later text in the same thread.
7. `chat_turns/{client_message_id}` remains a temporary, TTL-backed recovery record. It is not treated as the source of conversation history.
8. A model may request a side effect, but only an authoritative successful tool receipt may produce success language, a reminder card, or an action in a session summary.
9. Read/status intents such as "did it set?" never receive `set_reminder`. A repeated complaint never receives it unless the current turn contains a new explicit scheduling command.
10. The runtime, not the prompt, decides whether a tool call exists. Prompt text can request or suggest work, but it does not execute anything.

### Why the temporary 2-day TTL exists

The TTL-backed `chat_turns` record is not meant to be a second canonical transcript. It exists as a recovery cache for unfinished or recently retried text-turn side effects while the durable chat transcript and reminder state settle. This is a compatibility mechanism, not the preferred production design.

The short version is:

- It covers the gap between a user turn arriving and the durable transcript or tool result being fully committed.
- It supports retry and recovery after process death, app restart, or a transient upload failure without forcing the client to replay the full conversation from scratch.
- It bounds storage growth for data that is only useful during recovery, not for long-term history.

If the product can guarantee a fully durable local queue plus a single end-of-session flush that survives client crashes, the TTL can be shortened further or removed from the hot path entirely. That local-first model is the cleaner production contract because it reduces unnecessary Firestore reads and writes and keeps the cloud database focused on durable sync, not transient turn recovery.

What should not happen is treating the temporary recovery record as another source of truth. The canonical transcript should still live only once.

## Target data flow

Diagram 1 of 2, architecture and ownership:

```text
Flutter creates conversation_id
          |
          +---- local Drift thread and messages
          |
          +---- /voice/token(conversation_id, surface)
                         |
                         v
              LiveKit participant metadata
              conversation_id + surface
                         |
                         v
              Voice worker creates voice_run_id
                 |                    |
                 |                    +--> tool executor
                 |                         |
                 |                         v
                 |                    action receipt
                 |                         |
                 v                         v
     voice_sessions/{voice_run_id}    receipt-grounded speech
     metadata + conversation pointer  and structured summary
                 |
                 v
     chat_sessions/{conversation_id}/messages
     one canonical transcript, messages tagged by voice_run_id
```

The client remains local-first. Cloud child messages remain durable independently of session metadata. The worker may reconcile missing voice messages after close, but it must use deterministic message IDs shared with the client so retries become idempotent upserts, not new messages.

## Implementation plan

### Phase 1: Stop false side-effect claims and accidental reminder writes

1. Extend the voice action state in `backend/src/agent/voice/action_policy.py` to represent an owned reminder timezone clarification. A reminder turn containing multiple timezone names or a correction pattern such as "Eastern, no, Central" remains pending for exactly one next turn.
2. Change continuation ordering so a correction that exclusively supplies the owned missing slot is accepted before generic topic-change rejection. Unrelated text still expires the state after one turn.
3. In `backend/src/agent/buddy_agent.py`, expose `set_reminder` only when the current deterministic policy authorizes the reminder write. Preserve the one-turn ownership and expiry rules from the existing lessons.
4. Replace capability-based success checking in `backend/src/agent/voice/spoken_action_guard.py` with receipt-based checking. Block `reminder set`, `locked in`, `all set`, `scheduled`, `I set`, and equivalent claims unless the current turn has a successful `set_reminder` receipt.
5. Add a shared text intent gate in a new `backend/src/services/action_intent_policy.py`. `backend/src/handlers/chat.py` passes the resulting exclusions through the existing `extra_excluded_tools` path in `backend/src/services/claude_client.py`. Only an explicit current-turn create/schedule request exposes `set_reminder`.
6. Make `backend/src/services/chat_completion/completion.py` and the live stream produce reminder confirmation and reminder metadata from the actual tool result. A model-authored claim without a receipt is suppressed or replaced with an honest failure/clarification response.
7. Keep `backend/src/services/tool_executor.py` semantic duplicate detection as defense in depth. It is not the primary authorization gate.

Acceptance tests include the exact reported transcript, "did the reminder set?", "why didn't you set it?", a genuine new reminder request, a correction after one unrelated turn, tool failure, timeout, and a retried identical client message.

### Phase 2: Establish shared identity and preserve surface

1. Add `conversationId` to `VoiceSessionConfig` and the token request in `lib/data/models/voice_models.dart` and `lib/data/services/voice_session_service.dart`.
2. In `lib/presentation/viewmodels/home_viewmodel.dart`, pass the already-created chat session ID into the voice token request. Do not generate a second conversation ID.
3. In `backend/src/main.py`, validate and stamp `conversation_id` and `surface` into participant metadata.
4. In `backend/src/agent/voice_agent.py`, require both fields for schema-v2 runs and pass them into the recorder and post-session pipeline.
5. In `backend/src/services/voice_session_summarizer.py`, persist `schema_version`, `voice_run_id`, `conversation_id`, and `surface`.
6. In `backend/src/handlers/history.py`, return both IDs and `surface`. Preserve legacy reads when the fields are missing.
7. Add structured warnings for any new run missing `conversation_id` or `surface`; never silently write a schema-v2 record without them.

Backend deployment is additive and goes first. The Flutter client goes second. Old clients continue through the legacy path during the compatibility window.

### Phase 3: Replace transcript-based action inference and cluttered summaries

1. Extend `backend/src/agent/voice/recorder.py` to capture safe structured action receipts from executed tools: `tool_name`, `call_id`, `success`, `occurred_at`, and the minimum result fields needed by the UI. Do not store unrestricted tool outputs.
2. Change `backend/src/services/voice_session_summarizer.py` to request structured JSON for conversational memory: `recap`, `open_loops`, `decisions`, `emotional_context`, `facts`, and `follow_up`.
3. Build the `actions` field only from successful receipts. The LLM cannot assert that a reminder or calendar event was created.
4. Store a friendly one- or two-sentence `recap` for history UI. Remove the literal heading block from user-facing output.
5. Feed future voice sessions compact structured memory, not the user-facing recap string. Keep legacy `summary` reads during rollout.
6. Add a validation invariant: a summarized reminder action without a successful reminder receipt fails closed and emits an error metric.

### Phase 4: Make the chat transcript canonical and cut redundant writes

1. Add `voiceRunId` to voice message persistence in `lib/presentation/viewmodels/home_viewmodel.dart`, `lib/data/repositories/chat_repository.dart`, the Drift message schema in `lib/data/local/app_database.dart`, and Firestore serialization in `lib/data/services/chat_backup_service.dart`.
2. Define deterministic cross-client message IDs. Prefer the finalized LiveKit transcription segment ID if it is stable and available on both sides. If that assumption fails in a focused spike, the worker must assign and publish an event ID that the client persists. Do not use transcript text or timestamps as identity.
3. During the compatibility period, keep `raw_turns` and compare a normalized ordered transcript hash against canonical messages. Emit parity metrics.
4. Add worker reconciliation after session close. It upserts only canonical messages missing from cloud backup, using the shared message ID. It never creates a second thread.
5. Change `ChatBackupService._processJob` so a message job writes only the child message. Create the parent session once at local session creation, update it at session end/title change, and coalesce intermediate metadata updates to at most once per minute for long sessions.
6. Coalesce pending `sessionUpsert` jobs by `userId + sessionId`. The current Drift table has no uniqueness contract, so add a schema migration and a unique coalescing key rather than relying on timing or a single isolate.
7. Keep child message backup immediate. If the app dies, the parent already exists and child messages remain recoverable even if its preview/count is briefly stale.
8. Move per-tool idempotency receipt state into the owning `chat_turn` document so claim, status, result, and completed-tool truth share one contract. Deprecate `backend/src/services/chat_completion/tool_idempotency.py`; do not delete the old collection or file during initial rollout.
9. Keep the bounded `chat_turns.history` snapshot only while a text response is in flight. On normal foreground completion, transactionally delete the checkpoint and cancel its delayed Cloud Task. On exceptional background completion, remove prompt/history fields in the terminal write and retain only the hydratable reply under the two-day TTL.

Write targets after this phase:

- A 12-message voice transcript uses no more than 14 client backup writes: 12 child writes, one initial parent write, and one final parent update, down from 25.
- Core voice persistence, including voice run metadata and structured summary state, stays at or below 18 writes excluding explicitly separate Aura/reflection/analytics work.
- A text status question performs zero side-effect tool writes. Its chat backup and temporary recovery bookkeeping target is five writes or fewer.

### Phase 5: Repair history, deletion, and retention contracts

1. Update voice history detail to load transcript messages through `conversation_id` and filter by `voice_run_id`.
2. Make the existing voice-run delete operation remove only messages tagged with that `voice_run_id`, its voice metadata, and derived voice summary/state. Later text in the same conversation remains.
3. Add a distinct conversation-delete contract for deleting the whole `chat_session`, all child messages, all linked voice runs, summaries, and derived state.
4. Make archival remove or redact the same logical voice-run data across every linked store. A transcript must not survive indefinitely in one collection after another copy is archived or deleted.
5. Any desktop history consumer outside this repository must adopt the new IDs, `surface`, and recap fields before the legacy transcript field is retired.

## Failure, retry, and recovery behavior

Diagram 2 of 2, failure handling:

```text
explicit write intent
        |
        v
deterministic action gate
   | allowed                 | denied or ambiguous
   v                         v
tool claim in owning turn    clarification or status response
   |
   +--> tool failure/timeout --> receipt = failed --> no success claim
   |
   +--> process crash --------> same turn retries claim idempotently
   |
   +--> tool success ---------> receipt = success
                                      |
                                      v
                              grounded confirmation
                                      |
                                      v
                            summary action and UI card

message backup failure --> local queue retry --> same deterministic message ID
worker sees missing cloud message --> reconciliation upsert, never append
```

The app remains usable offline. Failed child backups stay in the local queue with existing backoff. A stale session preview/count is acceptable temporarily; a missing transcript message is not. Worker reconciliation is a safety net, not a competing writer of new IDs.

## Migration and compatibility

1. Release additive schema-v2 fields while retaining `raw_turns` and the legacy `summary` field.
2. Run a read-only backfill dry run that matches old voice runs to chat sessions by unique temporal overlap plus ordered transcript hash. Automatically link only one-to-one, high-confidence matches.
3. Leave ambiguous legacy records untouched. Their history detail continues to read `raw_turns`.
4. Set historical `surface` to `unknown`; do not infer `desktop` from weak signals.
5. After at least one full release window, require transcript parity and zero missing-ID alerts for new runs.
6. Request separate explicit approval before any destructive production backfill, removal of legacy `raw_turns`, deletion of old idempotency records, or source-file deletion.
7. Let legacy `chat_turns` and old idempotency records expire through their TTL policies. New foreground-complete `chat_turns` records are removed immediately.

## Verification matrix

Backend tests to add or extend:

- `backend/tests/test_voice_action_policy.py`: timezone correction ownership, one-turn expiry, unrelated topic, and exact reported transcript.
- `backend/tests/test_spoken_action_guard.py`: success phrases require a successful receipt; clarification language remains allowed.
- `backend/tests/test_buddy_agent_tool_policy.py`: authorized tool exposure and failed-tool speech.
- `backend/tests/test_chat.py` and a new `backend/tests/test_action_intent_policy.py`: status/read wording cannot expose `set_reminder`; explicit create wording can.
- `backend/tests/test_chat_completion.py`: receipt-grounded confirmation and retry behavior.
- `backend/tests/test_tool_idempotency.py`: owning-turn receipt claim, crash recovery, and no second execution for the same client message.
- `backend/tests/test_voice_session_summarizer.py`: structured recap, action-receipt invariant, field round trip, and legacy compatibility.
- A new `backend/tests/test_history.py`: linked transcript reads, voice-run delete, conversation delete, and legacy fallback.

Flutter tests to add or extend:

- Voice token serialization round trip for `conversation_id` and `surface`.
- Home view model test proving the Drift chat session ID is reused for voice.
- Chat repository and backup service tests proving message jobs do not rewrite parent sessions.
- Queue migration and coalescing tests, including app restart and two simultaneous enqueue attempts.
- Restore tests with a stale parent count and complete child messages.
- Voice message serialization round trip for `voice_run_id`.

Required verification commands before handoff:

- `cd backend; python -m pytest`
- `cd backend; ruff check src tests`
- `dart run build_runner build --delete-conflicting-outputs` if Drift or Mockito generated contracts change
- `flutter analyze`
- `flutter test`
- Read-only Firestore audit against a test account, followed by a narrowly scoped production canary query after deployment approval

## Observability and rollout gates

Add structured counters and logs for:

- `voice_action_success_claim_without_receipt`
- `voice_summary_action_without_receipt`
- `voice_run_missing_conversation_id`
- `voice_run_missing_surface`
- `voice_transcript_parity_mismatch`
- `reminder_write_tool_denied_by_intent_gate`
- `reminder_duplicate_suppressed`
- `chat_backup_child_writes` and `chat_backup_parent_writes` per conversation
- reconciliation inserts, no-ops, conflicts, and failures

Roll out in this order: backend receipt and intent correctness, additive identity fields, Flutter identity propagation, structured summary, canonical transcript reconciliation, backup write reduction, then legacy cleanup. Each stage has a kill switch or backward-compatible read path. Stop rollout if a success claim lacks a receipt, a new run lacks identity/surface, transcript parity drops below 100 percent for completed sessions, or restore tests fail.



## Approval boundary

Approval of this plan authorizes implementation and tests for Phases 1 through 5 in the repository, but not deployment, production writes, destructive migration, collection cleanup, commits, pushes, or deletion of deprecated source files. Those actions require separate explicit approval under the project rules.
