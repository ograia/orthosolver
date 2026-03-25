from __future__ import annotations

from datetime import UTC, datetime
import threading
import time
from nl_engine.domain.contracts import Agent2Input, Agent2Output
from nl_engine.domain.models import ProblemORM, WorkerJobORM
from nl_engine.execution.runtime import StageWorkerRuntime
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import ProblemRepository, WorkerJobRepository


def _create_problem(problem_id: str, *, config: dict | None = None) -> None:
    store = get_file_store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id=problem_id,
            title="t",
            lean_image_tag="img",
            config=config or {},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )


def test_stage_worker_dispatch_injects_problem_llm_overrides(monkeypatch) -> None:
    _create_problem(
        "prob_overrides",
        config={
            "llm": {
                "agent2": {
                    "model": "gpt-5.4",
                    "thinking_level": "low",
                    "verbosity": "medium",
                    "timeout_seconds": 600,
                },
                "agent2_first_root": {
                    "model": "gpt-5.4-pro",
                    "thinking_level": "xhigh",
                    "verbosity": "medium",
                    "timeout_seconds": 3600,
                },
            }
        },
    )
    runtime = StageWorkerRuntime()
    captured: dict[str, object] = {}

    def _fake_run(job, payload, artifact_prefix, *, override_key=None):  # noqa: ANN001
        captured["llm_overrides"] = job.llm_overrides
        captured["override_key"] = override_key
        return Agent2Output(status="completed", candidates=[])

    monkeypatch.setattr(runtime.facade, "run_decomposition_generation", _fake_run)

    payload = Agent2Input(theorem_nl="For all n, n = n", root_semantic_sketch={}, num_candidates=1).model_dump()
    result = runtime._dispatch(
        worker_job_id="wrk_root",
        problem_id="prob_overrides",
        worker_kind="decomposition_generation",
        payload=payload,
        execution_id="exec_1",
        artifact_prefix="problems/prob_overrides/decomposer/attempt_1",
        llm_override_key="agent2_first_root",
    )

    assert result["status"] == "completed"
    llm_overrides = captured["llm_overrides"]
    assert isinstance(llm_overrides, dict)
    assert llm_overrides["agent2_first_root"]["model"] == "gpt-5.4-pro"
    assert llm_overrides["agent2_first_root"]["thinking_level"] == "xhigh"
    assert captured["override_key"] == "agent2_first_root"


def test_stage_worker_renews_worker_job_lease_during_long_dispatch(monkeypatch) -> None:
    _create_problem("prob_lease")
    store = get_file_store()
    repo = WorkerJobRepository(store)
    repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="wrk_long",
            problem_id="prob_lease",
            worker_kind="lemma_solver",
            status="queued",
            payload={},
        )
    )

    runtime = StageWorkerRuntime()
    runtime.settings.worker_lease_seconds = 1

    def _slow_dispatch(**kwargs):  # noqa: ANN003
        time.sleep(1.2)
        return {}

    monkeypatch.setattr(runtime, "_dispatch", _slow_dispatch)

    thread = threading.Thread(
        target=runtime.process_next,
        args=("worker-a",),
        kwargs={"problem_id": "prob_lease"},
        daemon=True,
    )
    thread.start()
    time.sleep(1.05)

    reclaimed = repo.claim_next("lemma_solver", "worker-b", datetime.now(UTC), lease_seconds=1, problem_id="prob_lease")
    thread.join(timeout=5)

    assert reclaimed is None
    row = repo.get("wrk_long")
    assert row is not None
    assert row.status == "completed"
