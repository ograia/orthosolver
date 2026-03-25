from __future__ import annotations

import time
from pathlib import Path

import pytest

from nl_engine.domain.contracts import Agent2Input, Agent2Output, Agent4Input, Agent4Output
from nl_engine.services.agents import AgentExecutionError
from nl_engine.workers import WorkerFacade, WorkerJob


class CountingAgent:
    calls = 0

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        CountingAgent.calls += 1
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl="proof",
            proof_summary="ok",
            self_report={
                "confidence": 1.0,
                "suspected_gaps": [],
                "used_external_facts": [],
                "every_step_justified": True,
                "proves_exactly_the_statement": True,
            },
            stuck_point=None,
            candidate_counterexample=None,
            addressed_previous_feedback=None,
        )


class FlakyTransientAgent:
    calls = 0

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            raise AgentExecutionError(
                agent_key="agent4",
                error_class="infrastructure_transient",
                message="temporary connection failure",
                artifact_prefix=artifact_prefix,
            )
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl="proof",
            proof_summary="ok",
            self_report={
                "confidence": 1.0,
                "suspected_gaps": [],
                "used_external_facts": [],
                "every_step_justified": True,
                "proves_exactly_the_statement": True,
            },
            stuck_point=None,
            candidate_counterexample=None,
            addressed_previous_feedback=None,
        )


class SlowDecompositionAgent:
    calls = 0

    def decompose(self, payload, artifact_prefix, **kwargs):
        self.__class__.calls += 1
        time.sleep(0.1)
        return Agent2Output(status="completed", candidates=[])


def test_worker_runner_is_idempotent_by_job_id(tmp_path: Path) -> None:
    from nl_engine.artifacts.store import ArtifactStore

    store = ArtifactStore()
    store.root = tmp_path
    facade = WorkerFacade(store=store, agent_service_factory=CountingAgent)
    CountingAgent.calls = 0

    payload = Agent4Input(
        lemma_id="lem_1",
        statement_nl="For all n, n = n",
        semantic_sketch={},
        root_theorem_nl="For all n, n = n",
        root_semantic_sketch={},
        role_in_assembly="direct",
        shared_context=[],
        trusted_context_summaries=[],
        previous_feedback=None,
        attempt_number=1,
    )
    job = WorkerJob(job_id="job-idempotent", problem_id="prob_1", worker_kind="lemma_solver", payload=payload.model_dump())

    first = facade.run_lemma_solver(job, payload, "problems/prob_1/lemmas/lem_1/solver_attempt_1")
    second = facade.run_lemma_solver(job, payload, "problems/prob_1/lemmas/lem_1/solver_attempt_1")

    assert first.status == "proved"
    assert second.status == "proved"
    assert CountingAgent.calls == 1


def test_worker_transient_failure_does_not_poison_job_id(tmp_path: Path) -> None:
    from nl_engine.artifacts.store import ArtifactStore

    store = ArtifactStore()
    store.root = tmp_path
    facade = WorkerFacade(store=store, agent_service_factory=FlakyTransientAgent)
    FlakyTransientAgent.calls = 0

    payload = Agent4Input(
        lemma_id="lem_1",
        statement_nl="For all n, n = n",
        semantic_sketch={},
        root_theorem_nl="For all n, n = n",
        root_semantic_sketch={},
        role_in_assembly="direct",
        shared_context=[],
        trusted_context_summaries=[],
        previous_feedback=None,
        attempt_number=1,
    )
    job = WorkerJob(job_id="job-transient", problem_id="prob_1", worker_kind="lemma_solver", payload=payload.model_dump())

    with pytest.raises(AgentExecutionError) as excinfo:
        facade.run_lemma_solver(job, payload, "problems/prob_1/lemmas/lem_1/solver_attempt_1")
    assert excinfo.value.error_class == "infrastructure_transient"

    result = facade.run_lemma_solver(job, payload, "problems/prob_1/lemmas/lem_1/solver_attempt_1")
    assert result.status == "proved"
    assert FlakyTransientAgent.calls == 2


def test_background_decomposition_submission_reuses_inflight_future(tmp_path: Path) -> None:
    from nl_engine.artifacts.store import ArtifactStore

    store = ArtifactStore()
    store.root = tmp_path
    facade = WorkerFacade(store=store, agent_service_factory=SlowDecompositionAgent)
    SlowDecompositionAgent.calls = 0

    payload = Agent2Input(theorem_nl="For all n, n = n", root_semantic_sketch={}, num_candidates=1)
    job = WorkerJob(
        job_id="job-root-decompose",
        problem_id="prob_1",
        worker_kind="decomposition_generation",
        payload=payload.model_dump(),
    )

    first = facade.submit_decomposition_generation(job, payload, "problems/prob_1/decomposer/attempt_1")
    second = facade.submit_decomposition_generation(job, payload, "problems/prob_1/decomposer/attempt_1")

    assert first is second
    result = first.result(timeout=2)
    assert result.status == "completed"
    assert SlowDecompositionAgent.calls == 1
