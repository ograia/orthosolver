# Phase 08 Runbook

## Purpose

Phase 08 adds an optional local service wrapper around the existing Phase 01-07 engine pipeline.

The service is asynchronous and idempotent by `job_id`.

## Endpoints

- `POST /v1/jobs` and `POST /v2/jobs`
- `GET /v1/jobs/{job_id}` and `GET /v2/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel` and `POST /v2/jobs/{job_id}/cancel`
- `DELETE /v1/jobs/{job_id}` and `DELETE /v2/jobs/{job_id}`
- `POST /v2/operations/{operation}`
- `GET /v2/operations/{operation_id}`
- `DELETE /v2/operations/{operation_id}`
- `GET /v1/health` and `GET /v2/health`

## Start Service

```bash
PYTHONPATH=src python -m lean_engine.cli service \
  --host 127.0.0.1 \
  --port 8081 \
  --db-path .artifacts/lean_engine/service/jobs.sqlite3
```

## Job Request Shape

```json
{
  "job_id": "job_001",
  "mode": "run_full_pipeline | check_assembly | prepare_track | formalize_lemma | formalize_lemma_from_nl | split_proof_into_sublemmas | assemble_root | assemble_root_from_track | check_statement_plausibility",
  "operation": "optional v2 alias; defaults to mode",
  "payload": { "...": "mode specific" },
  "options": { "...": "optional runtime overrides" }
}
```

## Operation Request Shape (v2)

`POST /v2/operations/{operation}` accepts the same body shape as jobs plus an explicit operation id:

```json
{
  "operation_id": "op_001",
  "problem_id": "prob_123",
  "target_id": "lem_1",
  "target_kind": "lemma",
  "payload": { "...": "operation specific" },
  "options": { "...": "optional runtime overrides" }
}
```

## Mode Payload Notes

### `run_full_pipeline`

Required:

- `payload.source`
- optional `payload.source_kind = auto|path|text|json`

Useful options:

- `options.artifact_root`
- `options.max_repair_rounds`
- `options.max_attempts_per_lemma`
- `options.max_semantic_repairs`
- `options.max_root_attempts`
- `options.timeout_seconds`

### `check_assembly`

Required:

- `payload.source`

Useful options:

- `options.max_repair_rounds`
- `options.timeout_seconds`

### `prepare_track`

Required:

- decomposition source (`payload.source` or decomposition fields used by `check_assembly`)

Useful options:

- `options.max_repair_rounds`
- `options.timeout_seconds`

Returns stable per-lemma handles (`lemma_handles`) and `run_dir` for follow-up operations.

### `formalize_lemma`

Required:

- `payload.run_dir`
- `payload.lemma_id`

Useful options:

- `options.max_attempts_per_lemma`
- `options.timeout_seconds`

### `formalize_lemma_from_nl`

Required:

- `payload.lemma_id` or `payload.lemma_handle`
- `payload.run_dir` or `payload.track_run_dir`

Useful options:

- `options.max_attempts_per_lemma`
- `options.timeout_seconds`

### `split_proof_into_sublemmas`

Required:

- none (best results with `payload.proof_nl` and `payload.statement_nl`)

Returns a `sublemmas` array with generated intermediate claims.

### `assemble_root`

Required:

- `payload.run_dir`

Useful options:

- `options.max_root_attempts`
- `options.timeout_seconds`

### `assemble_root_from_track`

Required:

- `payload.run_dir` or `payload.track_run_dir`

Useful options:

- `options.max_root_attempts`
- `options.timeout_seconds`

## Idempotency

- Re-posting the same `job_id` returns the existing job status (`409`) and does not re-run work.

## Cancellation

- Cancelling queued/running work marks the job terminal as `cancelled`.
- If a worker thread is already executing, cancel is best-effort and terminal updates are blocked from overwriting `cancelled`.

## Fatal Classification

Fatal output classification from the pipeline is preserved in job results.

Example:

- `status = fatal`
- `error_class = major_proof_gap`

## Health Response

`GET /v1/health` reports:

- service status
- default model
- Lean template initialization status
- MCP command availability
- active jobs and queue depth

## Polling

Queued/running response includes:

- `status`
- `elapsed_seconds`
- `progress_snapshot` (`phase`, `round`, `attempt`, `last_error`)

Terminal response includes:

- `status = success | repairable | fatal`
- `result` payload from the executed mode
- `issue_kind` (`proof_issue` or `lean_issue`, when non-success)
- `confidence` and `fatality`
- `artifact_index` pointers when `run_dir` is present
