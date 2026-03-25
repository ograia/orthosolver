# NL Engine Overview

Consolidated implementation-facing overview of the NL engine/orchestrator.
This file combines the content from:
- [`docs/ARCHITECTURE_SUMMARY.md`](/Users/Omar/orthos-ai/nl-engine/docs/ARCHITECTURE_SUMMARY.md)
- [`docs/REPO_BLUEPRINT.md`](/Users/Omar/orthos-ai/nl-engine/docs/REPO_BLUEPRINT.md)
- [`docs/REQUIREMENT_MATRIX.md`](/Users/Omar/orthos-ai/nl-engine/docs/REQUIREMENT_MATRIX.md)
- [`docs/RUNBOOK.md`](/Users/Omar/orthos-ai/nl-engine/docs/RUNBOOK.md)
- [`docs/OPERATIONS.md`](/Users/Omar/orthos-ai/nl-engine/docs/OPERATIONS.md)
- [`docs/SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md`](/Users/Omar/orthos-ai/nl-engine/docs/SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md)

For architecture/spec authority order:
1. [`docs/nl_engine.tex`](/Users/Omar/orthos-ai/nl-engine/docs/nl_engine.tex)
2. Runtime code in `src/nl_engine/`
3. This overview and other markdown docs in `docs/`

## 1. Scope and Boundary
This repository owns the NL orchestrator:
- intake API + debug API/UI
- controller/state-machine and routing
- agent integration (Agents 1-5)
- persistence, migrations, artifacts, and observability
- Lean engine client boundary and mock Lean service
- infra baselines for deployment

This repository does **not** implement Lean engine internals.

## 2. Runtime Surfaces
Core runtime entry points:
- Main API: [`src/nl_engine/api/main.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/api/main.py)
- Debug API/UI: [`src/nl_engine/api/debug.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/api/debug.py), static assets in `src/nl_engine/api/static/debug/`
- Controller/orchestrator: [`src/nl_engine/controller/orchestrator.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/controller/orchestrator.py)
- Routing policy: [`src/nl_engine/routing/policy.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/routing/policy.py)
- Agent service: [`src/nl_engine/services/agents.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/services/agents.py)
- Worker facade/idempotency: [`src/nl_engine/workers/facade.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/workers/facade.py)
- Lean client boundary: [`src/nl_engine/lean_client/client.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/lean_client/client.py)
- Mock Lean service: [`mock_lean/main.py`](/Users/Omar/orthos-ai/nl-engine/mock_lean/main.py)

## 3. Lifecycle Truth (Current Runtime)
### Create phase
`POST /v1/problems` is create-only and does all of the following:
- validates payload
- runs Agent1 semantic sketching on the root objective
- persists create request/response artifacts
- creates `ProblemORM` + root `TheoremORM`
- appends `problem.created` event

Create does **not** start decomposition.

### Run phase
`POST /v1/problems/{problem_id}/start`:
- creates or reuses a durable execution row
- schedules background work and returns immediately
- when invoked as Continue (`X-Debug-Run-Trigger: continue_button`), starts a fresh continuation generation and supersedes old queued/running jobs

`POST /v1/problems/{problem_id}/run`:
- is preserved as a compatibility alias for `start`
- writes compatibility request/response artifacts
- no longer performs controller work inline

`POST /v1/problems/{problem_id}/resume`:
- resumes failed problems in place
- appends a new durable execution
- preserves prior run artifacts and returns resume execution metadata

`POST /v1/problems/{problem_id}/pause`:
- sets problem status to `paused`
- requests stop for the active execution

Execution progress is driven by DB-leased background workers, not repeated API ticks.

### Decomposition phase
- Agent2 produces decomposition candidates.
- Agent3 vets decomposition candidates.
- Lemmas are materialized **only for accepted decompositions**.
- Rejected decomposition nodes remain visible for audit/debug.
- Rejected-candidate lemmas do not enter operational lemma views.

Root-parallel decomposition:
- `parallel_root_decompositions_n` issues N independent durable root Agent2 worker jobs.
- `parallel_root_take_k` carries forward the first K vetted root tracks.
- Carried root tracks remain disjoint trees.
- Non-root decomposition remains single-track.

### Lemma phase
- Agent4 lemma solving and Agent5 vetting run as durable `worker_jobs`.
- Controller applies terminal lemma worker outputs through exactly-once consumption metadata on `worker_jobs`.
- Retry-first override behavior:
  - medium severity findings -> retry solver
  - high severity findings -> retry solver until fatal cap
- `max_consecutive_fatal_rejections_per_lemma` controls consecutive fatal rejection escalation.
- `max_minor_rejections_per_lemma` controls cumulative minor rejection escalation.
- `max_decompositions_per_failed_lemma` controls lemma decomposition attempt budget.
- In `nl_only_mode=true`, accepted NL proof can be terminal lemma success.

### Lean phase (standard mode)
Lean route modes include:
- `check_assembly`
- `formalize_lemma`
- `assemble_root`
- `check_statement_plausibility`

Controller routing depends on Lean result classes and problem config.

### Reconciliation
Interrupted legacy compatibility `/run` artifacts can be reconciled with:
`POST /v1/debug/problems/{problem_id}/reconcile-incomplete-runs`.
Infrastructure-failed problems can be resumed in place (without deleting prior artifacts) with:
`POST /v1/debug/problems/{problem_id}/resume-after-infrastructure-failure`.

## 4. Public and Debug API Surface
Required public endpoints:
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
- `GET /debug` (debug UI)

`POST /v1/debug/reset-local` blocks new execution starts, waits active in-process work to unwind, then deletes local rows + artifacts.

## 5. Config and Runtime Controls
Primary config definitions:
- request contract: [`src/nl_engine/domain/contracts.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/contracts.py)
- runtime defaults/schema: [`src/nl_engine/domain/config.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/config.py)
- environment defaults: [`src/nl_engine/settings.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/settings.py)

Per-agent overrides under `config.llm.agent1..agent5` support:
- `model` (`gpt-5.4|gpt-5.4-pro|gpt-5-mini`)
- `thinking_level` (`none|low|medium|high|xhigh`)
- `verbosity` (`low|medium|high`)
- `timeout_seconds` (default `600`)

Aliases accepted by runtime:
- `reasoning_effort` -> `thinking_level`
- `text_verbosity` or `text.verbosity` -> `verbosity`

Local defaults are cost-minimized for validation:
- `gpt-5-mini`
- `thinking_level=none`
- `verbosity=low`

User-facing config focus area:
- `config.llm.agent{1..5}.timeout_seconds` and optional per-agent model/thinking/verbosity
- `config.lemma_solving.max_consecutive_fatal_rejections_per_lemma`
- `config.lemma_solving.max_minor_rejections_per_lemma`
- `config.lemma_solving.max_total_lemma_nodes`
- `config.decomposition.parallel_root_decompositions_n`
- `config.decomposition.parallel_root_take_k`
- `config.decomposition.max_decompositions_per_failed_lemma`
- `config.decomposition.max_consecutive_fatal_rejections_per_node`

Runtime-internal defaults (not primarily user-facing):
- global timeout and ops threshold internals
- decomposition/Lean tool-call and timeout internals
- additional safety caps and routing defaults

## 6. Invariants and Requirement Mapping
Core invariants enforced in code:
- Root objective remains the only theorem.
- Non-root obligations are lemmas.
- Root theorem semantic sketch is immutable after creation.
- Every theorem/lemma has a semantic sketch.
- Formal success requires Lean-verified success unless `nl_only_mode=true`.
- NL-only outcomes are tagged (`verification_level = nl_only`).
- Major semantic drift is a hard stop.
- Trusted context includes only compiler-accepted Lean outputs.
- Assembly plans must be trivially composable.
- Controller limits are config-driven (no hardcoded retry/depth/timeout caps).
- Routing-critical state lives in first-class DB columns (not opaque JSON only).
- Agent outputs are valid JSON and persisted with artifacts.

Requirement anchors:
- controller/routing: [`src/nl_engine/controller/orchestrator.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/controller/orchestrator.py), [`src/nl_engine/routing/policy.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/routing/policy.py)
- model/contracts/config: [`src/nl_engine/domain/models.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/models.py), [`src/nl_engine/domain/contracts.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/contracts.py), [`src/nl_engine/domain/config.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/config.py)
- Lean boundary: [`src/nl_engine/lean_client/client.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/lean_client/client.py)

## 7. Data, Persistence, and Artifacts
Data model and schema:
- ORM models: [`src/nl_engine/domain/models.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/models.py)
- SQL migrations:
  - [`database/migrations/0001_initial.up.sql`](/Users/Omar/orthos-ai/nl-engine/database/migrations/0001_initial.up.sql)
  - [`database/migrations/0002_phase34_hardening.up.sql`](/Users/Omar/orthos-ai/nl-engine/database/migrations/0002_phase34_hardening.up.sql)

Phase-3/4 persistence includes:
- `worker_jobs`
- `llm_usage_records`
- `run_cost_rollups`

Local DB defaults:
- SQLite WAL mode
- busy timeout
- `check_same_thread=False`

Artifacts:
- stored under `.artifacts/` by default
- prompts, inputs, raw outputs, parsed outputs, validation errors, and key stage outputs are persisted for replay/debugging

## 8. Observability and Debug UX
Observability surfaces:
- event timeline via repositories and `/events` APIs
- progress and status surfaces via `/progress`
- cost/usage via `/cost` and debug usage endpoints
- stage artifacts via debug artifact APIs

Debug UI (`/debug`) capabilities:
- guided or raw JSON submission
- auto-start runs and live refresh (SSE + polling fallback)
- request log with completion classification (`completed|pending|failed`)
- request inspector visibility for provider `response_id` and provider terminal status
- tree explorer + node detail panes
- root-track selector when multiple root tracks are active
- artifact browser/viewer with safe path handling
- overview panels for status, token/cost stats, and NL-only final output JSON
- per-problem delete and global local reset

## 9. Lean Boundary Contract
Lean is an external asynchronous boundary:
- submit, poll, and cancel behavior via Lean client
- mock Lean for local development
- OIDC-ready auth mode through settings and token path

Operational rule:
- Lean outcomes route through controller classification and config-driven policy; this repo does not implement Lean repair internals.

## 10. Repo Blueprint (Current)
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
│   ├── OPERATIONS.md
│   └── SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md
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

Directory ownership highlights:
- `src/nl_engine/api/`: public/debug HTTP surfaces, envelopes, SSE, run-state coordination
- `src/nl_engine/controller/`: orchestrator tick/state-machine and routing transitions
- `src/nl_engine/domain/`: enums, contracts, config, ORM types
- `src/nl_engine/services/`: OpenAI integration, normalization, cleanup services
- `src/nl_engine/workers/`: idempotent in-process worker facade keyed by `job_id`
- `src/nl_engine/persistence/`: DB engine/session + repositories
- `src/nl_engine/observability/`: events, usage/cost accounting, metric hooks
- `prompts/`: system prompts for Agents 1-5
- `contracts/`: interface notes and schemas
- `database/migrations/`: SQL schema evolution source of truth
- `tests/`: unit + integration lifecycle and contract coverage

## 11. Local Setup and Minimal Flow
### Setup
```bash
cd /Users/Omar/orthos-ai/nl-engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Required environment:
- `OPENAI_API_KEY`
- `OPENAI_MODEL_AGENT1` ... `OPENAI_MODEL_AGENT5`
- `LEAN_ENGINE_BASE_URL` (mock default `http://localhost:8081`)
- optional Lean auth settings for real Lean (`LEAN_ENGINE_AUTH_MODE=oidc` etc.)

### Start services
```bash
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000
```

### Minimal API loop
Create:
```bash
curl -sS -X POST "http://localhost:8000/v1/problems" \
  -H "content-type: application/json" \
  -d '{"title":"Demo","statement_nl":"For all n, n = n","config":{"mode":{"nl_only_mode":true}}}' | jq
```

Tick until terminal:
```bash
PROB=<problem_id>
while true; do
  curl -sS -X POST "http://localhost:8000/v1/problems/${PROB}/run" | jq
  STATUS=$(curl -sS "http://localhost:8000/v1/problems/${PROB}" | jq -r '.problem.status')
  if [ "$STATUS" = "succeeded" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 0.5
done
```

Inspect:
```bash
curl -sS "http://localhost:8000/v1/problems/${PROB}/progress" | jq
curl -sS "http://localhost:8000/v1/problems/${PROB}/events?limit=200" | jq
curl -sS "http://localhost:8000/v1/problems/${PROB}/cost" | jq
```

Helper:
```bash
bash scripts/run_nl_only_local.sh --create
```

## 12. Operations, Triage, and Rollout
### Health checks
- API process boot and endpoint responsiveness
- Lean service health (`GET /v1/health` on Lean)
- orchestrator progress/events/cost signals

### Common incidents
Runs not advancing:
1. Check `GET /v1/problems/{id}/execution` and the debug execution list for a nonterminal execution.
2. Check debug request log states.
3. Check whether events continue appending.
4. Reconcile interrupted legacy compatibility `/run` artifacts if needed.

Agent infrastructure errors:
1. Verify `OPENAI_API_KEY` and runtime env loading.
2. Verify model and agent overrides.
3. Inspect request/raw-output/validation artifacts.

DB contention (`db_locked`, local SQLite):
1. Retry.
2. Reduce excessive concurrent worker/API activity and heavy polling.
3. Keep WAL/busy-timeout defaults.

Lean route failures (standard mode):
1. Inspect `lean_jobs` and `lean_results`.
2. Check `config.routing` classes.
3. Validate Lean auth mode and endpoint reachability.

### Rollout checklist (cloud-oriented)
1. Apply migrations.
2. Deploy orchestrator and worker services.
3. Verify secrets/config (`OPENAI`, DB, Lean auth).
4. Run smoke flow in NL-only mode and standard mode.
5. Confirm events/cost/usage telemetry emission.

Deployment skeleton references:
- [`infra/cloudbuild/deploy.yaml`](/Users/Omar/orthos-ai/nl-engine/infra/cloudbuild/deploy.yaml)
- [`infra/terraform/main.tf`](/Users/Omar/orthos-ai/nl-engine/infra/terraform/main.tf)

### Rollback
1. Roll back to last known-good runtime revision.
2. If schema-related, apply compatible down migration + redeploy.
3. Re-run smoke + regression checks.

### On-call triage flow
1. Identify failing `problem_id`.
2. Pull `/progress`, `/events`, and `/failure-report`.
3. Use `/debug` request log and artifacts to localize stage.
4. Classify root cause (agent infra, routing logic, Lean class, config/environment).
5. Mitigate and document.

## 13. Testing and Validation
Primary suite:
```bash
pytest -q
```

Useful subsets:
```bash
pytest -q -m regression
RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging
```

Current coverage shape includes:
- config loading and cap enforcement
- routing decisions and state transitions
- public/debug API contracts
- decomposition/lemma pipeline progression
- Lean boundary/auth/mock behavior
- migration/schema presence
- worker idempotency and run-lock semantics
- debug request completion classification

## 14. Known Runtime Deltas vs TeX Spec (2026-03-13)
This section summarizes intentional contract deltas tracked in
[`docs/SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md`](/Users/Omar/orthos-ai/nl-engine/docs/SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md).

1. Agent5 finding compatibility:
- TeX uses `minor|fatal` + `description`.
- Runtime supports `low|medium|high` plus aliases (`minor->low`, `fatal/critical->high`) and accepts finding text from either `finding` or `description`.

2. Simplified user-facing problem config:
- Runtime input surface is narrowed to practical user knobs (agent timeout/overrides, lemma retry/node limits, root parallel decomposition controls, fatal rejection streak cap).
- Some controls are internal defaults rather than user-facing knobs.

3. Global timeout enforcement:
- TeX references `global_timeout_seconds` lifecycle enforcement.
- Runtime currently disables global timeout failure path in controller advancement logic.

4. Root decomposition parallel controls:
- Runtime supports `parallel_root_decompositions_n` and `parallel_root_take_k`.
- Compatibility invariant maintained: single primary `active_decomposition_id` still exists.

5. Fatal decomposition rejection streak cap on lemma nodes:
- Runtime supports `decomposition.max_consecutive_fatal_rejections_per_node`.
- Streak increments on `rejected_fatal`, resets on `rejected_minor` or `accepted`.
- Exceeding cap halts further lemma-node decomposition retries and uses existing escalation paths.

## 15. Contributor Notes
- Keep changes modular and boundary-respecting.
- Update docs/tests when behavior changes.
- Do not edit TeX spec files unless explicitly requested:
  - `docs/nl_engine.tex`
  - `docs/lean_engine.tex`
