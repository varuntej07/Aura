> **Superseded planning draft.** This describes the Buddy Everywhere / Android keyboard feature
> as originally planned, before it shipped. For the current, as-built architecture, see
> `buddy-keyboard.md` and the "Buddy Everywhere / Android IME" section
> of the root `CLAUDE.md`. Kept here as historical planning context, not current fact.

---

# Buddy Everywhere: Architecture

> Two new surfaces that let users reach Buddy **outside** the Aura app: a one-tap **Voice Launcher** and a **Buddy Keyboard**. Ships on **Android and iOS**. Strategic goal: viral new-user acquisition + a clear path to revenue.
>
> Companion doc: `buddy-everywhere-engineering-plan.md` (task-by-task build plan + money model).
> Research backing: deep-research run (24 sources, 19 adversarially-verified claims) + Auri teardown (below) + codebase map.

---

## 0. Why this exists (the money thesis in one screen)

Aura today only reaches the user when the user opens the app or taps a notification. Every competitor that broke out did it by **living on a surface the user already touches all day**. Two such surfaces:

| Surface | Job | Funnel stage | Evidence |
|---|---|---|---|
| **Buddy Keyboard** (type in any app) | Acquisition | Top of funnel | Rizz keyboard: 130K users + 600K downloads + $250K MRR off the dating-reply wedge. Auri: 4.7★/1.3K ratings, $299 lifetime SKU. A great reply is seen by non-users, so the keyboard is a self-advertising channel. |
| **Voice Launcher** (one tap/gesture to Buddy voice) | Retention | Habit loop | Widgets reached ~15% of US iPhones within 2 months of iOS 14. ChatGPT registers as a default Android assistant, so the slot is open to non-OEM apps. |

The keyboard pulls users in. The voice launcher + memory keep them, which matters because **AI apps churn ~30% faster than non-AI apps (21% vs 31% annual retention)**. The keyboard without the companion memory is just another Rizz clone that spikes and leaks. Buddy's memory is the retention moat.

**How it makes money (maps onto the existing paywall):**
1. **Acquisition:** free Buddy Keyboard in the store, viral replies seen in friends' chats, "made with Buddy" wedge.
2. **Activation:** the magic moment is a drafted reply that sounds exactly like the user and references their life. Generic AI keyboards cannot do this.
3. **Monetization:** free tier with **daily draft/voice caps** (Auri's proven model: caps drive ~97% of upgrades) leading into the existing **Companion ($19.99/mo)** and **Pro ($34.99/mo)** tiers. The keyboard and always-on voice become the concrete reasons to upgrade.

---

## 1. Auri teardown (the closest precedent, and the gap)

Auri (`apps.apple.com/.../id6444628302`) is an "all-in-one personal AI assistant & keyboard." It is the proof that an iOS AI keyboard is shippable and monetizable, and the clearest map of what to copy vs. where to differentiate.

**What Auri's keyboard does (copy the mechanics):**
- Keyboard actions surfaced as tappable buttons + a "magic wand": **Continue Writing, Help Me Write, Email Reply, Grammar/Spelling, Paraphrase (10-25 styles), Translate (27 langs)**.
- **Voice typing / dictation in 80+ languages inside the keyboard.** This requires Full Access (mic + network are only reachable with it). Confirms in-keyboard voice IS possible on iOS, behind the trust tax.
- Works in X, iMessage, WhatsApp, Instagram, TikTok, Snapchat, Gmail, Telegram, Discord, etc.
- **Privacy-forward messaging is the conversion unlock:** "does not track, store, or collect what you type or say"; cannot read password/credit-card fields; only collects account email/name/UUID. This is how they get users past the Full Access warning.
- Degrades gracefully: without Full Access it is still a working plain keyboard (required by Apple guideline 4.4.1).

**What Auri does NOT do (this is Aura's wedge):**
- Auri's keyboard is a **generic writing utility**. Its "memory" lives only in its separate AI **chat**, not in the keyboard. The keyboard does not draft a reply *in your voice* using *your relationships and life*.
- No companion identity. It is a tool, not a friend.

**Auri's monetization (the template):** Free with daily limits, then Pro Weekly $7.99, and a ladder of SKUs up to $179.99 plus a $299 lifetime and a $14.99 family plan. Their own copy says the daily cap "affects nearly 97% of users." Caps are the engine.

**Design conclusion:** clone Auri's keyboard *mechanics and graceful-degradation/privacy posture*, then replace its generic writing engine with **Buddy's memory-aware, in-your-voice drafting**. That single swap is the entire differentiation, and it is exactly the product soul ("Buddy is obsessed with the user").

---

## 2. System overview

```
                         ┌──────────────────────────────────────────────┐
                         │   FastAPI backend (Cloud Run, existing)        │
                         │                                                │
   Buddy Keyboard ──────▶│  NEW  POST /keyboard/draft   (memory-aware)    │
   (Android IME /        │  NEW  POST /keyboard/token   (process auth)    │
    iOS kb extension)    │       reads UserAura/{uid}  (memory CONSUMER)  │
                         │       model_provider.cheap()/balanced()        │
                         │                                                │
   Voice Launcher ──────▶│       GET  /voice/token      (EXISTING)        │
   (widget / tile /      │                                                │
    assistant / Siri) ──┐│                                                │
                        ││└──────────────────────────────────────────────┘
                        ││
                        │└──▶ deep link  aura://voice  ──▶ main Flutter app
                        │                                  HomeViewModel.startSession()
                        │                                  VoiceSessionService (EXISTING,
                        │                                  30-min prewarmed LiveKit token)
                        │
                        └──── (no app launch needed for keyboard; it calls backend directly)
```

Two independent subsystems, deliberately decoupled:

- **Voice Launcher = thin.** Every surface does one thing: fire the `aura://voice` deep link, which launches the existing app and calls the existing `VoiceSessionService.startSession()`. No new backend. No new auth (the app process already holds Firebase auth).
- **Buddy Keyboard = thick.** Runs in a **separate OS process** that cannot see the app's Firebase session, must hold its own auth, and calls a **new backend endpoint**. This is where all the real engineering risk lives.

---

## 3. Component A: Voice Launcher

### A.1 Shared primitive: the `aura://voice` deep link
Today there is **no deep-link config** (`go_router` has no `deepLinks`, no custom URL scheme on either platform except Google OAuth). This is greenfield. We add ONE primitive every surface targets.

- Android: `<intent-filter>` on `MainActivity` for scheme `aura` host `voice` (+ an `https://auravoiceapp.com/voice` App Link for share-style entry).
- iOS: `CFBundleURLTypes` entry for `aura://` + Associated Domains for Universal Links.
- Flutter: `go_router` `deepLinks` (or `app_links` package listener) routes `aura://voice` to a handler that calls `HomeViewModel.startSession(uid)`. Reuse `VoiceSessionService.prewarm()` on app start so the cold-launch-to-voice latency is hidden by the 30-minute prewarmed token.

Cold-launch contract: if the deep link arrives before auth resolves, queue the intent and replay it once `AuthViewModel` reports a signed-in user (mirror the existing `handleThreadNotificationColdLaunch()` pattern).

### A.2 Android surfaces (ordered by impact)
1. **Default digital assistant** (`VoiceInteractionService` + `RoleManager` `android.app.role.ASSISTANT`). Power-button-hold / assist-gesture launches Buddy voice from anywhere. Precedent: ChatGPT does this. This is the "magic" tier.
2. **Home-screen widget** (`home_widget` Flutter package or native Glance) → `aura://voice`.
3. **Quick Settings tile** (`TileService.startActivityAndCollapse`) → `aura://voice`.

### A.3 iOS surfaces (ordered by impact)
1. **App Shortcut (Siri):** "Talk to Buddy" trigger phrase, hands-free, via App Intents.
2. **Action Button** (iPhone 15 Pro+) bound to the Buddy App Intent.
3. **Control Center control + Lock Screen widget** (iOS 18 `ControlWidget`, `OpenIntent`).
4. **Home-screen + Lock Screen widgets** (WidgetKit) → App Intent `.foreground` → start voice.

> iOS cannot make Buddy the *system* assistant (side-button reassignment is iOS 26.2 beta, Japan-only). The Siri shortcut + Action Button + Control Center are the ceiling and are sufficient.

### A.4 Auth
None new. Widgets/tiles/intents only *launch the app*; the app process holds Firebase auth and mints the LiveKit token via the existing `GET /voice/token`.

---

## 4. Component B: Buddy Keyboard

### B.1 What it does (the feature set)
A full working keyboard (must type normally with zero AI, per Apple 4.4.1), plus a **Buddy bar** above the keys with memory-aware actions:

| Action | What Buddy does | Memory used? |
|---|---|---|
| **Reply as me** | Reads the incoming message (text before cursor) and drafts 1-3 replies in the user's voice | Yes (UserAura) |
| **Continue** | Extends what the user started typing, in their style | Yes |
| **Rewrite / tone** | Rewrites selected text (warmer, funnier, shorter, flirtier, professional) | Light |
| **Grammar fix** | One-tap correct selected/all text | No |
| **Translate** | To/from N languages | No |
| **Voice → text** | Dictation (Android always; iOS with Full Access) | No |

The differentiator is **Reply as me** and **Continue**: they call the backend, which loads `UserAura/{uid}` and writes a reply that sounds like the user and references their life. Auri cannot do this.

### B.2 The hard constraint: separate-process auth
The keyboard runs in its own process and **cannot read the app's in-memory Firebase session**. Design:

- **Shared secure storage bridge.** The main app, on login and on token refresh, writes a credential the keyboard process can read:
  - **Android:** `EncryptedSharedPreferences` in the app's own sandbox (the IME runs in the same app UID, so it can read it).
  - **iOS:** **Keychain access group + App Group** shared between the main app target and the keyboard extension target.
- **What to share:** a backend-minted **refresh token / long-lived keyboard token**, NOT the raw Firebase refresh token. The keyboard exchanges it at **`POST /keyboard/token`** for a short-lived access token (or the backend accepts the keyboard token directly on `/keyboard/draft`). This keeps the blast radius small and lets the user revoke keyboard access without signing out everywhere.
- **Fallback / unauth state:** if no credential is present (user never signed in, or revoked), the Buddy bar shows a "Sign in to Aura to use Buddy" chip that deep-links to the app. The keyboard still types normally.

### B.3 The new backend endpoint
**`POST /keyboard/draft`** (auth: keyboard token → resolves `uid`)

```jsonc
// request
{
  "action": "reply | continue | rewrite | grammar | translate | tone",
  "context_before": "string (text before cursor / incoming message)",
  "context_after":  "string (optional)",
  "selected_text":  "string (optional, for rewrite/grammar/translate)",
  "tone":           "warmer | funnier | shorter | flirty | professional (optional)",
  "target_lang":    "string (optional)",
  "host_app":       "com.snapchat... (optional, for analytics + tone priors)",
  "n":              3
}
// response
{ "suggestions": ["...", "...", "..."], "request_id": "..." }
```

- **Memory integration:** for `reply`/`continue`/`rewrite`, load `UserAura/{uid}` through the **existing schema accessors** (`user_aura_schema.py`) and inject a compact digest (voice, interests, current storylines) into the prompt. Reuse the chat prompt-building infra in `services/chat_completion/`.
- **Model tier:** latency-critical, so `model_provider.cheap()` (Haiku/Flash) with the existing fallback chain. Hard timeout (the keyboard must never hang); on timeout return a graceful single suggestion or an empty list with a coded reason.
- **Privacy (critical, see §6):** the endpoint is a **memory CONSUMER, not a producer**. It does NOT persist what the user typed into UserAura by default. Optional, consent-gated "learn from my keyboard" can write back, off by default.

### B.4 Native module layout
- **Android:** `BuddyImeService : InputMethodService` (Kotlin) renders keys + the Buddy bar; calls backend via OkHttp with the cached token; inserts via `InputConnection.commitText`. New service declared in `AndroidManifest.xml` with `BIND_INPUT_METHOD` + an IME settings activity.
- **iOS:** new **Keyboard Extension target** (`UIInputViewController`, Swift) with `RequestsOpenAccess=true`; calls backend via `URLSession`; App Group + Keychain access group entitlements shared with `Runner`.
- **Flutter bridge:** no existing `MethodChannel` (greenfield). Add a thin channel only for the in-app keyboard onboarding flow (deep-link to enable keyboard, check enabled state, prompt for Full Access). The keyboard's runtime drafting path is **native → backend directly**, NOT through Flutter (Flutter engine is too heavy for a keyboard extension's ~60MB iOS memory budget).

---

## 5. Data flow sequences

**Voice Launcher (one tap → talking):**
```
User taps widget / holds power / "Hey Siri talk to Buddy"
  → OS fires aura://voice (or App Intent .foreground)
  → app launches (or foregrounds), auth already resolved
  → deep-link handler → HomeViewModel.startSession(uid)
  → VoiceSessionService: reuse 30-min prewarmed LiveKit token → connect → Buddy speaks
```

**Keyboard "Reply as me":**
```
User in WhatsApp taps Buddy bar → "Reply as me"
  → IME reads getTextBeforeCursor() (the incoming message)
  → reads cached keyboard token from EncryptedSharedPrefs / Keychain group
  → POST /keyboard/draft {action:"reply", context_before:...}
  → backend resolves uid, loads UserAura digest, cheap() LLM, returns 3 suggestions
  → IME shows 3 chips; user taps one → InputConnection.commitText() inserts it
  → (no typed text persisted; analytics event fired)
```

---

## 6. Privacy, security, and store-compliance architecture

This is not optional polish. A keyboard that reads everything typed is the single biggest trust and policy risk in the whole plan.

**Principles baked into the design:**
1. **Consumer not producer.** The keyboard sends the *current* context to fulfill an action the user explicitly tapped. It does not silently harvest everything typed into long-term memory. (Apple guideline: keyboard data collection only to enhance the keyboard. Building a profile from all typing violates it.)
2. **No keystroke logging, ever.** Nothing is sent to the backend except when the user taps a Buddy action. No background transmission. Match Auri's exact messaging: "Buddy only sees a message when you tap to draft."
3. **Field exclusions.** Never engage in password / credit-card / OTP fields (both OSes expose secure-field flags; respect them).
4. **Graceful degradation.** Typing works fully without Full Access (iOS) and without network. AI is additive (Apple 4.4.1 hard requirement).
5. **Revocable, scoped credential.** Keyboard uses a dedicated revocable token, not the raw Firebase session.
6. **Prominent disclosure + incremental consent** (Google Play requirement): a clear in-app disclosure screen before the keyboard is enabled, and Full Access asked for with plain-English justification.
7. **Do NOT use AccessibilityService** as a shortcut to read/act across apps. Google Play strictly prohibits autonomous plan-and-execute via the Accessibility API. The IME path is the only sanctioned one.

**GDPR/consent reuse:** gate memory-aware drafting behind the existing `aura_consent_granted` flag (same gate `user_aura_extractor.py` uses). No consent → keyboard still does grammar/translate/voice, just not memory-aware "reply as me."

---

## 7. Analytics and the funnel

Reuse the PostHog funnel pattern (`funnel_events.py` / `funnel_events.dart`, kept in sync by `test_funnel_event_contract.py`). New funnels:

- **Keyboard:** `keyboard_enabled` → `keyboard_full_access_granted` → `keyboard_draft_requested {action, host_app}` → `keyboard_suggestion_inserted` → (paywall) `keyboard_limit_hit` → `paywall_intent`.
- **Voice Launcher:** `voice_surface_added {type}` → `voice_launched_from_surface {type}` → `voice_session_completed`.

These two funnels are how you will *prove* the money thesis: install→activate→cap→upgrade for the keyboard, and surface→habit for voice.

---

## 8. Risks and open questions

| Risk | Severity | Mitigation |
|---|---|---|
| iOS Full Access opt-in rate is low | High | Auri-style privacy messaging; make grammar/translate/voice useful even pre-memory so users grant it for utility, then discover "reply as me". |
| Apple rejects AI-first keyboard (4.4.1 / data-collection) | High | Plain keyboard fully works without Full Access; frame AI as on-demand; no keystroke logging; explicit privacy copy. Auri passed review with this posture. |
| Keyboard latency makes drafting feel slow | Med | `cheap()` model, prewarm token, hard timeout, optimistic "thinking" chip, cache the user's memory digest server-side. |
| Separate-process auth complexity (esp. iOS Keychain group) | Med | Dedicated `/keyboard/token`; ship Android first to de-risk, port the proven contract to iOS. |
| Memory write-back violates Apple keyboard policy | Med | Default OFF; consumer-only; consent-gated opt-in. |
| Acquisition spike then churn (AI-app pattern) | Med | Voice launcher + memory as the retention counterweight; cap-to-upgrade rather than cap-to-quit. |

**Open questions for product:**
1. Keyboard free-tier cap: drafts/day? voice-minutes/day? (Set from Auri's "caps drive 97% of upgrades" but tuned to feel generous first week.)
2. Is "Reply as me" Companion-tier or Pro-tier? (Recommend: a few free/day on Free, unlimited on Companion.)
3. iOS keyboard: full build now, or iOS "reply assist via Share Sheet + App Intent" first while Android keyboard validates the wedge?
```
