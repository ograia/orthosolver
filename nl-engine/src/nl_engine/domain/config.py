from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
TextVerbosity = Literal["low", "medium", "high"]
SupportedModel = Literal["gpt-5.4", "gpt-5.4-pro", "gpt-5-mini", "gpt-5.4-mini", "gpt-5.4-nano"]


class LeanModeConfig(BaseModel):
    """Feature flags for Lean API v2 orchestration flow."""

    enabled: bool = False
    use_v2_endpoints: bool = False
    use_v2_prepare_track: bool = False
    fallback_to_v1_on_error: bool = True
    max_track_attempts: int = Field(default=3, ge=1)
    auto_split_sublemmas: bool = False
    stream_progress_payloads: bool = False
    strict_proof_issue_fail_fast: bool = False
    proof_issue_confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class ModeConfig(BaseModel):
    nl_only_mode: bool = False
    lean_mode: bool | None = None
    lean: LeanModeConfig = Field(default_factory=LeanModeConfig)

    @model_validator(mode="before")
    @classmethod
    def _accept_lean_mode_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "lean_mode" in payload and "nl_only_mode" not in payload:
            raw_lean_mode = payload.get("lean_mode")
            if isinstance(raw_lean_mode, bool):
                payload["nl_only_mode"] = not raw_lean_mode
        return payload

    @model_validator(mode="after")
    def _sync_mode_flags(self) -> "ModeConfig":
        if self.lean_mode is None:
            self.lean_mode = not self.nl_only_mode
            return self
        if self.lean_mode == self.nl_only_mode:
            raise ValueError("lean_mode and nl_only_mode are contradictory")
        return self


class DecompositionConfig(BaseModel):
    parallel_root_decompositions_n: int = Field(default=1, ge=1)
    parallel_root_take_k: int = Field(default=1, ge=1)
    parallel_root_join_timeout_seconds: int = Field(default=30, ge=1)
    root_solutions_required_for_termination: int = Field(default=1, ge=1)
    max_consecutive_fatal_rejections_per_node: int = Field(default=10, ge=1)
    max_decompositions_per_failed_lemma: int = Field(default=10, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_no_accept_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "max_decompositions_per_failed_lemma" not in payload and "max_no_accept_rounds_per_lemma" in payload:
            payload["max_decompositions_per_failed_lemma"] = payload.get("max_no_accept_rounds_per_lemma")
        return payload

    @model_validator(mode="after")
    def _validate_take_k(self) -> "DecompositionConfig":
        if self.parallel_root_take_k > self.parallel_root_decompositions_n:
            raise ValueError("parallel_root_take_k must be <= parallel_root_decompositions_n")
        if self.root_solutions_required_for_termination > self.parallel_root_take_k:
            raise ValueError("root_solutions_required_for_termination must be <= parallel_root_take_k")
        return self

    @property
    def max_active_decompositions(self) -> int:
        return 1

    @property
    def max_candidates_generated(self) -> int:
        # Internal default: one Agent2 call produces up to two candidates.
        return 2

    @property
    def assembly_check_repair_rounds(self) -> int:
        return 2

    @property
    def assembly_check_timeout_seconds(self) -> int:
        return 240

    @property
    def assembly_check_max_tool_calls(self) -> int:
        # Keep Lean payload valid while making tool-call limits effectively non-user-facing.
        return 1_000_000


class LemmaSolvingConfig(BaseModel):
    max_consecutive_fatal_rejections_per_lemma: int = Field(default=3, ge=1)
    max_minor_rejections_per_lemma: int = Field(default=10, ge=1)
    max_total_lemma_nodes: int = 5000
    max_infrastructure_failures: int = 5
    max_solver_attempts_per_lemma_total: int | None = Field(default=None, ge=1)
    max_consecutive_infrastructure_failures_per_lemma: int | None = Field(default=None, ge=1)
    max_solver_series_wall_clock_seconds_per_lemma: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_solver_retry_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "max_consecutive_fatal_rejections_per_lemma" not in payload and "max_solver_retries_per_lemma" in payload:
            payload["max_consecutive_fatal_rejections_per_lemma"] = payload.get("max_solver_retries_per_lemma")
        return payload

    @property
    def max_parallel_lemmas(self) -> int:
        return 16

    @property
    def max_recursive_decomposition_depth(self) -> int:
        return 5

    @property
    def solver_ancestry_depth_cap(self) -> int:
        return 2


class LeanEngineConfig(BaseModel):
    model: str | None = None
    max_repair_rounds: int = 5
    max_lean_jobs_per_lemma: int = 3
    repair_context_token_budget: int = 32000
    assemble_root_repair_rounds: int = 3
    assembly_check_timeout_seconds: int = 240
    lean_job_timeout_seconds: int = 300
    plausibility_check_timeout_seconds: int = 45
    assemble_root_timeout_seconds: int = 300

    @property
    def max_tool_calls_per_job(self) -> int:
        # Keep Lean payload valid while making tool-call limits effectively non-user-facing.
        return 1_000_000


class RoutingConfig(BaseModel):
    invalidation_confidence_threshold: float = 0.75
    repairable_lean_only_classes: list[str] = ["syntax", "type_mismatch", "missing_import"]
    repairable_nl_loop_classes: list[str] = ["tactic_failure", "missing_library_fact"]
    max_identical_fatal_class_repeats: int = 2


class DriftConfig(BaseModel):
    major_drift_blocks_progress: bool = True
    drift_check_on_every_lemma: bool = True
    minor_drift_adds_warning_only: bool = True


class FinalCheckConfig(BaseModel):
    fail_problem_on_fatal: bool = False


class BudgetConfig(BaseModel):
    max_estimated_cost_usd_per_problem: float | None = Field(default=None, ge=0)
    max_estimated_cost_usd_per_lemma: float | None = Field(default=None, ge=0)


class AgentLlmConfig(BaseModel):
    model: SupportedModel | None = None
    thinking_level: ReasoningEffort | None = None
    verbosity: TextVerbosity | None = None
    timeout_seconds: int = Field(default=600, ge=0)
    max_attempts: int = Field(default=2, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _accept_reasoning_effort_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "thinking_level" not in payload and "reasoning_effort" in payload:
            payload["thinking_level"] = payload.get("reasoning_effort")
        if "verbosity" not in payload:
            if "text_verbosity" in payload:
                payload["verbosity"] = payload.get("text_verbosity")
            elif isinstance(payload.get("text"), dict):
                payload["verbosity"] = payload["text"].get("verbosity")
        return payload

    @model_validator(mode="after")
    def _enforce_model_reasoning_compatibility(self) -> "AgentLlmConfig":
        if self.model in ("gpt-5-mini", "gpt-5.4-mini", "gpt-5.4-nano") and self.thinking_level == "xhigh":
            self.thinking_level = "high"
        return self


class LlmConfig(BaseModel):
    agent1: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    agent2: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    agent3: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    agent4: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    agent5: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    agent6: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    # First-attempt model overrides. When set, used for the first attempt
    # only; subsequent retries fall back to the standard agent2/agent4 config.
    agent2_first_root: AgentLlmConfig | None = None
    agent2_first_lemma: AgentLlmConfig | None = None
    agent4_first: AgentLlmConfig | None = None


class ProblemConfig(BaseModel):
    mode: ModeConfig = Field(default_factory=ModeConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    decomposition: DecompositionConfig = Field(default_factory=DecompositionConfig)
    lemma_solving: LemmaSolvingConfig = Field(default_factory=LemmaSolvingConfig)
    lean_engine: LeanEngineConfig = Field(default_factory=LeanEngineConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)
    final_check: FinalCheckConfig = Field(default_factory=FinalCheckConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
