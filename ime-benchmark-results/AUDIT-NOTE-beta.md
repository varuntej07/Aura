# IME scope audit, keyboard beta consent change

Baseline: `98b5bc9` (the cherry-picked `c440b37`, before the consent work).
Command: `python android/tools/keyboard_audit/audit_ime_scope.py . --base 98b5bc9`
Exit: 2 (`passed: false`). Report: `audit-ime-scope-beta.json`.

## Why it fails, and why that is the intended outcome

The audit exists to make an unreviewed change to the voice boundary impossible to land quietly.
Adding a consent gate in front of the microphone is a voice-boundary change, so it should trip.
This note is the deliberate re-baseline.

**Exactly one check fails:** `buddy_voice_lines_equal`. Everything else passes.

| Check | Result |
|---|---|
| `voice_excluded_path_changes` | none |
| `voice_named_path_changes` | none |
| `protected_voice_methods` (all 10 hashed) | **all unchanged** |
| `protected_manifest_and_drafting_files` | **all unchanged** (manifest, `method.xml`, `KeyboardDraftClient.kt`) |
| `voice_dependency_lines_equal` | true |
| `key_path.ordinary_letter_banned_tokens` | none |
| `key_path.prediction_publication_banned_tokens` | none |
| `key_path.separator_traversal_tokens` | none |
| `key_path.automatic_vocab_hints_reference` | false |
| `buddy_voice_lines_equal` | **false** |

No voice method body changed. `openVoice`, `startVoiceSession`, `handoffToAppVoice`,
`renderVoice`, `ensureVoiceStage`, `buildVoiceStage`, `teardownVoiceStage`, `onVoiceTranscript`,
`stopVoice` and `launchAppVoice` all hash identically to the baseline. That is by design: the
gate wraps the two CALL SITES of `openVoice()` rather than living inside the voice path, so the
hard boundary those methods define is exactly as small as it was.

## The complete set of changed voice-matching lines

Removed (2), both call sites:

```
            else -> openVoice()
        addView(makeToolbarIcon(R.drawable.ic_widget_mic, "Talk to Buddy") { openVoice() })
```

Added (11), the same two call sites plus the new consent code:

```
            else -> withVoiceConsent { openVoice() }
            withVoiceConsent { openVoice() }
    private enum class ConsentAsk { AI_TEXT, VOICE }
     * Open the takeover purely to ask. Used for the voice disclosure, which is reachable from
     * reads the clipboard rather than the field, and in a password or OTP field the voice
            ConsentAsk.VOICE -> {
            ConsentAsk.VOICE -> KeyboardConsentStore.setVoiceConsent(this, granted)
     * it. Deliberately wraps the CALL SITES rather than living inside the voice methods, so the
     * hard voice boundary those methods define stays as small as it already is.
    private fun withVoiceConsent(start: () -> Unit) {
        if (KeyboardConsentStore.voiceGranted(this)) start() else openConsentPanel(ConsentAsk.VOICE, start)
```

Nine of the eleven are the new consent code and its comments. The behavioural change is two
lines: the mic no longer starts a session until the user has seen what it sends and accepted.

## Re-baseline

After this lands, the audit baseline for future runs is the merge commit of this branch. Nothing
was renamed to avoid the check, and the check itself was not modified.
