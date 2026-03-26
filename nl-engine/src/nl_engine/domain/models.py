from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProblemORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    problem_id: str
    status: str = "created"
    title: str = ""
    input_mode: str = "nl_only"
    root_theorem_id: str | None = None
    active_decomposition_id: str | None = None
    standby_decomposition_id: str | None = None
    active_proof_graph_id: str | None = None
    lean_image_tag: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    nl_only_mode: bool = False
    verification_level: str = "formal"
    dependency_verification_level: str = "legacy"
    failure_report_artifact_id: str | None = None
    running_final_proof_artifact_id: str | None = None
    final_proof_artifact_id: str | None = None
    infrastructure_failure_count: int = 0
    continuation_generation: int = 0
    resume_anchor_lemma_id: str | None = None
    resume_anchor_owner_decomposition_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TheoremORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    theorem_id: str
    problem_id: str
    kind: str = "theorem"
    statement_nl: str = ""
    statement_lean: str | None = None
    statement_semantic_sketch: dict[str, Any] = Field(default_factory=dict)
    status: str = "open"
    active_decomposition_id: str | None = None
    final_decl_name: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DecompositionORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decomposition_id: str
    problem_id: str
    logical_decomposition_id: str | None = None
    revision_number: int = 1
    node_id: str
    node_kind: str
    proof_graph_id: str | None = None
    strategy_summary: str = ""
    shared_context: list[dict[str, Any]] = Field(default_factory=list)
    lemma_ids: list[str] = Field(default_factory=list)
    assembly_plan_id: str | None = None
    pinned_statement_signatures: dict[str, str] | None = None
    lean_run_dir: str | None = None
    lean_v2_track_id: str | None = None
    lean_v2_lemma_handles: dict[str, str] = Field(default_factory=dict)
    lean_v2_prepare_status: str | None = None
    formalization_cost_estimate: float | None = None
    llm_vetting_status: str = "pending"
    lean_assembly_status: str = "pending"
    controller_status: str = "pending"
    dependency_status: str = "legacy_unknown"
    equivalence_risk: str = "none"
    final_check_passed: bool = False
    final_check_job_id: str | None = None
    proof_bundle_artifact_id: str | None = None
    raw_candidate_artifact_id: str | None = None
    failure_origin: str | None = None
    failure_reason: str | None = None
    invalidated_by_lemma_id: str | None = None
    invalidated_by_counterexample_id: str | None = None
    previous_attempt_summaries: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AssemblyPlanORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assembly_plan_id: str
    problem_id: str
    decomposition_id: str
    root_node_id: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    proof_skeleton_nl: str = ""
    is_trivially_composable: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class LemmaORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lemma_id: str
    problem_id: str
    proof_graph_id: str | None = None
    claim_node_id: str | None = None
    parent_id: str
    parent_kind: str
    kind: str = "lemma"
    depth: int = 1
    statement_nl: str = ""
    statement_lean: str | None = None
    statement_semantic_sketch: dict[str, Any] = Field(default_factory=dict)
    shared_context_refs: list[str] = Field(default_factory=list)
    role_in_parent: str | None = None
    assembly_step_ids: list[str] = Field(default_factory=list)
    difficulty_estimate: float | None = None
    formalization_cost_estimate: float | None = None
    statement_status: str = "unvetted"
    truth_status: str = "unknown"
    proof_status: str = "open"
    routing_status: str = "open"
    latest_nl_proof: str | None = None
    proof_bundle_artifact_id: str | None = None
    latest_vetter_report_id: str | None = None
    latest_lean_result_id: str | None = None
    latest_lean_issue_class: str | None = None
    latest_lean_issue_kind: str | None = None
    counterexample_status: str = "none"
    active_counterexample_id: str | None = None
    dependency_status: str = "legacy_unknown"
    last_dependency_check_id: str | None = None
    solver_attempt_count: int = 0
    consecutive_fatal_rejections: int = 0
    minor_rejection_count: int = 0
    drift_retry_used: bool = False
    decomposition_count: int = 0
    decomposition_round_count: int = 0
    lean_attempt_count: int = 0
    lean_identical_fatal_count: int = 0
    consecutive_infrastructure_failures: int = 0
    solver_series_started_at: datetime | None = None
    next_action: str | None = None
    last_terminal_worker_result: str | None = None
    last_transition_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_activity_at: datetime = Field(default_factory=utcnow)


class VetterReportORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    report_id: str
    problem_id: str
    target_id: str
    target_kind: str
    statement_status: str | None = None
    proof_status: str | None = None
    drift_assessment: dict[str, Any] | None = None
    recommended_action: str | None = None
    reason: str | None = None
    confidence: float | None = None
    feedback_for_solver: str | None = None
    detailed_findings: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class CounterexampleORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    counterexample_id: str
    problem_id: str
    lemma_id: str
    parent_decomposition_id: str | None = None
    source_agent: str
    source_worker_job_id: str | None = None
    source_attempt_number: int | None = None
    status: str = "pending_vet"
    statement_fingerprint: str
    counterexample_text: str
    summary: str | None = None
    confidence: float | None = None
    accepted_by_report_id: str | None = None
    rejected_by_report_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class LemmaProofAttemptORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    proof_attempt_id: str
    problem_id: str
    lemma_id: str
    attempt_number: int
    solver_worker_job_id: str | None = None
    solver_status: str | None = None
    solver_summary: str | None = None
    solver_artifact_prefix: str | None = None
    proof_nl: str | None = None
    solver_candidate_counterexample: str | None = None
    counterexample_record_id: str | None = None
    counterexample_vetter_worker_job_id: str | None = None
    counterexample_vetter_status: str | None = None
    vetter_worker_job_id: str | None = None
    vetter_report_id: str | None = None
    vetter_statement_status: str | None = None
    vetter_proof_status: str | None = None
    vetter_reason: str | None = None
    terminal_disposition: str | None = None
    artifact_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class LeanJobORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    problem_id: str
    target_id: str
    target_kind: str
    mode: str
    operation: str | None = None
    remote_operation_id: str | None = None
    status: str = "queued"
    attempt_index: int = 1
    progress_snapshot: dict[str, Any] | None = None
    issue_kind: str | None = None
    confidence: float | None = None
    fatality: str | None = None
    last_error: str | None = None
    lean_image_tag: str = ""
    request_artifact_id: str | None = None
    result_artifact_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class LeanResultORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    result_id: str
    job_id: str
    status: str
    error_class: str | None = None
    issue_kind: str | None = None
    confidence: float | None = None
    fatality: str | None = None
    error_scope: str | None = None
    error_message: str | None = None
    progress_snapshot: dict[str, Any] | None = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    decl_name: str | None = None
    lean_code_artifact_id: str | None = None
    compiler_log_artifact_id: str | None = None
    artifact_index: dict[str, Any] = Field(default_factory=dict)
    recommended_next_step: str | None = None
    routing_confidence: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class TrustedContextORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    problem_id: str
    proof_graph_id: str | None = None
    context_scope: str = "problem_external"
    decl_name: str
    lean_code: str
    source_lemma_id: str
    source_job_id: str
    created_at: datetime = Field(default_factory=utcnow)


class ProofGraphORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    proof_graph_id: str
    problem_id: str
    root_theorem_id: str
    root_decomposition_id: str
    graph_status: str = "building"
    verification_status: str = "legacy_not_dag_verified"
    dag_version: str = "v1"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProofGraphNodeORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    graph_node_id: str
    proof_graph_id: str
    node_kind: str
    owner_kind: str
    owner_id: str
    statement_nl: str = ""
    semantic_sketch_json: dict[str, Any] = Field(default_factory=dict)
    normalized_claim_hash: str | None = None
    node_status: str = "pending"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProofGraphEdgeORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    edge_id: str
    proof_graph_id: str
    from_node_id: str
    to_node_id: str
    edge_kind: str
    dependency_scope: str = "legacy_unknown"
    introduced_by_worker_job_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ProofDependencyCheckORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dependency_check_id: str
    proof_graph_id: str
    target_node_id: str
    artifact_kind: str
    artifact_id: str
    check_status: str = "legacy_unknown"
    violations_json: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class FailureReportORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    failure_report_id: str
    problem_id: str
    failure_reason: str
    terminal_lemma_id: str | None = None
    terminal_error_class: str | None = None
    terminal_error_message: str | None = None
    partial_tree: dict[str, Any] = Field(default_factory=dict)
    trusted_context_at_failure: list[str] = Field(default_factory=list)
    all_decomposition_attempts: list[dict[str, Any]] = Field(default_factory=list)
    routing_log_summary: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class WorkerJobORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    worker_job_id: str
    problem_id: str
    worker_kind: str
    status: str = "queued"
    target_id: str | None = None
    target_kind: str | None = None
    execution_id: str | None = None
    continuation_generation: int = 0
    attempt_number: int | None = None
    artifact_prefix: str | None = None
    handler_key: str | None = None
    request_source: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    llm_override_key: str | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    superseded_at: datetime | None = None
    superseded_by_execution_id: str | None = None
    supersede_reason: str | None = None
    controller_consumed_at: datetime | None = None
    controller_consumed_by_execution_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProblemExecutionORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    execution_id: str
    problem_id: str
    continuation_generation: int = 0
    status: str = "queued"
    desired_state: str = "running"
    current_stage: str | None = None
    blocking_kind: str = "none"
    blocking_ref_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    wake_requested_at: datetime | None = None
    last_error_payload: dict[str, Any] | None = None
    trigger_source: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_activity_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class RequestRecordORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_record_id: str
    problem_id: str
    execution_id: str | None = None
    worker_job_id: str | None = None
    source: str
    target_id: str | None = None
    status: str = "queued"
    response_id: str | None = None
    error_class: str | None = None
    summary: str | None = None
    llm_model: str | None = None
    llm_reasoning_effort: str | None = None
    llm_text_verbosity: str | None = None
    llm_timeout_seconds: int | None = None
    request_artifact_key: str | None = None
    response_artifact_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class LlmUsageRecordORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    usage_id: str
    problem_id: str | None = None
    lemma_id: str | None = None
    worker_job_id: str | None = None
    stage: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    raw_usage: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utcnow)


class RunCostRollupORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rollup_id: str
    problem_id: str
    rollup_date: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_estimated_cost_usd: float = 0.0
    updated_at: datetime = Field(default_factory=utcnow)


class EventORM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: int = 0
    problem_id: str
    target_node_id: str | None = None
    stage: str
    old_status: str | None = None
    new_status: str | None = None
    worker_job_id: str | None = None
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# Backward compatibility: Base is no longer used but some test files may import it.
Base = None
