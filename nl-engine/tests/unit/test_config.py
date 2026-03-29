from nl_engine.domain.config import ProblemConfig
import pytest


def test_config_defaults_and_caps_present() -> None:
    cfg = ProblemConfig()
    assert cfg.mode.nl_only_mode is False
    assert cfg.mode.lean_mode is True
    assert cfg.budget.max_estimated_cost_usd_per_problem is None
    assert cfg.budget.max_estimated_cost_usd_per_lemma is None
    assert cfg.decomposition.parallel_root_decompositions_n >= 1
    assert cfg.decomposition.parallel_root_take_k >= 1
    assert cfg.decomposition.parallel_root_join_timeout_seconds >= 1
    assert cfg.decomposition.root_solutions_required_for_termination >= 1
    assert cfg.decomposition.lemma_decomposition_candidates_n == 1
    assert cfg.decomposition.max_consecutive_fatal_rejections_per_node >= 1
    assert cfg.decomposition.max_decompositions_per_failed_lemma >= 1
    assert cfg.decomposition.max_false_frontier_hops_per_branch >= 1
    assert cfg.lemma_solving.max_recursive_decomposition_depth > 0
    assert cfg.lemma_solving.max_consecutive_fatal_rejections_per_lemma > 0
    assert cfg.lemma_solving.max_minor_rejections_per_lemma > 0
    assert cfg.lemma_solving.max_solver_attempts_per_lemma_total is None
    assert cfg.lemma_solving.max_consecutive_infrastructure_failures_per_lemma is None
    assert cfg.lemma_solving.max_solver_series_wall_clock_seconds_per_lemma is None
    assert cfg.lean_engine.max_workers > 0
    assert cfg.lean_engine.no_lean4_refs is False
    assert cfg.lean_engine.claude_activity_timeout_seconds == 0
    assert cfg.lean_engine.claude_init_timeout_seconds == 0
    assert cfg.routing.max_identical_fatal_class_repeats >= 1
    assert cfg.final_check.fail_problem_on_fatal is False
    assert cfg.llm.agent1.model is None
    assert cfg.llm.agent1.thinking_level is None
    assert cfg.llm.agent1.verbosity is None
    assert cfg.llm.agent1.timeout_seconds == 600
    assert cfg.llm.agent1.max_attempts == 2
    assert cfg.llm.agent2.timeout_seconds == 600
    assert cfg.llm.agent2.coding_mode == "code_interpreter"
    assert cfg.llm.agent3.timeout_seconds == 600
    assert cfg.llm.agent3.coding_mode == "code_interpreter"
    assert cfg.llm.agent4.timeout_seconds == 600
    assert cfg.llm.agent4.coding_mode == "code_interpreter"
    assert cfg.llm.agent5.timeout_seconds == 600
    assert cfg.llm.agent5.coding_mode == "code_interpreter"
    assert cfg.llm.agent6.coding_mode == "code_interpreter"


def test_user_facing_config_excludes_global_ops_and_hidden_knobs() -> None:
    payload = ProblemConfig().model_dump(by_alias=True)
    assert "global" not in payload
    assert "ops" not in payload
    assert "max_parallel_lemmas" not in payload["lemma_solving"]
    assert "max_recursive_decomposition_depth" not in payload["lemma_solving"]
    assert "solver_ancestry_depth_cap" not in payload["lemma_solving"]
    assert "assembly_check_max_tool_calls" not in payload["decomposition"]
    assert "max_tool_calls_per_job" not in payload["lean_engine"]
    assert payload["lean_engine"]["model"] is None
    assert payload["lean_engine"]["no_lean4_refs"] is False
    assert payload["lean_engine"]["lean_job_timeout_seconds"] == 300
    assert payload["lean_engine"]["assemble_root_timeout_seconds"] == 300
    assert payload["lean_engine"]["claude_activity_timeout_seconds"] == 0
    assert payload["lean_engine"]["claude_init_timeout_seconds"] == 0
    assert "max_parallel_lean_jobs_per_problem" not in payload["lean_engine"]
    assert "max_lean_jobs_per_lemma" not in payload["lean_engine"]
    assert "assembly_check_timeout_seconds" not in payload["lean_engine"]
    assert "plausibility_check_timeout_seconds" not in payload["lean_engine"]
    assert "claude_stall_timeout_seconds" not in payload["lean_engine"]
    assert "claude_tool_wait_timeout_seconds" not in payload["lean_engine"]


def test_lean_engine_model_and_timeouts_round_trip() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "lean_engine": {
                "model": "claude-sonnet-4-6",
                "no_lean4_refs": True,
                "lean_job_timeout_seconds": 420,
                "assemble_root_timeout_seconds": 600,
                "claude_activity_timeout_seconds": 5400,
                "claude_init_timeout_seconds": 0,
            }
        }
    )
    assert cfg.lean_engine.model == "claude-sonnet-4-6"
    assert cfg.lean_engine.no_lean4_refs is True
    assert cfg.lean_engine.lean_job_timeout_seconds == 420
    assert cfg.lean_engine.assembly_check_timeout_seconds == 420
    assert cfg.lean_engine.plausibility_check_timeout_seconds == 420
    assert cfg.lean_engine.assemble_root_timeout_seconds == 600
    assert cfg.lean_engine.claude_activity_timeout_seconds == 5400
    assert cfg.lean_engine.claude_stall_timeout_seconds == 5400
    assert cfg.lean_engine.claude_tool_wait_timeout_seconds == 5400
    assert cfg.lean_engine.claude_init_timeout_seconds == 0


def test_lean_engine_legacy_aliases_collapse_to_simplified_inputs() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "lean_engine": {
                "max_workers": None,
                "max_parallel_lean_jobs_per_problem": 7,
                "assembly_check_timeout_seconds": 180,
                "plausibility_check_timeout_seconds": 90,
                "claude_stall_timeout_seconds": 3600,
                "claude_tool_wait_timeout_seconds": 5400,
                "claude_init_timeout_seconds": None,
            }
        }
    )
    assert cfg.lean_engine.max_workers == 7
    assert cfg.lean_engine.lean_job_timeout_seconds == 180
    assert cfg.lean_engine.claude_activity_timeout_seconds == 5400
    assert cfg.lean_engine.claude_init_timeout_seconds == 0


def test_mode_accepts_lean_mode_alias() -> None:
    cfg = ProblemConfig.model_validate({"mode": {"lean_mode": False}})
    assert cfg.mode.lean_mode is False
    assert cfg.mode.nl_only_mode is True


def test_mode_rejects_contradictory_flags() -> None:
    with pytest.raises(Exception):
        ProblemConfig.model_validate({"mode": {"lean_mode": True, "nl_only_mode": True}})


def test_llm_reasoning_effort_input_alias_maps_to_thinking_level() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "llm": {
                "agent2": {
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "high",
                }
            }
        }
    )
    assert cfg.llm.agent2.model == "gpt-5.4-mini"
    assert cfg.llm.agent2.thinking_level == "high"


def test_llm_model_alias_and_verbosity_alias() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "llm": {
                "agent1": {
                    "model": "gpt-5.4-mini",
                    "thinking_level": "xhigh",
                    "text_verbosity": "low",
                }
            }
        }
    )
    assert cfg.llm.agent1.model == "gpt-5.4-mini"
    assert cfg.llm.agent1.thinking_level == "high"
    assert cfg.llm.agent1.verbosity == "low"


def test_llm_coding_alias_maps_to_coding_mode() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "llm": {
                "agent2": {
                    "tool_mode": "shell",
                }
            }
        }
    )
    assert cfg.llm.agent2.coding_mode == "shell"


def test_llm_timeout_validation() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "llm": {
                "agent1": {"timeout_seconds": 0},
                "agent2": {"timeout_seconds": 99999},
            }
        }
    )
    assert cfg.llm.agent1.timeout_seconds == 0
    assert cfg.llm.agent2.timeout_seconds == 99999


def test_llm_max_attempts_validation() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "llm": {
                "agent1": {"max_attempts": 4},
            }
        }
    )
    assert cfg.llm.agent1.max_attempts == 4


def test_root_solution_threshold_must_not_exceed_take_k() -> None:
    with pytest.raises(Exception):
        ProblemConfig.model_validate(
            {
                "decomposition": {
                    "parallel_root_decompositions_n": 3,
                    "parallel_root_take_k": 2,
                    "root_solutions_required_for_termination": 3,
                }
            }
        )


def test_legacy_solver_retry_alias_maps_to_fatal_counter() -> None:
    cfg = ProblemConfig.model_validate({"lemma_solving": {"max_solver_retries_per_lemma": 7}})
    assert cfg.lemma_solving.max_consecutive_fatal_rejections_per_lemma == 7


def test_legacy_no_accept_alias_maps_to_lemma_decomposition_slots() -> None:
    cfg = ProblemConfig.model_validate({"decomposition": {"max_no_accept_rounds_per_lemma": 4}})
    assert cfg.decomposition.max_decompositions_per_failed_lemma == 4


def test_lemma_decomposition_candidate_count_validation_and_round_trip() -> None:
    cfg = ProblemConfig.model_validate({"decomposition": {"lemma_decomposition_candidates_n": 3}})
    assert cfg.decomposition.lemma_decomposition_candidates_n == 3
    assert cfg.model_dump()["decomposition"]["lemma_decomposition_candidates_n"] == 3

    with pytest.raises(Exception):
        ProblemConfig.model_validate({"decomposition": {"lemma_decomposition_candidates_n": 0}})


def test_final_check_policy_config_round_trips() -> None:
    cfg = ProblemConfig.model_validate({"final_check": {"fail_problem_on_fatal": True}})
    assert cfg.final_check.fail_problem_on_fatal is True


def test_budget_and_solver_guardrails_round_trip() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "budget": {
                "max_estimated_cost_usd_per_problem": 12.5,
                "max_estimated_cost_usd_per_lemma": 1.75,
            },
            "lemma_solving": {
                "max_solver_attempts_per_lemma_total": 6,
                "max_consecutive_infrastructure_failures_per_lemma": 2,
                "max_solver_series_wall_clock_seconds_per_lemma": 1800,
            },
        }
    )
    assert cfg.budget.max_estimated_cost_usd_per_problem == 12.5
    assert cfg.budget.max_estimated_cost_usd_per_lemma == 1.75
    assert cfg.lemma_solving.max_solver_attempts_per_lemma_total == 6
    assert cfg.lemma_solving.max_consecutive_infrastructure_failures_per_lemma == 2
    assert cfg.lemma_solving.max_solver_series_wall_clock_seconds_per_lemma == 1800


def test_zero_disables_optional_budget_and_solver_caps() -> None:
    cfg = ProblemConfig.model_validate(
        {
            "budget": {
                "max_estimated_cost_usd_per_problem": 0,
                "max_estimated_cost_usd_per_lemma": 0,
            },
            "lemma_solving": {
                "max_solver_attempts_per_lemma_total": 0,
                "max_solver_series_wall_clock_seconds_per_lemma": 0,
            },
        }
    )
    assert cfg.budget.max_estimated_cost_usd_per_problem is None
    assert cfg.budget.max_estimated_cost_usd_per_lemma is None
    assert cfg.lemma_solving.max_solver_attempts_per_lemma_total is None
    assert cfg.lemma_solving.max_solver_series_wall_clock_seconds_per_lemma is None
