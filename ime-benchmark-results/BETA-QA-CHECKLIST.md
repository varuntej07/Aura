# Buddy Keyboard beta: device QA checklist

Nothing below is verified. Everything in this branch is verified only to the level of "compiles
and the existing 150 unit tests pass". Per `CLAUDE.md`, a fix is done when the symptom is
observed gone, and this is a typing engine plus a consent flow: both are felt, not reasoned about.

Run on the Pixel 10 Pro XL. Benchmark data must stay isolated from real learned data.

## A. Consent, the part Play cares about

- [ ] Fresh install, enable the keyboard from Android Settings **without opening the Aura app**.
- [ ] First focus on a normal text field: the strip shows "Your typing stays on this phone. Tap
      to see how" at full width, not ellipsized.
- [ ] It appears at most 3 times, then stops on its own.
- [ ] Tapping it opens the intro panel. "Don't learn my words" turns off learning and that
      sticks in keyboard settings. "Sounds good" leaves learning on. Both retire the banner.
- [ ] Tap any Aura writing action. The disclosure appears **before any network call**. Confirm
      with a proxy or by airplane mode that nothing hit `/keyboard/draft` first.
- [ ] Tap "Not now". **Then confirm ordinary typing, autocorrect, suggestions, next-word,
      emoji, themes, and the clipboard chip all still work.** This is the single most important
      check in this document.
- [ ] Tap the same action again. The panel re-offers (a decline is not permanent).
- [ ] Tap "Send it". The draft arrives. Tapping a second action does **not** ask again.
- [ ] "Reply as me" copy says "the message you copied", not "what you typed".
- [ ] Mic button in a normal field: panel mentions both audio and the field text.
- [ ] Mic button **in a password field**: panel says the field is not sent. Verify no
      `context_before` on the wire.
- [ ] The writing panel is unreachable in password / OTP / numeric / phone / email / URL fields.
- [ ] Settings toggles for "Aura AI writing" and "Talk to Buddy" revoke, and the panel returns.

## B. Deletion, the part that was silently broken

- [ ] Type enough to learn words, use some emoji, then tap Clear.
- [ ] Before the success message renders, confirm via `adb shell run-as dev.varuntej.aura ls
      shared_prefs/` that `buddy_keyboard_vocab.xml` and `buddy_kb_emoji.xml` are **gone**.
- [ ] Confirm the encrypted snapshot in `no_backup/` is gone and the Keystore alias is gone.
- [ ] Confirm learned words no longer appear in the strip.
- [ ] Negative case: if any store survives, the failure copy must show, not the success copy.

## C. Account boundary

- [ ] Sign in as account A, type enough to learn distinctive words, grant AI consent.
- [ ] Sign out, sign in as account B, focus a text field (may take one extra focus, the uid
      check runs after the credential decrypt completes).
- [ ] Account A's learned words are gone from the strip.
- [ ] Both consents are re-asked for account B.
- [ ] Signing out **alone** clears nothing.

## D. Typing performance (review TODO 8, still open)

Sustained fast typing, backspace bursts, capitalization, newline, field switching, app switching.

Report:

- [ ] p50 / p95 / p99 for key release to committed character
- [ ] p50 / p95 / p99 for committed character to app display
- [ ] missed, duplicated, reordered, delayed character counts
- [ ] ONNX initialization state actually reached, and the execution provider
- [ ] Watch `LEXICAL_DEBOUNCE_MS = 24`: a separator pressed within 24 ms of the last letter
      finds no cached decision and silently skips autocorrect. Note if it is observable.

## E. ONNX evidence (review TODO 2, deliberately deferred)

- [ ] After a real typing session, open keyboard settings, Advanced, Developer diagnostics.
- [ ] Record "Reranks attempted", "Reranks that changed the top suggestion" (with its
      percentage), and "Reranks that fell back to lexical".
- [ ] After the beta bundle is live: Play Console, App bundle explorer, Download size, per ABI.
      Compare against a build with the ONNX dependency removed.
- [ ] Both numbers go into `KEYBOARD_REMAINING_TODO.md` TODO 4 to settle keep versus remove.

## F. Release hygiene

- [ ] Build the release AAB with `auraImeBenchmarkTarget` unset. Confirm `applicationId` is
      `dev.varuntej.aura` and `uploadCrashlyticsMappingFileRelease` actually ran.
- [ ] Force a test crash in the beta and confirm it symbolicates in Crashlytics.
