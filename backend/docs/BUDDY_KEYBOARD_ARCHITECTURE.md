# Buddy Keyboard architecture

Status: implemented source architecture, audited 2026-08-16

This is the canonical architecture and data-flow document for Aura's system-wide Android
keyboard. It describes the Kotlin `InputMethodService`, local suggestion stack, ONNX reranker,
local personalization, encrypted persistence, native keyboard settings, and performance harness
as they exist in this repository.

It does **not** claim that an uncommitted physical-device result exists. The source contains the
acceptance harness and gates, but the repository contains no `latency-report.json`, ORT profile,
or equivalent result bundle proving p99 latency or the execution provider selected on a device.

## Scope and invariants

- The keyboard is a native Android IME implemented by
  [`BuddyImeService`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt)
  and declared as an `InputMethodService` in
  [`AndroidManifest.xml`](../../android/app/src/main/AndroidManifest.xml). Android documents this
  as the system-wide keyboard integration point in
  [Create an input method](https://developer.android.com/develop/ui/views/touch-and-input/creating-input-method).
- Ordinary typing, lexical prediction, ONNX reranking, and personalization use local data only.
- A key does not wait for dictionary search, correction search, ONNX inference, persistence,
  Firestore, or an HTTP response.
- Glyph delivery and model work are decoupled. If the model never loads, the keyboard remains a
  usable lexical keyboard.
- Voice behavior is outside this architecture change. Existing
  [`KeyboardVoiceController`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceController.kt),
  [`KeyboardVoiceHandoff`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceHandoff.kt),
  and audio code remain a separate, user-invoked path and are not modified by this design.
- The explicit Buddy draft action is separate from prediction: it may use
  [`KeyboardDraftClient`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardDraftClient.kt);
  automatic suggestions do not. Its **editor mutation** is not separate, because a writing tool
  deletes text the user already has in another app's field. That contract is
  [Writing tools: span selection and replacement](#writing-tools-span-selection-and-replacement).
- The IME runs in the application's default Android process and UID. The APK declares Internet
  permission for other Aura capabilities, so "local prediction" is an application data-flow
  property, not an OS-enforced no-network sandbox.

## Architecture and primary data flow

```text
 Android host app                                              Aura APK process
 +--------------------+                         +---------------------------------------+
 | focused text field |<--- InputConnection ----| IME main thread / BuddyImeService     |
 | host glyph + frame |     commit/delete only  |                                       |
 +--------------------+                         | key touch -> WordComposer -> commit    |
                                                |       |                 |              |
                                                |       |                 +-> trace      |
                                                |       v                                |
                                                | PredictionCoordinator                  |
                                                | latest request + generation counter    |
                                                +-------------------+-------------------+
                                                                    |
                                               one AuraImePrediction worker
                                                                    v
 +---------------------+   +------------------+   +-------------------------------+
 | mmap packed radix   |   | immutable local  |   | Android system dictionary     |
 | trie: en_us.pdict   |   | personal snapshot|   | snapshot/cache                |
 +----------+----------+   +---------+--------+   +---------------+---------------+
            \________________________|_____________________________/
                                     v
                       lexical candidates + cached correction
                                     |
                         24 ms lexical publication
                                     |
                      suggestion chips on IME main thread
                                     |
                    180 ms deferred stage, newest input only
                                     v
                      one warmed ORT session, max 8 candidates
                      fixed tensors -> reorder only -> chips

 Separator / accepted suggestion / undo / manual edit
                                     |
                                     v
                         bounded personalization queue
                                     |
                         one AuraImePersonalization worker
                                     v
        filter -> pending evidence -> mature lexeme/n-gram/correction -> decay/evict
                          |                              |
                          v                              v
              immutable snapshot generation    AES-256-GCM + AtomicFile
                 read by prediction worker         noBackupFilesDir
```

The UI thread owns touch handling, the composing buffer, the minimum required
[`InputConnection`](https://developer.android.com/reference/android/view/inputmethod/InputConnection)
mutation, and view updates. `AuraImePrediction` owns lexical and neural work.
`AuraImePersonalization` owns migration, reduction, snapshot publication, and persistence.

## Component contracts

| Component | Thread | Contract |
|---|---|---|
| [`KeyTouchHandler`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/input/KeyTouchHandler.kt) | IME main | Records `ACTION_DOWN`; dispatches the completed key action. |
| [`BuddyImeService`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt) | IME main | Updates `WordComposer`, performs the editor mutation, publishes latest prediction state, and applies current results. |
| [`PredictionCoordinator`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PredictionCoordinator.kt) | Main publisher; prediction worker | Holds one latest request, one active cancellation handle, and a monotonically increasing generation. No executor queue accumulates. |
| [`LexicalPredictionEngine`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/LexicalPredictionEngine.kt) | Prediction worker | Produces deterministic current-word or next-word candidates, a cached correction decision, and optional deferred neural ordering. |
| [`PackedDictionary`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PackedDictionary.kt) | Prediction worker | Read-only Patricia/radix traversal for prefix completion and trie-guided bounded edit search. |
| [`OnDeviceReranker`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/OnDeviceReranker.kt) | Prediction worker | Owns one lazy, warmed ORT session and reusable fixed-size input/output storage; reorders candidates only. |
| [`LocalPersonalizationDictionary`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalDictionary.kt) | Lock-free reads; personalization worker writes | Accepts bounded learning events and atomically publishes immutable snapshots. Overflow drops learning, not input. |
| [`PersonalizationState`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalizationState.kt) | Personalization worker | Filters evidence, delays maturation, assigns provenance, applies decay, and enforces caps. |
| [`EncryptedPersonalizationStore`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/EncryptedPersonalizationStore.kt) | Personalization worker | Encrypts and atomically replaces the local snapshot; handles legacy migration, corruption, and clear. |
| [`KeyboardPerformanceTrace`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/performance/KeyboardPerformanceTrace.kt) | All keyboard threads | Emits static trace slices/counters without building a per-key logging payload. |
| [`KeyboardSettingsActivity`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/settings/KeyboardSettingsActivity.kt) | Android activity main | Provides IME-specific typing, appearance, privacy/reset, and content-free diagnostics controls without starting Flutter. |
| [`KeyboardSettingsStore`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/settings/KeyboardSettings.kt) | Activity writes; IME reads per focus | Stores behavior preferences only. It never stores typed text or learned vocabulary. |

## Steady-state keystroke path

For a normal letter key, `BuddyImeService` performs only the work required to make the typed glyph
visible:

1. `ACTION_DOWN` establishes the latency trace identity.
2. `ACTION_UP` dispatches the character.
3. `WordComposer` updates the in-memory word/cursor state.
4. One traced `InputConnection.commitText` sends the character to the focused editor.
5. The service publishes a small immutable prediction request.
6. Shift/key visuals are updated only if their visual mode changed.

The steady-state letter path does not synchronously read surrounding editor text. Cursor mismatch,
undo/manual correction, punctuation, stale suggestion commit validation, and capitalization recovery
can read editor context because they are reconciliation paths rather than the common glyph path.

The main-thread publication performed by `PredictionCoordinator` is a generation increment, an
atomic latest-request replacement, cancellation of the active request, and an unpark of the single
worker. At most the newest lexical request and its deferred continuation are logically pending.

## Suggestion tiers and complexity

Aura uses a bounded hybrid. The trie supplies candidates, local n-grams supply personal next-word
results, and the neural model optionally reranks the lexical set. The neural tier does not replace
the dictionary or generate free-form text.

| Tier | Data structure / algorithm | Per-request bound and tradeoff |
|---|---|---|
| Base completion | Memory-mapped Patricia/radix trie with cached top IDs | Prefix traversal is proportional to the prefix path, with binary search over node edges; decoding is capped to the requested cached candidates. It avoids scanning 30,000 words at the cost of a generated binary asset. |
| Personal completion | Immutable [`PersonalPrefixIndex`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalPrefixIndex.kt) with top-eight caches | Prefix traversal plus at most eight results. Snapshot rebuild moves mutation cost off prediction reads. |
| System vocabulary | [`SystemUserDictionary`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/SystemUserDictionary.kt) cache | Includes words known to Android without querying a content provider on each key. Freshness work is initiated away from the character commit. |
| Autocorrect | Trie-guided, bounded Damerau-Levenshtein traversal plus [`KeyboardGeometry`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/KeyboardGeometry.kt) | Work follows reachable trie/edit states and checks cancellation every 64 work units. It avoids an all-word edit-distance scan; edit-distance-two work is deferred. |
| Next word | Personal unigram/bigram/trigram snapshot, then a 99-word static unigram fallback | Context lookup is bounded by snapshot caps and a top-eight result limit. It is cheap and personal but initially less expressive than a generative language model. |
| Neural | Fixed eight-candidate, eight-feature, 121-parameter feed-forward reranker | Compute and storage are capped. It can improve ordering but cannot recover a missing candidate. |

This follows the verifiable LatinIME principle of a compact binary dictionary backed by native
dictionary machinery, visible in AOSP's
[`BinaryDictionary.java`](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/master/java/src/com/android/inputmethod/latin/BinaryDictionary.java)
and [native build definition](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/master/native/jni/Android.bp).
Aura's exact Kotlin format and ranking policy are its own implementation. AOSP does not establish
the private internals of Gboard, SwiftKey, or Fleksy.

## Scheduling, debounce, cancellation, and staleness

- Lexical publication delay: **24 ms**.
- Deferred edit-distance-two and neural delay: **180 ms**.
- Maximum active prediction work: **one** request.
- Cancellation: cooperative token checks throughout lexical work; ORT receives
  `RunOptions.setTerminate(true)` when active neural work is superseded.
- Staleness: every request and result carries a generation. The main thread applies a result only
  when the generation is still current.
- Queue policy: new input replaces the atomic latest request; it is not appended behind old input.
- Separator policy: correction commit consumes the already cached decision only when its raw word,
  generation, and manual-correction state match. Missing or stale cache means "keep what the user
  typed," not "search synchronously now."
- The recent-commit reconciliation window is **3,000 ms**; it is an edit/undo association guard,
  not prediction debounce.

Historical LatinIME is useful evidence for asynchronous suggestion computation, but not an exact
template. Its source includes a message-driven
[`LatinIME` UI handler](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/5657746/java/src/com/android/inputmethod/latin/LatinIME.java),
an older [100 ms suggestion-delay change](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/b528e2511377b58f2c7e0d1046b3645a3716e0ab%5E2..b528e2511377b58f2c7e0d1046b3645a3716e0ab/),
and [sequence-based stale-result rejection](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/f603fa1f0e8fc909bf2e1bc6e2d0c9b5a01c02c6%5E1..f603fa1f0e8fc909bf2e1bc6e2d0c9b5a01c02c6/).
Aura additionally terminates superseded work so scarce mobile CPU is not spent merely to discard
its output.

## Dictionary artifact

The checked-in base dictionary is
[`en_us.pdict`](../../android/app/src/main/assets/dictionaries/en_us.pdict), built deterministically
by [`build_packed_dictionary.py`](../../android/tools/keyboard_dictionary/build_packed_dictionary.py)
from the hermitdave/FrequencyWords corpus, compiled ahead of time into
[`en_us.pdict`](../../android/app/src/main/assets/dictionaries/en_us.pdict); the raw word list
is not shipped in the app.

| Property | Measured value |
|---|---:|
| Packed asset size | 2,240,030 bytes |
| Source word-list size | 407,151 bytes |
| Packed SHA-256 | `8341c99ade5a407d9731e9ac3deb04792abb506d9ff4ffcf28326027b81262c5` |
| Format magic / version | `AURAPD01` / 1 |
| Words | 30,000 |
| Nodes / edges | 35,925 / 35,924 |
| Cached top IDs | 77,783, with up to 8 per node |
| Label / word bytes | 70,050 / 205,492 |

[`BaseDictionary`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/BaseDictionary.kt)
opens the uncompressed APK asset and maps it read-only with `FileChannel.map`. Loading happens once
away from the UI hot path and fails open. The Gradle packaging configuration leaves `pdict` and
`onnx` uncompressed so both can be memory-mapped.

The source list is the English portion of
[`hermitdave/FrequencyWords`](https://github.com/hermitdave/FrequencyWords); the checked-in
[`LICENSE`](../../android/app/src/main/assets/dictionaries/LICENSE) records the corpus license.
The Patricia design is grounded in Morrison's original
[PATRICIA paper](https://dl.acm.org/doi/10.1145/321479.321481), while the exact on-disk format is
Aura-specific.

## ONNX reranker

[`OnDeviceReranker`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/OnDeviceReranker.kt)
uses ONNX Runtime Android **1.27.0**, pinned in
[`build.gradle.kts`](../../android/app/build.gradle.kts). The session is created lazily on the
prediction worker, warmed once, and reused. Telemetry is disabled; execution mode is sequential;
graph optimization is enabled; intra-op and inter-op thread counts are each one.

The implementation requests CPU by default and can request XNNPACK with one thread. A requested
provider is not proof that every graph node ran there. The benchmark's ORT profiling step parses
the per-node `provider` field to establish the provider actually selected at runtime. No resulting
profile is committed, so actual provider selection on a physical target device is currently
**unverified from repository artifacts**.

The checked-in model and provenance are
[`keyboard_reranker_int8.onnx`](../../android/app/src/main/assets/models/keyboard_reranker_int8.onnx)
and
[`keyboard_reranker_provenance.json`](../../android/app/src/main/assets/models/keyboard_reranker_provenance.json).

| Property | Measured / recorded value |
|---|---:|
| Model size | 2,911 bytes |
| SHA-256 | `dd544d7ca84b62bd5ea28be71fb7fde1521ecc8dc9735a746687c93bc992dec3` |
| Parameters | 121 |
| Quantization | dynamic int8 weights |
| Candidate cap | 8 |
| Features per candidate | 8 |
| Hidden units | 12 |
| Deterministic seed | 20260815 |
| Synthetic train / validation groups | 4,096 / 1,024 |
| Synthetic validation top-1 / top-3 | 0.861328 / 0.903320 |

The eight features are lexical rank, log frequency, common-prefix ratio, edit similarity, keyboard
proximity, length similarity, personal-source membership, and next-word mode. The synthetic
validation figures prove reproducible model construction on that generated task; they do not prove
real-user keyboard quality.

The input and output are fixed direct buffers for eight candidates. Input/output tensors and maps
are retained across calls, and the reusable scores array avoids per-call result allocation. This
implements the allocation-control purpose of ONNX Runtime
[I/O binding](https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html) at the Java
boundary. Relevant ORT primary references are the
[mobile guide](https://onnxruntime.ai/docs/tutorials/mobile/),
[execution-provider overview](https://onnxruntime.ai/docs/execution-providers/),
[Java session options](https://onnxruntime.ai/docs/api/java/ai/onnxruntime/OrtSession.SessionOptions.html),
[threading guide](https://onnxruntime.ai/docs/performance/tune-performance/threading.html), and
[Android build guide](https://onnxruntime.ai/docs/build/android.html).

## Local personalization

Learning is evidence-based rather than "save every token." Events have explicit provenance:
manual typing, explicit suggestion acceptance, automatic correction, autocorrect undo, manual
correction, word deletion, explicit add/remove, and legacy import.

### Eligibility and privacy

[`FieldProfile`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/FieldProfile.kt)
enforces the field boundary:

- Secure/password/PIN fields: no prediction, autocorrect, or learning.
- `NO_PERSONALIZED_LEARNING` and sensitive private/incognito/payment/OTP metadata: no learning.
- QWERTY prose: prediction, autocorrect, and learning are eligible.
- Email and URL fields: prediction can be eligible; autocorrect and learning are not.
- A no-suggestions flag suppresses prediction.

Token filtering rejects lengths outside 2-48, whitespace/control characters, URL/email markers,
characters other than letters/apostrophe/hyphen, tokens with less than 70% letters, and high-entropy
values.

### Evidence and maturation

| Signal | State transition |
|---|---|
| Manual word or accepted suggestion | Held as pending evidence for 1,500 ms. Delete/undo prevents maturation. |
| Mature positive | Adds lexeme evidence and eligible unigram/bigram/trigram evidence. |
| Automatic autocorrect | Records provenance only; it does not teach vocabulary by itself. |
| Autocorrect undo | Cancels pending credit and adds negative correction evidence. |
| Manual replacement | Adds independent correction-pair evidence. |
| Correction maturity | Net evidence of 3, with repeated evidence separated by a 30,000 ms independence window. |
| Trigram eligibility | Aggregate context evidence of at least 2. |

Lexeme weights are manual `1.0`, accepted suggestion `0.8`, explicit add `4.0`, and legacy import
`0.25`. Lexeme evidence uses 90-day exponential decay; n-gram evidence uses 45-day decay. Caps are
10,000 lexemes, 20,000 n-grams, 5,000 corrections, 128 pending events, and eight results per
context. Eviction removes the lowest decayed evidence.

The runtime command queue contains at most 256 record events. If it fills, new learning is dropped;
the key path is never blocked. The personalization worker batches persistence after 750 ms idle and
publishes a new immutable snapshot generation for prediction reads.

## Encrypted storage, reset, and migration

The v2 snapshot is `buddy_keyboard_personalization.v2.enc` under Android's `noBackupFilesDir`.
[`EncryptedPersonalizationStore`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/EncryptedPersonalizationStore.kt)
uses AES-256-GCM with Android Keystore alias `aura_keyboard_personalization_v2`. Authenticated
additional data binds the file magic, version, and generation. `AtomicFile` provides crash-safe
replacement, and plaintext is capped at 8 MiB.

Migration reads the legacy `buddy_personal_dictionary.db` once. It validates a read of the encrypted
v2 result before deleting the legacy database, WAL, SHM, and journal. A migration failure leaves v1
untouched for retry and continues memory-only; it never falls back to writing plaintext. Corruption
destroys the unusable encrypted artifact/key and starts with an empty snapshot. Persistence failure
also degrades to memory-only rather than blocking typing.

`clearAll()` immediately publishes an empty snapshot with a new epoch, then the worker removes v2,
legacy artifacts, and the Keystore key and verifies absence. The epoch prevents already queued work
from resurrecting cleared data. `KeyboardSettingsActivity` exposes this through a confirmation and
an explicit success/failure state. The activity and IME share one process-local personalization
owner, preventing an older in-memory dictionary from persisting cleared state again.

## Native keyboard settings

Android's IME metadata names `KeyboardSettingsActivity`, so the system keyboard selector can open
Aura Keyboard settings directly without booting Flutter. The keyboard toolbar's palette action
opens the same activity. This surface is deliberately separate from Aura account/cloud memory.

The settings store contains booleans and a theme choice only. Suggestions, autocorrect, automatic
learning, and use of local personalization are independently controlled. System/Light/Dark selects
the corresponding Android resource configuration, so keyboard views and drawables remain
theme-derived. Optional haptic, keypress sound, and the shared key-preview popup are applied when
the IME rebuilds for the next focused field.

Developer diagnostics are hidden behind an Advanced switch. They read the live process-memory ONNX
state, configured provider, model version, inference count, and error category. No typed text is
accepted, persisted, displayed, or logged by this diagnostics path.

## Writing tools: span selection and replacement

A writing tool (Proofread, Rephrase, Professional, Friendly, Translate) transforms text the user
already has in the host field and puts the result back **in place of it**. The rule that makes that
safe is one sentence: *the span sent to the model and the span deleted from the field are the same
object.* `BuddyImeService.DraftTarget` is that object; `buildDraftTarget` is the only thing that
creates one, and `insertDraft` is the only thing that acts on one.

Reply is the exception and always appends: its source is the clipboard, because an IME cannot read
the chat bubble being answered, so there is nothing on screen to replace. `buildDraftTarget`
returns null for it.

### Choosing the span

| Order | Condition | Span |
|---|---|---|
| 1 | Action is Reply | None. Append at the cursor. |
| 2 | The host has a selection | The selection. The user stated the blast radius. Over `DRAFT_MAX_CHARS` the keyboard refuses and asks for a smaller selection, rather than transforming a prefix and deleting the whole thing. |
| 3 | No selection, whole field under `DRAFT_AUTO_WINDOW_MAX`, neither read truncated | The whole field, before and after the cursor. |
| 4 | Otherwise | A boundary-snapped window around the cursor: paragraph break, else sentence end, else word gap. The text outside it is sent as `context_before` / `context_after` and is never replaced. |

Two different ceilings, on purpose:

- `DRAFT_MAX_CHARS` (2000) mirrors `CONTEXT_MAX_CHARS` in
  [`drafter.py`](../src/services/keyboard/drafter.py). Past it the server truncates silently, so the
  model would never see the tail while the keyboard deleted it.
- `DRAFT_AUTO_WINDOW_MAX` (1200) bounds a span the keyboard picks *by itself*. The draft runs on the
  lite tier under `KEYBOARD_DRAFT_TIMEOUT_SECONDS`, and a grammar pass emits about as many tokens as
  it consumes, so a 2000-character rewrite nobody asked for is a timeout risk. An explicit selection
  is the user's own call and gets the full 2000.

`getTextBeforeCursor` / `getTextAfterCursor` are best-effort: a host may return less than asked, and
they return null once the connection is stale. A read that comes back at exactly the requested
length is therefore treated as possibly truncated, and its outer edge is never used as a span
boundary — the window snaps inward to the first real one instead.

### Applying it

`insertDraft` re-reads the span and compares it to what was sent **before deleting anything**. If
the user typed while the request was in flight, the host edited the field, or focus moved to a
different field (`inputSession` stamp), it deletes nothing, inserts nothing, and says the text
changed. Appending on a mismatch is the exact duplicate-text failure this path exists to prevent.

The delete uses `deleteSurroundingTextInCodePoints`, not `deleteSurroundingText`: the latter counts
UTF-16 units and will split a surrogate pair sitting on a span edge into a replacement glyph. Delete
and commit are wrapped in `beginBatchEdit` / `endBatchEdit` so the host sees one edit, then
`markResync()` re-seeds the cursor from the next `onUpdateSelection`.

The wire fields `selected_text` and `context_after` already existed in `DraftRequest` and are
already validated server-side; the client simply began sending them. Older installed keyboards that
send only `context_before` continue to work unchanged, so this is not a cross-repo contract change.

## Failure, retry, and recovery flow

```text
 New key arrives
      |
      +--> old lexical/deferred work active? -- yes --> cancel token + ORT terminate
      |                                                   |
      |                                                   v
      |                                     worker observes cancellation
      |                                                   |
      +--> publish newest generation <--------------------+
      |             |
      |             +--> stale result returns --> generation mismatch --> discard
      |             |
      |             +--> dictionary unavailable --> empty/base fallback; typing continues
      |             |
      |             +--> ORT init/run fails --> lexical order only; typing continues
      |
 Learning event
      |
      +--> sensitive/ineligible --> discard before storage
      +--> queue full -----------> drop learning event; typing continues
      +--> persist fails --------> retain memory snapshot; retry after later activity
      +--> encrypted corruption -> remove artifact/key -> publish empty snapshot
      +--> clear requested ------> advance epoch -> empty snapshot -> delete + verify
```

No recovery branch waits on the key commit. Prediction and learning failures reduce suggestion
quality or durability; they do not remove the user's ability to type.

## Walkthrough 1: sustained fast typing

The user types the next letter while the previous 180 ms deferred stage is running. The main thread
updates `WordComposer`, commits the glyph through `InputConnection`, and publishes generation N+1.
The coordinator replaces the latest request, terminates the active neural call if necessary, and
wakes its worker. The host application can render the new glyph without waiting. The worker later
publishes lexical results for N+1, followed by an optional neural reorder only if N+1 is still
current. Stable suggestion chip listeners remain installed; only changed chip text, visibility, or
accent is written.

## Walkthrough 2: autocorrect, undo, and learning

The lexical stage computes a correction candidate in advance. When the user types a separator,
the main thread checks the cached decision for the exact word and generation. A missing or stale
decision leaves the typed word unchanged. A valid decision can be committed without running a
search on the separator path. If the user immediately undoes it, the service records undo evidence,
cancels pending positive credit, and penalizes that correction pair. Repeated independent manual
corrections must reach the evidence threshold before the learned pair can affect future ranking.

## Performance instrumentation and acceptance gate

The dedicated [`ime-benchmark`](../../android/ime-benchmark) module and
[`run_profile.ps1`](../../android/ime-benchmark/run_profile.ps1) implement the physical-device
profile/release procedure. The runner rejects emulators and ambiguous device selection, captures
device/build/thermal identity, enables Aura as the selected IME, runs the workload, captures
Perfetto, samples process and UID I/O counters, and produces a parsed report.

The deterministic workload in
[`ImeLatencyWorkloadTest`](../../android/ime-benchmark/src/androidTest/kotlin/dev/varuntej/aura/imebenchmark/ImeLatencyWorkloadTest.kt)
types `teh quick brown fox jumps over the lazy dog. aura helps me write, without delay.` 32 times at
24, 28, 32, 36, 42, and 50 ms inter-key intervals, with a 5,000 ms model warmup, 240 ms correction
settle window, and an injected edit every 89 events.

[`parse_trace.py`](../../android/ime-benchmark/tools/parse_trace.py) calculates each latency from
injected `ACTION_DOWN` to the actual FrameTimeline presentation containing the host glyph. It reports
key-handler, `InputConnection`, lexical/deferred prediction, glyph draw, FrameTimeline, RenderThread,
ORT, allocation, GC, PSS, process disk, and UID network evidence.

The acceptance gate is:

- all injected events traced and presented;
- no dropped, duplicated, or reordered output;
- **p99 keystroke-to-presented-glyph under 16 ms**;
- ONNX warmed and active;
- maximum prediction pending count at most 2 and active count at most 1;
- all key handlers traced; and
- zero measured process read/write bytes, UID receive/transmit bytes, and traced disk/network events
  during the steady-state typing interval.

### Evidence ledger

| Claim | Repository evidence | Status |
|---|---|---|
| Native system-wide Android IME | Manifest + `BuddyImeService` | Implemented |
| Latest-only prediction and real cancellation | `PredictionCoordinator`, `PredictionCancellation`, ORT termination | Implemented |
| Mapped packed dictionary and fixed model artifacts | Assets, loaders, builders, provenance | Implemented and measured in tree |
| Local correction/personalization policy | Prediction and personalization source | Implemented |
| Encrypted no-backup persistence and migration | `EncryptedPersonalizationStore`, backup rules | Implemented |
| Physical-device p99 < 16 ms | Benchmark gate exists; no result bundle committed | **Unverified on device** |
| Actual ORT execution provider | ORT profiler/analyzer exists; no device profile committed | **Unverified on device** |
| User-visible delete-personalization action | Native settings confirmation + shared dictionary owner + `clearAll()` | Implemented in source; rendered/device behavior unverified |
| Federated learning | Delta interface only; no serializer, uploader, aggregation, or rollout code | **Future boundary, not implemented** |

## Federated boundary

[`LocalPersonalizationDelta`](../../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/LocalPersonalizationDelta.kt)
defines an in-process delta abstraction only. There is deliberately no current transport, Cloud
Storage upload, Firestore pointer document, server aggregation, or versioned model rollout in this
keyboard implementation. No claim of federated learning should be made until those components and
their privacy/security contracts exist and are verified.

Gboard federated learning is publicly documented at the research level, including
[federated n-gram language models](https://research.google/pubs/federated-learning-of-n-gram-language-models/),
[differentially private Gboard language models](https://research.google/pubs/federated-learning-of-gboard-language-models-with-differential-privacy/),
[federated reconstruction](https://arxiv.org/pdf/2102.03448), and
[production federated learning system design](https://arxiv.org/pdf/1902.01046). These publications
do not make Gboard's complete current production implementation public, and Aura must not treat
research experiments as an implementation specification or invent uncited model sizes/update
cadences.

## External design references and limits

| Source | What it supports | What it does not prove |
|---|---|---|
| [Android IME guide](https://developer.android.com/develop/ui/views/touch-and-input/creating-input-method) | `InputMethodService`, editor connection, lifecycle, field types, resource preloading | Aura latency or privacy behavior |
| [Android `InputConnection`](https://developer.android.com/reference/android/view/inputmethod/InputConnection) | The IME-to-editor communication contract | That a particular call is cheap in every host app |
| [AOSP LatinIME `BinaryDictionary`](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/master/java/src/com/android/inputmethod/latin/BinaryDictionary.java) and [native build](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/master/native/jni/Android.bp) | Open binary-dictionary/native-engine precedent | Proprietary Gboard internals |
| [AOSP `RichInputConnection`](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/master/java/src/com/android/inputmethod/latin/RichInputConnection.java) | Cached/reconciled editor context precedent | Permission to block Aura's common key path |
| [AOSP historical synchronous wait path](https://android.googlesource.com/platform/packages/inputmethods/LatinIME/+/ecea8551c39a497e036be5c010d7ddb6b51a36bc/java/src/com/android/inputmethod/latin/inputlogic/InputLogic.java) | A concrete design hazard examined during Aura's design | A pattern Aura should copy |
| [Google: machine intelligence behind Gboard](https://research.google/blog/the-machine-intelligence-behind-gboard/) | Public neural typing architecture context | Complete/current Gboard code or timing constants |
| [Gboard on-device neural language model paper](https://arxiv.org/pdf/1811.03604) | Published neural language-model design and measurements in that study | Aura's model quality or current Gboard implementation |
| [Gboard spatial-model personalization paper](https://arxiv.org/pdf/2209.11311) | Published on-device spatial personalization research | A public source tree or Aura's lexical-learning policy |
| [Gboard federated n-gram paper](https://research.google/pubs/federated-learning-of-n-gram-language-models/) | Federated n-gram research on keyboards | That Aura currently implements federation |
| [Gboard differential-privacy paper](https://research.google/pubs/federated-learning-of-gboard-language-models-with-differential-privacy/) | Published private federated language-model training | Aura privacy parameters or server design |
| [Federated analytics paper](https://arxiv.org/pdf/2009.10031) | Population-level on-device analytics techniques | Permission to upload raw text; Aura forbids that design |
| [Microsoft SwiftKey support](https://support.microsoft.com/en-US/swiftkey-keyboard/how-to-use-the-microsoft-swiftkey-keyboard) | Public product behavior | Proprietary ranking structures, threads, or intervals |
| [Fleksy Core SDK](https://docs.fleksy.com/core-sdk/android/) and [prediction guide](https://docs.fleksy.com/core-sdk/guides/working-with-current-and-next-wordprediction/) | Verifiable SDK-level current/next-word APIs | Fleksy's private engine internals |
| [Fleksy Android changelog](https://docs.fleksy.com/sdk-android/changelog/) | Public SDK evolution | Unpublished production architecture |
| [ONNX Runtime mobile](https://onnxruntime.ai/docs/tutorials/mobile/) and [execution providers](https://onnxruntime.ai/docs/execution-providers/) | Supported mobile runtime/provider concepts | Provider actually selected for Aura on a device |
| [ONNX Runtime NNAPI](https://onnxruntime.ai/docs/execution-providers/NNAPI-ExecutionProvider.html) | NNAPI provider capabilities and constraints | A recommendation to use NNAPI for this 2,911-byte model |

## Reproduction entry points

- Rebuild the packed dictionary with
  `python android/tools/keyboard_dictionary/build_packed_dictionary.py --help`, then use the
  documented source/output arguments from that command.
- Rebuild the reranker and provenance with
  `python android/tools/keyboard_model/train_reranker.py --help`; Python dependencies are pinned in
  [`requirements.txt`](../../android/tools/keyboard_model/requirements.txt).
- Run the physical-device acceptance procedure from
  [`run_profile.ps1`](../../android/ime-benchmark/run_profile.ps1). Its parameters and preflight
  checks are the source of truth; do not substitute an emulator or debug build.
- Prove the runtime provider with
  [`analyze_ort_profile.py`](../../android/ime-benchmark/tools/analyze_ort_profile.py), and compare
  only like-for-like device/build/workload runs with
  [`compare_runs.py`](../../android/ime-benchmark/tools/compare_runs.py).

## Decision record

Aura's Android keyboard keeps native Kotlin rendering and editor integration. It adopts the
production-safe hybrid that can be verified from open sources and measured locally: compact lexical
candidate generation first, local n-gram personalization second, and a small cancellable neural
reranker last. This is the correct boundary for instant typing because candidate quality work can
fail, cancel, or arrive late without delaying the glyph.

The implementation does not imitate unverifiable Gboard internals. It uses AOSP and Android for the
open IME/editor precedents, ONNX Runtime's documented mobile controls for local inference, and
published Gboard research only for research context. Repository source and physical-device traces,
not competitor mythology, determine Aura's acceptance status.
