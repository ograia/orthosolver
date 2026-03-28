from __future__ import annotations

from types import SimpleNamespace

from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.config import AgentLlmConfig, ProblemConfig
from nl_engine.domain.contracts import Agent2Input
from nl_engine.domain.models import ProblemORM
from nl_engine.workers.contracts import WorkerJob


def test_root_generation_jobs_use_configured_first_root_retry_budget() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    captured_jobs = []
    theorem_state = SimpleNamespace(agent2_first_root_consumed=False)

    cfg = ProblemConfig()
    cfg.llm.agent2.max_attempts = 4
    cfg.llm.agent2_first_root = AgentLlmConfig(max_attempts=2)

    orch.execution_id = "exec_test"
    orch.artifacts = SimpleNamespace(save_json=lambda *args, **kwargs: None)
    orch.worker_jobs = SimpleNamespace(enqueue_if_absent=lambda job: captured_jobs.append(job))
    orch.theorems = SimpleNamespace(
        get=lambda theorem_id: theorem_state,
        save=lambda theorem: theorem,
    )
    orch.event_logger = SimpleNamespace(transition=lambda *args, **kwargs: None)
    orch._root_parallel_take_k = lambda _cfg: 1
    orch._harvest_completed_root_generation_attempts = lambda **kwargs: (False, 0)
    orch._current_root_track_count = lambda *args, **kwargs: 0
    orch._list_root_generation_attempts = lambda *args, **kwargs: []
    orch._previous_attempt_summaries = lambda *args, **kwargs: []
    orch._trusted_context_summaries = lambda *args, **kwargs: []
    orch._continuation_generation = lambda problem: 0
    orch._mark_root_generation_attempt_queued = lambda **kwargs: None
    orch._remaining_decomposition_slots = lambda *args, **kwargs: 1
    orch._root_generation_inflight_attempt_count = lambda *args, **kwargs: 1
    orch._mark_failed = lambda *args, **kwargs: None
    orch._build_decomposition_generation_payload = lambda **kwargs: Agent2Input(
        theorem_nl="For all n, n = n",
        root_semantic_sketch={},
        num_candidates=1,
    )
    orch._build_decomposition_generation_job = lambda **kwargs: WorkerJob(
        job_id="wrk_root_retry_budget",
        problem_id=kwargs["problem_id"],
        worker_kind="decomposition_generation",
        payload=kwargs["payload"].model_dump(),
    )

    problem = ProblemORM(problem_id="prob_root_retry_budget")
    root = SimpleNamespace(
        theorem_id="thm_root_retry_budget",
        statement_nl="For all n, n = n",
        statement_semantic_sketch={},
    )

    changed = orch._generate_root_decompositions(problem, root, cfg)

    assert changed is True
    assert len(captured_jobs) == 1
    assert captured_jobs[0].max_attempts == 2


def test_root_generation_consumes_first_root_override_once() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    captured_jobs = []
    theorem_state = SimpleNamespace(agent2_first_root_consumed=False)

    cfg = ProblemConfig()
    cfg.decomposition.parallel_root_decompositions_n = 2
    cfg.decomposition.parallel_root_take_k = 2
    cfg.llm.agent2_first_root = AgentLlmConfig(max_attempts=2)

    orch.execution_id = "exec_test"
    orch.artifacts = SimpleNamespace(save_json=lambda *args, **kwargs: None)
    orch.worker_jobs = SimpleNamespace(enqueue_if_absent=lambda job: captured_jobs.append(job))
    orch.theorems = SimpleNamespace(
        get=lambda theorem_id: theorem_state,
        save=lambda theorem: theorem,
    )
    orch.event_logger = SimpleNamespace(transition=lambda *args, **kwargs: None)
    orch._root_parallel_take_k = lambda _cfg: 2
    orch._harvest_completed_root_generation_attempts = lambda **kwargs: (False, 0)
    orch._current_root_track_count = lambda *args, **kwargs: 0
    orch._list_root_generation_attempts = lambda *args, **kwargs: []
    orch._previous_attempt_summaries = lambda *args, **kwargs: []
    orch._trusted_context_summaries = lambda *args, **kwargs: []
    orch._continuation_generation = lambda problem: 0
    orch._mark_root_generation_attempt_queued = lambda **kwargs: None
    orch._remaining_decomposition_slots = lambda *args, **kwargs: 2
    orch._root_generation_inflight_attempt_count = lambda *args, **kwargs: 0
    orch._mark_failed = lambda *args, **kwargs: None
    orch._build_decomposition_generation_payload = lambda **kwargs: Agent2Input(
        theorem_nl="For all n, n = n",
        root_semantic_sketch={},
        num_candidates=1,
    )
    orch._build_decomposition_generation_job = lambda **kwargs: WorkerJob(
        job_id=f"wrk_root_retry_budget_{len(captured_jobs) + 1}",
        problem_id=kwargs["problem_id"],
        worker_kind="decomposition_generation",
        payload=kwargs["payload"].model_dump(),
    )

    problem = ProblemORM(problem_id="prob_root_retry_budget")
    root = SimpleNamespace(
        theorem_id="thm_root_retry_budget",
        statement_nl="For all n, n = n",
        statement_semantic_sketch={},
    )

    changed = orch._generate_root_decompositions(problem, root, cfg)

    assert changed is True
    assert len(captured_jobs) == 2
    assert captured_jobs[0].llm_override_key == "agent2_first_root"
    assert captured_jobs[1].llm_override_key is None
