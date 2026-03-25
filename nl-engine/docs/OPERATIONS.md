# Operations Guide

Operational guide for running the orchestrator in local/staging/prod style environments.

## 1. Health Surfaces
- API process health: FastAPI app boot + endpoint responses
- Lean boundary health: `GET /v1/health` on Lean service (mock or real)
- Orchestrator health signals:
  - `GET /v1/problems/{id}/progress`
  - `GET /v1/problems/{id}/events`
  - `GET /v1/problems/{id}/cost`

## 2. Typical Incident Classes
### A) Runs not advancing
Checks:
1. Is `/execution` present and nonterminal for the problem?
2. Is the problem shown as active in the debug execution list or request log?
3. Does `/events` continue appending transitions?

Likely causes:
- in-flight durable execution waiting on worker/Lean work
- process interruption during background execution
- upstream agent/Lean latency

Actions:
1. Inspect debug request log entry state (`completed|pending|failed`).
2. Inspect `GET /v1/problems/{id}/execution` and `GET /v1/debug/problems/{id}/executions`.
3. Run `POST /v1/debug/problems/{id}/reconcile-incomplete-runs` only for legacy compatibility `/run` artifacts left incomplete.
3. Verify API process stability and no restart loops.

### B) Agent infrastructure errors
Symptoms:
- `agent.infrastructure_retry` (transient upstream request failure; problem remains `running`)
- `agent_infrastructure_fatal` (non-retryable infrastructure/config failure)
- missing or invalid OpenAI credential/config

Actions:
1. Confirm `OPENAI_API_KEY` available in running environment.
2. Check model names and per-agent overrides.
3. Inspect artifacts for request-state attempts and `*_request_error_attempt_*.json` retry metadata.
4. If only retry events appear, keep the execution active; transient errors do not consume decomposition/solver logical attempt budgets.
5. For root-parallel decomposition, a single accepted track is allowed to proceed even if sibling tracks hit transient transport failures.

### C) DB contention (local SQLite)
Symptoms:
- `db_locked` responses

Actions:
1. Retry request.
2. Avoid excessive concurrent worker/API activity and heavy debug polling in the same process.
3. Keep WAL/busy-timeout defaults enabled.

### D) Lean route failures (standard mode)
Symptoms:
- repeated `repairable`/`fatal` Lean statuses

Actions:
1. Inspect `lean_jobs` + `lean_results` for error classes.
2. Confirm routing classes in `config.routing`.
3. Validate Lean auth mode and endpoint reachability.

## 3. Rollout Checklist (Cloud-Oriented)
1. Apply SQL migrations (`database/migrations`).
2. Deploy orchestrator and worker services.
3. Verify secret/access setup:
   - OpenAI key
   - DB credentials
   - Lean auth settings (if OIDC)
4. Run smoke flow:
   - create problem
   - start durable execution and wait to terminal in NL-only mode
   - run standard-mode sample with mock/real Lean
5. Confirm logs/events/cost data are emitted.

Reference deployment skeleton:
- [`infra/cloudbuild/deploy.yaml`](/Users/Omar/orthos-ai/nl-engine/infra/cloudbuild/deploy.yaml)
- [`infra/terraform/main.tf`](/Users/Omar/orthos-ai/nl-engine/infra/terraform/main.tf)

## 4. Rollback Strategy
1. Roll back Cloud Run revision to last known-good image.
2. If migration-related issue: apply compatible down migration and redeploy.
3. Re-run smoke flow and regression tests.

## 5. On-Call Triage Flow
1. Identify failing `problem_id`.
2. Pull:
   - `/progress`
   - `/events?limit=...`
   - `/failure-report` (if terminal failed)
3. If needed, use `/debug`:
   - request log to identify stalled stage
   - artifact viewer for exact payload/response trail
4. Classify root cause:
   - agent infra
   - routing logic
   - Lean result class
   - environment/config
5. Mitigate and document.

## 6. Useful Commands
Run full tests:
```bash
pytest -q
```

Regression subset:
```bash
pytest -q -m regression
```

Local NL-only one-shot:
```bash
bash scripts/run_nl_only_local.sh --create
```
