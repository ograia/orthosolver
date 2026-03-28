# Operations Guide

## Health Surfaces

- NL API health: app boot and request success
- Lean service health: `GET /v1/health` or `GET /v2/health`
- Problem health:
  - `GET /v1/problems/{id}/progress`
  - `GET /v1/problems/{id}/lean-jobs`
  - `GET /v1/problems/{id}/events`
  - `GET /v1/problems/{id}/cost`

## Storage and Deployment Model

There is no SQL deployment dependency.

- NL state uses JSON objects under the configured state prefix.
- NL artifacts use a separate artifact prefix.
- Lean service job state uses JSON objects under its own state prefix.
- Shared deployments should use GCS-backed storage for both services.

## Common Incident Classes

### Execution not advancing

Check:

1. `/execution`
2. `/progress`
3. `/lean-jobs`
4. debug request log and snapshot

Typical causes:

- waiting on agent work
- waiting on Lean `prepare_track` or lemma formalization
- infrastructure restart during execution

### Repeated `proof_issue`

Treat this as an NL-quality or decomposition-quality problem first:

1. inspect the accepted decomposition
2. inspect lemma statements and pinned signatures
3. inspect Lean classification confidence

### Repeated `lean_issue`

Treat this as Lean-search or Lean-environment trouble first:

1. inspect artifact links from `/lean-jobs` or `/debug`
2. inspect `error_class` and `progress_snapshot`
3. verify Lean service health and auth

### Storage issues

If using GCS-backed storage:

1. verify `GCS_BUCKET`
2. verify state and artifact prefixes
3. verify service-account storage permissions

## Rollout Checklist

1. Deploy NL API and worker services with GCS state/artifact env vars.
2. Deploy the Lean service with GCS-backed job-state env vars.
3. Verify secrets and service-account permissions.
4. Run NL-only smoke.
5. Run standard-mode smoke with `lean_mode=true`.
6. Verify `/progress`, `/lean-jobs`, SSE, and `/debug`.

## Rollback

1. Roll back Cloud Run revisions.
2. Keep the existing object-store prefixes intact.
3. Re-run the smoke tests before reopening traffic.

## Useful Commands

```bash
pytest -q
pytest -q -m regression
bash scripts/run_nl_only_local.sh --create
```
