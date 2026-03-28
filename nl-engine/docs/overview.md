# NL Engine Overview

This document is the consolidated implementation guide for the NL orchestrator.

## Scope

The NL engine owns:

- the public FastAPI surface
- the durable orchestration runtime
- the OpenAI agent boundary for Agents 1 through 6
- JSON-backed state and artifact persistence
- the Lean HTTP client boundary
- progress, events, debug APIs, and `/debug`

The NL engine does not implement Lean internals.

## Runtime Lifecycle

### Create

`POST /v1/problems` validates input, persists the create artifacts, creates the root theorem, and queues the root semantic-sketch worker. It does not run the full theorem lifecycle inline.

### Start / Run

- `POST /v1/problems/{id}/start` creates or reuses a durable execution and returns immediately.
- `POST /v1/problems/{id}/run` remains a compatibility alias.
- `POST /v1/problems/{id}/resume` appends a fresh execution generation for failed runs.
- `POST /v1/problems/{id}/pause` requests stop and leaves the problem paused.

### Unified Standard Mode

When `config.mode.lean_mode=true`, standard mode uses the Lean v2 flow:

1. Agent2 generates decomposition candidates.
2. Agent3 vets them.
3. Accepted decompositions materialize lemmas and immediately submit Lean `prepare_track`.
4. Decomposition readiness is gated by `lean_v2_prepare_status == "success"`.
5. When Agent5 returns `send_to_lean`, the orchestrator submits `formalize_lemma_from_nl`.
6. Lean results classify failures as `proof_issue` or `lean_issue`.
7. Auto-split can turn Lean bottlenecks into synthetic child decompositions.
8. Once all lemmas in the winning track are formalized, the orchestrator submits `assemble_root_from_track`.

### NL-Only Mode

When `config.mode.nl_only_mode=true`, accepted NL proofs can terminate lemma success without Lean formalization.

## Routing Rules

- `proof_issue` above the configured confidence threshold routes back into NL plausibility or decomposition logic.
- `lean_issue` consumes Lean-only retry budget first.
- `strict_proof_issue_fail_fast` can skip confirmation retries for high-confidence proof issues.
- Hard caps remain the final infinite-loop breaker.

## Storage

There is no SQL layer in the NL engine.

- State is stored as JSON objects.
- Events and usage records are object-per-record for atomic uploads.
- Local development uses filesystem storage.
- Shared deployments use GCS-backed state and artifact prefixes.

Important state paths include:

- `problems_index.json`
- `{problem_id}/problem.json`
- `{problem_id}/execution/{id}.json`
- `{problem_id}/decompositions/{id}.json`
- `{problem_id}/lemmas/{id}.json`
- `{problem_id}/worker_jobs/{id}.json`
- `{problem_id}/lean_jobs/{id}.json`
- `{problem_id}/lean_results/{id}.json`
- `{problem_id}/events/{event_id}.json`
- `{problem_id}/llm_usage/{usage_id}.json`

## Observability

Primary monitoring surfaces:

- `GET /v1/problems/{id}/progress`
- `GET /v1/problems/{id}/lean-jobs`
- `GET /v1/problems/{id}/events`
- `GET /v1/problems/{id}/events/stream`
- `GET /v1/debug/problems/{id}/snapshot`
- `GET /debug`

The debug UI shows the decomposition tree, per-lemma status, Lean classification badges, bottleneck summaries, artifact links, and request logs.

## Key Files

- `src/nl_engine/api/main.py`
- `src/nl_engine/api/debug.py`
- `src/nl_engine/controller/orchestrator.py`
- `src/nl_engine/execution/runtime.py`
- `src/nl_engine/persistence/db.py`
- `src/nl_engine/persistence/repositories.py`
- `src/nl_engine/lean_client/client.py`
- `src/nl_engine/services/proof_graphs.py`
