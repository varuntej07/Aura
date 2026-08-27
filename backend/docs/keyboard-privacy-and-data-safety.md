# Aura Keyboard: privacy, disclosure, and Play Data Safety

Three surfaces have to say the same thing, or the keyboard is misrepresenting itself:

1. the in-product strings (`android/app/src/main/res/values/strings.xml` and the in-keyboard
   disclosure panels in `BuddyImeService`),
2. the hosted Privacy Policy at `https://auravoiceapp.com/privacy-policy` (source lives in the
   separate `Aura-Web` repo, `src/app/privacy`),
3. the Google Play Data Safety form.

This document is the single source those three copy from. Change it first, then the three.

A keyboard is a special case on Play: it can observe everything a person types, including in
other apps. Shipping one whose declared behaviour does not match its actual behaviour is the
kind of mismatch that gets an app removed rather than rejected.

---

## What actually happens, mechanically

### Stays on the device, always

- **Every keystroke.** Input handling, composing, autocorrect, and suggestion ranking are local.
- **Learned personalization.** Words, n-grams, and corrections are stored in an AES-GCM snapshot
  under an Android Keystore key, with generation-bound AAD, in `noBackupFilesDir`
  (`EncryptedPersonalizationStore`). It is excluded from cloud backup and device transfer
  (`res/xml/backup_rules.xml`, `res/xml/data_extraction_rules.xml`) because a Keystore-wrapped
  file restored onto a different device is undecryptable anyway.
- **Suggestion reranking.** The ONNX model runs locally. No inference leaves the phone.
- **Diagnostics.** Status and counters only, in process memory, never written to disk or logs
  (`KeyboardRuntimeDiagnostics`, `NeuralRerankMetrics`).

### Leaves the device, only on an explicit tap, only after consent

| Trigger | What is sent | Where | Gate |
|---|---|---|---|
| An Aura writing action (Proofread, Rephrase, Continue, Translate, tone actions) | Up to 2,000 characters before the cursor in the current field | `POST /keyboard/draft` | `KeyboardConsentStore.aiTextGranted` |
| "Reply as me" | The clipboard contents, skipped entirely when they look like an OTP or a generated credential (`looksLikeSecret`) | `POST /keyboard/draft` | same |
| The microphone button | Live audio, plus up to 2,000 characters of the current field as `screen_context` | LiveKit room to the Aura voice agent | `KeyboardConsentStore.voiceGranted` |

**Never sent, structurally:** anything in a password, PIN, OTP, numeric, phone, email, or URL
field. `FieldProfile.memoryActionsAllowed` gates the writing panel out of those fields entirely,
and suppresses `screen_context` for voice in them, so a voice session started from a password
field carries audio only.

### Consent behaviour

- Each of the two transmitting features is disclosed **before its first transmission**, in an
  in-keyboard panel that states concretely what is sent.
- The disclosure copy is field- and action-aware. Reply says "the message you copied"; a voice
  session in a password field says the field is not sent.
- Declining is recorded and returns to the keys. **Every local feature keeps working**: typing,
  autocorrect, suggestions, learning, emoji, themes.
- A decline is not permanent. Tapping the action again re-offers the panel. It is not re-offered
  unprompted.
- Both are revocable at any time in Aura Keyboard settings, under "Privacy and learned data".
- Consent is versioned (`CURRENT_CONSENT_VERSION`). If the copy changes what is actually sent,
  a stored "granted" reads back as "never asked" and the user agrees to the new wording.

### Deletion

"Clear learned words and personalization" enumerates every keyboard-owned store and **verifies
each is gone before reporting success**:

- the encrypted personalization snapshot,
- its Android Keystore key alias,
- the legacy plaintext SQLite dictionary (migration-window leftover),
- `buddy_keyboard_vocab` (cloud-derived vocabulary hints),
- `buddy_kb_emoji` (recently used emoji).

Any store that survives throws, and the user sees the failure message, not a success message.
See `EncryptedPersonalizationStore.clearAll` and `KeyboardOwnedStores`.

Behaviour preferences (theme, haptics) and the consent record itself are deliberately **not**
cleared: they contain no typed text or learned vocabulary, and silently re-asking for consent
someone already answered is not what "clear my learned words" means.

### Account boundary

Personalization is device-local and not namespaced by account. When the signed-in Aura account
changes on a device, the keyboard clears the personalization snapshot and resets both consents,
so a second user never inherits the first user's vocabulary or their agreement to transmit.
Signing out alone clears nothing.

---

## Privacy Policy section copy

Paste into `Aura-Web` `src/app/privacy` under a heading of "Aura Keyboard", anchored at
`#aura-keyboard` so `KeyboardSettingsActivity.PRIVACY_URL` can link straight to it.

> ### Aura Keyboard
>
> Aura Keyboard is an optional Android keyboard. When you enable it, Android tells you it can
> read what you type. Here is exactly what we do with that.
>
> **Your typing stays on your phone.** Keystrokes, autocorrect, and word suggestions are
> processed entirely on your device. We do not receive, store, or transmit what you type.
>
> **What the keyboard learns, it keeps locally.** Words, phrases, and corrections it picks up
> are stored encrypted on your device using a key held in the Android Keystore. This data is
> excluded from Google cloud backup and from device-to-device transfer. It is never uploaded to
> us, and we cannot read it.
>
> **Text is sent only when you explicitly ask for it.** Two features send data, and each asks
> for your permission before the first time it does:
>
> - *Aura writing actions* send the text in the field you are typing in, up to 2,000 characters,
>   to our servers so Buddy can draft or rewrite it. "Reply as me" sends the message you copied
>   instead. If what you copied looks like a one-time code or a password, it is not sent.
> - *Talking to Buddy* streams your microphone audio to our servers while the mic is on, along
>   with the text in the field so Buddy knows what you are working on.
>
> If you decline either, the keyboard keeps working normally for everything else. You can turn
> either back on, or off again, in Aura Keyboard settings at any time.
>
> **Private fields are excluded.** In password, PIN, one-time-code, numeric, phone, email, and
> web address fields, the writing features are unavailable and no field text is ever sent, even
> if you start a voice session.
>
> **Processing.** Text and audio you explicitly send are processed by us and by our model and
> speech providers to produce a response. They are not used to train models.
>
> **Deleting it.** "Clear learned words and personalization" in Aura Keyboard settings removes
> everything the keyboard has learned on that device, including saved vocabulary hints and your
> recently used emoji. The keyboard confirms only after it has verified each one is gone. If you
> sign in with a different Aura account on the same device, the previous account's learned data
> is cleared automatically.
>
> **Diagnostics.** The optional developer diagnostics screen shows status values and counters
> only. It never displays, stores, or transmits anything you typed.

---

## Play Data Safety answers

Answer the console form exactly as below. "Collected" means it leaves the device; "shared" means
it goes to a third party.

| Data type | Collected? | Shared? | Purpose | Optional? | Notes |
|---|---|---|---|---|---|
| **Personal info: user IDs** | Yes | No | App functionality, account management | No | The Firebase uid authenticates `/keyboard/draft`. |
| **Messages: other in-app messages** | Yes | Yes | App functionality | **Yes** | Only the field text or copied message you explicitly submit to an Aura writing action. Shared with model providers to generate the response. Not collected in secure fields. Not used for training. |
| **Audio: voice or sound recordings** | Yes | Yes | App functionality | **Yes** | Only while you have started a Buddy voice session from the keyboard. Shared with speech and model providers. Not retained after the session. |
| **App activity: in-app search history** | No | No | | | |
| **App info and performance: crash logs, diagnostics** | Yes | No | Analytics, crash prevention | No | Crashlytics. Contains no typed text. |
| **Device or other IDs** | Yes | No | Analytics, crash prevention | No | |
| **Keystrokes / typed text generally** | **No** | No | | | Processed on-device only. This is the answer that must stay true. |
| **Contacts, location, photos, files, calendar, health, financial** | No | No | | | The keyboard requests none of these. |

Also declare:

- **Data is encrypted in transit:** yes.
- **Users can request data deletion:** yes. In-product, via Aura Keyboard settings, plus the
  account deletion path in the Aura app.
- **Independent security review:** do not claim one. There has not been one.
- **Committed to the Play Families policy:** unchanged from the app's current answer.

If any answer above stops being true, this file, the policy, and the form all change together.

---

## In-product wording, and the claim each one must not exceed

| String / surface | Claim it makes | Must stay true because |
|---|---|---|
| `keyboard_settings_ai_summary`: "Ordinary typing stays local. Text or audio is sent only when you tap an Aura AI or voice action." | Nothing transmits without an explicit tap | Gates in `runDraft` and at the `openVoice` call sites |
| `keyboard_settings_local_summary`: "Learned words, corrections, and personalization are encrypted on this device." | Local, encrypted, not uploaded | `EncryptedPersonalizationStore`, Keystore-backed AES-GCM |
| Intro panel: "stays encrypted on this device" | as above | as above |
| AI panel: "up to 2000 characters ... to Aura's servers" | The exact volume | `sourceTextFor` takes 2000, matching backend `CONTEXT_MAX_CHARS` |
| Voice panel, secure field: "nothing you've typed here is sent" | No `screen_context` | `fieldProfile.memoryActionsAllowed` is false there |
| `keyboard_settings_clear_confirm_message` | Names every store cleared | `KeyboardOwnedStores` plus the encrypted and legacy artifacts |
| `keyboard_settings_diagnostics_note`: "status and counters only, never typed text" | Diagnostics are content-free | `NeuralRerankMetrics` and `NeuralRuntimeDiagnostics` hold numbers only |

---

## Deliberately not claimed

- **No federated learning.** There is none. Do not add consent language for it.
- **No independent audit.** Do not imply one.
- **No claim that vocabulary hints personalize typing.** `VocabHintsCache` and `ProperNounIndex`
  are dormant in this release and `GET /keyboard/vocab` has no consumer.
- **No claim that declining degrades typing.** It does not, and the beta QA pass verifies that
  explicitly.
