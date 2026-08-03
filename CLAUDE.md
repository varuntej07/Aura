# Aura

An AI companion app in production beta. The assistant persona is Buddy. Surfaces:
Flutter mobile app, FastAPI backend, LiveKit voice worker, an Android keyboard
IME, and a separate Tauri Windows client (Aura-Desktop, its own repo).

**Product soul: Buddy is obsessed with the user, in the best way.** Every feature
should feel like a true companion: warm, curious, proactive, remembering what
matters. When a product choice is ambiguous, pick the one that makes Buddy feel
more like a close friend, never a neutral tool, form, or content feed.

## Architecture

```text
Flutter MVVM -> FastAPI handlers -> feature services -> Firestore/providers
      |                                      ^
      +-> LiveKit room -> voice worker -> authenticated MCP tools

Scheduler/Cloud Tasks -> durable internal handlers -> the same services
All notification intent -> NotificationProposal -> one delivery orchestrator
```

Three things worth knowing that the layout does not show:

- **Every notification goes through one funnel.** Producers build a
  `NotificationProposal` and call `orchestrator.submit()`. The authoritative
  source list and priority ladder live in
  `backend/src/services/notifications/proposal.py`, never duplicated here.
- **`model_provider.py` is the single entry point for all LLM tiers**
  (`cheap`, `balanced`, `expert`, `reason_turn`, `grounded`), reached as methods
  via `get_model_provider()`. Every backend LLM call goes through it, and each
  tier has an explicit fallback chain. Content ingestion is RSS-first: **never**
  use `grounded()` in background ingest. Grounding is on-demand only.
- **Voice is ONE `BuddyAgent`.** Do not turn Buddy into a global dialogue state
  machine. Fix drift by de-duping the prompt.

## Subsystem map

Read the doc before changing a subsystem. Paths are references, not imports.

| Subsystem | Doc |
|---|---|
| Index of all of the below | `architectures/README.md` |
| Chat and tools | `architectures/chat-and-tools.md` |
| Turn completion after disconnect | `architectures/background-chat-completion.md` |
| Notification funnel | `architectures/notification-delivery.md` |
| News signal ranking | `architectures/signal-engine.md` |
| Post-session follow-ups | `architectures/session-followup.md` |
| Event-driven agent dispatch | `architectures/reactive-orchestration.md` |
| Topic tracking | `architectures/tracking-agent.md` |
| Curiosity threads | `architectures/thread-engine.md` |
| Icebreakers | `architectures/icebreaker.md` |
| Daily briefing | `architectures/daily-briefing.md` |
| User memory and Aura profile | `architectures/user-aura.md` |
| Voice, plus all prompt rules | `architectures/voice-agent.md` |
| Guide Mode (armed screen guidance) | `architectures/guide-mode.md` |
| Keyboard IME | `backend/docs/BUDDY_KEYBOARD_ARCHITECTURE.md` |
| Desktop Meeting Recording V2 | `backend/docs/meeting-recording-v2.md` |
| Cross-repo contracts | `ECOSYSTEM.md` |

Update `ECOSYSTEM.md` only when a change alters a cross-repo contract (an
endpoint shape, a shared Firestore collection, an auth handshake, shared config
identity, a deploy or version linkage), not for internal changes.

## Failure modes already hit here

Full write-ups in `lessons-learnt.text`. These are agent mistakes, not code facts.

- **A fix is done when the symptom is observed gone, not when it compiles and
  not when the reasoning is sound.** Visual bugs need real rendered pixels.
  Delivery bugs need the message actually arriving. Say "unverified" rather than
  "fixed." Three separate fixes were reported done by reasoning alone and each
  was still broken.
- **If one symptom survives two independent reasonable fixes, stop tuning and
  attack the premise.** Ask whether the mechanism can affect the thing on screen
  at all, and check whether other people hit the same wall, before attempt three.
- **Never trust a comment about behaviour. Trace the path.** A catch block
  promising "LOUD, never silent" reached no console, no Crashlytics, and no log
  file.
- **Deleting user data means every store, not just the database.** Enumerate
  Firestore, GCS, Auth, caches. Remove non-Firestore bytes first so a failure
  stays retryable.
- **Zero rows and healthy must never look identical.** Probe and log loudly when
  a query returns nothing while data plainly exists.
- **A soft preference that can push a score under a threshold is a gate, not a
  preference.** Rank within the already-sendable set; never let it decide
  sendability. This deadlocked notifications for four weeks.
- **Write an "already did this" cache only after the side effect succeeds.**
- **A comment is not a test.** If a rule matters it needs a test that breaks CI, so
  when the freeze below lifts, that is the first debt to pay down.

## Repo-wide rules

- **TEST FREEZE. YOU MUST NOT write any new test file, test function, test case, or
  fixture.** In force until Varun bumps the version or asks for tests in the current
  message. Nothing else lifts it: not a rule that "needs" a test, not a bug you just
  fixed, not a coverage gap you noticed. Never offer to add tests while it is in force.

  **YOU MUST NOT write a test in order to find, reproduce, or diagnose an error.**
  This is the specific waste the freeze exists to stop: writing a test, compiling it,
  running it, and iterating on the test itself burns far more time than asking the
  real object directly. To verify something, in this order:
  1. `cd backend && python -c "import src.main; print('OK')"` for import and wiring.
  2. A throwaway `python -c` that inspects the real value (`print` the live registry,
     the built prompt, the serialized schema). This is inspection, not a test, and is
     always allowed. Never save it to a file.
  3. Run the EXISTING suite. It is comprehensive; if a change is wrong it almost
     always already breaks something.

  Repairing an EXISTING test IS allowed and expected: a stale fake, a drifted import,
  or an assertion that no longer matches shipped behaviour is a broken verification
  path, not new test surface. Fixing a fake's signature, re-pointing an assertion at
  a renamed mechanism, or adding a helper an existing test needs to keep working is
  repair. A new case, file, or fixture is not.

  When a change would normally warrant a test, say so in one line and move on.
- **No feature flags.** Features ship unconditionally on. Never add a
  `*_ENABLED` setting or boolean gate. If a feature isn't ready for everyone,
  don't merge it.
- **This is production.** Every change defaults to scalability, maintainability,
  and robustness, with a bird's-eye view of downstream impact: data integrity,
  other users, deploy safety, and backward compatibility with already-released
  app clients. A change that works on one device but breaks shared Firestore,
  the live backend revision, or older installs is a regression. When in doubt
  about blast radius, treat it as production-impacting and say so.
- **GDPR gate:** `user_aura_extractor.py` reads `users/{uid}.aura_consent_granted`
  before every extraction. Explicit `false` blocks it; an absent field (legacy
  accounts) does not.
- **Payments are in beta interest-capture mode.** Real IAP is disabled; tier CTAs
  record intent. Plan and price constants live in `subscription_plan.dart`.
- **Never state a cost is negligible.** Reason about scaling to hundreds of
  users, not today's dollar amount.

## Run

Backend API:

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker (run `download-files` once first):

```powershell
python -m backend.src.agent.voice_agent download-files   # once, first time
cd backend && python -m src.agent.voice_agent start
```

Flutter app (analyze first to catch compile errors before the Gradle build):

```powershell
flutter analyze
flutter build appbundle --release --obfuscate --split-debug-info=build/symbols
```

The `.aab` on disk is NOT the user download. Play strips the R8 mapping and
native symbols and delivers one ABI per device. Check the real number in Play
Console -> App bundle explorer -> Download size.

## Deploy

**YOU MUST** verify the backend imports cleanly before deploying. This catches
broken imports before Docker:

```powershell
cd backend && python -c "import src.main; print('OK')"
```

Deploy the backend from the repo root (requires Git Bash). The voice worker is
no longer deployed here: it runs on LiveKit Cloud Agents and ships separately via
`lk agent deploy`.

```powershell
& "C:\Program Files\Git\bin\bash.exe" backend/deploy.sh juno-2ea45 us-central1
```

Production backend: `https://juno-backend-620715294422.us-central1.run.app`.
Legal pages live at `https://auravoiceapp.com`.

### Dark deploy (test on your phone first)

`deploy.sh` shifts 100% of traffic immediately. To test against the same prod
Firestore first, deploy a candidate at 0% traffic:

```powershell
docker build -f backend/Dockerfile.api -t gcr.io/juno-2ea45/juno-backend:latest backend
docker push gcr.io/juno-2ea45/juno-backend:latest

gcloud run deploy juno-backend --image=gcr.io/juno-2ea45/juno-backend:latest `
  --region=us-central1 --project=juno-2ea45 --no-traffic --tag=candidate

flutter run --dart-define=API_BASE_URL=https://candidate---juno-backend-620715294422.us-central1.run.app `
            --dart-define=WS_BASE_URL=wss://candidate---juno-backend-620715294422.us-central1.run.app

# promote: gcloud run services update-traffic juno-backend --region=us-central1 `
#   --project=juno-2ea45 --to-tags=candidate=100
```

A tagged URL always routes to its revision even at 0% traffic. **Caveat:** any
all-users write (collection-group batch, migration, backfill) cannot be
dark-tested on shared prod Firestore. Gate those behind an explicit trigger.

Deploys build from local disk, not from git. Never gate a deploy decision on
commit status. Check the SERVING revision, not the latest one: prod has silently
run days behind the repo before.

## Skills

When a request matches an installed skill, invoke it as the first action rather
than asking whether to. Routing: product ideas -> office-hours · strategy ->
plan-ceo-review · bugs -> investigate · ship/PR -> ship · QA -> qa · code review
-> review · docs -> document-release · architecture -> plan-eng-review.

**Boundaries always win over skill routing.** Anything that commits, pushes,
deploys, sends, publishes, or schedules needs explicit confirmation in the
current message, even via a skill. Run up to the outward action, then stop.
