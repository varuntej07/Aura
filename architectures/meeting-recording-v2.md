# Meeting Recording V2 backend

This page describes the backend implementation that serves Aura-Desktop. The
canonical product and cross-repository design remains
`Aura-Desktop/MEETING_RECORDING_V2_ARCHITECTURE.md`; if this page conflicts with
that document, the sibling-repository architecture wins.

## Trust boundary and invariants

- A capture claim belongs to an `installation_id`; `runtime_instance_id` is
  diagnostic only.
- `capture_run_id` is immutable. Every ownership recovery increments the
  monotonic `capture_fence`.
- One run and sequence identifies one plaintext SHA-256 digest. A different
  digest is terminal split-brain evidence, never last-write-wins.
- Upload acknowledgements bind the meeting, run, fence, sequence, digest,
  byte length, object path, and GCS generation.
- Completion verifies persisted receipts and ordered identities. Client counts
  alone are never authoritative.
- Firestore jobs and outbox rows exist before Cloud Tasks dispatch.
- Worker progress and publication require the current random lease token and
  attempt. A stale worker cannot publish.
- `ready` requires immutable artifacts and a passing versioned quality report,
  except dual-evidence `verified_silence`, which intentionally publishes no note.
- Successful upload, completion, transcription, and publication do not delete
  source audio.
- User deletion is explicit, exact-generation, retryable, fenced, and
  receipt-bearing.

## Authenticated desktop API

All public meeting routes require a Firebase ID token.

### Claim

`POST /meetings/claim`

The request requires:

```json
{
  "installation_id": "stable-installation-identity",
  "runtime_instance_id": "process-lifetime-runtime-identity"
}
```

`event_id`, `installation_id`, and `runtime_instance_id` are required.
`title`, `start_time`, and `end_time` remain meeting metadata.

The response includes:

```json
{
  "meeting_id": "…",
  "capture_run_id": "…",
  "capture_fence": 1,
  "lease_expires_at": "ISO-8601",
  "protocol_version": 2,
  "cap_minutes": 60,
  "max_capture_minutes": 60,
  "rejoined": false
}
```

A same-installation recovery keeps the run. It retains the fence for the same
runtime instance and increments it for a different runtime instance. A live
claim owned by another installation returns 409 `meeting_already_claimed`.

### Immutable segment upload

`PUT /meetings/{meeting_id}/capture-runs/{capture_run_id}/segments/{seq}`

Required headers:

- `Idempotency-Key`
- `X-Capture-Fence`
- `X-Content-SHA256`
- `X-Byte-Length`
- `X-Start-Ms`
- `X-Duration-Ms`
- `X-Channel-Count`
- `X-Sample-Rate-Hz`
- `X-Incomplete`

The body must be two-channel 16 kHz FLAC. The backend verifies identity headers,
body digest and length, a complete FLAC decode, channel/sample-rate identity,
and duration tolerance `max(2000 ms, 1%)` before persistence.

Audio is create-only at:

```text
audio/v2/{uid}/{meeting_id}/{capture_run_id}/{seq:06}/{plaintext_sha256}.flac
```

GCS writes use `if_generation_match=0`. If the object already exists, matching
metadata, digest, and size reconcile idempotently; any mismatch returns 409
`immutable_object_conflict` and preserves the conflict evidence.

The successful response is the persisted receipt:

```json
{
  "receipt_id": "…",
  "object": "audio/v2/…",
  "generation": "…",
  "content_sha256": "…",
  "byte_length": 12345,
  "accepted_at": "ISO-8601"
}
```

### Verified completion

`POST /meetings/{meeting_id}/capture-runs/{capture_run_id}/complete`

The request requires `Idempotency-Key`.

The request supplies `capture_fence`, `segment_count`, `total_duration_ms`,
`reason`, ordered `segment_digests`, the canonical `manifest_sha256`, and
ordered segments with their audio metrics.

Completion first finalizes the capture run, then verifies:

- the current meeting/run fence;
- contiguous sequences beginning at zero;
- unique sequence/digest identities;
- every deterministic segment document and real upload receipt;
- manifest identities against persisted digest and byte length;
- the canonical manifest hash and duration tolerance;
- absence of blocking integrity or split-brain evidence.

An identical retry returns the original completion receipt. A different retry
returns a conflict without replacing evidence. The successful transaction also
creates the authoritative job, outbox row, and audit event.

### Read, retry, and deletion

- `GET /meetings/recent` returns the bounded dashboard projection without
  transcript turns.
- `GET /meetings/{meeting_id}` returns the allowlisted detail projection.
- `POST /meetings/{meeting_id}/retry` re-drives only retryable durable work.
- `DELETE /meetings/{meeting_id}` blocks new work and runs the deletion saga.

Deletion currently returns `state`, `deletion_id`, and `completed_at`. A
retryable interruption returns 503 `meeting_deletion_retry_required`.

## Internal synthesis delivery

`POST /internal/meetings/synthesize`

Cloud Tasks calls this scheduler-OIDC-protected endpoint with `user_id`,
`meeting_id`, and the durable `job_id`. Firestore remains authoritative; the
task is only an at-least-once delivery attempt.

The handler response is part of the retry contract:

- A terminal or already-complete run returns 200, so Cloud Tasks removes that
  delivery.
- A job that cannot currently be claimed, or a worker that loses its fenced
  lease before a durable commit, returns 409 with
  `{"status":"lease_busy","retryable":true}`. The handler records structured
  INFO metadata instead of emitting an unhandled application traceback. Cloud
  Tasks retries the non-2xx delivery according to the queue retry policy.
- Other retryable infrastructure failures remain 5xx so Cloud Tasks retries
  them without treating the meeting as settled.

If the active worker finishes, the next contending delivery observes the
completed job and returns 200. If that worker dies, the 30-minute job lease
expires and a later delivery can reclaim the job with a new random lease token
and incremented attempt. Every progress, failure, and publication write still
checks that new token and attempt, so the stale worker cannot commit.

## Durable state

```text
users/{uid}/meetings/{meeting_id}
  /capture_runs/{capture_run_id}
    /segments/{seq:06d}
  /audit_events/{sequence-event}

users/{uid}/meeting_claims/{event_key}
users/{uid}/meeting_jobs/{job_id}
users/{uid}/meeting_job_outbox/{job_id}
users/{uid}/meeting_deletions/{meeting_id}
users/{uid}/usage/meetings_{YYYYMM}
```

The main state progression is:

```text
capturing -> uploaded -> synthesizing -> ready
                                  \----> needs_attention | failed

capture run: capturing -> finalized -> uploaded_verified
                         \---------> split_brain

job: pending -> dispatched -> leased -> complete
                              \-----> retry -> leased
                              \-----> failed | blocked
```

Every state-changing transaction appends a create-only audit document with
sequence and event identity, timestamps, hashed actor identity, runtime/run/fence,
job/attempt/hashed lease identity, prior and next state, artifact evidence,
stable reason code, versions, and correlation/causation IDs.

## Transcription, quality, and artifacts

Workers transcribe each segment separately. A transient provider failure retries
only the failed segment. Raw and normalized provider evidence is create-only at:

```text
transcripts/v2/{uid}/{meeting_id}/attempts/{attempt_id}/segments/{seq}.json
```

Revision artifacts are create-only under:

```text
transcripts/v2/{uid}/{meeting_id}/revisions/{revision_id}/
```

Each revision contains canonical transcript JSON, WebVTT, the
`meeting-quality-v1` report, and the note-input snapshot. Stored pointers include
the object path, generation, SHA-256, schema version, quality-policy version,
and revision.

`meeting-quality-v1` gates capture integrity, decoded FLAC format/duration,
energy/clipping/zero-ratio/VAD, empty-with-speech, one-sided
recognition, minimum word count, timing coverage, and implausibly short
long-meeting output. Malformed, truncated, missing, or unexpectedly empty
provider output is a typed failure, never inferred silence. Forced-English
retry applies when the first evidence requires it. A truthful incomplete-segment
marker is retained as `warning_codes: ["unaccounted_gap"]`; usable recognized
speech still publishes a note marked `partial: true`.

Quality enforcement is unconditional because this repository has no meeting
quality feature-flag system. Do not introduce an ad-hoc shadow flag; add one
only through the shared configuration system if shadow rollout becomes a
product requirement.

## Deletion

The server saga is:

```text
delete_requested
  -> block_new_work
  -> local coordination state persisted by the caller
  -> exact cloud-audio deletion
  -> exact transcript-artifact deletion
  -> Firestore tombstone
  -> delete_complete
```

Each object target comes from a durable receipt and is deleted using its exact
path and generation precondition. Every step is retryable and receipt-bearing.
No prefix deletion is allowed. Uploads and worker commits check deletion state,
so in-flight or stale work cannot recreate published data.

The intended cross-repository contract is resumable: the server first commits
`block_new_work`, Aura-Desktop then records its durable `local_delete` receipt,
and only an acknowledged continuation may delete cloud artifacts. That handoff
is not yet implemented. The current backend records
`local_coordination_state: server_block_committed` but continues into cloud
deletion in the same request, so it cannot prove the desktop completed its
local step. Split/acknowledge the route and add the desktop retry flow before
calling the deletion saga end-to-end.

## Operations and rollout

Environment/configuration surface:

- `MEETINGS_AUDIO_BUCKET`: immutable source-audio bucket.
- `MEETINGS_TRANSCRIPT_BUCKET`: immutable transcript-artifact bucket; defaults
  to the audio bucket when absent.
- `CLOUD_TASKS_PROJECT`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`: delivery
  queue location.
- `BACKEND_INTERNAL_URL`, `SCHEDULER_SA_EMAIL`: authenticated internal delivery.
- `SENTRY_DSN`: optional metadata-only error reporting.

Required infrastructure:

1. Provision the bucket or buckets in the expected region.
2. Grant create, metadata-read, exact-generation-read, and
   exact-generation-delete permissions to the backend service account.
3. Verify lifecycle rules do not violate the product retention policy.
4. Deploy `firestore.indexes.json`, including meeting outbox,
   protocol-version, and capture-run reconciliation fields. This is a separate
   manual step (`firebase deploy --only firestore:indexes`) that `deploy.sh`
   does not run, and it must complete **before** any backend revision carrying
   the reconciliation query ships. Reconciliation filters the `meetings`
   collection group on `protocol_version`; Firestore's default single-field
   config covers COLLECTION scope only, so the explicit COLLECTION_GROUP
   override must be READY, not merely submitted. Shipping first fails every
   hourly tick with `meeting_reconciliation_failed` ("400: query requires a
   collection-group index") until the index exists, which is exactly what
   happened on 2026-07-30 for 35 hours. `deploy.sh` now gates on this.
5. Enable the existing `meetings.expires_at` TTL policy for non-Pro public rows.
6. Configure Cloud Tasks OIDC delivery to `/internal/meetings/synthesize`.
7. Keep `/scheduler/tick` active. It dispatches pending meeting outbox records
   every five minutes at minute `% 5 == 2` and runs reconciliation hourly at
   minute `27`.

Roll out in this order:

```text
desktop runtime lease
  -> backend immutable ingest
  -> V2 workers
  -> publication quality enforcement
```

Useful verification:

```powershell
cd backend
python -c "import src.main; print('OK')"
python scripts/check_meeting_storage.py --check
python -m pytest -q
ruff check src tests
```

Production validation must also inspect registered V2 routes, bucket generation
preconditions, Firestore indexes, Cloud Tasks authentication, scheduler
delivery, a duplicate synthesis delivery returning handled 409 before a later
200 without an application traceback, and a packaged Aura-Desktop
capture/recovery/deletion flow.

## Remaining architecture gaps

These are implementation gaps, not optional documentation debt:

- The current `DELETE` route advances from `block_new_work` into cloud deletion
  in one request. It needs a resumable desktop `local_delete` receipt/acknowledge
  boundary.
- Deletion target discovery currently depends on the verified completion
  `segment_count`. Deleting during capture/upload can therefore miss already
  created or receipted segment objects. The saga must reconcile every exact
  V2 object path and generation before writing `delete_complete`.
- Reconciliation does not yet report upload jobs overdue by retry deadline,
  quality failures partitioned by app/device/model/language, or local retention
  deletion overdue/early.
- The sanitized support incident-bundle export described by the source
  architecture is not implemented.
- Quality evaluation is immediately enforced and has no
  `retry_transcription` decision or shadow-evaluation mode. The existing
  configuration system has no meeting-quality rollout control.
- The source architecture's Phase C/D/E checklists and final status still say
  the backend is blocked/read-only. That sibling file must be updated in the
  Aura-Desktop repository after both repositories satisfy the deletion and
  rollout acceptance criteria.
