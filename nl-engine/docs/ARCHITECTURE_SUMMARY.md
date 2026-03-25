# Architecture Summary

Implementation-facing architecture summary for the current codebase.
Authoritative behavioral spec remains [`docs/nl_engine.tex`](/Users/Omar/orthos-ai/nl-engine/docs/nl_engine.tex).

Contract divergence notes are tracked in
[`docs/SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md`](/Users/Omar/orthos-ai/nl-engine/docs/SPEC_DELTA_TEX_RUNTIME_CONTRACTS.md).

## 1. System Boundaries
This repository implements the **NL orchestrator**:
- intake API
- controller and routing logic
- NL agents (1-5) execution boundary
- persistence and artifacts
- Lean client boundary
- observability/debug interfaces

This repository does **not** implement Lean engine internals.

## 2. Runtime Surfaces
- Main API app: [`src/nl_engine/api/main.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/api/main.py)
- Debug router/UI: [`src/nl_engine/api/debug.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/api/debug.py), static assets in `src/nl_engine/api/static/debug/`
- Controller: [`src/nl_engine/controller/orchestrator.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/controller/orchestrator.py)
- Durable execution runtime: [`src/nl_engine/execution/runtime.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/execution/runtime.py)
- Routing table: [`src/nl_engine/routing/policy.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/routing/policy.py)
- Agent service: [`src/nl_engine/services/agents.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/services/agents.py)
- Worker boundary: [`src/nl_engine/workers/facade.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/workers/facade.py)
- Lean client: [`src/nl_engine/lean_client/client.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/lean_client/client.py)

## 3. Core Lifecycle Truth
### Create phase
- `POST /v1/problems`:
  - validates payload
  - runs Agent1 root semantic sketch
  - persists create request/response artifacts
  - creates `ProblemORM` and root `TheoremORM`
  - appends `problem.created` event

### Execution phase
- `POST /v1/problems/{id}/start`:
  - creates or reuses a durable `problem_executions` row
  - schedules background execution work
  - returns execution metadata immediately
- `POST /v1/problems/{id}/run`:
  - is preserved as a compatibility alias for `start`
  - writes compatibility request/response artifacts
  - no longer performs orchestration inline
- `execution_worker` claims `problem_executions` by DB lease and advances the controller until blocked.
- `stage_worker` claims `worker_jobs` by DB lease, executes agent work, persists results, and wakes the owning execution.
- In-memory run-state remains only as local stop/reset coordination, not as the source of truth for execution correctness.

### Decomposition phase
- Agent2 generates candidates.
- Agent3 vets candidates.
- For root-only parallel search, `parallel_root_decompositions_n` issues that many independent durable Agent2 `worker_jobs`.
- Root parallel generation no longer depends on request-scoped join windows or later manual ticks for harvesting.
- Transient Agent2 transport failures are persisted on the worker job and retried/re-woken through durable execution state.
- `parallel_root_take_k` carries forward only the first `K` root decomposition tracks to finish vetting; their lemma trees stay disjoint from one another.
- `root_solutions_required_for_termination` controls how many solved root tracks are required before terminal success (default `1`, max `parallel_root_take_k`).
- Losing root tracks with transient transport failures do not block a surviving accepted track from being kept.
- Non-root decomposition remains single-track per lemma node.
- Rejected decomposition nodes are kept for audit/debug.
- **Lemmas are materialized only for accepted decompositions.**
- In standard mode, accepted decompositions can require Lean assembly checks before activation.

### Lemma phase
- Agent4 lemma solving and Agent5 vetting are durable `worker_jobs` claimed by `stage_worker`.
- Controller harvests completed/failed lemma worker rows on wake and applies routing exactly once per terminal worker row.
- Accepted lemma proofs are persisted as recursive proof-bundle artifacts under `problems/{problem_id}/proof_bundles/...`.
- If a lemma is solved through recursive subdecomposition, the controller assembles a parent lemma proof bundle instead of relying only on flat `latest_nl_proof`.
- Route override behavior in controller:
  - medium severity findings -> retry solver
  - high severity findings -> retry solver until fatal cap is reached
- Lemma escalation controls:
  - `max_consecutive_fatal_rejections_per_lemma` (consecutive fatal cap)
  - `max_minor_rejections_per_lemma` (cumulative minor cap)
  - `max_decompositions_per_failed_lemma` (max decomposition attempts per failed lemma)
- In `nl_only_mode=true`, accepted NL proof becomes terminal lemma success.

### Lean phase (standard mode)
- Modes:
  - `check_assembly`
  - `formalize_lemma`
  - `assemble_root`
  - `check_statement_plausibility`
- Lean results route back through controller according to error class and config.

### Final check phase
- Agent6 consumes a recursive root proof bundle, not a flat direct-lemma list.
- `config.final_check.fail_problem_on_fatal` controls fatal routing:
  - `false` (default): localized lemma-scoped Agent6 findings reopen only the cited lemmas on the same accepted root branch
  - `true`: fatal Agent6 findings fail the whole problem immediately
- Agent6 no longer triggers implicit root replanning on fatal output.

## 4. Data and Persistence
- SQLAlchemy models: [`src/nl_engine/domain/models.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/models.py)
- Config model: [`src/nl_engine/domain/config.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/config.py)
- API/debug contracts: [`src/nl_engine/domain/contracts.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/domain/contracts.py)
- Migrations:
  - `0001_initial` (core entities)
  - `0002_phase34_hardening` (`worker_jobs`, `llm_usage_records`, `run_cost_rollups`)
  - `0003_durable_problem_execution` (`problem_executions`, `request_records`, durable worker job extensions)
  - `0004_worker_job_controller_consumption` (controller terminal-consumption metadata for exactly-once harvest)

Local defaults:
- SQLite + WAL + busy timeout in [`src/nl_engine/persistence/db.py`](/Users/Omar/orthos-ai/nl-engine/src/nl_engine/persistence/db.py)
- artifact storage rooted at `.artifacts/`

## 5. Observability and Replay
- Event log and state transitions via `EventRepository` and `EventLogger`.
- Progress and events APIs for external monitoring.
- DB-backed request ledger via `request_records`, with artifacts as supporting evidence.
- Durable execution state via `problem_executions`.
- LLM usage + cost tracking:
  - stage-level `llm_usage_records`
  - rollups and `/v1/problems/{id}/cost`
  - debug usage summary endpoint

## 6. Debug Interface
UI route: `/debug`.

Key features:
- request submission (guided + raw JSON)
- live refresh + SSE + poll fallback
- request log with completion classification
- request log exposure of provider `response_id` and provider terminal status for failed background requests
- execution list for durable background runs
- tree explorer + node details, including root-track toggle when multiple root decompositions are active
- node detail split between `Proof JSON` and `Status JSON`
- overview exposure of running/final recursive proof bundles
- artifact explorer with safe path handling
- per-problem delete and global reset
- legacy interrupted-compatibility-run reconciliation endpoint
- infrastructure-failure resume endpoint that appends a fresh execution without deleting prior artifacts
- NL-only final output JSON panel

Operational visibility:
- failed decomposition nodes remain visible
- rejected-candidate lemmas are hidden from operational lemma views

## 7. Invariants Enforced in Code
- root theorem sketch immutability
- semantic sketch on every theorem/lemma
- major semantic drift hard stop
- trusted context promotion only on compiler-accepted Lean outputs
- root remains theorem, non-root obligations are lemmas
- config-driven caps (no hardcoded controller limits)

## 8. Test Coverage Shape
Current tests cover:
- public API flows (NL-only + standard)
- durable start/execution compatibility flows
- decomposition/lemma pipeline progression
- retry/decompose routing behaviors
- debug API/UI data surfaces
- Lean client auth and mock contract behavior
- migrations presence and phase-3/4 tables
- worker idempotency/backpressure and durable execution leasing
