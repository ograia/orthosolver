from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.settings import get_settings
from tests.integration.test_api_standard_mode import MockLeanClientProxy


pytestmark = pytest.mark.regression


def _assert_in_order(stages: list[str], expected: list[str]) -> None:
    idx = 0
    for stage in stages:
        if stage == expected[idx]:
            idx += 1
            if idx == len(expected):
                return
    raise AssertionError(f"expected subsequence not found: {expected}")


def test_golden_timeline_standard_mode(monkeypatch) -> None:
    monkeypatch.setenv("MOCK_LEAN_DEFAULT_DELAY_SECONDS", "0")
    monkeypatch.setattr(orchestrator_module, "LeanClient", MockLeanClientProxy)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Regression standard theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": False}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(120):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break
    assert terminal == "succeeded"

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    rows = events.json()["events"]
    stages = [row["stage"] for row in rows]

    _assert_in_order(
        stages,
        [
            "problem.created",
            "problem.start",
            "decomposition.generated",
            "decomposition.selected",
            "lemma.solver_attempt",
            "lemma.vetter_route",
            "lean.job_submitted",
            "problem.succeeded",
        ],
    )

    worker_stage_rows = [row for row in rows if row["stage"] in {"lemma.solver_attempt", "lemma.vetter_route"}]
    assert worker_stage_rows
    assert all(row["worker_job_id"] for row in worker_stage_rows)

    lean_jobs = [row["worker_job_id"] for row in rows if row["stage"] == "lean.job_submitted" and row["worker_job_id"]]
    assert lean_jobs
    artifact_root = Path(get_settings().artifact_store_dir)
    for job_id in lean_jobs:
        request_path = artifact_root / "problems" / problem_id / "lean_jobs" / job_id / "request.json"
        assert request_path.exists()
