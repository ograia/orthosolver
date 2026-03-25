# Repo Blueprint (Current)

Implementation-oriented layout guide for this repository.
This is a map of the current structure, not a speculative target tree.

```text
.
├── AGENTS.md
├── README.md
├── contracts/
│   ├── agents/
│   ├── evaluator/
│   ├── json_schemas/
│   ├── lean_engine/
│   └── public_api/
├── database/
│   └── migrations/
├── docs/
│   ├── nl_engine.tex
│   ├── lean_engine.tex
│   ├── ARCHITECTURE_SUMMARY.md
│   ├── RUNBOOK.md
│   ├── REQUIREMENT_MATRIX.md
│   ├── REPO_BLUEPRINT.md
│   ├── PHASE34_GAP_MATRIX.md
│   └── OPERATIONS.md
├── infra/
│   ├── cloudbuild/
│   └── terraform/
├── mock_lean/
├── prompts/
│   ├── agent1_semantic_sketch/
│   ├── agent2_decomposer/
│   ├── agent3_decomposition_vetter/
│   ├── agent4_lemma_solver/
│   └── agent5_lemma_vetter/
├── scripts/
├── src/nl_engine/
│   ├── api/
│   ├── artifacts/
│   ├── controller/
│   ├── domain/
│   ├── lean_client/
│   ├── observability/
│   ├── persistence/
│   ├── routing/
│   ├── services/
│   ├── workers/
│   └── settings.py
├── tests/
│   ├── integration/
│   └── unit/
└── tools/
    └── evaluator/
```

## Ownership by Directory
### `src/nl_engine/api/`
- public and debug HTTP surfaces
- error envelope handling
- SSE stream endpoint
- run-lock coordination (`run_state.py`)

### `src/nl_engine/controller/`
- durable execution-driven orchestrator state machine
- decomposition selection/promotion
- lemma solve/vet/decompose routing
- Lean dispatch/poll route handling

### `src/nl_engine/domain/`
- typed API/debug/agent contracts
- config schema and defaults
- enums and ORM models

### `src/nl_engine/services/`
- OpenAI integration and output normalization
- debug cleanup service

### `src/nl_engine/workers/`
- idempotent in-process worker boundary keyed by job id
- concurrency and pending backpressure controls

### `src/nl_engine/persistence/`
- DB engine/session setup
- repositories for all routing-critical entities

### `src/nl_engine/observability/`
- event logging helpers
- LLM usage/cost accounting
- metrics emitter hooks

### `prompts/`
- system prompts for Agents 1-5

### `contracts/`
- human-readable interface notes + evaluator schema

### `database/migrations/`
- SQL source of truth for schema evolution

### `tests/`
- unit: normalization/routing/config/storage details
- integration: full lifecycle behaviors and debug surfaces

## Contributor Guidance
- Prefer edits in narrow modules; keep boundaries explicit.
- Update contract docs and tests when changing behavior.
- Keep `docs/nl_engine.tex` and `docs/lean_engine.tex` unchanged unless explicitly requested.
