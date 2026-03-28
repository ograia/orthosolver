# CLAUDE.md

## Project Overview

Orthosolver NL Engine — the natural-language orchestration layer for theorem decomposition, solving, vetting, and assembly. FastAPI backend with OpenAI-powered agents, JSON object-backed persistence, and a Lean engine integration boundary.

The active implementation docs are the markdown files in `docs/`. This repo does **not** implement Lean engine internals.

## Quick Reference

```bash
# Setup
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env

# Run API
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000

# Run mock Lean service (for local dev)
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081

# Run all tests
pytest -q

# Run regression suite only
pytest -q -m regression

# Run staging Lean tests (requires real Lean service)
RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging

# Lint
ruff check src/ tests/

# Quick NL-only local run
bash scripts/run_nl_only_local.sh --create
```

## Architecture

```
src/nl_engine/
├── api/              # FastAPI routes (main.py=public, debug.py=debug UI+API)
├── controller/       # Orchestrator state machine (single-tick)
├── domain/           # Pydantic contracts, data models (Pydantic BaseModel), enums, config
├── execution/        # Durable execution runtime (runtime.py)
├── persistence/      # FileStore (db.py), repository pattern over JSON files (repositories.py)
├── routing/          # Vetter/Lean result routing policy (policy.py)
├── services/         # Agent service (OpenAI), execution coordination, cleanup
├── workers/          # Idempotent worker facade with backpressure
├── worker/           # Standalone worker process (signal handling, polling)
├── lean_client/      # Async HTTP client for Lean engine (submit/poll/cancel)
├── artifacts/        # Filesystem artifact store + browser
├── observability/    # Metrics, events, cost tracking
└── settings.py       # Pydantic BaseSettings (all env var defaults)
```

Key files for common tasks:
- **API shapes/envelopes**: `domain/contracts.py` + `api/main.py`
- **Debug API/UI**: `api/debug.py` + `api/static/debug/*`
- **Controller/routing**: `controller/orchestrator.py` + `routing/policy.py`
- **Agent prompts/parsing**: `prompts/*/system.txt` + `services/agents.py`
- **Config defaults**: `domain/config.py` + `settings.py`
- **Data model/schema**: `domain/models.py` (Pydantic BaseModel classes)
- **Persistence**: `persistence/db.py` (FileStore — reads/writes JSON in `data/` directory)
- **Cleanup/reset**: `services/debug_cleaner.py`

## Lifecycle

1. `POST /v1/problems` — creates a problem and queues the root semantic sketch durably
2. `POST /v1/problems/{id}/start` — begins durable background execution
3. `POST /v1/problems/{id}/run` — compatibility alias for durable start
4. Poll `GET /v1/problems/{id}` or `GET /v1/problems/{id}/execution` until terminal (`succeeded`/`failed`)

## Agent Pipeline

1. **Agent 1** — Semantic sketch: normalizes theorem statement into structured form
2. **Agent 2** — Decomposition: breaks theorem into lemmas + assembly plan
3. **Agent 3** — Decomposition vetter: checks decomposition soundness, lemma plausibility
4. **Agent 4** — Lemma solver: produces NL proofs for individual lemmas
5. **Agent 5** — Lemma vetter: checks proof correctness, routes (accept/retry/decompose)
6. **Agent 6** — Final vetter: checks complete assembled proof for consistency and correctness

## Testing

- Python >=3.11, pytest >=8.2, pytest-asyncio
- `pythonpath = ["src", "."]` in pyproject.toml
- Markers: `regression`, `staging`
- Tests are in `tests/unit/` and `tests/integration/`
- Integration tests spin up the full FastAPI app with a temp FileStore directory

## Non-Negotiable Rules

- Formal success = Lean-verified, except when `nl_only_mode = true`
- NL-only outcomes tagged `verification_level = nl_only`
- Root theorem sketch is immutable after creation
- Major semantic drift is a hard stop
- No hardcoded retry/depth/timeout caps — read from problem config
- Agent outputs must be valid JSON
- Prompts, raw outputs, and key artifacts must be persisted for replay/debug
- Assembly plans must be trivially composable
- Routing-critical state in first-class JSON fields, not opaque nested blobs

## Safety Rules

- Treat markdown docs as the current source of truth. TeX files are historical material.
- Do not invent alternative public API contracts when existing ones cover the surface
- Do not silently weaken drift checks or trusted-context constraints
- Do not collapse architecture boundaries into a monolithic script
- If spec and code diverge, document the gap explicitly before changing behavior

## Vetter Routing Policy

- Medium-severity findings → retry solver on same lemma
- First high-severity finding → retry solver
- Repeated high-severity findings → decompose the lemma

## Environment Variables

Core: `OPENAI_API_KEY`, `LEAN_ENGINE_BASE_URL`

Storage: `DATA_DIR` (default: `data/`), `ARTIFACT_STORE_DIR` (default: `.artifacts/`)

Per-agent model overrides: `OPENAI_MODEL_AGENT1` through `OPENAI_MODEL_AGENT6` (default: `gpt-5-mini`).

All defaults in `src/nl_engine/settings.py`. Local defaults are cost-minimized (`gpt-5-mini`, `thinking_level=none`, `verbosity=low`).

## Persistence (FileStore)

All state is stored as JSON files on disk and can optionally be mirrored through the object-store backends.

```
data/
├── index.json                          # List of all problem IDs
└── prob_{id}/
    ├── problem.json                    # Problem metadata
    ├── events.jsonl                    # Append-only event log
    ├── theorems/{id}.json
    ├── decompositions/{id}.json
    ├── lemmas/{id}.json
    ├── vetter_reports/{id}.json
    ├── worker_jobs/{id}.json
    ├── execution/{id}.json
    ├── request_records/{id}.json
    ├── llm_usage_records/{id}.json
    └── run_cost_rollups/{id}.json
```

- `FileStore` class in `persistence/db.py` handles all reads/writes
- Repositories in `persistence/repositories.py` wrap FileStore with typed access
- All models are Pydantic `BaseModel` subclasses
- Writes are atomic (write to temp file, then rename)
- No transactions, no locks, no connection pools
- Data survives code updates, server restarts, and git operations
- `get_file_store()` returns the singleton; `reset_file_store()` for tests

## Source Priority

1. `docs/overview.md`
2. `docs/ARCHITECTURE_SUMMARY.md`
3. Current runtime code in `src/nl_engine/`
4. Other docs in `docs/`

## Versioning

The debug dashboard version is displayed in `src/nl_engine/api/static/debug/index.html` inside the `<h1>` tag (as a `<small>` element, e.g. `v1.03`). Bump this version number when:
- The user says this will be a new version
- A major change is made (new features, significant refactors, breaking changes)

## Git Conventions

- Do NOT add a Co-Authored-By line for Claude in commit messages. Only the user should appear as contributor.

## Working Conventions

- Read the relevant module before editing
- Keep changes modular and reviewable
- Prefer additive, explicit contracts over implicit behavior
- For local validation hitting OpenAI, default agents to `gpt-5-mini` with `thinking_level=none`
- Update docs when behavior changes (routing, debug endpoints, config schema, persistence artifacts)
