# Aura Android Keyboard Privacy-First Personalization Plan

Status: design review; no implementation has been authorized or completed
Last reviewed: 2026-08-03
Scope: Aura's native Android IME, its local suggestion and personalization stack, and the
keyboard-specific boundaries around drafting and LiveKit voice

## Goal

Build a responsive, adaptive Android keyboard inspired by Gboard while enforcing a stricter
privacy contract:

- No keystrokes, surrounding text, clipboard text, learned vocabulary, phrases, application
  profiles, analytics, gradients, model updates, or other keyboard-personalization data may leave
  the device.
- Password, payment, private, incognito, secure, and no-personalized-learning fields must never
  contribute learning signals.
- Personalization must create no idle network traffic and no scheduled background training.
- Every retained personalization artifact must be encrypted locally and user-deletable.
- Suggestions must remain responsive while typing.
- Existing LiveKit voice may continue sending and receiving intentional voice audio, but it must
  not receive field text or keyboard-personalization data.

## Executive conclusion

Aura has a credible low-latency local suggestion foundation, but it does not currently satisfy
this privacy contract.

The largest blockers are:

- Field and clipboard text is sent to `POST /keyboard/draft`.
- Up to 2,000 characters of surrounding field text is published through LiveKit and the app voice
  handoff.
- Personal vocabulary is stored in plaintext SQLite.
- UserAura vocabulary is downloaded from the backend and stored in plaintext preferences.
- Keyboard draft activity sends user-, application-, action-, and field-level analytics.
- Personalization learns autocorrect and suggestion outputs as though they were user-authored,
  creating a feedback loop.
- The IME belongs to the same network-capable APK/UID as Aura. The manifest grants `INTERNET`, with
  no isolated process or component-level network boundary. Therefore, "cannot reach cloud at any
  cost" is not technically enforceable in the current packaging.

The recommended direction is local counts, n-grams, correction/confusion statistics, encrypted
per-application profiles, and carefully matured feedback. Federated learning and on-device neural
fine-tuning should not be used.

## Verified current architecture

```text
Android host application
        |
        | EditorInfo + InputConnection
        v
+------------------------- Aura APK / default application process -------------------------+
| BuddyImeService                                                                         |
|   |                                                                                     |
|   +--> FieldProfile: layout / predict / autocorrect / learn / memory gates               |
|   |                                                                                     |
|   +--> tap key --> commitText immediately --> WordComposer mirror                       |
|   |                                      |                                              |
|   |                                      +-- 30 ms debounce --> predictionExecutor       |
|   |                                                            |                        |
|   |       Base 30K PrefixIndex --------------------------------+                        |
|   |       PersonalDictionary cache + plaintext SQLite ---------+--> SuggestionRanker     |
|   |       Android SystemUserDictionary ------------------------+         |               |
|   |       cloud-derived VocabHintsCache -----------------------+         v               |
|   |                                                                 suggestion strip    |
|   |                                                                                     |
|   +--> separator --> synchronous edit-1 autocorrect --> learn final output               |
|   +--> pause --> deferred edit-1/edit-2 correction suggestions                          |
|   +--> space --> static 99-word unigram next-word fallback                              |
|                                                                                         |
| Network-capable adjacent paths:                                                         |
|   GET /keyboard/vocab --> UserAura terms --> plaintext SharedPreferences                |
|   POST /keyboard/draft <-- field/clipboard text --> model provider + server analytics   |
|   LiveKit voice <--> audio + transcription + field text screen_context                  |
|   app fallback --> encrypted handoff containing field text --> LiveKit screen_context   |
+-----------------------------------------------------------------------------------------+
        |
        +--> Backend / LLM provider / PostHog / LiveKit
```

### IME and lifecycle

- [`BuddyImeService`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt)
  owns the `InputConnection`, UI, composing mirror, suggestion rendering, autocorrect, learning,
  drafting, and in-keyboard voice entry.
- The implementation commits letters directly with `InputConnection.commitText`; it no longer
  uses a real composing region. The older architecture document's `setComposingText` description
  has drifted from the source.
- `onStartInputView` recomputes `FieldProfile`, starts lazy dictionary loads, refreshes system and
  cloud vocabulary, opens the personal dictionary, warms encrypted credentials, and resets field
  state.
- `onFinishInputView` drops composing state and stops voice.
- `onDestroy` stops voice, cancels main-handler work, shuts down the prediction executor, and
  closes the personal dictionary if it was initialized.
- The service entry in
  [`AndroidManifest.xml`](../android/app/src/main/AndroidManifest.xml) has no `android:process`
  or `isolatedProcess`. Despite comments describing a separate keyboard process, the declared
  service uses the application's default process and UID.

### Suggestion engine and prefix index

- [`BaseDictionary`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/BaseDictionary.kt)
  lazily loads `dictionaries/en_wordlist.txt` on a process-lifetime executor.
- The bundled asset contains exactly 30,000 unique entries, occupies 407,151 bytes, and has a
  maximum word length of 17.
- [`PrefixIndex`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PrefixIndex.kt)
  stores sorted lowercase keys, display forms, and frequencies in parallel arrays. Prefix lookup
  uses binary lower-bound search and scans the matching range while keeping a tiny top-K set.
- Prediction is single-threaded and last-write-wins, with a 30 ms debounce. Correction suggestions
  are deferred another 170 ms.
- [`SuggestionRanker`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/SuggestionRanker.kt)
  uses strict source tiers: cloud vocab over personal words over base words. Frequency only breaks
  ties within a tier.

### Next-word prediction

- [`NextWordPredictor`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/NextWordPredictor.kt)
  contains a bigram seam, but the runtime bigram map is empty.
- The current implementation falls back to `en_top100.txt`, which contains 99 static high-frequency
  English words.
- There is no contextual, per-application, multilingual, phrase, or personalized next-word model.

### Autocorrection and typo recovery

- [`SpellChecker`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/SpellChecker.kt)
  generates classic delete, transpose, replace, and insert candidates.
- Edit-distance-one autocorrect runs synchronously when a separator is pressed.
- Edit-distance-two search is capped and only offered asynchronously after a pause; it is never
  auto-applied.
- Ranking uses base dictionary frequency. It does not use keyboard geometry, personalized typo
  patterns, application context, or previous words.
- A separator learns the resulting `finalWord`, including an automatically generated correction.
- A suggestion tap learns the selected candidate immediately.
- Undo adds the original word as known but does not retract or decrement learning attached to the
  generated correction. These paths can turn predictions into their own training labels.

### Personal vocabulary and names

- [`SqlitePersonalDictionary`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalDictionary.kt)
  stores `word`, `display`, `count`, and `last_used` in `buddy_personal_dictionary.db`.
- The file explicitly states that the database is intentionally plaintext.
- Writes update an in-memory cache synchronously and are mirrored best-effort on a background
  executor. Persistence exceptions are swallowed.
- The store is not namespaced by Aura account, has no global deletion API, and has no size cap.
- [`PersonalDictionaryCache`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalDictionaryCache.kt)
  ranks entries using `count * exp(-elapsedDays / 90)` and then recency.
- Names and interests also arrive from the backend through
  [`VocabHintsCache`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/VocabHintsCache.kt),
  which fetches up to 80 UserAura-derived tokens and stores them in ordinary `SharedPreferences`.
- The Android system personal dictionary is read locally and cached in RAM.

### Phrase, code, language, and swipe support

- Learning accepts only tokens of at least two letters. Code terms containing digits,
  underscores, or punctuation are not learned.
- No phrase or n-gram persistence exists.
- The IME declares one `en_US` subtype and has only an English prediction dictionary.
- There are no per-application language profiles.
- Swipe typing is not implemented. Moving off a character key cancels its tap. The only swipe
  behavior is left-swipe backspace to delete words.

### Logging, networking, and analytics

- The keyboard package contains no direct Android `Log` calls.
- `KeyboardDraftClient`, `KeyboardVoiceController`, `KeyboardAuth`, and `VocabHintsCache` perform
  network/authentication work.
- [`KeyboardDraftClient`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardDraftClient.kt)
  serializes `context_before`, host application, field type, action, tone, and language to
  `/keyboard/draft`.
- [`BuddyImeService`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt)
  derives that context from up to 2,000 characters before the cursor or from the clipboard.
- In-keyboard voice publishes a reliable LiveKit `screen_context` message containing field text,
  field type, and package name.
- [`KeyboardVoiceHandoff`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceHandoff.kt)
  encrypts a low-RAM/no-microphone fallback payload locally, but the app subsequently sends it to
  the voice agent.
- [`backend/src/handlers/keyboard.py`](../backend/src/handlers/keyboard.py) emits a tool span and
  PostHog event containing UID, action, host application, and field type. The handler does not log
  typed content, but keyboard analytics still leave the device.
- [`backend/src/services/keyboard/drafter.py`](../backend/src/services/keyboard/drafter.py)
  sends the source text through the configured model provider. Not persisting the request does not
  satisfy a rule forbidding cloud transmission.

### Memory and idle behavior

- The 30K dictionary is compact on disk, but the runtime index holds two object-reference arrays,
  an `IntArray`, and strings. No measured PSS benchmark exists in the repository.
- Personal words are unbounded and scanned for matching prefixes on every personalized lookup.
- `BaseDictionary`, `NextWordPredictor`, `SystemUserDictionary`, `VocabHintsCache`,
  `KeyboardCredentialStore`, and `KeyboardDraftClient` own process-lifetime executors or pools.
- Parked threads should consume little CPU, but retain thread stacks and other memory.
- Vocab refresh runs when a prediction-allowed field is focused and the cache is stale. A failed
  refresh leaves the timestamp stale, allowing another focus to retry rather than waiting a day.
- No WorkManager or scheduled local training job exists.
- LiveKit is lazy, but it brings a second WebRTC stack into the app. Active and post-disconnect
  memory have not been measured.

## Privacy audit

| Severity | Current behavior | Assessment |
|---|---|---|
| Critical | Draft actions send field or clipboard text | Direct violation of the no-text-cloud rule |
| Critical | Voice publishes field text through LiveKit | Voice may remain, but it must be audio-only |
| Critical | Voice fallback hands field text to the app | Encryption at rest does not make later upload compliant |
| Critical | Personal vocabulary uses plaintext SQLite | Violates encrypted-retention requirement |
| Critical | The APK/UID has `INTERNET` | The IME cannot provide an OS-enforced network prohibition |
| High | UserAura vocabulary is downloaded and cached plaintext | Cloud-backed and unencrypted personalization |
| High | Draft telemetry sends UID, action, host app, and field type | Keyboard analytics leave the device |
| High | Manifest has no personalization backup exclusions | App-private files may participate in Android backup |
| High | No clear-all personalization operation exists | User deletion is incomplete |
| High | Privacy depends on host-supplied field metadata | Unmarked private/payment fields cannot be recognized reliably |
| High | Generated suggestions/corrections become positive labels | Creates self-reinforcing errors |
| Medium | Several failures are swallowed | Durability and degraded state are invisible |
| Medium | Process-lifetime executors remain allocated | Memory remains after individual IME sessions |
| Medium | Startup DB load can publish an older cache snapshot | Fresh session learning can disappear from RAM until restart |

The secure-field classifier is otherwise reasonably conservative: password, visible-password,
web-password, numeric PIN, email/URL autocorrection, `NO_SUGGESTIONS`, and
`IME_FLAG_NO_PERSONALIZED_LEARNING` are covered by
[`FieldProfile`](../android/app/src/main/kotlin/dev/varuntej/aura/keyboard/FieldProfile.kt).

An Android IME cannot prove that a plain-text field is not semantically private when the host app
fails to set the appropriate flags. A literal guarantee therefore needs fail-closed defaults,
explicit local app approval/private mode, and ideally a no-network IME package.

## Non-negotiable cloud boundary

The implementation must enforce all of the following:

1. No `InputConnection` text, clipboard text, token, n-gram, name, phrase, correction event,
   package profile, metric, gradient, weight, adapter, or serialized personalization state may be
   passed to a network-capable API.
2. No keyboard analytics event may be uploaded, including aggregate counts, package names, field
   types, acceptance rates, or latency.
3. LiveKit may send and receive intentional voice audio. It must receive no field text, clipboard
   text, package name, field type, learned data, or personalization payload from the keyboard.
4. Voice transcripts must not become keyboard learning input.
5. Cloud UserAura vocabulary must not seed strict local personalization. Names may come from
   explicit local additions, the read-only Android user dictionary, or an explicitly chosen local
   import.
6. Android backup must exclude personalization ciphertext and keys.
7. Key or decryption failure must degrade to base-dictionary-only operation, never plaintext.
8. Secure, no-learning, private/incognito, and unknown-sensitive contexts must create no events,
   including temporary negative-feedback events.

### Capability boundary

Code review and module restrictions can provide a strong policy boundary, but the current APK
cannot provide a capability boundary because Android permissions are UID-wide.

The strongest target is a separate IME APK/UID without `INTERNET`. Aura's network-enabled app
would own LiveKit and expose only an audio start/stop interface. The keyboard must not send text to
that interface. Keeping LiveKit inside the current same-UID IME can preserve product behavior, but
cannot prove that text is technically incapable of reaching the network.

## Proposed local personalization engine

| Capability | Local design |
|---|---|
| Next word | Decayed global bigram/trigram counts mixed with the active app profile and bundled unigram prior |
| Autocorrection | Edit candidates plus keyboard adjacency, transposition, local confusion pairs, word prior, and local bigram context |
| Names | Repeated clean casing evidence and explicit remember actions; never infer a name from one sentence-start capital |
| Phrases | Encrypted 2-4 token continuations promoted only after repeated mature confirmations |
| Code terms | Exact camelCase, snake_case, digit, and punctuation forms in local code profiles; reject high-entropy secrets |
| Per-app language | Encrypted package-keyed mixture over installed local language packs plus prose/code mode |
| Recency/frequency | Separate counters and decay for manual commits, accepted completions, and explicit additions |
| Swipe | Future local path sampling and trie beam search; trajectories remain RAM-only |

### Next-word prediction

Use a deterministic mixture rather than neural training:

- Bundled static unigram prior.
- Device-global decayed bigram/trigram counts.
- Active application profile counts.
- Phrase continuation score.
- Mature acceptance and deletion evidence.

Require multiple mature observations before promoting a phrase. Interpolate application and global
statistics so new or sparse app profiles fall back cleanly. Never persist full messages.

### Autocorrection and typo recovery

Generate candidates off the input thread from:

- Edit distance one and bounded edit distance two.
- Physical key adjacency.
- Transposition and missing-space hypotheses.
- Mature local typo-to-target confusion counts.
- Exact known names and code terms.

Score candidates using word prior, error likelihood, local previous-word context, app profile, and
candidate provenance. Auto-apply only when both an absolute confidence threshold and a margin over
the second candidate are met.

The separator path must consume a previously computed result. It must not enumerate correction
candidates synchronously on the main thread.

### Names and personalized vocabulary

- An explicit "remember" action is the strongest signal.
- A manually typed unchanged token may become a name after consistent non-sentence-start casing
  evidence across multiple mature commits.
- One capitalized token at a sentence start is insufficient.
- The Android system dictionary may remain a read-only in-memory input.
- Optional contact/name import must be an explicit local action. Any retained copy goes into the
  encrypted store and is included in clear-all.
- Account changes must not expose a prior Aura user's local vocabulary. Either maintain an
  explicit device-local keyboard profile or namespace and clear data at account boundaries.

### Phrase and code-term suggestions

- Persist only bounded 2-4 token aggregates, not source sentences.
- Require repetition across mature events before surfacing a phrase.
- Decay and evict unused phrases.
- Code profiles preserve case and punctuation and suppress ordinary prose autocorrect.
- Detect and reject OTP-like tokens, card-like digit runs, private keys, long random strings, and
  other high-entropy values before any temporary event is created.

### Per-application language profiles

- Key profiles by an encrypted package identifier.
- Retain language mixture, prose/code mode, local lexeme deltas, local n-gram deltas, confidence,
  and last use.
- Do not create a profile until enough safe events exist.
- Cap profile count and evict least-recently used inactive profiles.
- Allow a user to mark any app as never learn/private.
- Full multilingual behavior requires bundled offline dictionaries or models first. The current
  English-only subtype cannot provide it.

### Swipe typing

Swipe is not a small extension of the current per-key listeners. It requires a keyboard-wide
gesture surface, trajectory resampling, spatial key likelihoods, trie beam search, and language
rescoring.

If implemented later:

- Raw trajectories remain in RAM and are discarded after decoding.
- Only a mature accepted final word can affect aggregates.
- Gesture traces never enter analytics, logs, backups, or training files.
- Target p95 decode-to-suggestion latency after finger lift is at most 50 ms.

## Feedback and label maturation

A displayed or generated suggestion is never a label by itself.

| User event | Durable effect |
|---|---|
| Unchanged manually typed word reaches a boundary | Provisional positive; durable only after the edit/undo window closes |
| Suggestion tapped | Provisional acceptance; credit after subsequent typing without deletion/replacement |
| Autocorrect applied | No vocabulary-frequency credit; retain RAM-only pending correction |
| Autocorrect survives the guard window | Small credit to the typo-to-target confusion pair only |
| Undo/backspace after autocorrect | Retract pending credit and add bounded negative evidence for that exact pair |
| Word deleted shortly after commit | Cancel its pending positive event |
| Manual correction | Credit only the final manually produced token and a bounded edit observation |
| Suggestion merely ignored | No negative label; continuing to type is ambiguous |
| Secure/private field | No event is created |

Every pending event must carry provenance: manual typing, explicit addition, completion tap,
next-word tap, automatic correction, manual correction, or import. Ranking counters must remain
separate by provenance.

## Proposed failure and recovery behavior

```text
EditorInfo + user privacy controls
        |
        v
Fail-closed LocalPrivacyPolicy
   | secure / no-learn / incognito / unknown-sensitive
   +-----------------------> clear RAM token state; no event; base typing only
   |
   | allowed
   v
RAM-only PendingFeedback with provenance and expiry
   | process death before maturity -------> event disappears safely
   | undo / delete / correction ----------> retract or bounded negative aggregate
   | guard window completes --------------> mature aggregate
   v
Encrypted PersonalizationStore transaction
   | key unavailable / decrypt failure ---> destroy unreadable store; base-only fallback
   | write failure ------------------------> keep old snapshot; do not claim durability
   | successful atomic commit ------------> publish new immutable in-memory snapshot
   |
User "clear learning"
   +--> increment generation --> reject queued old writes --> destroy key --> delete DB/WAL
                                                       --> fresh empty key/store

Network boundary:
PersonalizationStore --X--> network
IME text/clipboard   --X--> Aura/LiveKit
Aura voice broker   <----> LiveKit audio only, owned by separate network-capable UID
```

### Failure contracts

- Prediction failure: render base or empty suggestions; never block `commitText`.
- Store read/decryption failure: surface base-only behavior and clear or quarantine the unreadable
  local store. Never attempt plaintext recovery.
- Store write failure: retain the previous durable snapshot and keep the new observation
  non-durable. Do not claim that learning succeeded.
- Process death: immature RAM feedback is lost intentionally; mature committed aggregates recover
  from the encrypted snapshot.
- Stale asynchronous prediction: retain the existing monotonic token check and drop stale results.
- Clear-data concurrency: increment a generation before clearing. Every queued write carries its
  originating generation and is rejected after a clear.
- Voice failure: audio session surfaces a bounded error or opens Aura without transferring field
  text. Typing remains available.

## Database design and migration

Replace `buddy_personal_dictionary.db` v1 with a versioned encrypted store.

Suggested logical records:

- `lexemes`: normalized/display form, manual count, accepted count, last accepted time, casing
  confidence, source/provenance flags.
- `ngrams`: previous-token context, continuation, per-provenance counts, decay timestamp.
- `confusions`: source edit pattern, target pattern, positive/negative evidence, recency.
- `app_profiles`: encrypted package identifier, language mixture, mode, application-local lexeme and
  n-gram deltas, last use.
- `meta`: schema version, encryption-key version, store generation, caps, and last successful
  migration.

Because the current engine already loads small datasets into RAM, a low-overhead option is AES-GCM
encrypted row or snapshot payloads using a random data key wrapped by Android Keystore. Counts and
timestamps must remain inside ciphertext. No raw event journal should be written.

Suggested caps:

- 10,000 global lexemes.
- 2,000 active-application lexemes.
- 20,000 n-gram edges.
- 2,000 phrase continuations.
- 16-32 application profiles.
- 8 MiB encrypted-on-disk ceiling.

### Migration ordering

1. Open v1 read-only and load valid rows.
2. Generate or load a Keystore-wrapped v2 data key.
3. Write the complete encrypted v2 snapshot to a temporary database/file.
4. Reopen and validate schema, authentication tags, row counts, and limits.
5. Atomically publish v2.
6. Destroy plaintext v1 database, journal, and WAL artifacts.
7. Only then expose v2 to the live engine.

If validation fails, continue base-only and leave v1 untouched only long enough to retry the same
foreground migration. The product must not resume plaintext learning.

### User deletion

1. Increment the store generation.
2. Clear all in-memory snapshots and pending events.
3. Reject queued writes from older generations.
4. Destroy the wrapping/data key.
5. Delete database, WAL, journal, encrypted vocab/emoji preferences, app profiles, phrases,
   confusion data, and local metrics.
6. Recreate an empty store only when personalization is used again.

Android backup/data-extraction rules must exclude the encrypted store and related keys. Key
destruction provides cryptographic deletion even where flash storage cannot guarantee physical
block erasure.

## Latency and memory budgets

These are proposed acceptance budgets. The repository contains no runtime latency benchmark or
PSS evidence proving that the current implementation meets them.

| Surface | Budget |
|---|---:|
| Key touch to `commitText` | p95 <= 8 ms; p99 <= 12 ms; never exceed one 16 ms frame |
| Current-word suggestions including 30 ms debounce | p50 <= 45 ms; p95 <= 70 ms |
| Next-word suggestions after space | p95 <= 25 ms |
| Deferred typo suggestions | p95 <= 220 ms after the final key |
| Separator autocorrect | No candidate generation on main thread |
| App-profile activation | <= 20 ms asynchronously; immediate global fallback |
| Personalization incremental PSS | <= 10 MiB over base typing |
| Bundled 30K index | <= 4 MiB steady-state heap target |
| Active app/profile cache | <= 4 MiB |
| Encrypted database | <= 8 MiB |
| Idle behavior | No timers, scheduled training, polling, or personalization network |
| Voice | Separate budget; release LiveKit/WebRTC resources promptly after stop |

Typing-only low-memory targets should be evaluated separately from active voice. After a voice
session stops, process memory should return close to the warm typing baseline rather than retaining
the LiveKit room, audio handler, captions, coroutines, or native WebRTC resources.

## Federated learning versus single-device personalization

Federated learning is still cloud learning. Raw text may remain local, but gradients or model
deltas leave the device for aggregation. It is forbidden by this plan.

Single-device continual personalization is permitted because inputs, aggregates, and parameters
remain on one device. It does not require gradients:

- Decayed counters, n-grams, confusion matrices, and deterministic feature weights are realistic
  now.
- A static quantized language model shipped in the APK could later rescore a small candidate set,
  subject to device benchmarks.
- Local adapter or LoRA training is not realistic for the current engine. Sparse/noisy labels,
  optimizer and activation memory, foreground latency, battery consumption, and self-label
  poisoning outweigh likely quality improvements.
- Background "train while charging" conflicts with the near-zero idle CPU requirement.
- No persistent gradients should be created. Reconsider static local inference only after the
  statistical engine has measurable limitations.

## Evaluation and acceptance metrics

Production measurements must remain encrypted and local. Development evaluation should use
synthetic or canned corpora and dedicated devices, never extracted user typing.

### Quality

- Top-1 and top-3 completion recall.
- Mean reciprocal rank.
- Next-word top-3 recall.
- Autocorrect precision and false-correction rate.
- Autocorrect undo rate.
- Manual-correction recovery rate.
- Keystrokes saved per committed character.
- Mature acceptance rate by provenance.
- Deleted-within-window rate.
- Code-term exact-case preservation.
- Cross-application contamination rate.
- Secure/private-field event count: exactly zero.

### Performance and privacy

- Key-to-commit and key-to-suggestion p50, p95, and p99.
- CPU time per 100 typed words.
- Idle wakeups and network bytes.
- PSS after cold start, dictionary load, maximum profile load, voice connect, and voice disconnect.
- Encrypted database size and migration duration.
- Clear-data completion and proof that queued writes cannot resurrect deleted data.
- Packet inspection proving that no text, personalization payload, keyboard analytics, or app
  profile metadata leaves the device.

Existing JVM tests cover the pure field classifier, cache, ranker, spell checker, and next-word
helper. There is no IME-level coverage of networking, encrypted storage, deletion, or lifecycle.
The repository's test freeze prohibits adding cases; this remains verification debt.

The existing `:app:testDebugUnitTest` task was attempted during the review. After downloading the
pinned Gradle distribution, it exceeded the 184-second execution limit without a result. This
document therefore makes no passing-suite claim.

## Walkthrough 1: mature manual vocabulary learning

1. `BuddyImeService` focuses a user-approved normal prose field.
2. `LocalPrivacyPolicy` allows local learning and activates the encrypted application profile
   asynchronously. Global suggestions remain immediately available.
3. The user manually types `Thiru` and commits it unchanged.
4. `PendingFeedback` records a manual provenance event in RAM. Nothing is durable and no network
   API is reachable.
5. The correction/deletion guard window closes without replacement or deletion.
6. The event matures. `PersonalizationStore` atomically updates lexeme casing, frequency, recency,
   and applicable local n-grams inside encrypted storage.
7. Only after the durable transaction succeeds does the repository publish a new immutable
   in-memory snapshot.
8. On the next matching prefix in that application, the application-local candidate outranks the
   generic base candidate.

## Walkthrough 2: incorrect autocorrect, undo, focus change, and process death

1. The engine provisionally autocorrects `form` to `from` using a precomputed candidate.
2. It creates a RAM-only pending correction but does not increase `from` vocabulary frequency.
3. The user immediately backspaces and restores `form`.
4. The pending positive is retracted. A bounded negative is associated only with the exact
   `form -> from` confusion.
5. The user moves to a secure field before the feedback window closes.
6. `LocalPrivacyPolicy` clears all token, correction, suggestion, and pending-feedback state. No
   secure-field event is created.
7. If the process dies before a permitted event matures, that event is intentionally lost.
8. On restart, only fully committed encrypted aggregates are restored; base-only behavior remains
   available if the store cannot be read.

## Implementation phases

### Phase 0: privacy cut

- Remove field and clipboard text from LiveKit data messages.
- Make app voice handoff carry no field text.
- Disable keyboard cloud drafting that serializes field or clipboard content until it has a local
  replacement.
- Stop `/keyboard/vocab` refresh from the IME.
- Preserve ordinary typing, the local 30K dictionary, and intentional LiveKit voice audio.
- Remove keyboard-specific server analytics made unreachable by the client cut.

### Phase 1: encrypted storage and deletion

- Introduce the versioned encrypted personalization store.
- Migrate and destroy the plaintext dictionary safely.
- Encrypt or remove emoji recents and other retained keyboard preferences.
- Add backup exclusions.
- Add clear-all with generation-based queued-write rejection and cryptographic erasure.
- Resolve device-local versus Aura-account-local profile ownership.

### Phase 2: provenance and label maturation

- Introduce RAM-only pending feedback.
- Stop immediately learning automatic corrections and generated suggestions.
- Mature manual, accepted, deleted, undone, and manually corrected outcomes separately.
- Preserve last-write-wins prediction cancellation across field changes.

### Phase 3: local next-word personalization

- Add bounded decayed global and per-application bigrams/trigrams.
- Blend local counts with the bundled unigram prior.
- Add eviction and sparse-profile fallback.

### Phase 4: safer autocorrection

- Add keyboard adjacency and bounded local confusion statistics.
- Incorporate previous-word and application context.
- Move all separator candidate generation off the input thread.
- Require confidence and winner-margin thresholds before automatic replacement.

### Phase 5: names, phrases, and code

- Add explicit local names and casing confidence.
- Add repeated mature phrase continuations.
- Add exact code-term storage and code-mode ranking.
- Add secret/high-entropy rejection before temporary event creation.

### Phase 6: offline language profiles

- Ship local dictionaries or static inference assets for supported languages.
- Infer per-application language mixtures locally.
- Keep language assets and profiles offline and user-deletable where personalized.

### Phase 7: swipe typing

- Replace per-key cancellation-only gesture handling with a keyboard-wide path surface.
- Add local spatial decoding and language rescoring.
- Retain no gesture trajectory after decoding.

### Phase 8: static model feasibility gate

- Benchmark a small quantized static candidate rescorer only if the statistical engine has a
  measured quality ceiling.
- Do not add local model training, adapters, gradients, or idle optimization jobs.

Every phase ships unconditionally in accordance with the repository's no-feature-flag rule.
Staged Play rollout may control deployment percentage; runtime feature flags may not.

## Files and modules likely to change

Verified existing ownership points:

- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/BuddyImeService.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/FieldProfile.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardDraftClient.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceController.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/KeyboardVoiceHandoff.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalDictionary.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/PersonalDictionaryCache.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/NextWordPredictor.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/SpellChecker.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/SuggestionRanker.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/prediction/VocabHintsCache.kt`
- `android/app/src/main/kotlin/dev/varuntej/aura/keyboard/input/KeyTouchHandler.kt`
- `android/app/src/main/AndroidManifest.xml`
- `backend/src/handlers/keyboard.py`
- `backend/src/services/keyboard/drafter.py`
- `backend/src/services/keyboard/vocab.py`
- `lib/data/services/voice_session_service.dart`
- `android/app/src/main/kotlin/dev/varuntej/aura/MainActivity.kt`

New paths and class names beyond these are design choices, not verified current files.

## Rollback strategy

Privacy changes are monotonic. Never roll back to the current text-upload or plaintext-storage
behavior.

- Keep the existing deterministic base/system ranker as the permanent degraded mode.
- Make encrypted schemas additive and preserve a reader for the immediately previous encrypted
  version.
- Publish new in-memory snapshots only after complete decryption and validation.
- On corruption, key invalidation, or scorer failure, use base-only suggestions and retain or
  cryptographically erase the bad store.
- A release rollback must retain the encrypted-store reader and no-cloud boundary.
- If personalization causes latency or quality regressions, roll forward to an unconditional
  base-only hotfix rather than reinstalling the current implementation.
- Do not re-enable cloud-backed keyboard vocab or typed-text drafting as a recovery mechanism.

## Risks and unresolved decisions

- Android cannot reliably identify an unmarked private field. Strict fail-closed behavior and a
  user-visible private/app policy are required even with good `EditorInfo` handling.
- Removing UserAura vocab will initially reduce name quality.
- Per-application models fragment limited evidence and need minimum-event thresholds.
- Incorrect feedback provenance can still poison confusion statistics if the maturity window is
  too short.
- Encryption/key invalidation can cause safe but user-visible personalization loss.
- The current LiveKit dependency adds a second WebRTC stack and materially affects process memory.
- A separate no-network IME APK/UID is the only strong capability boundary, but it creates
  installation, onboarding, voice-broker, release, and cross-package authentication work.
- The device-local versus Aura-account-local ownership model must be decided before migration.
- Multilingual assets affect APK size and must be measured using actual Play-delivered size, not
  only the local app bundle.

## Smallest first task

Ship one cohesive privacy-cut change before improving prediction:

1. Make in-keyboard LiveKit audio-only by passing no `screen_context`.
2. Make app voice handoff carry no field text.
3. Disable cloud drafting that serializes field or clipboard content until a local replacement
   exists.
4. Stop `/keyboard/vocab` refresh from the IME.
5. Leave the existing local dictionary, system dictionary, current personal dictionary, and
   current ranking behavior otherwise unchanged for this task.

This is the smallest deployable change that closes the active keyboard text and personalization
cloud paths while preserving ordinary typing and LiveKit voice. Encryption and safer feedback
must follow before adding adaptive prediction features.
