> **Superseded planning draft.** This describes the Buddy Everywhere / Android keyboard feature
> as originally planned, before it shipped. For the current, as-built architecture, see
> `buddy-keyboard.md` and the "Buddy Everywhere / Android IME" section
> of the root `CLAUDE.md`. Kept here as historical planning context, not current fact.

---

# Buddy Everywhere

> One readable overview of the two new surfaces that let people reach Buddy **outside** the Aura app: a one-tap **Voice Launcher** and a **Buddy Keyboard**. Read this to understand the problem, how it is built, how it looks, how people benefit, what it costs, how fast it is, what ships, on which platforms, and how long it takes to build with Claude Code.
>
> This is the plain-language merge of `buddy-everywhere-architecture.md` (the system design) and `buddy-everywhere-engineering-plan.md` (the task-by-task build plan). Those two stay as the detailed source of truth; this is the one-sitting read.

---

## 1. The problem

Aura only reaches a user when that user opens the app or taps a notification. That is the ceiling on how often Buddy can help, and it is the ceiling on growth.

Two facts make this urgent:

1. **Every breakout AI companion lived on a surface the user already touches all day.** A reply keyboard, a widget, a voice shortcut. Aura has none of these yet. There is currently no deep-link scheme at all (the router has no `deepLinks`, and the only existing custom URL handling is Google OAuth). So the surfaces a user touches between app sessions are blank space for us today.
2. **AI apps churn faster than normal apps** (roughly 21% annual retention for AI apps versus 31% for non-AI). A viral spike that leaks out the bottom is the default failure mode for this category.

So the problem is two-sided: **we cannot get in front of new people** (no top-of-funnel surface), and **the people we get tend to leave** (the churn pattern). Buddy's memory is the thing that fixes the second half, but only if Buddy is reachable on the surfaces where the churn happens.

**The wedge that makes this different from a generic AI keyboard:** Buddy already knows the user (voice, interests, relationships, current storylines via `UserAura/{uid}`). A reply drafted *in the user's own voice, referencing their actual life* is something a generic writing keyboard structurally cannot produce. That is the entire differentiation, and it lines up exactly with the product soul: Buddy is obsessed with the user.

---

## 2. What we are building

Two surfaces, deliberately decoupled, each with a different job:

| Surface | What it is | Its job | Funnel role |
|---|---|---|---|
| **Voice Launcher** | One tap or gesture (widget, tile, assistant, Siri, Action Button) that drops you straight into a Buddy voice session | Kill the friction of starting a conversation | **Retention.** Friction is the #1 reason companion habits die. One gesture means many sessions a day, which is the habit a subscription is built on. |
| **Buddy Keyboard** | A real keyboard you can type in inside any app, with a "Buddy bar" above the keys that drafts replies *in your voice* using *your memory* | Make the user look good to other people in the apps they already live in | **Acquisition.** Every memory-aware reply sent in someone else's chat is a silent ad. This is the Rizz-keyboard mechanic (one such keyboard reached ~130K users and ~$250K MRR off the dating-reply wedge alone). |

The keyboard pulls people in. The voice launcher plus memory keeps them. The keyboard without the memory is just another clone that spikes and leaks; the memory is the moat.

**The closest precedent is Auri**, an iOS AI keyboard that proves the model is shippable and monetizable (4.7 stars, paid tiers up to a $299 lifetime SKU). Auri's keyboard is a *generic* writing utility. Its memory lives only in its separate chat, never in the keyboard. We copy Auri's mechanics, graceful degradation, and privacy posture, then swap its generic writing engine for Buddy's memory-aware, in-your-voice drafting. That single swap is the product.

---

## 3. How it is built

### The shape: one thin subsystem, one thick subsystem

```
                         ┌──────────────────────────────────────────────┐
                         │   FastAPI backend (Cloud Run, existing)       │
                         │                                               │
   Buddy Keyboard ──────▶│  NEW  POST /keyboard/draft   (memory-aware)   │
   (Android IME /        │  NEW  POST /keyboard/token   (process auth)   │
    iOS kb extension)    │       reads UserAura/{uid}  (memory CONSUMER) │
                         │       model_provider.cheap()/balanced()       │
                         │                                               │
   Voice Launcher ──────▶│       GET  /voice/token      (EXISTING)       │
   (widget / tile /      │                                               │
    assistant / Siri)    └──────────────────────────────────────────────┘
        │
        └──▶ deep link  aura://voice  ──▶ main Flutter app
                                          HomeViewModel.startSession()
                                          VoiceSessionService (EXISTING,
                                          30-min prewarmed LiveKit token)
```

**Voice Launcher = thin.** Every surface does exactly one thing: fire the `aura://voice` deep link. That launches the existing app and calls the existing `VoiceSessionService.startSession()`. No new backend. No new auth, because the app process already holds the Firebase session and already mints the LiveKit token through `GET /voice/token`. All the work is wiring OS surfaces to one deep link.

**Buddy Keyboard = thick.** A keyboard runs in its **own OS process**. It cannot see the app's in-memory Firebase session, so it must carry its own credential, and it calls a **new backend endpoint**. This is where the real engineering risk lives.

### The one shared primitive: `aura://voice`

This is greenfield (no deep links exist today). We add one custom URL scheme plus an App/Universal Link that every Voice Launcher surface targets:

- **Android:** an `<intent-filter>` on `MainActivity` for scheme `aura` host `voice`, plus an `https://auravoiceapp.com/voice` App Link.
- **iOS:** a `CFBundleURLTypes` entry for `aura://` plus Associated Domains for Universal Links.
- **Flutter:** `go_router` `deepLinks` (or the `app_links` package) routes `aura://voice` to a handler that calls `HomeViewModel.startSession(uid)`.
- **Cold-launch contract:** if the deep link arrives before auth resolves, the intent is queued and replayed once the user is signed in. This mirrors the existing `handleThreadNotificationColdLaunch()` pattern, so we are reusing a proven approach, not inventing one.

### The keyboard's hard part: separate-process auth

Because the keyboard cannot read the app's Firebase session, the main app writes a **revocable keyboard credential** to shared secure storage on login and on token refresh:

- **Android:** `EncryptedSharedPreferences` in the app's own sandbox. The IME runs under the same app UID, so it can read it.
- **iOS:** a **Keychain access group + App Group** shared between the `Runner` target and the keyboard extension target. This is the fiddliest piece of the whole project.

What gets shared is a backend-minted **keyboard token**, not the raw Firebase refresh token. The keyboard exchanges it at `POST /keyboard/token` for a short-lived access token. This keeps the blast radius small and lets a user revoke keyboard access without signing out everywhere. If no credential is present (never signed in, or revoked), the Buddy bar shows a "Sign in to Aura" chip and the keyboard still types normally.

### The new backend endpoint

`POST /keyboard/draft` (auth: keyboard token resolves to `uid`)

```jsonc
// request
{
  "action": "reply | continue | rewrite | grammar | translate | tone",
  "context_before": "text before cursor / the incoming message",
  "context_after":  "optional",
  "selected_text":  "optional, for rewrite/grammar/translate",
  "tone":           "warmer | funnier | shorter | flirty | professional (optional)",
  "target_lang":    "optional",
  "host_app":       "com.snapchat... (optional, for analytics + tone priors)",
  "n":              3
}
// response
{ "suggestions": ["...", "...", "..."], "request_id": "..." }
```

- For `reply` / `continue` / `rewrite` it loads `UserAura/{uid}` through the existing schema accessors (`user_aura_schema.py`) and injects a compact digest (voice, interests, current storylines) into the prompt, reusing the chat prompt-building infra in `services/chat_completion/`.
- It runs on `model_provider.cheap()` (Haiku/Flash) with the existing fallback chain, with a hard timeout so the keyboard never hangs.
- It is a **memory consumer, not a producer.** It does not persist what the user typed into `UserAura` by default. Memory-aware drafting is gated behind the existing `aura_consent_granted` flag, the same GDPR gate `user_aura_extractor.py` uses. No consent means grammar/translate/voice still work, just not "reply as me."

### Native module layout

- **Android keyboard:** `BuddyImeService : InputMethodService` (Kotlin) renders keys plus the Buddy bar, calls the backend with the cached token, and inserts text via `InputConnection.commitText`.
- **iOS keyboard:** a new Keyboard Extension target (`UIInputViewController`, Swift) with `RequestsOpenAccess=true`, calling the backend via `URLSession`, sharing App Group + Keychain entitlements with `Runner`.
- **Important constraint:** the keyboard's runtime drafting path is **native to backend directly, not through Flutter.** The Flutter engine is too heavy for an iOS keyboard extension's ~60MB memory budget. A thin `MethodChannel` is used only for the in-app onboarding flow (enable the keyboard, check enabled state, prompt for Full Access).

### Data flow, end to end

**Voice (one tap to talking):**
```
tap widget / hold power / "Hey Siri, talk to Buddy"
  → OS fires aura://voice (or an App Intent .foreground)
  → app launches or foregrounds, auth already resolved
  → deep-link handler → HomeViewModel.startSession(uid)
  → VoiceSessionService reuses the 30-min prewarmed LiveKit token → connect → Buddy speaks
```

**Keyboard "Reply as me":**
```
in WhatsApp, tap Buddy bar → "Reply as me"
  → IME reads getTextBeforeCursor() (the incoming message)
  → reads the cached keyboard token from EncryptedSharedPrefs / Keychain group
  → POST /keyboard/draft {action:"reply", context_before:...}
  → backend resolves uid, loads UserAura digest, cheap() LLM, returns 3 suggestions
  → IME shows 3 chips; tap one → InputConnection.commitText() inserts it
  → nothing typed is persisted; an analytics event fires
```

---

## 4. How it looks and feels (UI/UX)

**Voice Launcher** has almost no UI of its own. It is a set of native OS entry points that each launch straight into the existing voice session screen:

- A home-screen widget that says "Talk to Buddy."
- A pull-down Quick Settings tile.
- The system assist gesture (power-button hold) when Buddy is the chosen assistant.
- On iOS: a Siri phrase, the Action Button, a Control Center control, and Lock Screen / home-screen widgets.

The felt experience is "Buddy is one gesture away from anywhere," and the conversation lands in the app's existing voice surface, so there is nothing new to learn.

**Buddy Keyboard** is a normal keyboard with one extra strip: the **Buddy bar** above the keys. It holds the memory-aware actions as tappable chips plus a magic-wand affordance, in the spirit of Auri's button layout. When you tap an action like "Reply as me," the bar shows a short "thinking" state, then presents up to 3 suggestion chips; tapping one inserts it inline. In password, credit-card, and OTP fields the Buddy bar hides itself entirely. The in-app onboarding (to enable the keyboard and explain privacy) uses the app's own cream/glass design system (`app_colors.dart`, `glass_card.dart`), so enabling it feels like part of Aura, not a system dialog dump.

The design rule throughout: **the keyboard must be a great plain keyboard first.** AI is additive. Apple's guideline 4.4.1 requires typing to fully work with no AI and no network, and Android should not regress basic typing either. Everything degrades gracefully: no Full Access, no network, or no sign-in still leaves you with a working keyboard.

---

## 5. Features

| Surface | Feature | Memory used? |
|---|---|---|
| Voice Launcher | Home-screen widget, Quick Settings tile, default-assistant gesture (Android) | n/a |
| Voice Launcher | Siri phrase, Action Button, Control Center, Lock Screen + home widgets (iOS) | n/a |
| Keyboard | **Reply as me** (drafts 1-3 replies to the incoming message in your voice) | Yes |
| Keyboard | **Continue** (extends what you started typing, in your style) | Yes |
| Keyboard | **Rewrite / tone** (warmer, funnier, shorter, flirtier, professional) | Light |
| Keyboard | **Grammar fix** (one-tap correct) | No |
| Keyboard | **Translate** (to/from N languages) | No |
| Keyboard | **Voice to text** (dictation; Android always, iOS with Full Access) | No |
| Monetization | Daily free-tier caps on memory-aware drafts (and optionally voice minutes) that lift on upgrade | n/a |

The two features that only Buddy can do are **Reply as me** and **Continue**, because they read `UserAura` and write in the user's voice referencing the user's life. Everything else (grammar, translate, voice) is table-stakes utility whose job is to be useful enough that people grant Full Access on iOS, after which they discover the memory features.

---

## 6. Platforms

Ships on **Android and iOS**, Android first on purpose.

- **Android** is the unrestricted platform: a keyboard (IME) is a first-class citizen, and an app can register as the default digital assistant (the power-button-hold "magic" tier, the same slot ChatGPT uses). So we validate the wedge and the money loop here where there is no platform tax.
- **iOS** follows the proven contract. iOS keyboards work but sit behind the Full Access "trust tax," and iOS cannot make Buddy the *system* assistant. The realistic iOS ceiling for voice is the Siri shortcut + Action Button + Control Center, which is enough. The keyboard on iOS follows Auri's exact Full-Access pattern.

One thing we explicitly do **not** do on Android: use an `AccessibilityService` to read or act across apps. Google Play strictly prohibits autonomous plan-and-execute via the Accessibility API. The sanctioned IME path is the only one we take.

---

## 7. How users benefit (with non-obvious examples)

The obvious benefit is "faster replies." The non-obvious benefits are where the memory earns its keep:

1. **The landlord text you keep putting off.** Your property manager messages about a lease renewal. You open the reply box, tap "Reply as me," and Buddy drafts a polite, firm reply that sounds like you and already knows you mentioned wanting a 12-month term last week. A generic keyboard would give you a bland template; Buddy gives you *your* answer, so you actually send it instead of leaving it on read for three days.

2. **"Continue" that knows which trip "the trip" is.** You start typing "so excited for the trip, I was thinking we could" and hit Continue. Because Buddy tracks your storylines, it extends with the actual destination and the friend you are going with, not a generic guess. The non-obvious part: the value is not the writing, it is that you did not have to re-explain context the keyboard already had.

3. **Hands-free accountability while driving.** You are in the car, brain racing, and you hold the power button: "Buddy, remind me to email the clinic when I get home, and talk me through what I'm dreading about it." No unlocking, no app hunting. For the ADHD-accountability use case Aura is aimed at, removing the unlock-and-navigate friction is the difference between a habit and an abandoned app.

4. **Replying warmly to a friend about a thing you forgot they care about.** A friend texts about their marathon. You tap "Reply as me" and Buddy, which already logged that they had a knee injury, drafts a reply that asks about the knee. You look like a thoughtful friend. The keyboard quietly made your relationship better, which is the product soul showing up in someone else's chat.

5. **Code-switching tone without rewriting.** Same message, you tap "tone: professional" for the work Slack and "tone: funnier" for the group chat. Light memory keeps it sounding like you in both, instead of like a corporate auto-responder in one and a meme bot in the other.

The throughline: people pay for tools that make them **look good to other people** and **save time** (Grammarly and Auri prove it). Memory is the part neither of those has, and it is what makes the replies feel like *you* rather than like AI.

---

## 8. Privacy and store compliance (this is part of how it works, not polish)

A keyboard that reads everything typed is the single biggest trust and policy risk in the plan, so the design bakes in protection:

- **Consumer, not producer.** The keyboard sends the *current* context only when you tap an action. It does not silently harvest everything you type into long-term memory. (Optional "learn from my keyboard" write-back exists but is OFF by default and consent-gated.)
- **No keystroke logging, ever.** Nothing reaches the backend except on an explicit tap. The user-facing promise mirrors Auri: "Buddy only sees a message when you tap to draft."
- **Field exclusions.** Never engages in password, credit-card, or OTP fields.
- **Graceful degradation.** Typing fully works without Full Access and without network.
- **Revocable scoped credential.** A dedicated keyboard token, not the raw Firebase session.
- **Prominent disclosure + incremental consent.** A clear in-app screen before the keyboard is enabled (a Google Play requirement), and Full Access requested with plain-English justification.

These are not just store-compliance checkboxes; they are the conversion unlock. Auri got users past the scary Full Access warning precisely with this posture, and we copy it.

---

## 9. Cost

**Voice Launcher: effectively zero new infrastructure cost.** It reuses the existing voice path end to end (no new backend, no new auth, no new always-on service). The only marginal cost is that more voice sessions get started, which is the existing per-session LiveKit + LLM cost, and that is the cost we *want* because it means the habit is working.

**Buddy Keyboard: a deliberately cheap per-draft cost.** Drafting runs on `model_provider.cheap()` (Haiku/Flash), the lowest tier, and the user's memory digest is cached server-side so each draft is a small, fast prompt rather than a full context rebuild. The two new endpoints (`/keyboard/draft`, `/keyboard/token`) run on the existing Cloud Run service, so there is no new infrastructure to stand up.

A hard per-draft dollar figure is not pinned down in the source plans, and I will not invent one. What *is* decided is the cost-control lever: cheapest model tier + cached digest + a hard timeout, with daily free-tier caps (see §11) putting a ceiling on free-user spend so cost scales with paying usage rather than running away on free traffic.

---

## 10. Latency

Latency is treated as a feature, because a keyboard that feels slow is dead. The design levers, all stated in the plan:

- **Voice:** the 30-minute prewarmed LiveKit token hides the cold-launch-to-voice delay, so "tap to Buddy talking" feels near-instant on a warm token.
- **Keyboard:** the `cheap()` (fast) model tier; a server-side cached memory digest so the draft prompt is small; a **hard request timeout** so it can never hang; an optimistic "thinking" chip so the wait is visible and bounded; and a coded graceful fallback (a single suggestion or a clean empty state) on timeout instead of a spinner that never ends.
- The acceptance bar in the plan is **p95 latency under the keyboard timeout** for `/keyboard/draft`.

The exact timeout value and a target millisecond budget are not fixed in the source docs, so treat "p95 under the timeout, never hang, always show a bounded state" as the contract rather than a specific number. This also matches the app-wide rule that no user-facing wait is ever unbounded.

---

## 11. Outcome (what success looks like, and the money model)

Success is a closed loop that fights the AI-app churn pattern:

1. **Acquisition (free, viral).** A free Buddy Keyboard in the store. Every memory-aware reply sent in a friend's chat is a silent ad. This is the proven Rizz mechanic (~130K users, ~$250K MRR off one wedge).
2. **Activation.** The magic moment is the first "Reply as me" that nails the user's voice and references their life. This is instrumented so we can see exactly when it happens and how fast.
3. **Conversion.** Auri's proven engine: generous-feeling daily free caps that nearly all active users eventually hit (Auri's own copy says the cap "affects nearly 97% of users"), leading into the existing **Companion ($19.99/mo)** and **Pro ($34.99/mo)** tiers. The cap is a tasteful upgrade sheet, not a wall. Voice-always-on and unlimited drafts are the carrots.
4. **Retention (the moat).** The voice launcher habit plus memory that deepens over time is the counterweight to the ~30% faster AI-app churn. The strategy is cap-to-upgrade, never cap-to-quit.

Two PostHog funnels prove the thesis, reusing the existing funnel-contract pattern (`funnel_events.py` / `funnel_events.dart`, kept in sync by `test_funnel_event_contract.py`):

- **Keyboard:** `keyboard_enabled` → `keyboard_full_access_granted` → `keyboard_draft_requested` → `keyboard_suggestion_inserted` → `keyboard_limit_hit` → `paywall_intent`.
- **Voice:** `voice_surface_added` → `voice_launched_from_surface` → `voice_session_completed`.

**Pricing reference points:** Auri runs from $7.99/week up to $179.99 plus a $299 lifetime and a $14.99 family plan. Our existing $19.99 / $34.99 monthly tiers are in-market; an annual or lifetime SKU is a "later, if the keyboard validates" decision, not a launch decision.

---

## 12. How long to build with Claude Code

The plan estimates effort per task on this scale (assuming a build that knows this codebase, which Claude Code working in-repo does):

> S = up to ~1 day · M = 2-4 days · L = ~1 week · XL = 2+ weeks

Mapped to the recommended Android-first sequence:

| Phase | Work | Effort |
|---|---|---|
| 1. Foundations | `aura://voice` deep link (M) + keyboard auth bridge & `/keyboard/token` (L) | ~1.5 weeks |
| 2. Android voice + keyboard backend | widget (M), QS tile (S), default-assistant (L) **in parallel with** `/keyboard/draft` (L) + privacy/tests (M) | ~1-2 weeks |
| 3. Android keyboard | IME skeleton (L), Buddy bar + draft flow (L), voice-to-text (M), onboarding (M) | ~3 weeks |
| 4. Monetization | caps (M) + activation instrumentation (S) | ~0.5-1 week |
| 5. iOS voice | App Intent (M), Siri shortcut (S), Action Button/Control Center/widgets (M) | ~1-1.5 weeks |
| 6. iOS keyboard | extension skeleton (L), Buddy bar + draft + voice (L), Full Access onboarding + review prep (M) | ~2.5-3 weeks |

**Realistic read:** the **viral wedge is live on Android (through phase 4) in roughly 6-8 weeks** of focused work, and **both platforms complete in roughly 11-14 weeks**.

Two honest caveats on "with Claude Code":

- Claude Code compresses the parts that are code (the FastAPI endpoints, the Flutter wiring, the prompt building, the tests) a lot, because those live in a codebase it can read and edit directly.
- It compresses the parts that are **native platform plumbing and real-device QA much less**: the iOS Keychain-group sharing, the keyboard extension memory budget, the Android default-assistant role across OEM gesture fragmentation, and App Store / Play review all need actual devices, actual accounts, and actual review cycles. Those are the long poles, and they are why iOS is sequenced last and Android leads.

---

## 13. Open questions (product decisions still needed)

1. **Free-tier caps:** how many drafts per day, and is voice capped by minutes per day? Default plan is generous-first-week, Auri-style.
2. **Tiering:** is "Reply as me" a Companion feature or a Pro feature? Recommendation: a few free per day on Free, unlimited on Companion.
3. **iOS keyboard now or later:** full iOS keyboard in phase 6, or a lighter iOS "reply assist via Share Sheet + App Intent" first while Android validates? Lean is full build, since Auri proves it works.
4. **Naming:** "Buddy Keyboard" in the stores, or a distinct brand for app-store discoverability?

---

### Top risks, in one place

| Risk | Severity | Mitigation |
|---|---|---|
| iOS Full Access opt-in rate is low | High | Auri-style privacy messaging; make grammar/translate/voice useful pre-memory so users grant access for utility, then discover "reply as me." |
| Apple rejects an AI-first keyboard (4.4.1 / data collection) | High | Plain keyboard fully works without Full Access; AI framed as on-demand; no keystroke logging; explicit privacy copy. Auri passed review with this exact posture. |
| Keyboard latency feels slow | Med | `cheap()` model, cached digest, hard timeout, optimistic chip. |
| Separate-process auth complexity (iOS Keychain group) | Med | Dedicated `/keyboard/token`; ship Android first, port the proven contract to iOS. |
| Acquisition spike then churn | Med | Voice launcher + memory as the retention counterweight; cap-to-upgrade, never cap-to-quit. |
| Memory write-back violating Apple keyboard policy | Med | Default OFF, consumer-only, consent-gated. |
