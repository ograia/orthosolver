# Orthosolver NL Engine

The NL engine owns the public API, the orchestration runtime, the OpenAI agent boundary, the Lean client boundary, and the debug/observability surfaces.

This repo does not implement Lean internals. It drives the external Lean service through the HTTP contract in `contracts/lean_engine/http_contract.md`.

## Authoritative Docs

The authoritative implementation docs are the markdown files in this repository:

1. `docs/overview.md`
2. `docs/ARCHITECTURE_SUMMARY.md`
3. `docs/RUNBOOK.md`
4. `docs/OPERATIONS.md`
5. `contracts/lean_engine/http_contract.md`

## Quick Start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000
```

Mock Lean:

```bash
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081
```

## Runtime Shape

- `POST /v1/problems` is create-only.
- Root semantic sketching runs through a durable worker job, not a request-scoped thread.
- Durable execution starts with `POST /v1/problems/{problem_id}/start`.
- `POST /v1/problems/{problem_id}/run` remains as a compatibility alias.
- Standard mode hard-switches to the Lean v2 lifecycle when `mode.lean_mode=true`.

Unified standard-mode lifecycle:

1. Create problem.
2. Root semantic sketch worker runs.
3. Agent2/Agent3 produce and vet decompositions.
4. Accepted decompositions immediately submit Lean `prepare_track`.
5. Vetted lemmas dispatch `formalize_lemma_from_nl`.
6. Lean failures classify into `proof_issue` or `lean_issue`.
7. Optional auto-split can materialize synthetic child decompositions.
8. Final success uses `assemble_root_from_track`.

## Config

Public mode controls:

- `config.mode.nl_only_mode`
- `config.mode.lean_mode`

Active Lean feature flags:

- `config.mode.lean.auto_split_sublemmas`
- `config.mode.lean.stream_progress_payloads`
- `config.mode.lean.strict_proof_issue_fail_fast`
- `config.mode.lean.proof_issue_confidence_threshold`

Transitional `mode.lean.enabled`, `use_v2_endpoints`, `use_v2_prepare_track`, and `fallback_to_v1_on_error` are still accepted for compatibility but are now no-ops.

## Storage

There is no SQL persistence in this repo.

- State is stored as JSON objects through `src/nl_engine/storage/object_store.py`.
- Local development uses the filesystem backend.
- Shared deployments use GCS-backed state and artifact prefixes.
- Events and usage records use object-per-record storage for atomic uploads.

Key environment variables live in `src/nl_engine/settings.py`:

- `STORAGE_BACKEND`
- `GCS_BUCKET`
- `GCS_STATE_PREFIX`
- `GCS_ARTIFACT_PREFIX`
- `LEAN_ENGINE_BASE_URL`
- `LEAN_ENGINE_AUTH_MODE`

## API Surfaces

Primary endpoints:

- `POST /v1/problems`
- `GET /v1/problems/{problem_id}`
- `POST /v1/problems/{problem_id}/start`
- `POST /v1/problems/{problem_id}/run`
- `POST /v1/problems/{problem_id}/resume`
- `POST /v1/problems/{problem_id}/pause`
- `GET /v1/problems/{problem_id}/execution`
- `GET /v1/problems/{problem_id}/progress`
- `GET /v1/problems/{problem_id}/lean-jobs`
- `GET /v1/problems/{problem_id}/events`
- `GET /v1/problems/{problem_id}/events/stream`
- `GET /debug`

## Development Notes

- `src/nl_engine/controller/orchestrator.py`: orchestration lifecycle and Lean routing
- `src/nl_engine/execution/runtime.py`: durable execution and worker driving
- `src/nl_engine/persistence/`: JSON-backed repositories
- `src/nl_engine/api/debug.py`: debug API and snapshot payloads
- `src/nl_engine/api/static/debug/`: debug UI

## Validation

```bash
pytest -q
pytest -q -m regression
RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging
PYTHONPATH=src python tools/evaluator/run_eval.py --cases tools/evaluator/cases.sample.json
```
