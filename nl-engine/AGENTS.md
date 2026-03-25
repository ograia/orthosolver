# Repository Guidelines

## Project Structure & Module Organization
- `src/nl_engine/` contains production code: `api/` for FastAPI and `/debug`, `controller/` and `execution/` for orchestration, `services/`, `routing/`, `lean_client/`, `domain/`, and `persistence/`.
- Tests live in `tests/unit/` and `tests/integration/`. Other key directories: `prompts/`, `database/migrations/`, `mock_lean/`, `docs/`, and `infra/`.
- Source priority is `docs/nl_engine.tex`, then runtime code, then `docs/ARCHITECTURE_SUMMARY.md`. Document any spec/runtime gap before changing behavior.

## Build, Test, and Development Commands
- `python3.11 -m venv .venv && source .venv/bin/activate` - create the supported environment.
- `pip install -e '.[dev]'` - install app and dev tooling.
- `uvicorn --app-dir src nl_engine.api.main:app --reload --port 8000` - run the API.
- `uvicorn mock_lean.main:app --reload --port 8081` - run the local Lean mock.
- `pytest -q` - run the full suite.
- `pytest -q -m regression` - run deterministic regression tests.
- `RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging` - run staging Lean integration intentionally.
- `ruff check .` - lint before a PR.

## Coding Style & Naming Conventions
- Use Python 3.11, 4-space indentation, type-aware Pydantic/SQLAlchemy patterns, and `snake_case` for modules, functions, and config keys.
- Keep contracts explicit in `src/nl_engine/domain/contracts.py`; avoid hiding routing-critical state in opaque JSON.
- Preserve invariants: root is the only theorem, non-root obligations are lemmas, agent outputs are valid JSON, and retry/depth caps come from config.

## Testing Guidelines
- Add unit tests for config, policy, and model logic; add integration tests for API, controller, migration, and mock Lean flows.
- Name files `test_*.py`; cover both happy-path and failure/recovery behavior for changes that affect orchestration.

## Commit & Pull Request Guidelines
- Match the existing history: short, imperative subjects such as `Add recursive proof bundles` or `Fix worker idempotency`; use version prefixes only for release commits.
- PRs should describe the behavior change, list validation commands, link the issue or spec delta, and include screenshots when `/debug` UI behavior changes.

## Architecture & Safety Notes
- Do not edit `docs/nl_engine.tex` or `docs/lean_engine.tex` during normal implementation work.
- Keep Lean engine internals out of scope; this repo owns the HTTP boundary, routing, mocks, persistence, and observability.
- For Codex-driven validation that would hit OpenAI, default agents to `gpt-5-mini` with `thinking_level = none` unless a more expensive path is explicitly required.
