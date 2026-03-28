from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta

from nl_engine.domain.models import ProblemExecutionORM, ProblemORM, WorkerJobORM
from nl_engine.execution.runtime import ExecutionDriver, WorkerSupervisor
from nl_engine.execution import runtime as runtime_module
from nl_engine.api.run_state import reset_all_run_state
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import ProblemExecutionRepository, ProblemRepository, WorkerJobRepository


def _store() -> FileStore:
    td = tempfile.mkdtemp()
    return FileStore(td)


def test_execution_driver_reconciles_expired_last_attempt_worker(monkeypatch) -> None:
    store = _store()
    problem_repo = ProblemRepository(store)
    execution_repo = ProblemExecutionRepository(store)
    worker_repo = WorkerJobRepository(store)

    problem_repo.create(
        ProblemORM(
            problem_id="prob_exec",
            title="t",
            lean_image_tag="img",
            config={},
            status="running",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )
    execution_repo.create(
        ProblemExecutionORM(
            execution_id="exec_1",
            problem_id="prob_exec",
            status="waiting",
            desired_state="running",
            current_stage="lemma.solver_submitted",
            blocking_kind="worker_job",
            blocking_ref_id="job_stuck",
            wake_requested_at=datetime.now(UTC) - timedelta(seconds=1),
            trigger_source="test",
        )
    )
    worker_repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_stuck",
            problem_id="prob_exec",
            worker_kind="lemma_solver",
            status="running",
            execution_id="exec_1",
            request_source="lemma_solver",
            target_id="lem_1",
            target_kind="lemma",
            payload={"lemma_id": "lem_1", "attempt_number": 1},
            attempt_number=1,
            attempt_count=5,
            max_attempts=5,
            lease_owner="worker-a",
            lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            error_payload={"error_class": "infrastructure_transient", "message": "timed out"},
        )
    )

    class _FakeLeanJobs:
        @staticmethod
        def list_non_terminal(problem_id: str) -> list[object]:
            return []

    class _FakeOrchestrator:
        def __init__(self, store_arg, execution_id=None) -> None:
            self.lean_jobs = _FakeLeanJobs()

        def run_once(self, problem_id: str):
            return ProblemRepository(store).get(problem_id)

    monkeypatch.setattr(runtime_module, "get_file_store", lambda: store)
    monkeypatch.setattr(runtime_module, "Orchestrator", _FakeOrchestrator)

    driver = ExecutionDriver()
    advanced = driver.advance_until_blocked("exec_1")

    assert advanced is True
    worker_row = worker_repo.get("job_stuck")
    assert worker_row is not None
    assert worker_row.status == "failed"

    execution_row = execution_repo.get("exec_1")
    assert execution_row is not None
    assert execution_row.status == "waiting"
    assert execution_row.blocking_kind == "none"
    assert execution_row.blocking_ref_id is None


def test_reconcile_external_progress_wakes_and_pumps_execution(monkeypatch) -> None:
    reset_all_run_state()
    store = _store()
    problem_repo = ProblemRepository(store)
    execution_repo = ProblemExecutionRepository(store)
    worker_repo = WorkerJobRepository(store)

    problem_repo.create(
        ProblemORM(
            problem_id="prob_reconcile",
            title="t",
            lean_image_tag="img",
            config={},
            status="running",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )
    execution_repo.create(
        ProblemExecutionORM(
            execution_id="exec_reconcile",
            problem_id="prob_reconcile",
            status="waiting",
            desired_state="running",
            current_stage="waiting",
            blocking_kind="worker_job",
            blocking_ref_id="job_done",
            trigger_source="test",
        )
    )
    worker_repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_done",
            problem_id="prob_reconcile",
            worker_kind="lemma_solver",
            status="completed",
            execution_id="exec_reconcile",
            request_source="lemma_solver",
            target_id="lem_1",
            target_kind="lemma",
            payload={"lemma_id": "lem_1", "attempt_number": 1},
            attempt_number=1,
            result_payload={"status": "completed"},
        )
    )

    class _FakeLeanClient:
        @staticmethod
        def get_job(job_id: str) -> dict[str, str]:
            return {"status": "running"}

    class _FakeSessionManager:
        def __init__(self, _store) -> None:
            pass

        def client_for_problem(self, _problem, create_if_missing: bool = True):
            return _FakeLeanClient()

    calls: list[str] = []
    monkeypatch.setattr(runtime_module, "get_file_store", lambda: store)
    monkeypatch.setattr(runtime_module, "LeanSessionManager", _FakeSessionManager)

    supervisor = WorkerSupervisor()
    supervisor.execution_driver.advance_until_blocked = lambda execution_id: calls.append(execution_id) or True

    assert supervisor._reconcile_external_progress() is True
    assert calls == ["exec_reconcile"]

    execution_row = execution_repo.get("exec_reconcile")
    assert execution_row is not None
    assert execution_row.current_stage == "execution.external_progress_reconciled"
