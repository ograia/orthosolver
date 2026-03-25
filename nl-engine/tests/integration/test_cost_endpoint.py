from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from tests.integration.test_api_standard_mode import MockLeanClientProxy


def test_problem_cost_summary_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_module, "LeanClient", MockLeanClientProxy)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Cost summary theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": False}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    for _ in range(120):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        if run.json()["status"] in {"succeeded", "failed"}:
            break

    cost = client.get(f"/v1/problems/{problem_id}/cost")
    assert cost.status_code == 200
    body = cost.json()
    assert body["problem_id"] == problem_id
    assert "total_input_tokens" in body
    assert "total_output_tokens" in body
    assert "total_estimated_cost_usd" in body
    assert "by_stage" in body
