# Orthosolver NL Engine / Orchestrator

Implementation repository for the NL orchestration system defined in [`docs/nl_engine.tex`](/Users/Omar/orthos-ai/nl-engine/docs/nl_engine.tex).

## What This Repo Owns
- public orchestrator API
- controller/state-machine for theorem -> decomposition -> lemma lifecycle
- SQL persistence + migrations
- prompt + JSON contracts for Agents 1-5
- Lean engine client boundary + local mock Lean service
- artifact persistence and replay/debug observability
- local debug website (`/debug`)
- infra baselines (Terraform + Cloud Build skeleton)

This repo does **not** implement Lean engine internals.

## Read This First
1. [`docs/nl_engine.tex`](/Users/Omar/orthos-ai/nl-engine/docs/nl_engine.tex) (authoritative spec)
2. [`AGENTS.md`](/Users/Omar/orthos-ai/nl-engine/AGENTS.md) (implementation constraints + edit map)
3. [`docs/overview.md`](/Users/Omar/orthos-ai/nl-engine/docs/overview.md) (default consolidated implementation guide)
4. [`docs/ARCHITECTURE_SUMMARY.md`](/Users/Omar/orthos-ai/nl-engine/docs/ARCHITECTURE_SUMMARY.md) (detailed architecture view)
5. [`docs/RUNBOOK.md`](/Users/Omar/orthos-ai/nl-engine/docs/RUNBOOK.md) (hands-on operation/debugging)

## Quick Start
```bash
cd /Users/Omar/orthos-ai/nl-engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Run API:
```bash
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000
```

Run local mock Lean:
```bash
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081
```

## Environment Variables (Core)
- `OPENAI_API_KEY`
- `OPENAI_MODEL_AGENT1` ... `OPENAI_MODEL_AGENT5`
- `OPENAI_REASONING_EFFORT` (`none|low|medium|high|xhigh`)
- `OPENAI_TEXT_VERBOSITY` (`low|medium|high`)
- `OPENAI_TIMEOUT_SECONDS` (legacy fallback only; per-agent config in request body is preferred)
- `OPENAI_USAGE_PERSISTENCE_MODE` (`immediate|buffered|disabled`)
- `LEAN_ENGINE_BASE_URL` (mock or real)
- `LEAN_ENGINE_AUTH_MODE` (`none|oidc`)

Defaults live in [`src/nl_engine/settings.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/settings.py).

## Public API
Required endpoints:
- `POST /v1/problems`
- `GET /v1/problems/{problem_id}`
- `POST /v1/problems/{problem_id}/start`
- `POST /v1/problems/{problem_id}/run`
- `POST /v1/problems/{problem_id}/resume`
- `GET /v1/problems/{problem_id}/execution`
- `POST /v1/problems/{problem_id}/cancel`
- `POST /v1/problems/{problem_id}/pause`
- `GET /v1/problems/{problem_id}/tree`
- `GET /v1/problems/{problem_id}/failure-report`

Monitoring endpoints:
- `GET /v1/problems/{problem_id}/progress`
- `GET /v1/problems/{problem_id}/events?after_event_id=&limit=`
- `GET /v1/problems/{problem_id}/events/stream` (SSE)
- `GET /v1/problems/{problem_id}/cost`

Debug endpoints:
- `GET /v1/debug/problem-create-template`
- `GET /v1/debug/problems?limit=...`
- `GET /v1/debug/problems/{problem_id}/input-json`
- `GET /v1/debug/problems/{problem_id}/snapshot?events_limit=...`
- `GET /v1/debug/problems/{problem_id}/request-log?limit=...&source=...`
- `GET /v1/debug/problems/{problem_id}/executions`
- `POST /v1/debug/problems/{problem_id}/reconcile-incomplete-runs`
- `POST /v1/debug/problems/{problem_id}/resume-after-infrastructure-failure`
- `GET /v1/debug/problems/{problem_id}/llm-usage`
- `GET /v1/debug/problems/{problem_id}/artifacts?prefix=...&include_worker_jobs=...&limit=...`
- `GET /v1/debug/artifacts/{artifact_key:path}`
- `DELETE /v1/debug/problems/{problem_id}`
- `POST /v1/debug/reset-local`
- `GET /debug` (UI)

`POST /v1/debug/reset-local` blocks new execution starts, waits for active in-process work to unwind, and then deletes all local rows and artifacts.

## Runtime Lifecycle (Important)
- `POST /v1/problems` is create-only. It persists the create artifact and runs Agent1 semantic sketching.
- Durable background execution starts with:
  - `POST /v1/problems/{problem_id}/start`
- `POST /v1/problems/{problem_id}/run` is preserved as a compatibility alias that schedules execution and returns current status; progress no longer depends on repeated ticks.
- `POST /v1/problems/{problem_id}/resume` re-enters orchestration for failed problems and appends a new execution while preserving prior run artifacts.
- `POST /v1/problems/{problem_id}/pause` moves non-terminal problems to `paused` and requests active execution stop.
- Continue paths (`/start` with `X-Debug-Run-Trigger: continue_button`, `/resume`, infra-resume) run a fresh generation: queued/running old-generation jobs are superseded and ignored.
- Use `GET /v1/problems/{problem_id}` or `GET /v1/problems/{problem_id}/execution` to monitor state until terminal: `succeeded` or `failed`.

Helper script:
```bash
bash scripts/run_nl_only_local.sh --create
```

## Request Config
Request body type is [`ProblemCreateRequest`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/contracts.py).
Config defaults are in [`ProblemConfig`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/config.py).

Per-agent overrides:
```json
{
  "config": {
    "llm": {
      "agent1": { "model": "gpt-5.4", "thinking_level": "low", "verbosity": "medium", "timeout_seconds": 600 },
      "agent2": { "model": "gpt-5.4-pro", "thinking_level": "high", "verbosity": "medium", "timeout_seconds": 600 },
      "agent3": { "model": "gpt-5-mini", "thinking_level": "medium", "verbosity": "low", "timeout_seconds": 600 },
      "agent4": { "model": "gpt-5.4", "thinking_level": "high", "verbosity": "high", "timeout_seconds": 600 },
      "agent5": { "model": "gpt-5.4", "thinking_level": "medium", "verbosity": "medium", "timeout_seconds": 600 }
    }
  }
}
```

Notes:
- Supported model values are exactly: `gpt-5.4`, `gpt-5.4-pro`, `gpt-5-mini`.
- `reasoning_effort` is accepted as an alias for `thinking_level`.
- `text_verbosity` and `text.verbosity` are accepted as aliases for `verbosity`.
- `timeout_seconds` defaults to `600` per agent.
- `gpt-5-mini` does not use `xhigh`; requests are normalized to `high`.
- Local backend defaults are cost-minimized: `gpt-5-mini`, `thinking_level=none`, `verbosity=low` unless overridden.
- Root decomposition controls also include:
  - `config.decomposition.root_solutions_required_for_termination` (minimum solved root tracks required before terminal success; must be `<= parallel_root_take_k`)
  - legacy `parallel_root_join_timeout_seconds` is still accepted in payloads for backward compatibility but is not used by runtime scheduling and is hidden from `/debug` guided form controls

## Debug UI (`/debug`)
Purpose: local, replay-first lifecycle debugging.

Current capabilities:
- submit requests via guided form (full `config.*` controls) or raw JSON
- auto-start durable execution after submit
- live refresh loop + SSE updates
- decomposition/lemma tree explorer and node detail panes
- request log with completion status + LLM token/cost summaries (agent/Lean focused; `api_create`, `api_start`, and `api_run` rows are hidden in the UI)
- request inspector support for provider `response_id` and provider terminal status on failed OpenAI background requests
- artifact browser and direct JSON/text viewer
- NL-only final output JSON summary panel
- per-problem delete and global local reset
- execution list and legacy reconcile endpoint for interrupted compatibility `/run` requests
- infrastructure-failure in-place resume action that preserves prior run artifacts and appends a new execution

Operational visibility rules:
- failed decomposition nodes remain visible
- lemmas from rejected decompositions are hidden from operational lemma views
- medium/high vetter findings route through retry-first solver behavior before decomposition escalation
- root parallel decomposition fan-out sends separate Agent2 requests concurrently when `parallel_root_decompositions_n > 1`
- when `parallel_root_take_k > 1`, `/debug` exposes those carried root tracks as separate toggleable trees
- Agent2 root generation and Agent4/Agent5 lemma solving/vetting are persisted as durable `worker_jobs` and harvested by background execution workers

## Architecture Map (Code)
- API: [`src/nl_engine/api/main.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/api/main.py)
- Debug API/UI: [`src/nl_engine/api/debug.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/api/debug.py)
- Controller: [`src/nl_engine/controller/orchestrator.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/controller/orchestrator.py)
- Durable execution runtime: [`src/nl_engine/execution/runtime.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/execution/runtime.py)
- Routing policy: [`src/nl_engine/routing/policy.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/routing/policy.py)
- Agent integration: [`src/nl_engine/services/agents.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/services/agents.py)
- Execution service: [`src/nl_engine/services/executions.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/services/executions.py)
- Worker idempotency/backpressure: [`src/nl_engine/workers/facade.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/workers/facade.py)
- Data model: [`src/nl_engine/domain/models.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/models.py)
- SQL migrations: [`database/migrations`](/Users/Omar/orthos-ai/nl-engine/database/migrations)
- Lean boundary: [`src/nl_engine/lean_client/client.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/lean_client/client.py)
- Mock Lean: [`mock_lean/main.py`](/Users/Omar/orthos-ai/nl-engine/mock_lean/main.py)

## Persistence and Artifacts
- DB: SQLAlchemy ORM over SQLite (local default) or Postgres.
- SQLite local reliability settings:
  - WAL mode
  - busy timeout
  - `check_same_thread=False`
- Artifacts: filesystem store under `.artifacts/` by default.
- Prompts, inputs, raw outputs, parsed outputs, validation errors, and key stage results are persisted for replay/debugging.

## Infra Assets
- Terraform baseline: [`infra/terraform/main.tf`](/Users/Omar/orthos-ai/nl-engine/infra/terraform/main.tf)
- Cloud Build skeleton: [`infra/cloudbuild/deploy.yaml`](/Users/Omar/orthos-ai/nl-engine/infra/cloudbuild/deploy.yaml)
- Operations notes: [`docs/OPERATIONS.md`](/Users/Omar/orthos-ai/nl-engine/docs/OPERATIONS.md)

## Tests
Run all tests:
```bash
pytest -q
```

Useful subsets:
```bash
pytest -q -m regression
RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging
```

Evaluator harness:
```bash
PYTHONPATH=src python tools/evaluator/run_eval.py --cases tools/evaluator/cases.sample.json
```
