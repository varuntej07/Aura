# Notion Capture abstraction audit (2026-09-04)

Scope: the Phase 1 + Phase 2 Notion Capture feature across both repos, on
`feat/notion-capture` (Aura commits bb35299 + c91a7fa, Aura-Desktop 4469a04 +
2b227ec). Criteria per the running log: wrong-layer logic, duplicated helpers,
leaky abstractions, single-entry-point violations, missing abstractions.
Unlike earlier entries, this audit ran together with a hardening pass, so each
finding is marked FIXED (in that same pass) or OPEN (direction only).

## Fixed in the hardening pass

1. **Run-identity scheme copy-pasted, and the copies collided.** FIXED.
   `agent/voice/research_dispatch.py` reimplemented `tool_executor.py`'s
   `voice:{session}:{sha256(request)[:16]}` byte for byte, so `start_research`
   and `research_to_notion` minted the SAME run id for the same words in one
   session; `create_run`'s replay branch then silently dropped the delivery
   binding while Buddy claimed "I'll save it to X". Now one helper
   (`services/research/run_identity.py`) whose digest includes the tool name,
   used by both callers, plus the dead-run/wrong-binding replay guard ported
   to the dispatch path. This is the audit's thesis case: the duplication was
   not a style problem, it WAS the bug.

2. **The voice worker's backend-HTTP layer existed twice.** FIXED. `_headers`
   was byte-identical and `_backend_request`/`_post_backend` differed only in
   409 mapping, with three different timeout constants for the same two
   routes. Now one module, `agent/voice/notion_backend.py`.

3. **The resolve->bind/ask/propose decision tree was written twice** (capture
   and dispatch, same state machine, different copy). FIXED: shared
   `decide_destination` parameterized by a two-string `DestinationCopy`;
   spoken sentences stay with each tool.

4. **`buddy_agent`'s two ~100-line twin executors.** FIXED. The lock / LRU
   cache / telemetry / span / shield-through-cancellation / action-receipt
   skeleton of `_execute_notion_capture` and `_execute_research_dispatch` is
   now one `_run_finalized_notion_action` with per-tool callbacks, so the
   exactly-once ordering can no longer be fixed in one tool and missed in the
   other.

5. **Unbounded module-global caches and locks.** FIXED. The resolve title
   cache and the schema cache share one bounded policy
   (`services/notion/cache_policy.py`); the schema cache is now keyed by
   (uid, data_source_id) instead of the id alone and is evicted on database
   create; `notion_connector._REFRESH_LOCKS` is capped with idle eviction.

6. **Per-endpoint 429 handling on one of five Notion endpoints.** FIXED. The
   connector's `authorized_request` now owns 429/transient-5xx retries with
   Retry-After and jittered backoff (per developers.notion.com guidance) for
   every caller, with an explicit `idempotent` opt-in for retryable writes.
   The one-off handler in `write.py` is gone.

7. **Dead pagination in `resolve.py`.** FIXED. The `while` loop could execute
   exactly once (`_MAX_DATA_SOURCES == _SEARCH_PAGE_SIZE`); it is now one
   documented single-page fetch. The sequential per-title embed loop it fed is
   also gone (batch `embed_texts`).

8. **`handlers/notion.py` duplicate import; stale `maxAttempts is 3` comment
   in `handlers/research.py`.** FIXED.

9. **Desktop: the `notion.saved` caption was built in the wrong hook and its
   return value discarded.** FIXED. `useScreenSight` (legacy armed path)
   carried `savedConfirmation` state nothing rendered, so the shipped feature
   had NO save confirmation; the handler now lives in the live
   `useTurnScreenCapture` notice path, deduped by `page_id`, and the dead
   plumbing is deleted.

10. **Desktop: the 3x connector enable/disable flow and 3x status readers.**
    FIXED. One `runEnable`/`runAction` pair in `useConnectors.ts`
    parameterized by copy; one `readConnectorStatus`/`postConnectorAction`
    pair in `connectors.ts` (which also gave Calendar the 409 contract its
    drifted copy checked at only one of three call sites, and a hard timeout
    everywhere).

11. **Desktop: the privacy gate lived only in React.** FIXED. `voiceScreenContext`
    was enforced solely by not mounting the hook with a room, while
    `security.rs` calls itself "the one authorization decision point". The
    setting is now mirrored into `SecurityState` (`set_voice_screen_context`)
    and `Operation::CaptureTurnScreen` denies with `ScreenContextDisabled`.

## Open (direction only, not fixed here)

12. **`FIELD_CAPS` in `agentData.ts` is one flat map across all message
    types**, so `database_name`'s cap silently applies to any future message
    reusing the field name. Direction: fold the caps into the existing
    per-type validation switch. Left open deliberately: restructuring the
    security validator wholesale under the test freeze trades a cosmetic win
    for real regression risk.

13. **`useTurnScreenCapture` and `useScreenSight` still share the frame
    counter / geometry LRU / `streamBytes` attribute block.** The dead halves
    are gone (finding 9), but the transport plumbing remains duplicated.
    Direction: a `publishFrame` helper in `screenFrame.ts`; the two hooks keep
    their different capture commands and triggers.

14. **`ResearchPage.tsx` is a ~600-line file mixing every layer** (page shell,
    detail view, brief renderer, drawer, polling, mutation orchestration).
    Reads honour the three-layer dashboard split via `useDashboardResource`;
    writes bypass it with an ad-hoc `mutate`. Direction: split the detail view
    and brief renderer out, and give mutations a cache-layer path. The polling
    loop did gain error-backoff in the fix pass.

15. **Screen-context contract-version drift has no guard.** `screenContext.ts`
    names three files that must agree on `SCREEN_CONTEXT_SCHEMA_VERSION` and
    hand-maintains the quality-reason union against `uia/contract.rs`.
    Direction: a build-time assertion once the freeze lifts.

16. **`notify_result` has two owners of body copy** (the `_COPY` table plus
    the inline Notion-delivery override, now grown a third branch for
    never-attempted and unreceipted outcomes). Direction: a
    `delivery_body(run_doc)` helper next to the table; not done now because
    the branch logic was the subject of a correctness fix this pass and
    another reshape would re-risk it.

17. **`CLARIFICATION_ROUNDS` looks like a dead field but is not.**
    `classify_plan` enforces the round cap from `len(clarification_answers)`
    while `store.advance` still writes `F.CLARIFICATION_ROUNDS` — however the
    stored field IS read, as the `clarify{rounds}` notify-job ordinal that
    keeps a second question's job from colliding with the first. Two counters,
    two jobs, intentionally different sources. Recorded here so the next
    audit does not re-flag it; direction: a comment now exists in neither
    place, add one when store.py is next touched.

18. **Gmail/Calendar rows still hide `last_error`.** The Notion row now
    renders its attention line (`db-connector-attention`); the other two
    connectors parse the field and drop it. Direction: same one-liner per row.
