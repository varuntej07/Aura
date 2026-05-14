# Project Overview

Aura is my personal Flutter and Python FastAPI assistant app. The assistant persona is Buddy. The app covers text chat, LiveKit voice, reminders, memory, nutrition, notifications, scheduled agents, and Google Calendar tools.

Keep the project simple. Prefer one clear working path over broad architecture changes.

## Architecture

The Flutter app uses MVVM with Provider.

Screens live in `lib/presentation/screens`.

ViewModels live in `lib/presentation/viewmodels`.

Repositories live in `lib/data/repositories`.

Services live in `lib/data/services`.

Shared app code lives in `lib/core`.

Provider wiring lives in `lib/di/providers.dart`.

The backend is a FastAPI app in `backend/src/main.py`.

Handlers live in `backend/src/handlers`.

Backend services live in `backend/src/services`.

Scheduled domain agents live in `backend/src/agents`.

Voice runs through `backend/src/agent/voice_agent.py` as a separate LiveKit worker.

`backend/src/services/user_aura_extractor.py` builds a passive behavioral profile per user.
It fires as a fire-and-forget `asyncio.create_task` from the chat handler after every message.
Profile documents live in the `UserAura/{uid}` Firestore collection.
The extractor always passes the user's previous query (`prev_user_query` field) alongside the
current message to Gemini Flash, which decides when prior context is needed — no hardcoded
heuristics. Failed extractions are swallowed silently so the chat stream is never affected.

## UI System

The app uses a glass morphism design system defined in `lib/core/theme/`.

**`app_colors.dart`** — All color constants including glass-specific ones:
- `deepBackground` (`0xFF080812`) — base dark background
- `glassWhiteFill`, `glassBorderLight`, `glassBorderDim`, `glassHighlight` — glass surface layers
- `glassOrb1`, `glassOrb2` — ambient background gradient orb colors

**`glass_card.dart`** — Four UI primitives:
- `GlassCard` — real `BackdropFilter` blur (σ=12). Use only on static, non-scrolling elements. Always wrapped in `RepaintBoundary`.
- `FauxGlassCard` — gradient + border only, no blur. Use everywhere inside scroll lists, message bubbles, tiles, pills.
- `GlassIconButton` — circular glass button with real blur. Use for icon buttons in app bars.
- `AmbientBackground` — `Stack` with two radial gradient orbs over `deepBackground`. Wrap entire screens that need the glass effect to have something to blur.

Performance rule: never put `BackdropFilter` inside a `ListView` or `GridView`. Use `FauxGlassCard` there instead.

**AppShell** (`lib/presentation/screens/app_shell.dart`) — wraps child in `AmbientBackground`, uses `extendBody: true` so content flows under the floating nav bar. The floating glass nav bar is ~58px tall. Screens that scroll to the bottom must add `SizedBox(height: MediaQuery.of(context).viewPadding.bottom + 96)` at the bottom to avoid content being hidden.

## Auth

`AuthViewModel` uses a stream subscription to `authRepository.userModelStream` (backed by Firebase `authStateChanges()`). Auth state updates reactively — no polling. The router's `refreshListenable: authViewModel` handles redirects automatically.

Sign-in supports Google and Email/Password. Email sign-in auto-creates an account on `user-not-found`.

The home screen drawer checks `authVm.user != null` and shows a sign-in button when unauthenticated, hiding the session list.

## Onboarding

New accounts are stamped `onboarding_complete: false` in Firestore at creation. Existing accounts without the field default to `true` so they are never shown the flow.

The router redirect enforces onboarding before any authenticated screen: if `AuthViewModel.needsOnboarding` is true, any route redirects to `/onboarding`.

**Flow:** `/onboarding` (`OnboardingScreen` — 5-slide PageView) → pushes `AuraConsentScreen` (age gate + Aura consent toggle).

`AuraConsentScreen` writes all three fields atomically via `OnboardingRepository.saveOnboardingResult`:
- `onboarding_complete: true`
- `date_of_birth: ISO date`
- `aura_consent_granted: bool` (forced false for users under 18)
- `aura_consent_timestamp: ISO datetime`

After a successful write, it calls `AuthViewModel.markOnboardingComplete()` (updates in-memory model) and then `context.go('/home')` explicitly. The screen was pushed via `Navigator.push`, not GoRouter, so explicit navigation is required to clear the mixed stack correctly.

`backend/src/services/user_aura_extractor.py` reads `users/{uid}.aura_consent_granted` before every extraction and returns early if not granted. This is the GDPR gate — behavioural profiling only runs with explicit opt-in.

## Paywall

`/paywall` route renders `PaywallScreen` with three tiers: Free, Monthly (`aura_starter_monthly`), Annual (`aura_starter_annual`). Calls `SubscriptionViewModel.purchaseStarter(annual: bool)`.

## Run

Backend API:

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker:

```powershell
cd backend
python -m src.agent.voice_agent start
```

Flutter app:

```powershell
flutter run
```

Production backend URL:

```text
https://juno-backend-620715294422.us-central1.run.app
```

Aura app legal pages (hosted on varuntej.dev portfolio):

```text
https://varuntej.dev/aura
https://varuntej.dev/aura/privacy-policy
https://varuntej.dev/aura/terms-of-service
```

## Reliability Notes

This is useful as a personal project, but reliability still depends on clean local configuration and external services.

Keep `.env`, service account JSON, OAuth client JSON, and platform Google service files out of commits. `.env` is intentionally not ignored so variable names stay visible locally.

The backend depends on several external services: Firebase, Anthropic, Gemini, LiveKit, Deepgram, Cartesia, Google Calendar, Cloud Scheduler, Cloud Tasks, and FCM. Treat every integration as optional at development time and make failures explicit.

The Flutter and Dart analyzer commands timed out in this environment during review. Recheck locally before relying on the current app state.

## Naming Conventions

Names must describe what something is or does in plain terms.

Constants: state the full context of what they represent. Use `EXCLUDED_TOOLS_FOR_GENERAL_CHAT` not `_CHAT_EXCLUDED_TOOLS`.

Functions: name the action and the subject together so the return value is obvious without reading the body. Use `_get_user_local_datetime` not `_formatted_now`. 

Avoid abbreviations, cryptic prefixes, and names that only make sense after reading the body. If a name needs a comment to explain it, rename it instead.


## Working Style

This is a personal project, so default to the simplest useful change.

Start every response with the actual answer.
No preamble, no acknowledgment of the question.
Just the information.

Always show options before acting. If you are uncertain about any fact, statistic, date, quote, or piece of information, say so explicitly before including it.

Never fill gaps in your knowledge with plausible-sounding information.
When in doubt, say so.

Match response length to task complexity.

Simple questions get direct, short answers. Complex tasks get full, detailed responses.

Never compress or summarize work that requires real depth.
Never pad responses with restatements of the question or closing sentences that repeat what you just said.

Before making any change that significantly alters content I've already created (rewriting sections, removing paragraphs, restructuring the flow, changing tone), stop completely.

Describe exactly what you're about to change and why.
Wait for my confirmation before proceeding.

"I think this would be better" is not permission to change it.

Only change what I specifically asked you to change.

Do not rewrite, rephrase, restructure, or "improve" anything I didn't ask about, even if you think it would be better.

If you notice something that could be improved elsewhere, mention it at the end of your response.
Do not touch it unless I explicitly ask you to.

After completing any editing or writing task, always end with a brief summary:
- What was changed: [description]
- What was left untouched: [if relevant]
- What needs my attention: [anything requiring a decision or review]

Keep it short. This is a status update, not a recap of everything you just did.

Never commit, send, post, publish, share, or schedule anything on my behalf without my explicit confirmation in the current message.

Only modify files, functions, and lines of code directly and specifically related to the current task.

Do not refactor, rename, reorganize, reformat, or "improve" anything I did not explicitly ask you to change.

If you notice something worth fixing elsewhere, mention it in a note.
Do not touch it. Ever.

Before deleting any file, overwriting existing code, dropping database records, removing dependencies, or making any change that cannot be trivially undone, stop completely. List exactly what will be affected. Ask for explicit confirmation. Only proceed after I say yes in the current message.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming -> invoke office-hours
- Bugs, errors, "why is this broken", 500 errors -> invoke investigate
- Ship, deploy, push, create PR -> invoke ship
- QA, test the site, find bugs -> invoke qa
- Code review, check my diff -> invoke review
- Update docs after shipping -> invoke document-release
- Weekly retro -> invoke retro
- Design system, brand -> invoke design-consultation
- Visual audit, design polish -> invoke design-review
- Architecture review -> invoke plan-eng-review
- Save progress, checkpoint, resume -> invoke checkpoint
- Code quality, health check -> invoke health
