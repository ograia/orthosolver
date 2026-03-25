from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.domain.models import WorkerJobORM
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import ProblemExecutionRepository, ProblemRepository, WorkerJobRepository


def test_pause_endpoint_sets_problem_paused_and_continue_unpauses() -> None:
    client = TestClient(app)
    created = client.post(
        "/v1/problems",
        json={"title": "pause flow", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert created.status_code == 200
    problem_id = created.json()["problem_id"]

    started = client.post(f"/v1/problems/{problem_id}/start")
    assert started.status_code == 200
    first_exec = started.json()["execution"]["execution_id"]
    first_generation = started.json()["execution"]["continuation_generation"]

    paused = client.post(f"/v1/problems/{problem_id}/pause")
    assert paused.status_code == 200
    pause_payload = paused.json()
    assert pause_payload["problem_id"] == problem_id
    assert pause_payload["status"] == "paused"
    assert pause_payload["execution"]["execution_id"] == first_exec

    blocked_start = client.post(f"/v1/problems/{problem_id}/start")
    assert blocked_start.status_code == 409
    assert blocked_start.json()["error"]["code"] == "problem_paused_use_continue"

    resumed = client.post(
        f"/v1/problems/{problem_id}/start",
        headers={"X-Debug-Run-Trigger": "continue_button"},
    )
    assert resumed.status_code == 200
    resume_payload = resumed.json()
    assert resume_payload["problem_id"] == problem_id
    assert resume_payload["status"] == "running"
    assert resume_payload["execution"]["execution_id"] != first_exec
    assert resume_payload["execution"]["continuation_generation"] >= first_generation + 1


def test_continue_supersedes_old_generation_execution_and_worker_jobs() -> None:
    client = TestClient(app)
    created = client.post(
        "/v1/problems",
        json={"title": "fresh continue", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert created.status_code == 200
    problem_id = created.json()["problem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    execution_repo = ProblemExecutionRepository(store)
    worker_repo = WorkerJobRepository(store)

    # Seed a running generation with a stale queued/running worker.
    problem = problem_repo.get(problem_id)
    assert problem is not None
    problem.status = "running"
    problem.continuation_generation = 1
    problem_repo.save(problem)

    seeded = client.post(f"/v1/problems/{problem_id}/start")
    assert seeded.status_code == 200
    old_exec_id = seeded.json()["execution"]["execution_id"]
    old_exec = execution_repo.get(old_exec_id)
    assert old_exec is not None
    old_exec.continuation_generation = 1
    execution_repo.save(old_exec)

    worker_repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="wrk_stale_solver",
            problem_id=problem_id,
            worker_kind="lemma_solver",
            status="running",
            target_id="lem_stale",
            target_kind="lemma",
            execution_id=old_exec_id,
            continuation_generation=1,
            attempt_number=1,
            request_source="lemma_solver",
            payload={"lemma_id": "lem_stale", "attempt_number": 1},
        )
    )

    continued = client.post(
        f"/v1/problems/{problem_id}/start",
        headers={"X-Debug-Run-Trigger": "continue_button"},
    )
    assert continued.status_code == 200
    payload = continued.json()
    new_exec_id = payload["execution"]["execution_id"]
    new_generation = payload["execution"]["continuation_generation"]
    assert new_exec_id != old_exec_id
    assert new_generation >= 2

    old_row = execution_repo.get(old_exec_id)
    assert old_row is not None
    assert old_row.status == "cancelled"

    stale_worker = worker_repo.get("wrk_stale_solver")
    assert stale_worker is not None
    assert stale_worker.status == "superseded"
    assert stale_worker.superseded_at is not None
    assert stale_worker.superseded_by_execution_id == new_exec_id


def test_pause_request_record_is_visible_in_debug_request_log() -> None:
    client = TestClient(app)
    created = client.post(
        "/v1/problems",
        json={"title": "pause request log", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert created.status_code == 200
    problem_id = created.json()["problem_id"]

    started = client.post(f"/v1/problems/{problem_id}/start")
    assert started.status_code == 200

    paused = client.post(f"/v1/problems/{problem_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    reqlog = client.get(f"/v1/debug/problems/{problem_id}/request-log?source=api_pause&limit=50")
    assert reqlog.status_code == 200
    entries = reqlog.json()["entries"]
    assert entries
    assert any(entry["source"] == "api_pause" and entry["completion_status"] == "completed" for entry in entries)
