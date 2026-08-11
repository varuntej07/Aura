# Buddy Keyboard Architecture

The native Android IME (`android/app/src/main/kotlin/dev/varuntej/aura/keyboard/`) is a real
QWERTY keyboard with Buddy bolted on top: an on-device composing + prediction + correction stack,
plus the AI whiteboard, reply-as-me drafting, and in-keyboard voice that no stock keyboard has.

This file is the design + rationale for the typing engine (the "smart typing" layer), the same way
`signal_engine.md` documents the notification engine. It is honest about what is and is not
implemented: the moat features are real and shipped; swipe typing, a neural decoder, and dynamic
key hit zones are named as future work, not claimed.

## Why this exists

A companion you talk to all day lives or dies on whether typing to it feels good. The composing
stack (M1 to M8) reached feature breadth quickly but had one structural flaw: all prediction ran
on the main thread, so fast typing felt laggy. This milestone (T1 to T7) fixes the lag at the root
and closes the invisible-but-expected gaps versus Gboard (next-word prediction, two-edit
correction, temporal decay, double-space-to-period, proper-noun casing, a clipboard paste chip),
while keeping the on-device, privacy-first contract: nothing the user types is ever uploaded.

---

## 1. The lag root cause (and the fix)

Every keystroke ran the full pipeline synchronously on the main thread:

```
BEFORE (main thread, must finish before the next key animates)
USER PRESSES KEY
   |
   v
commitChar()
   composer.append(letter)                 ~0ms
   updateComposing()   <-- everything below blocked the main thread
     |- BaseDictionary.completions()        ~1ms   (binary search, cheap)
     |- PersonalDictionary.completions()    ~1ms   (small map scan)
     |- SystemUserDictionary.completions()  ~1ms
     |- VocabHintsCache.completions()       ~1ms
     |- SuggestionRanker.rank()             ~1ms
     |- SpellChecker.isMisspelled()/        <-- DOMINANT COST
        corrections(): edits1(word) builds
        ~54*len candidates, each checked
        via isKnown() (dictionary lookup)
   setComposingText()  <-- Binder IPC to the host app, unpredictable latency
   |
   v
NEXT KEY PRESS WAITS FOR ALL OF THE ABOVE
```

The dominant cost is the spell checker: `edits1` of a 5-letter word is ~200 to 400 candidate
strings, each probed against the dictionary union. Doing that per keystroke on the main thread is
what users feel as lag.

```
AFTER (T1: main thread does almost nothing)
USER PRESSES KEY
   |
   v
commitChar() -> composer.append -> updateComposing()
   ic.setComposingText(word)              ~<1ms  (letter shows instantly, no dict work)
   predictionToken.incrementAndGet()             (last-write-wins token)
   mainHandler.postDelayed(30ms) -------> debounce; only the latest keystroke survives
   |
   v  (next key is free immediately)

PREDICTION THREAD (predictionExecutor, single background thread)
   computePrediction(word, token)
     bail if token superseded
     Base/Personal/System/Vocab completions + SuggestionRanker.rank()
     if ranked empty AND misspelled:
        sleep SPELLCHECK_DEFER_MS (170ms)   <-- squiggle only after typing pauses
        spellChecker.corrections()           (edit distance 1, then 2)
   |
   v
mainHandler.post { if token still current && composing -> renderSuggestions / squiggle }
```

Two ideas carry the fix:
- **Last-write-wins token.** `predictionToken` (an `AtomicLong`) is bumped on every keystroke and
  on every commit/reset (`finishComposing`, `flushComposingWord`, `resetToTyping`). An off-thread
  result is dropped on the way back unless its token is still current and a word is still
  composing, so a stale suggestion never lands on the field.
- **Two-stage debounce.** A 30ms main-thread debounce coalesces fast keystrokes before any work
  hits the executor; the spell-check pass is deferred a further 170ms so the red squiggle surfaces
  only once typing pauses, never flickering mid-word.

Thread-safety: `BaseDictionary`, `SystemUserDictionary`, and `VocabHintsCache` were already
read-safe (a `@Volatile` immutable `PrefixIndex`). `PersonalDictionaryCache` was main-thread-only;
T1 switches its backing map to a `ConcurrentHashMap` and forces the dictionary's first
initialization on the main thread (in `onStartInputView`), so the background reads are safe.

---

## 2. The keystroke pipeline (current)

```
KEY (letter)                         SEPARATOR (space / punctuation / enter)
  |                                    |
  v                                    v
commitChar                           commitSeparator
  composer.append(cased)               flushComposingWord()
  updateComposing() --[async]-->         autocorrect (Autocorrector.onSeparator)
    setComposingText (instant)           proper-noun casing (T6: "kcr" -> "KCR")
    computePrediction (off-thread):      learn the committed word
      completions x4 sources             lastCommittedWord = committed
      SuggestionRanker.rank (tiered)   commitText(separator)
      else squiggle + corrections      maybeAutoSpaceAfterPunctuation (T5)
    renderSuggestions (posted back)    updateAutoCap
                                       if autocorrected -> undo chip
                                       else if space    -> next-word suggestions (T2)
```

Suggestion ranking is **tiered** (`SuggestionRanker`): vocab (the user's own people/topic names) >
personal (words they type) > base (the bundled en_wordlist). Frequency only breaks ties within a
tier, so a friend's name is never shoved aside by a common word. The personal tier's frequency now
carries a time-decayed score (T4), so stale words sink on their own.

---

## 3. Feature matrix (Buddy vs Gboard)

```
                                     BUDDY   GBOARD   NOTE
--- INPUT & GESTURE -----------------------------------------------------
Composing-text pipeline               yes     yes
Key haptics / preview bubble          yes     yes
Long-press accent popup               yes     yes
Double-space -> period                 T5      yes
Auto-space after punctuation           T5      yes
Swipe / gesture typing                no      yes    future (P6)
Dynamic Bayesian key hit zones        no      yes    future (P5)
--- PREDICTION ----------------------------------------------------------
Word completion (current word)        yes     yes
Next-word prediction (after space)     T2      yes    unigram now; bigram drop-in ready
N-gram / neural language model        no      yes    future
Emoji / GIF suggestions               no      yes    future
--- CORRECTION & LEARNING -----------------------------------------------
Single-edit spell correction          yes     yes
Two-edit spell correction              T3      yes
Undo autocorrect (chip + revert)      yes     yes
Auto-learn on commit                  yes     yes
Personal dictionary (SQLite)          yes     yes
System user dictionary                yes     yes
Temporal decay of learned words        T4      yes
Proper-noun auto-capitalize            T6      yes
Sentence auto-capitalize              yes     yes
Caps lock via double-tap              yes     yes
--- CONTEXT -------------------------------------------------------------
Field-type layouts (FieldProfile)     yes     yes    numeric/phone/pin/email/url
Security in password fields           yes     yes
Clipboard paste chip                   T7      yes    explicit gesture (privacy)
--- PERFORMANCE ---------------------------------------------------------
Async prediction (off main thread)     T1      yes    the lag fix
Debounced prediction                   T1      yes
--- BUDDY-ONLY (the moat) -----------------------------------------------
UserAura vocab hints                  yes     no     consent-gated /keyboard/vocab
AI Whiteboard (reply/rewrite/...)     yes     no
Reply-as-me (UserAura voiced)         yes     no
In-keyboard LiveKit voice             yes     no
Strong-password generator             yes     no
```

The moat is not raw autocomplete; it is "reply as me" and the companion features. The smart-typing
layer exists so typing to Buddy feels at least as good as Gboard, so those features get used.

---

## 4. Data structures (DSA reference)

```
CONCERN                     STRUCTURE / ALGORITHM                 COMPLEXITY
------------------------------------------------------------------------------
Dictionary prefix lookup    PrefixIndex: sorted parallel arrays   O(log n) + short scan
                            + binary lower-bound + top-K offer
Membership ("known word")   PrefixIndex binary search             O(log n)
Suggestion merge/dedup      LinkedHashMap, tiered compare         O(n)
Async debounce              AtomicLong token (last-write-wins)    O(1)
Two-edit spell (T3)         Norvig edits1->edits2, first hop      O(branch * 54*len) probes,
                            capped at EDITS2_BRANCH_CAP=200        gated to the edits1-empty case
Temporal decay (T4)         count * e^(-elapsedDays / 90)         O(1) per entry
Next-word (T2)              HashMap<prev, list> + unigram prior   O(1) lookup
Proper-noun casing (T6)     HashMap<lowercase, display>           O(1) lookup
Double-space FSA (T5)       1-symbol lookahead, time window       O(1)
Clipboard chip (T7)         on-demand read on explicit tap        O(1)
```

Pure logic is extracted into deterministic, unit-tested classes (`PrefixIndex`, `SpellChecker`,
`PersonalDictionaryCache`, `SuggestionRanker`, `NextWordPredictor`, `ProperNounIndex`,
`PunctuationRules` (`input/PunctuationRules.kt`), `ShiftState`, `SentenceCapitalizer`). `BuddyImeService`
owns the `InputConnection` and wires those pieces; it has no Robolectric test (it is an
`InputMethodService`), which is exactly why the logic lives in the pure helpers.

---

## 5. This milestone (T1 to T7)

| # | Feature | Where | Note |
|---|---|---|---|
| T1 | Async prediction (the lag fix) | `BuddyImeService`, `PersonalDictionaryCache` | executor + token + 30ms debounce + 170ms spell defer; cache made thread-safe |
| T2 | Next-word prediction after space | `NextWordPredictor` + `en_top100.txt` | unigram prior now; bigram asset drops in with no code change |
| T3 | Two-edit spell correction | `SpellChecker` | edits1 -> edits2 fallback, gated + capped, runs off-thread |
| T4 | Temporal decay of learned words | `PersonalDictionaryCache` | weight = count * e^(-t/90d); injected clock keeps it pure |
| T5 | Double-space -> period + auto-space | `PunctuationRules` + `BuddyImeService` | pure recognizers, wired into space/separator handling |
| T6 | Proper-noun auto-capitalize | `ProperNounIndex` + `VocabHintsCache` | "kcr" -> "KCR" from consent-gated vocab hints |
| T7 | Clipboard paste chip | `BuddyImeService` | explicit-gesture only, so the OS paste toast never fires on every focus |

No feature flags: everything ships on. T6 consumes the existing `/keyboard/vocab` endpoint
(`backend/src/services/keyboard/vocab.py`), which already returns proper-cased, lowercase-deduped
tokens, so there is no backend change, no new Firestore index, and no deploy in this work.

---

## 6. Priority ladder (impact vs effort)

```
DONE  T1  Async prediction              lag gone               (the fix)
DONE  T2  Next-word after space         strip stays useful
DONE  T3  Two-edit correction           catches harder typos
DONE  T4  Temporal decay                old mistakes fade
DONE  T5  Double-space + auto-space      expected smoothness
DONE  T6  Proper-noun casing            names look right
DONE  T7  Clipboard chip                paste in one tap
NEXT  P5  Dynamic key hit zones         accuracy, "feels magic"   high effort
NEXT  P6  Swipe / gesture typing        speed for swipe users     very high effort
LATER     Neural / n-gram model, emoji + GIF suggestions, context-aware
          autocorrect (their/there), per-word language detection
```

## 7. Surfaces beyond typing

The keyboard is a Buddy Everywhere surface, not just an IME. Four integrations
ride on top of the composing pipeline above.

### Field-type-aware layouts (`FieldProfile`)

`FieldProfile` gates behaviour per input field: `predictionsAllowed`,
`autocorrectAllowed`, `learningAllowed`. It selects numeric, phone, PIN, email,
and URL layouts, and suppresses the prediction bar entirely in non-text and
secure fields. `WordComposer` is gated on the same profile, so a password field
never reaches the dictionary or the learn-on-commit path.

### Voice handoff

A "Talk to Buddy" chip fires a safe `aura://voice` deep link. The app handles the
link, sends `screen_context` over the data channel, and opens the voice session.

In-keyboard LiveKit duplex is deliberately deferred: it needs device attestation
and WebRTC pinning first.

### Password chip

The `StrongPassword` chip generates a strong password and lets OS autofill save
it. No backend hop.

### Backend endpoints

| Endpoint | Purpose |
|---|---|
| `POST /keyboard/draft` | AI draft; uses `prompt_builder` plus `field_type` |
| `GET /keyboard/vocab` | Consent-gated interest subjects and storyline entities, returned as known-word tokens; cached client-side in `VocabHintsCache` |

**IMPORTANT: `context_before` is untrusted input.** It is whatever happens to be
in the user's text field, so it is a prompt-injection vector. Always pass it
inside a delimited block. Never interpolate it raw into a prompt.
