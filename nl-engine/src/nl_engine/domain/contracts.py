from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nl_engine.domain.config import ProblemConfig


def _normalize_findings_list(value: Any, *, string_key: str = "finding") -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(item)
            continue
        text = str(item or "").strip()
        if text:
            normalized.append({string_key: text})
    return normalized


# ---------------------------------------------------------------------------
# Public API contracts
# ---------------------------------------------------------------------------


class ApiEnvelope(BaseModel):
    request_id: str
    server_time: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProblemCreateRequest(BaseModel):
    title: str
    statement_nl: str
    statement_lean: str | None = None
    imports: list[str] = Field(default_factory=lambda: ["Mathlib"])
    lean_image_tag: str = "Orthosolver-lean-4.18.0-mathlib-v4.18.0"
    initial_trusted_context: list[dict[str, str]] = Field(default_factory=list)
    config: ProblemConfig = Field(default_factory=ProblemConfig)


class ProblemCreateResponse(ApiEnvelope):
    problem_id: str
    root_theorem_id: str
    status: Literal["created"]


class ProblemSummary(BaseModel):
    problem_id: str
    status: str
    verification_level: str
    nl_only_mode: bool
    root_theorem_id: str | None
    active_decomposition_id: str | None
    standby_decomposition_id: str | None
    execution_id: str | None = None
    execution_status: str | None = None
    current_stage: str | None = None
    blocking_kind: str | None = None
    blocking_ref_id: str | None = None


class ProblemGetResponse(ApiEnvelope):
    problem: ProblemSummary
    tree_summary: dict[str, Any]


class ProblemRunResponse(ApiEnvelope):
    status: str
    execution_id: str | None = None


class ProblemExecutionSummary(BaseModel):
    execution_id: str
    problem_id: str
    continuation_generation: int = 0
    status: str
    desired_state: str
    current_stage: str | None
    blocking_kind: str
    blocking_ref_id: str | None
    trigger_source: str | None
    wake_requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


class ProblemStartResponse(ApiEnvelope):
    problem_id: str
    status: str
    execution: ProblemExecutionSummary


class ProblemResumeResponse(ApiEnvelope):
    problem_id: str
    status: str
    previous_failure_report_id: str | None
    execution: ProblemExecutionSummary


class ProblemExecutionResponse(ApiEnvelope):
    problem_id: str
    execution: ProblemExecutionSummary | None


class ProblemCancelResponse(ApiEnvelope):
    problem_id: str
    execution: ProblemExecutionSummary | None


class ProblemPauseResponse(ApiEnvelope):
    problem_id: str
    status: str
    execution: ProblemExecutionSummary | None


class TreeNode(BaseModel):
    id: str
    kind: str
    status: str
    children: list["TreeNode"] = Field(default_factory=list)


class ProblemTreeResponse(ApiEnvelope):
    root: TreeNode


class FailureReportResponse(ApiEnvelope):
    failure_report: dict[str, Any]


class ProgressResponse(ApiEnvelope):
    problem_id: str
    status: str
    verification_level: str
    execution_id: str | None = None
    execution_status: str | None = None
    execution_desired_state: str | None = None
    active_decomposition_id: str | None
    active_decomposition_status: str | None
    standby_decomposition_id: str | None
    standby_decomposition_status: str | None
    lemma_counts: dict[str, Any]
    lean_job_counts: dict[str, Any]
    current_stage: str | None
    blocking_kind: str | None = None
    blocking_ref_id: str | None = None
    latest_reason: str | None
    latest_blocking_reason: str | None
    last_event_id: int | None
    resume_anchor_lemma_id: str | None = None
    resume_anchor_owner_decomposition_id: str | None = None


class EventItem(BaseModel):
    event_id: int
    problem_id: str
    target_node_id: str | None
    stage: str
    old_status: str | None
    new_status: str | None
    worker_job_id: str | None
    reason: str | None
    created_at: datetime


class EventsResponse(ApiEnvelope):
    events: list[EventItem]


class ProblemCostSummaryResponse(ApiEnvelope):
    problem_id: str
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_usd: float
    by_stage: dict[str, dict[str, float]]


# ---------------------------------------------------------------------------
# Debug API contracts
# ---------------------------------------------------------------------------


class DebugProblemItem(BaseModel):
    problem_id: str
    title: str
    status: str
    verification_level: str
    nl_only_mode: bool
    root_theorem_id: str | None
    active_decomposition_id: str | None
    standby_decomposition_id: str | None
    created_at: datetime
    updated_at: datetime


class DebugProblemsListResponse(ApiEnvelope):
    problems: list[DebugProblemItem]


class DebugProblemCreateTemplateResponse(ApiEnvelope):
    template: dict[str, Any]


class DebugProblemInputJsonResponse(ApiEnvelope):
    problem_id: str
    artifact_key: str
    input_json: dict[str, Any]


class DebugArtifactItem(BaseModel):
    artifact_key: str
    size_bytes: int
    modified_at: datetime
    content_type: str | None
    source: str


class DebugArtifactsResponse(ApiEnvelope):
    problem_id: str
    prefix: str
    include_worker_jobs: bool
    count: int
    artifacts: list[DebugArtifactItem]


class DebugArtifactContentResponse(ApiEnvelope):
    artifact_key: str
    format: Literal["json", "text"]
    content: Any
    size_bytes: int
    content_type: str | None


class DebugNodeGraphNode(BaseModel):
    id: str
    kind: str
    label: str
    status: str
    parent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DebugNodeGraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_id: str = Field(alias="from")
    to: str
    relation: str


class DebugNodeGraph(BaseModel):
    nodes: list[DebugNodeGraphNode]
    edges: list[DebugNodeGraphEdge]


class DebugRequestLogEntry(BaseModel):
    entry_id: str
    timestamp: datetime
    source: str
    target_id: str | None
    worker_job_id: str | None = None
    artifact_key: str
    summary: str
    completion_status: Literal["completed", "pending", "failed", "unknown"]
    completion_detail: str | None = None
    response_id: str | None = None
    provider_terminal_status: str | None = None
    llm_model: str | None = None
    llm_reasoning_effort: str | None = None
    llm_text_verbosity: str | None = None
    llm_timeout_seconds: int | None = None
    size_bytes: int
    llm_call_count: int = 0
    llm_input_tokens: int = 0
    llm_cached_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_total_tokens: int = 0
    llm_estimated_cost_usd: float = 0.0
    llm_models: list[str] = Field(default_factory=list)


class DebugRequestLogResponse(ApiEnvelope):
    problem_id: str
    source_filter: str | None
    count: int
    entries: list[DebugRequestLogEntry]


class DebugExecutionItem(BaseModel):
    execution_id: str
    problem_id: str
    continuation_generation: int = 0
    status: str
    desired_state: str
    current_stage: str | None
    blocking_kind: str
    blocking_ref_id: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    wake_requested_at: datetime | None
    trigger_source: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class DebugExecutionListResponse(ApiEnvelope):
    problem_id: str
    count: int
    executions: list[DebugExecutionItem]


class DebugLlmUsageStage(BaseModel):
    stage: str
    call_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    models: list[str] = Field(default_factory=list)


class DebugLlmUsageSummaryResponse(ApiEnvelope):
    problem_id: str
    pricing_usd_per_1m: dict[str, float]
    totals: dict[str, float | int]
    by_stage: list[DebugLlmUsageStage]


class DebugCleanupResponse(ApiEnvelope):
    scope: Literal["problem", "global"]
    problem_id: str | None
    deleted_rows: dict[str, int]
    deleted_artifacts: dict[str, int]


class DebugProblemSnapshotResponse(ApiEnvelope):
    problem_id: str
    problem: dict[str, Any]
    root_theorem: dict[str, Any] | None
    tree_summary: dict[str, Any]
    root_tree: TreeNode | None
    node_graph: DebugNodeGraph
    root_track_decomposition_ids: list[str]
    visible_lemma_ids: list[str]
    hidden_candidate_lemma_ids: list[str]
    lemma_owner_decomposition: dict[str, str | None]
    running_final_proof: dict[str, Any] | None = None
    final_proof: dict[str, Any] | None = None
    legacy_reconstructed_proof: dict[str, Any] | None = None
    nl_only_final_output: dict[str, Any] | None
    lemma_by_id: dict[str, dict[str, Any]]
    decomposition_by_id: dict[str, dict[str, Any]]
    logical_decomposition_by_id: dict[str, dict[str, Any]] = Field(default_factory=dict)
    lean_job_by_id: dict[str, dict[str, Any]]
    final_check_by_id: dict[str, dict[str, Any]] = Field(default_factory=dict)
    lemmas: list[dict[str, Any]]
    decompositions: list[dict[str, Any]]
    logical_decompositions: list[dict[str, Any]] = Field(default_factory=list)
    lean_jobs: list[dict[str, Any]]
    trusted_context: list[dict[str, Any]]
    proof_graphs: list[dict[str, Any]] = Field(default_factory=list)
    proof_graph_nodes: list[dict[str, Any]] = Field(default_factory=list)
    proof_graph_edges: list[dict[str, Any]] = Field(default_factory=list)
    proof_dependency_checks: list[dict[str, Any]] = Field(default_factory=list)
    events: list[EventItem]
    artifact_refs: list[str]


# ---------------------------------------------------------------------------
# Lean job contracts
# ---------------------------------------------------------------------------


class LeanJobSubmitRequest(BaseModel):
    job_id: str
    problem_id: str
    target_id: str
    target_kind: Literal["lemma", "theorem", "assembly"]
    mode: Literal["check_assembly", "formalize_lemma", "assemble_root", "check_statement_plausibility"]
    lean_image_tag: str
    callback_url: str | None = None
    payload: dict[str, Any]


class LeanJobSubmitResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    created_at: datetime
    estimated_duration_seconds: int


# ---------------------------------------------------------------------------
# Agent contracts from docs/nl_engine.tex (Sections Agent 1..5)
# ---------------------------------------------------------------------------


class SemanticSketch(BaseModel):
    variables: list[dict[str, Any]]
    quantifier_order: list[str]
    domain_restrictions: list[str]
    witness_dependencies: list[str]
    normalized_claim: str


class Agent1Input(BaseModel):
    statement_nl: str


class Agent1Output(BaseModel):
    status: Literal["completed"]
    statement_nl_received: str
    semantic_sketch: SemanticSketch
    implicit_assumptions_surfaced: list[str]
    ambiguities: list[str]


class Agent2Input(BaseModel):
    theorem_nl: str
    root_semantic_sketch: dict[str, Any]
    shared_context: list[dict[str, Any]] = Field(default_factory=list)
    num_candidates: int = 2
    previous_attempt_summaries: list[dict[str, Any]] = Field(default_factory=list)
    trusted_context_summaries: list[dict[str, Any]] = Field(default_factory=list)


class Agent2Lemma(BaseModel):
    local_id: str
    statement_nl: str
    semantic_sketch: SemanticSketch
    role_in_assembly: str
    lemma_relation_to_parent: Literal["strictly_weaker", "orthogonal", "bottleneck"] = "strictly_weaker"
    strictly_easier_reason: str | None = None
    bottleneck_reason: str | None = None
    formalization_cost_estimate: float
    self_check_true: bool
    self_check_notes: str


class Agent2AssemblyPlan(BaseModel):
    steps: list[dict[str, Any]]
    proof_skeleton_nl: str
    final_step_yields_exact_root: bool


class Agent2Candidate(BaseModel):
    candidate_index: int
    strategy_summary: str
    shared_context: list[dict[str, Any]]
    context_items: list[dict[str, Any]] = Field(default_factory=list)
    lemmas: list[Agent2Lemma]
    assembly_plan: Agent2AssemblyPlan
    formalization_cost_estimate_total: float
    drift_self_check: dict[str, Any]


class Agent2Output(BaseModel):
    status: Literal["completed"]
    candidates: list[Agent2Candidate]


class Agent3Input(BaseModel):
    theorem_nl: str
    root_semantic_sketch: dict[str, Any]
    decomposition: dict[str, Any]


class Agent3Output(BaseModel):
    status: Literal["completed"]
    decision: Literal["accepted", "minor_fix", "fatal"]
    summary: str
    lemma_findings: list[dict[str, Any]]
    assembly_check: dict[str, Any]
    coverage_check: dict[str, Any]
    drift_assessment: dict[str, Any]
    formalization_risk: Literal["low", "medium", "high"]
    fixes_required: list[str]
    fatal_reason: str | None
    dependency_risk_assessment: dict[str, Any] = Field(default_factory=dict)
    equivalence_findings: list[dict[str, Any]] = Field(default_factory=list)
    context_purity_findings: list[dict[str, Any]] = Field(default_factory=list)
    accepted_bottleneck: bool = False

    @field_validator("lemma_findings", mode="before")
    @classmethod
    def _normalize_lemma_findings(cls, value: Any) -> list[dict[str, Any]]:
        return _normalize_findings_list(value, string_key="finding")

    @field_validator("equivalence_findings", mode="before")
    @classmethod
    def _normalize_equivalence_findings(cls, value: Any) -> list[dict[str, Any]]:
        return _normalize_findings_list(value, string_key="finding")

    @field_validator("context_purity_findings", mode="before")
    @classmethod
    def _normalize_context_purity_findings(cls, value: Any) -> list[dict[str, Any]]:
        return _normalize_findings_list(value, string_key="finding")


class DependencyManifestItem(BaseModel):
    item_id: str
    item_kind: str
    label: str
    source: str
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DependencyCitation(BaseModel):
    citation_kind: str
    item_id: str
    label: str
    detail: str | None = None
    cited_text: str | None = None


class Agent4Input(BaseModel):
    lemma_id: str
    statement_nl: str
    semantic_sketch: dict[str, Any]
    root_theorem_nl: str = ""
    root_semantic_sketch: dict[str, Any]
    role_in_assembly: str
    shared_context: list[dict[str, Any]] = Field(default_factory=list)
    definition_context: list[dict[str, Any]] = Field(default_factory=list)
    trusted_context_summaries: list[dict[str, Any]] = Field(default_factory=list)
    allowed_dependency_manifest: list[DependencyManifestItem] = Field(default_factory=list)
    forbidden_claims: list[dict[str, Any]] = Field(default_factory=list)
    proof_attempt_node_id: str | None = None
    previous_feedback: str | None = None
    previous_proof_nl: str | None = None
    attempt_number: int


class Agent4Output(BaseModel):
    status: Literal["proved", "failed", "possibly_false"]
    lemma_id: str
    attempt_number: int
    proof_nl: str | None
    proof_summary: str | None
    self_report: dict[str, Any]
    stuck_point: str | None
    candidate_counterexample: str | None
    counterexample_flag: dict[str, Any] | None = None
    addressed_previous_feedback: str | None
    citations: list[DependencyCitation] = Field(default_factory=list)
    dependency_self_check: dict[str, Any] = Field(default_factory=dict)
    used_forbidden_claim: bool = False


class Agent5Input(BaseModel):
    lemma_id: str
    statement_nl: str
    semantic_sketch: dict[str, Any]
    root_semantic_sketch: dict[str, Any]
    proof_nl: str
    role_in_assembly: str
    lean_diagnostics: str | None
    attempt_number: int
    vetting_mode: Literal["proof", "counterexample"] = "proof"
    candidate_counterexample: str | None = None
    allowed_dependency_manifest: list[DependencyManifestItem] = Field(default_factory=list)
    citations: list[DependencyCitation] = Field(default_factory=list)
    ancestry_summary: list[dict[str, Any]] = Field(default_factory=list)


class Agent5Output(BaseModel):
    status: Literal["completed"]
    lemma_id: str
    statement_status: Literal["plausible", "suspect", "false"]
    proof_status: Literal["complete", "localized_gap", "major_gap", "wrong_strategy"]
    drift_assessment: dict[str, Any]
    recommended_action: Literal["send_to_lean", "retry_solver", "decompose_current_lemma", "flag_suspected_false"]
    confidence: float
    reason: str
    feedback_for_solver: str | None
    candidate_counterexample: str | None
    counterexample_status: Literal["accepted", "rejected", "undetermined"] | None = None
    detailed_findings: list[dict[str, Any]]
    dependency_assessment: dict[str, Any] = Field(default_factory=dict)
    undeclared_citation_findings: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_dependency_findings: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("detailed_findings", mode="before")
    @classmethod
    def _normalize_detailed_findings(cls, value: Any) -> list[dict[str, Any]]:
        return _normalize_findings_list(value, string_key="finding")

    @field_validator("undeclared_citation_findings", mode="before")
    @classmethod
    def _normalize_undeclared_citation_findings(cls, value: Any) -> list[dict[str, Any]]:
        return _normalize_findings_list(value, string_key="finding")

    @field_validator("forbidden_dependency_findings", mode="before")
    @classmethod
    def _normalize_forbidden_dependency_findings(cls, value: Any) -> list[dict[str, Any]]:
        return _normalize_findings_list(value, string_key="finding")


class Agent6Input(BaseModel):
    problem_id: str
    proof_bundle: dict[str, Any]
    proof_graph_summary: dict[str, Any] = Field(default_factory=dict)
    dependency_checks_summary: list[dict[str, Any]] = Field(default_factory=list)
    track_identity: dict[str, Any] = Field(default_factory=dict)


class Agent6Output(BaseModel):
    status: Literal["completed"]
    verdict: Literal[
        "approved",
        "problem_issue",
        "decomposition_issue",
        "assembly_issue",
        "lemma_issues",
        "localized_lemma_repair",
    ]
    confidence: float
    summary: str
    decomposition_findings: list[dict[str, Any]] = Field(default_factory=list)
    assembly_findings: list[dict[str, Any]] = Field(default_factory=list)
    lemma_findings: list[dict[str, Any]] = Field(default_factory=list)
    circularity_found: bool = False
    cross_track_contamination: bool = False
    unresolved_bottleneck_use: bool = False


TreeNode.model_rebuild()
