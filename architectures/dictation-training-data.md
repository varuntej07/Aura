# Dictation training-data collection

## Goal and verified implementation

Aura Desktop may upload a dictation trace only after its 15-minute correction
window reaches `Finalized` under consent version 1. The backend authenticates the
Firebase user, strictly validates the fixed JSON contract and real FLAC stream,
reserves one of 500 monthly trace slots transactionally, and stores immutable
audio separately from indefinitely retained text metadata.

This implementation does not transcribe dictation, fine-tune NeMo, export ONNX,
quantize a model, or ship a model. Dictation inference remains on-device and
separate from Buddy voice, LiveKit, screen context, and the keyboard IME.

```text
[Aura Desktop: Finalized + consent v1]
        |
        | Firebase bearer token
        | PUT metadata, then PUT audio/flac
        v
[FastAPI /dictation/*]
        |-- auth --> [Firebase Admin token verification]
        |-- policy -> [strict fields, no extras, trace/digest/FLAC validation]
        |
        +-- transaction --> users/{uid}/usage/dictation_{YYYYMM}
        |                  reserve <= 500; retry of same fingerprint is free
        |
        +-- transaction --> users/{uid}/dictation_traces/{trace_id}
        |                  label, edits, consent, receipt, deletion fence
        |
        +-- create-only --> gs://DICTATION_AUDIO_BUCKET/
                           dictation/v1/{uid}/{trace_id}/{sha256}.flac
                                      |
                                      +--> 180-day prefix lifecycle DELETE
                                      |
                                      +--> admin export: FLAC + NeMo JSONL
```

## Component contracts

| Owner | Contract and durable write |
|---|---|
| `src/handlers/dictation.py` | Authenticates, rejects unknown JSON fields, enforces finalized label fields, validates 16 kHz mono PCM16 FLAC, and maps conflicts/quotas/failures to HTTP. It never logs speech text. |
| `src/services/dictation/store.py` | Atomically creates metadata plus the monthly counter. The normalized metadata SHA-256 makes an identical retry a no-op and a changed reuse of `trace_id` a 409. |
| `src/services/dictation/gcs_audio.py` | Creates the content-addressed object with generation-match zero. A precondition failure is accepted only when immutable metadata and size match. All blocking GCS calls use `asyncio.to_thread`. |
| `scripts/export_dictation_corpus.py` | Downloads exact recorded generations, verifies SHA-256, emits `groundTruth` as manifest `text`, and writes recognition versus style edits to separate files. Firebase UIDs become keyed HMAC pseudonyms. |
| `handlers/scheduler.py` | At minute 40 each hour, checks expired receipts. Only a confirmed missing exact generation flips `has_audio` false. |
| `handlers/account.py` | Deletes dictation and meeting GCS bytes before recursively deleting Firestore and finally Firebase Auth. A storage failure leaves the account retryable. |

`traceId` is accepted in the current desktop JSON body only when it exactly
matches the 24-character lowercase hexadecimal path parameter. Server state is
authoritative for `has_audio`, object path, generation, upload timestamps,
deletion state, and quota counters. Window titles, control names, field names,
IP addresses, machine IDs, and every other undeclared property are rejected.

## Failure, retry, and recovery

```text
[metadata request]
   | invalid/auth/quota -> 4xx; no Firestore write
   | first valid ------> transaction(metadata + counter)
   | identical retry --> 200, counter unchanged
   | changed/deleted ---> 409
   v
[audio request]
   | invalid digest/FLAC/size -> 4xx; no GCS write
   | create object ---------> attach exact receipt transaction -> 200
   | process dies after GCS -> retry reconciles same generation -> attach -> 200
   | concurrent DELETE -----> deletion fence wins -> exact blob cleanup -> 409
   v
[retention/delete]
   | GCS lifecycle deletes after 180 days
   | hourly exact-generation probe sees NotFound -> has_audio=false
   | explicit DELETE: pending fence -> exact blob delete -> tombstone
   | failure before tombstone -> next DELETE resumes from pending fence
   v
[terminal visibility]
   client backs off on retryable HTTP; export skips missing audio and never
   emits a dead audio_filepath; account deletion returns 500 until bytes purge
```

## Walkthroughs

Happy path:

1. Desktop sends finalized metadata. The handler validates schema and consent;
   the Firestore transaction creates the trace and increments the current UTC
   month counter. That transaction is the first durability point.
2. Desktop sends FLAC with its SHA-256. The handler verifies bytes and stream
   properties; GCS creates generation 1 with an overwrite precondition.
3. Firestore attaches that exact path and generation and sets `has_audio=true`.
   The exporter later downloads that generation, verifies its digest, and emits
   the label from `groundTruth`.

Non-obvious retry/delete race:

1. An audio PUT creates GCS bytes while a user DELETE starts. Firestore serializes
   the attach and deletion-fence transactions.
2. If attach commits first, DELETE reads and removes its exact receipt. If the
   deletion fence commits first, attach refuses the receipt and the uploader
   removes the just-created exact generation.
3. DELETE writes a minimal tombstone only after GCS deletion succeeds. A process
   death leaves `deletion_state=pending`; the next idempotent DELETE resumes, and
   every metadata or audio re-PUT remains a 409.

## Deployment and rollback order

1. Provision the separate dictation bucket in the backend region, apply
   `scripts/lifecycle-dictation-180day-delete.json`, and grant the backend service
   account `roles/storage.objectAdmin`.
2. Deploy `firestore.indexes.json` and wait for the collection-group index on
   `dictation_traces.audio_expires_at` to become ready.
3. Run `python scripts/check_dictation_storage.py --check ...` and then deploy
   the backend with `DICTATION_AUDIO_BUCKET` set.
4. Run the authenticated metadata/audio/retry/delete/quota acceptance flow
   against the candidate revision before shifting production traffic.

Before any production trace is accepted, the candidate can be rolled back
normally. After a trace exists, rollback must target a revision that retains the
dictation branch in `handlers/account.py`; an older revision would omit required
audio purge during account deletion. Existing desktop queues treat an absent
endpoint as retryable and back off. Never delete the bucket or lifecycle rule
during rollback, so already-retained audio still expires.
