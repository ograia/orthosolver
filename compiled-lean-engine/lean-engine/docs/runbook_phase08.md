# Phase 08 Runbook

## Purpose

Phase 08 adds an optional local service wrapper around the existing Phase 01-07 engine pipeline.

The service is asynchronous and idempotent by `job_id`.

## Endpoints

- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `GET /v1/health`

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
  "mode": "run_full_pipeline | check_assembly | formalize_lemma | assemble_root",
  "payload": { "...": "mode specific" },
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

### `formalize_lemma`

Required:

- `payload.run_dir`
- `payload.lemma_id`

Useful options:

- `options.max_attempts_per_lemma`
- `options.timeout_seconds`

### `assemble_root`

Required:

- `payload.run_dir`

Useful options:

- `options.max_root_attempts`
- `options.timeout_seconds`

## Idempotency

- Re-posting the same `job_id` returns the existing job status (`409`) and does not re-run work.

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

Terminal response includes:

- `status = success | repairable | fatal`
- `result` payload from the executed mode
