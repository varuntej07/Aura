# Repository Guidelines

## Required Project Instructions

Before changing this repository, read `CLAUDE.md` completely and follow its project-specific architecture, product, safety, testing, and working-style instructions. Re-read relevant sections when a task touches their subsystem. Higher-priority instructions and the user's current explicit request take precedence.

## Project Structure & Module Organization

Aura combines a Flutter client with a Python backend. Flutter code lives in `lib/`: UI and Provider view models are under `lib/presentation/`, repositories and services under `lib/data/`, shared infrastructure under `lib/core/`, and dependency wiring in `lib/di/`. Flutter tests mirror these areas in `test/`.

The FastAPI service is in `backend/src/`; endpoints are in `handlers/`, integrations in `services/`, and the LiveKit worker in `agent/`. Python tests live in `backend/tests/`.

## Build, Test, and Development Commands

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

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
