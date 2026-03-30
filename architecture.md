# NL Engine Full Architecture

Last verified against code on 2026-03-29.

This document is the single, full architecture reference for the NL engine in this repository.
It is built from direct source inspection of:

- API layer (`src/nl_engine/api/main.py`, `src/nl_engine/api/debug.py`)
- Runtime and orchestration (`src/nl_engine/execution/runtime.py`, `src/nl_engine/controller/orchestrator.py`)
- Agent boundary (`src/nl_engine/services/agents.py`, `src/nl_engine/workers/facade.py`, `src/nl_engine/workers/contracts.py`)
- Contracts and config (`src/nl_engine/domain/contracts.py`, `src/nl_engine/domain/config.py`, `src/nl_engine/domain/enums.py`)
- Persistence and storage (`src/nl_engine/persistence/*`, `src/nl_engine/storage/object_store.py`, `src/nl_engine/artifacts/store.py`)
- Lean boundary (`src/nl_engine/lean_client/*`, `contracts/lean_engine/http_contract.md`)

## 1. System Boundary and Ownership

The NL engine owns:

- Public FastAPI APIs for problem lifecycle, progress, events, costs, and debug surfaces.
- Durable execution orchestration (problem executions and worker jobs).
- Agent orchestration for Agents 1 through 8.
- Routing policy from vetter and Lean classifications into retries/decomposition/formalization actions.
- JSON object persistence for state, artifacts, events, and usage/cost records.
- Lean service integration over HTTP (v1 and v2 endpoints, with standard flow centered on v2 operations).

The NL engine does not implement Lean internals. It treats Lean as an external service boundary.

## 2. High-Level Runtime Architecture

Core architecture layers:

1. API layer: FastAPI endpoints, webhook handler, SSE stream, debug routes.
2. Durable execution layer: `ProblemExecutionORM` state machine (`queued -> running -> waiting -> terminal`).
3. Orchestrator layer: theorem/decomposition/lemma state transitions and routing logic (`Orchestrator.run_once`).
4. Worker layer: idempotent worker jobs by worker kind with leases, retries, and durable outputs.
5. Agent layer: OpenAI request lifecycle, structured JSON contracts, parse/validation safeguards.
6. Lean layer: operation submit/poll/cancel (`prepare_track`, `formalize_lemma_from_nl`, `assemble_root_from_track`, optional split operations).
7. Persistence layer: JSON repositories on filesystem or GCS-backed object storage.
8. Observability layer: events, request records, debug snapshot surfaces, usage/cost rollups.

## 3. End-to-End Pipeline Lifecycle

### 3.1 Create

Endpoint:

- `POST /v1/problems`

Behavior:

- Validates and stores `ProblemCreateRequest`.
- Creates:
	- `ProblemORM` with `status=created`
	- `TheoremORM` root node with empty semantic sketch
	- optional initial trusted context rows
- Writes API request/response artifacts and request records.
- Emits `problem.created` event.

Important: create is create-only. It does not run the full lifecycle inline.

### 3.2 Start / Run / Resume / Pause / Cancel

Endpoints:

- `POST /v1/problems/{id}/start`
- `POST /v1/problems/{id}/run` (compatibility alias)
- `POST /v1/problems/{id}/resume`
- `POST /v1/problems/{id}/pause`
- `POST /v1/problems/{id}/cancel`

Behavior:

- `start`/`run` schedules or wakes a durable execution via `ProblemExecutionService.ensure_running`.
- `resume` requires problem `failed`; resets failure state, applies resume anchor, and starts a fresh continuation generation.
- `pause` sets problem status `paused`, requests execution stop, and cancels Lean work.
- `cancel` requests execution stop and cancels Lean work.

### 3.3 Execution Driver Loop

`ExecutionDriver.advance_until_blocked(execution_id)` drives progress.

Loop behavior:

- Claims `queued` execution as `running`.
- Validates generation freshness and stop conditions.
- Renews execution lease each tick.
- Requeues expired worker leases each tick.
- Calls `Orchestrator.run_once(problem_id)` up to 32 ticks before yielding.
- Transitions execution to:
	- `waiting` with `blocking_kind=worker_job` when worker jobs are inflight
	- `waiting` with `blocking_kind=lean_job` when Lean jobs are inflight
	- `waiting` with `blocking_kind=none` when no progress is detected
	- `succeeded`, `failed`, or `cancelled` at terminal/problem-stop states

### 3.4 Orchestrator Main Loop

`Orchestrator.run_once(problem_id)` implements theorem/decomposition/lemma advancement.

Main sequence:

1. Validate problem/root theorem and apply mode synchronization.
2. Harvest Lean terminal results and route them.
3. Ensure root semantic sketch exists (durable worker path).
4. Generate root decompositions if needed.
5. Submit root/decomposition Lean `prepare_track` (standard mode).
6. Select active decomposition (or fail when exhausted).
7. Process decomposition lemmas:
	 - solve
	 - vet
	 - route
	 - formalize
	 - optional split/decompose-further
8. Run final checks (Agent6) on solved decompositions.
9. Finalize success if completion criteria are met.
10. Detect dead frontier and mark failure if no runnable action remains.

## 4. Decomposition Architecture

### 4.1 When decomposition is generated

Root decomposition generation:

- Triggered when root has semantic sketch but no root decomposition yet.
- Also continues in parallel until configured root track target is reached.
- Uses configurable parallel root generation queueing and harvesting.

Lemma decomposition generation:

- Triggered when lemma routes to `decompose_further`.
- Triggered when proof exhaustion can still consume decomposition slots.
- Triggered from split-existing-proof materialization path.

### 4.2 Candidate materialization rules

For each Agent2 candidate:

- Canonicalize each lemma semantic sketch through Agent1 before solving.
- Reject candidate before vetting if `final_step_yields_exact_root=false`.
- Run Agent3 vetting unless bundle is pre-vetted by split-vetter path.
- Accept/reject logic:
	- fatal or major drift or false lemma finding => reject fatal
	- minor_fix + allowed minor-drift warning policy => can accept
	- otherwise accepted

Accepted candidate creates:

- `DecompositionORM`
- `AssemblyPlanORM`
- `LemmaORM` rows for candidate lemmas
- proof graph bootstrap and dependency checks

### 4.3 Active decomposition selection

- Root: selects from accepted root decompositions according to configured parallel take-K policy.
- Lemma: promotes selected accepted lemma candidate into active child decomposition.
- Standby promotion is used when active branch fails and alternates exist.

## 5. Agent and Worker Architecture

### 5.1 Worker kinds

Worker kind enum:

- `root_semantic_sketch`
- `decomposition_generation`
- `decomposition_vetting`
- `lemma_solver`
- `lemma_vetter`
- `proof_split_generation`
- `proof_split_vetting`
- `final_check`
- `lean_dispatch` (declared, but Lean dispatch is orchestrator/lean-client driven)

### 5.2 Agent surfaces

Agent methods in `AgentService`:

- Agent1: `semantic_sketch`
- Agent2: `decompose`
- Agent3: `vet_decomposition`
- Agent4: `solve_lemma`
- Agent5: `vet_lemma_proof`
- Agent6: `final_check`
- Agent7: `split_existing_proof`
- Agent8: `vet_split_existing_proof`

Prompt files:

- `prompts/agent1_semantic_sketch/system.txt`
- `prompts/agent2_decomposer/system.txt`
- `prompts/agent3_decomposition_vetter/system.txt`
- `prompts/agent4_lemma_solver/system.txt`
- `prompts/agent5_lemma_vetter/system.txt`
- `prompts/agent6_final_checker/system.txt`
- `prompts/agent7_split_existing_proof/system.txt`
- `prompts/agent8_split_bundle_vetter/system.txt`

### 5.3 Worker idempotency boundary

`WorkerFacade` enforces job-id idempotency:

- Durable result key: `worker_jobs/{job_id}/result.json`
- Completed cached result is replayed.
- Cached transient infrastructure failure can be deleted and retried.
- Non-transient cached failure re-raises immediately.
- Per-kind concurrency and pending backpressure limits are enforced.

## 6. Routing and Decision Policies

### 6.1 Vetter routing (`route_vetter_result`)

Inputs:

- `statement_status`
- `proof_status`
- `drift_level`
- config drift policy

Outputs include:

- `send_to_lean`
- `retry_solver`
- `decompose_further`
- `flag_suspected_false`
- `blocked` (major drift when configured to block)

### 6.2 Lean routing (`route_lean_result`)

Inputs:

- terminal Lean status
- `error_class`
- `issue_kind` (`proof_issue` or `lean_issue`)
- routing config classes

Outputs include:

- `done`
- `retry_lean_only`
- `retry_nl_proof`
- `retry_assembly_plan`
- `decompose_further`

Deterministic Lean setup errors route directly to decomposition.

## 7. Run Stop, Pause, Cancel, and Terminal Semantics

### 7.1 Status enums

Problem status:

- `created`, `running`, `paused`, `succeeded`, `failed`

Execution status:

- `queued`, `running`, `waiting`, `succeeded`, `failed`, `cancel_requested`, `cancelled`

Execution desired state:

- `running`, `stopped`

Lemma proof status:

- `open`, `proof_found`, `proof_flawed`, `proof_vetted`, `proof_formalized`, `nl_accepted`, `failed`, `proof_exhausted`

Lemma routing status:

- `open`, `retry_solver`, `decompose_further`, `split_existing_proof`, `ready_for_lean`, `send_to_lean`, `blocked`, `done`

### 7.2 When a run is considered blocked/stopped

Blocked (`execution.waiting`) when:

- inflight worker jobs exist
- inflight Lean jobs exist
- no progress in current tick
- execution yields after loop budget

Stopped/cancelled execution when:

- `execution.desired_state == stopped`
- problem is `paused`
- execution generation is superseded by a newer continuation generation

Problem-level pause:

- `POST /pause` sets problem `paused`, requests execution stop, cancels Lean work.

Problem-level cancel:

- `POST /cancel` requests execution stop and cancels Lean work (problem may remain non-terminal unless later transitioned).

### 7.3 Terminal outcomes

Succeeded when:

- NL-only path: required solved root decompositions have final check pass and lemma NL acceptance.
- Standard path: required solved root decompositions are formalized and root assembly succeeds (`assemble_root_from_track`).

Failed when:

- dead frontier is detected
- proof/decomposition exhausted
- unrecoverable infrastructure failure threshold exceeded
- terminal route marks failure with failure report generation

Paused is non-terminal and resumable.

## 8. Full Input Inventory

This section lists input sources and fields consumed by the NL engine pipeline.

### 8.1 External API inputs

`POST /v1/problems` (`ProblemCreateRequest`):

- `title`
- `statement_nl`
- `statement_lean`
- `imports`
- `lean_image_tag`
- `initial_trusted_context[]`:
	- `decl_name`
	- `lean_code`
- `config` (`ProblemConfig`)

`POST /v1/problems/{id}/start`:

- header `X-Debug-Run-Trigger` (optional)

`POST /v1/problems/{id}/run`:

- header `X-Debug-Run-Trigger` (optional)

`POST /v1/problems/{id}/resume`:

- no body

`POST /v1/problems/{id}/pause`:

- no body

`POST /v1/problems/{id}/cancel`:

- no body

`POST /v1/openai/webhook`:

- webhook event body
- request headers used for signature verification when webhook secret is configured

### 8.2 Runtime/environment inputs (`Settings`)

- `env`
- `api_host`
- `api_port`
- `data_dir`
- `storage_backend`
- `gcs_bucket`
- `gcs_state_prefix`
- `gcs_artifact_prefix`
- `openai_api_key`
- `openai_model_agent1`
- `openai_model_agent2`
- `openai_model_agent3`
- `openai_model_agent4`
- `openai_model_agent5`
- `openai_model_agent6`
- `openai_model_agent7`
- `openai_model_agent8`
- `openai_reasoning_effort`
- `openai_text_verbosity`
- `openai_timeout_seconds`
- `openai_usage_persistence_mode`
- `openai_webhook_secret`
- `lean_engine_base_url`
- `lean_engine_runtime_mode`
- `lean_engine_api_version`
- `lean_engine_timeout_seconds`
- `lean_engine_submit_http_retries`
- `lean_engine_poll_http_retries`
- `lean_engine_retry_backoff_seconds`
- `lean_poll_interval_seconds`
- `lean_engine_auth_mode`
- `lean_engine_oidc_audience`
- `lean_engine_oidc_token_source`
- `lean_engine_oidc_token_env_var`
- `mock_lean_default_delay_seconds`
- `artifact_store_dir`
- `sse_heartbeat_seconds`
- `worker_default_max_concurrency`
- `worker_default_max_pending`
- `worker_lease_seconds`
- `worker_poll_interval_seconds`
- `worker_enable_embedded_supervisor`
- `openai_price_input_per_1k`
- `openai_price_cached_input_per_1k`
- `openai_price_output_per_1k`
- `enable_cloud_monitoring`
- `gcp_project_id`

### 8.3 Per-problem config inputs (`ProblemConfig`)

Mode:

- `mode.nl_only_mode`
- `mode.lean_mode`
- `mode.lean.enabled` (compatibility/no-op in standard orchestrator behavior)
- `mode.lean.use_v2_endpoints` (compatibility/no-op)
- `mode.lean.use_v2_prepare_track` (compatibility/no-op)
- `mode.lean.fallback_to_v1_on_error` (compatibility/no-op)
- `mode.lean.max_track_attempts`
- `mode.lean.auto_split_sublemmas`
- `mode.lean.stream_progress_payloads`
- `mode.lean.strict_proof_issue_fail_fast`
- `mode.lean.proof_issue_confidence_threshold`

Decomposition:

- `decomposition.parallel_root_decompositions_n`
- `decomposition.parallel_root_take_k`
- `decomposition.parallel_root_join_timeout_seconds`
- `decomposition.root_solutions_required_for_termination`
- `decomposition.lemma_decomposition_candidates_n`
- `decomposition.max_consecutive_fatal_rejections_per_node`
- `decomposition.max_decompositions_per_failed_lemma`
- `decomposition.max_false_frontier_hops_per_branch`

Lemma solving:

- `lemma_solving.max_consecutive_fatal_rejections_per_lemma`
- `lemma_solving.max_minor_rejections_per_lemma`
- `lemma_solving.max_total_lemma_nodes`
- `lemma_solving.max_infrastructure_failures`
- `lemma_solving.max_solver_attempts_per_lemma_total`
- `lemma_solving.max_consecutive_infrastructure_failures_per_lemma`
- `lemma_solving.max_solver_series_wall_clock_seconds_per_lemma`

Lean engine:

- `lean_engine.model`
- `lean_engine.no_lean4_refs`
- `lean_engine.max_repair_rounds`
- `lean_engine.max_workers`
- `lean_engine.internal_packaging_retry_count`
- `lean_engine.repair_context_token_budget`
- `lean_engine.assemble_root_repair_rounds`
- `lean_engine.lean_job_timeout_seconds`
- `lean_engine.assemble_root_timeout_seconds`
- `lean_engine.claude_activity_timeout_seconds`
- `lean_engine.claude_init_timeout_seconds`

Routing:

- `routing.invalidation_confidence_threshold`
- `routing.repairable_lean_only_classes`
- `routing.repairable_nl_loop_classes`
- `routing.max_identical_fatal_class_repeats`

Drift/final/budget:

- `drift.major_drift_blocks_progress`
- `drift.drift_check_on_every_lemma`
- `drift.minor_drift_adds_warning_only`
- `final_check.fail_problem_on_fatal`
- `budget.max_estimated_cost_usd_per_problem`
- `budget.max_estimated_cost_usd_per_lemma`

LLM overrides:

- `llm.agent1`..`llm.agent8` each with:
	- `model`
	- `thinking_level` (alias `reasoning_effort`)
	- `verbosity` (alias `text_verbosity`)
	- `coding_mode`
	- `timeout_seconds`
	- `max_attempts`
- first-attempt overrides:
	- `llm.agent2_first_root`
	- `llm.agent2_first_lemma`
	- `llm.agent4_first`

### 8.4 Worker job envelope inputs

`WorkerJob` envelope fields:

- `job_id`
- `problem_id`
- `worker_kind`
- `payload`
- `execution_id`
- `llm_overrides`
- `worker_attempt_count`

`WorkerJobORM` runtime state includes additional scheduler inputs:

- `target_id`, `target_kind`
- `continuation_generation`
- `attempt_number`, `attempt_count`, `max_attempts`
- `artifact_prefix`
- `handler_key`
- `request_source`
- lease fields and supersede metadata

### 8.5 Agent input schemas

Agent1 input (`Agent1Input`):

- `statement_nl`

Agent2 input (`Agent2Input`):

- `theorem_nl`
- `root_semantic_sketch`
- `shared_context`
- `num_candidates`
- `previous_attempt_summaries`
- `trusted_context_summaries`

Agent3 input (`Agent3Input`):

- `theorem_nl`
- `root_semantic_sketch`
- `decomposition`
- `previous_attempt_summaries`
- `risk_audit`

Agent4 input (`Agent4Input`):

- `lemma_id`
- `statement_nl`
- `semantic_sketch`
- `root_theorem_nl`
- `root_semantic_sketch`
- `role_in_assembly`
- `shared_context`
- `definition_context`
- `trusted_context_summaries`
- `allowed_dependency_manifest`
- `forbidden_claims`
- `proof_attempt_node_id`
- `previous_feedback`
- `previous_proof_nl`
- `attempt_number`

Agent5 input (`Agent5Input`):

- `lemma_id`
- `statement_nl`
- `semantic_sketch`
- `root_semantic_sketch`
- `proof_nl`
- `role_in_assembly`
- `lean_diagnostics`
- `attempt_number`
- `vetting_mode`
- `candidate_counterexample`
- `allowed_dependency_manifest`
- `citations`
- `ancestry_summary`

Agent6 input (`Agent6Input`):

- `problem_id`
- `proof_bundle`
- `proof_graph_summary`
- `dependency_checks_summary`
- `track_identity`

Agent7 input (`Agent7Input`):

- `lemma_id`
- `parent_statement_nl`
- `parent_semantic_sketch`
- `parent_proof_nl`
- `role_in_parent`
- `root_theorem_nl`
- `root_semantic_sketch`
- `ancestry_summary`
- `trusted_context_summaries`
- `lean_failure`
- `previous_attempt_summaries`

Agent8 input (`Agent8Input`):

- `lemma_id`
- `parent_statement_nl`
- `parent_semantic_sketch`
- `parent_proof_nl`
- `lean_failure`
- `proposed_split`

### 8.6 Lean job envelope and operation payload inputs

Lean job request envelope (`LeanJobSubmitRequest`):

- `job_id`
- `problem_id`
- `target_id`
- `target_kind`
- `mode`
- `lean_image_tag`
- `callback_url`
- `payload`

Operation payloads submitted by orchestrator:

`prepare_track` payload includes:

- `track_id`
- `decomposition_id`
- `source`
- `source_kind`
- `source_name`

`prepare_track.source` includes:

- problem identity/title/verification_level
- root theorem statement and semantic sketch
- selected decomposition strategy/shared context/assembly plan
- lemma statements/sketches/proof text snapshots

`formalize_lemma_from_nl` payload includes:

- `run_dir`
- `track_run_dir`
- `track_id`
- `lemma_id`
- `lemma_handle`
- `statement_nl`
- `semantic_sketch`
- `proof_nl`
- `proof_fingerprint`
- `pinned_statement_signature`
- `trusted_context[]`
- `imports`
- `max_repair_rounds`
- `max_tool_calls`
- `timeout_seconds`
- `internal_packaging_retry_count`
- `repair_context_token_budget`
- `model`
- `proof_issue_class`
- `lean_issue_class`

`assemble_root_from_track` payload includes:

- `run_dir`
- `track_run_dir`
- `track_id`
- `infer_dependencies`

Other supported payload patterns used by orchestrator:

- `split_proof_into_sublemmas` (`lemma_id`, `statement_nl`, `proof_nl`)
- `check_statement_plausibility` (`statement_nl`, `semantic_sketch`, `imports`, `timeout_seconds`)
- legacy `check_assembly` retry payload with source package

### 8.7 Retry/history/resume inputs

Historical and resume-driven inputs consumed during runs:

- failure report:
	- `failure_reason`
	- `terminal_lemma_id`
	- `terminal_error_class`
	- `terminal_error_message`
	- `partial_tree`
	- `trusted_context_at_failure`
	- `all_decomposition_attempts`
	- `routing_log_summary`
- problem resume anchor fields:
	- `resume_anchor_lemma_id`
	- `resume_anchor_owner_decomposition_id`
- lemma retry context:
	- `previous_feedback`
	- `previous_proof_nl`
	- prior proof attempt rows/reports
- decomposition retry context:
	- `previous_attempt_summaries`
- trusted context rows:
	- `decl_name`
	- `lean_code`
	- `context_scope`
	- source metadata
- request records and webhook correlation:
	- `provider_response_id`
	- provider status/retry counters

### 8.8 Runtime control inputs (in-memory run state)

Run-state inputs used during scheduling and cancellation:

- per-problem run guard membership
- active problem activity counts
- per-problem stop requests
- global stop flag

These inputs gate claim/dispatch logic and interruption checks.

## 9. Persistence Architecture and Data Layout

No SQL database is used.

### 9.1 State store

State root layout:

```text
{data_dir}/
	problems_index.json
	{problem_id}/
		problem.json
		theorem.json
		execution/{execution_id}.json
		decompositions/{decomposition_id}.json
		decomposition_candidates/{candidate_id}.json
		assembly_plans/{assembly_plan_id}.json
		lemmas/{lemma_id}.json
		vetter_reports/{report_id}.json
		counterexamples/{counterexample_id}.json
		proof_attempts/{proof_attempt_id}.json
		lean_jobs/{job_id}.json
		lean_results/{result_id}.json
		proof_graphs/{proof_graph_id}.json
		proof_graph_nodes/{graph_node_id}.json
		proof_graph_edges/{edge_id}.json
		proof_dependency_checks/{check_id}.json
		trusted_context.json
		failure_report.json
		request_records/{request_record_id}.json
		worker_jobs/{worker_job_id}.json
		events/{event_id}.json
		events.jsonl                  # legacy compatibility
		llm_usage/{usage_id}.json
		llm_usage.jsonl               # legacy compatibility
		cost_rollup.json
```

### 9.2 Artifact store

Artifact roots include API, agent, Lean, and proof bundle artifacts:

- API request/response JSON
- agent prompts/input/request-state/raw-output/parsed-output
- worker job durable result JSON
- Lean request/submit/response artifacts
- proof bundles:
	- root running
	- root final
	- per decomposition
	- per lemma

### 9.3 Storage backends

- Filesystem mode for local development.
- GCS-backed mode with local cache and distributed per-problem lease support.

## 10. Concurrency and Supervisors

Worker supervisor thread model:

- 1 execution loop thread
- 1 reconcile loop thread
- N stage worker threads (N = `worker_default_max_concurrency`, default 8)

Total default threads: 10.

Lease and claim model:

- Executions claimed with lease and renewed every tick.
- Worker jobs claimed with lease and heartbeat renewal thread.
- Expired running jobs are requeued or failed based on retry counters.
- Reconcile loop wakes waiting executions when terminal external progress appears.

## 11. Observability and Debug Surfaces

Primary runtime visibility APIs:

- `GET /v1/problems/{id}/execution`
- `GET /v1/problems/{id}/progress`
- `GET /v1/problems/{id}/lean-jobs`
- `GET /v1/problems/{id}/events`
- `GET /v1/problems/{id}/events/stream` (SSE)
- `GET /v1/problems/{id}/cost`

Debug APIs (router prefix `/v1/debug`) include:

- create template
- problems list
- problem input JSON
- full snapshot
- Lean files by node
- request log
- executions list
- reconcile incomplete runs
- resume after infrastructure failure
- LLM usage summary
- artifact list/read
- log download
- per-problem debug delete
- local reset

UI routes:

- `GET /debug`
- `GET /debug/static/{asset_path}`

## 12. Failure and Recovery Architecture

### 12.1 Failure classification and failure report

Failure reason enum includes:

- `lemma_false`
- `proof_exhausted`
- `dead_frontier`
- `lean_difficulty`
- `assembly_composition_failure`
- `global_timeout`
- `unknown`

On failure, system persists a full `FailureReportORM` and a failure report artifact.

### 12.2 Resume architecture

Resume flow:

1. `POST /resume` validates failed state.
2. Clears failure artifact pointer and infra failure counter.
3. Applies resume anchor derived from failure report terminal lemma.
4. Resets root theorem if needed.
5. Starts fresh continuation generation.
6. Supersedes older inflight worker jobs and cancels stale Lean work.

### 12.3 Supersession and stale-work protection

- Continuation generation is attached to execution and worker rows.
- Older generations are cancelled/superseded.
- Request records tied to superseded execution/job IDs are marked superseded and best-effort provider cancellation is attempted.

## 13. Known Contract and Documentation Deltas

Observed deltas between some historical docs/contracts and runtime:

- Some historical contract docs mention Agents 1-5 only, while runtime supports Agents 1-8 and worker kinds include final check and split-vetting paths.
- Public notes may describe only older endpoint subsets; runtime has expanded lifecycle and debug endpoints.
- Compatibility flags for legacy Lean flow are accepted in config but standard orchestrator behavior is v2-first for standard mode.

## 14. Direct Answers to Requested Focus Areas

When decomposition happens:

- Root: after root semantic sketch is available and root decomposition set is missing or below configured root-track goals.
- Lemma: when routing says `decompose_further`, when exhaustion still has decomposition slots, or after split-existing-proof generation/vetting acceptance.

When a run is stopped:

- Explicit pause/cancel requests set execution desired state to stopped and cause cancellation.
- Problem paused state causes execution cancellation.
- Superseded continuation generation causes old execution cancellation.
- Global/local stop flags can interrupt worker execution and prevent new claims.

All agents in pipeline:

- Agent1 semantic sketch
- Agent2 decomposition generation
- Agent3 decomposition vetting
- Agent4 lemma solver
- Agent5 lemma vetter
- Agent6 final checker
- Agent7 split existing proof generator
- Agent8 split bundle vetter

All major input classes included in this file:

- API request inputs
- runtime env settings
- full problem config tree
- worker envelope inputs
- agent inputs 1-8
- Lean envelope/operation payloads
- historical/retry/resume inputs
- in-memory stop/control inputs
- prompt files consumed as static inputs
