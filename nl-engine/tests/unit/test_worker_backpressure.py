from __future__ import annotations

import threading
import time
from uuid import uuid4

from nl_engine.domain.contracts import Agent4Input, Agent4Output
from nl_engine.settings import get_settings
from nl_engine.workers import WorkerFacade, WorkerJob


class SlowAgent:
    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        time.sleep(0.25)
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


def test_worker_concurrency_limit(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_DEFAULT_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("WORKER_DEFAULT_MAX_PENDING", "2")
    get_settings.cache_clear()

    facade = WorkerFacade(agent_service_factory=SlowAgent)
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
    job1 = WorkerJob(job_id=f"job_conc_{uuid4().hex}", problem_id="p", worker_kind="lemma_solver", payload=payload.model_dump())
    job2 = WorkerJob(job_id=f"job_conc_{uuid4().hex}", problem_id="p", worker_kind="lemma_solver", payload=payload.model_dump())

    failures: list[Exception] = []

    def run_first():
        facade.run_lemma_solver(job1, payload, "problems/p/lemmas/lem_1/solver_attempt_1")

    t = threading.Thread(target=run_first)
    t.start()
    time.sleep(0.05)
    try:
        facade.run_lemma_solver(job2, payload, "problems/p/lemmas/lem_1/solver_attempt_1")
    except Exception as exc:
        failures.append(exc)
    t.join()

    assert failures, "expected second dispatch to be rejected by concurrency limit"
    get_settings.cache_clear()
