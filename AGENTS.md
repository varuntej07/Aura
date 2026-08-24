# Repository Guidelines

## Subjective Product Feedback

When the user gives direct feedback about sound, visuals, wording, animation, or
other subjective product qualities, treat the user's judgment as authoritative.

Make the smallest requested change and stop. Do not perform research studies,
benchmark alternatives, analyze waveforms/spectrums, compare multiple candidates,
or attempt to predict whether the user will like the result unless explicitly
asked.

If the user asks to find an asset online, do one targeted search, choose one
clearly usable result matching their description, integrate it, and leave
subjective acceptance testing to the user. Do not inspect or evaluate the asset's
subjective quality. Preserve everything the user said was already correct.

## Never Match Keywords On User Intent

Never add a word list, phrase list, substring check, `startswith`, or regex over what
the user said to decide what they *meant* (invoke, authorize, gate, reset, confirm,
cancel, route, pick an argument). No exceptions for "narrow" or "temporary" — every
such list here shipped a misfire; "can't you see my other monitor?" authorized a
tracking subscription. Instead: gate on a structural fact (surface, finalization,
entitlement, connector, STT confidence), or let the model judge it via the tool
description or a classifier field, or derive it from collected state. No tool behind
it? Register one. Regex on non-intent (BM25, validators, parsers, redaction, IDs,
dates) is fine. See `lessons-learnt.text` 2026-08-16.

## Required Project Instructions

Before changing this repository, read `CLAUDE.md` completely and follow its project-specific architecture, product, safety, testing, and working-style instructions. Re-read relevant sections when a task touches their subsystem. Higher-priority instructions and the user's current explicit request take precedence.

## Skills

When a request matches an installed skill, invoke it as the first action rather
than asking whether to. Routing: CTO-level technical/product questions ->
cto-briefing.

## Voice And Chat Architecture Latency Gate

Before changing the architecture or data flow of either voice or chat, get
explicit approval from Varun. The approval request must explain whether the
change is expected to improve, preserve, or degrade user-perceived latency and
why. Any voice change expected to worsen latency is a hard no: do not implement
it; report the impact and stop. For chat, do not implement an architecture or
data-flow change without a reasonable, defensible justification for the change
and its latency tradeoff, followed by Varun's explicit approval.

## Project Structure & Module Organization

Aura combines a Flutter client with a Python backend. Flutter code lives in `lib/`: UI and Provider view models are under `lib/presentation/`, repositories and services under `lib/data/`, shared infrastructure under `lib/core/`, and dependency wiring in `lib/di/`. Flutter tests mirror these areas in `test/`.

The FastAPI service is in `backend/src/`; endpoints are in `handlers/`, integrations in `services/`, and the LiveKit worker in `agent/`. Python tests live in `backend/tests/`.

## Build, Test, and Development Commands

### Proportional Verification

For straightforward, low-risk edits, make the requested change directly without
running analyzers, tests, builds, formatters, `git diff --check`, diff statistics,
repository-wide completeness searches, process inspection, or process termination.
This applies even when a small, mechanical change touches multiple files. The
number of files alone is never a reason to run checks. Use verification only when
the change is materially large, complex, behaviorally risky, or the user explicitly
requests it. Almost never run Git statistics; they do not validate correctness. If
an optional check stalls, stop and report it without investigating or killing
processes unless the stalled process is demonstrably causing a real problem.

- `flutter pub get` installs Dart dependencies.
- `flutter run` starts the client on a selected device.
- `flutter analyze` applies the strict analyzer and `flutter_lints` rules.
- `flutter test` runs all Flutter unit and widget tests.
- `cd backend; python -m pip install -e ".[dev]"` installs the API and development tools.
- `cd backend; uvicorn src.main:app --reload --port 8000` runs the API locally.
- `cd backend; python -m pytest` runs backend tests; `ruff check src tests` checks Python style.

## Coding Style & Naming Conventions

Format Dart with `dart format .` (standard two-space indentation). Use `snake_case.dart` filenames, `UpperCamelCase` types/widgets, and `lowerCamelCase` members. Keep UI, logic, and data responsibilities within the existing MVVM layers. Python uses four spaces, `snake_case` modules/functions, `PascalCase` classes, Ruff rules `E`, `F`, `I`, and `UP`, and a 100-character line limit.

## Testing Guidelines

Use `flutter_test` for Dart and `pytest`/`pytest-asyncio` for Python. Name tests `*_test.dart` and `test_*.py`, colocated by feature within their test tree. Add focused regression coverage for behavior changes. Regenerate Mockito files after annotated mock changes with `dart run build_runner build --delete-conflicting-outputs`.

## Commit & Pull Request Guidelines

History favors short imperative or descriptive summaries, sometimes followed by a PR number; Conventional Commit prefixes are not required. Keep commits scoped and state the user-visible outcome. PRs should explain intent and risk, link issues, list verification commands, and include screenshots or recordings for UI changes. Call out new environment variables, Firestore indexes/rules, migrations, or cross-repo contracts.

## Security & Configuration

Never commit `.env` files, service-account JSON, API keys, or generated build artifacts. Use local configuration and managed deployment secrets; redact user data from logs, fixtures, screenshots, and review notes.
