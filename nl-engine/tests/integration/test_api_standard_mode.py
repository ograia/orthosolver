from fastapi.testclient import TestClient

from mock_lean.main import JOBS, app as mock_lean_app
from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module


class MockLeanClientProxy:
    def __init__(self) -> None:
        self.client = TestClient(mock_lean_app)

    def submit_job(self, body, request_id: str, mock_behavior=None):
        headers = {"X-Request-Id": request_id, "X-Idempotency-Key": body["job_id"]}
        if mock_behavior:
            headers["X-Mock-Behavior"] = mock_behavior
        response = self.client.post("/v1/jobs", json=body, headers=headers)
        assert response.status_code in {202, 409}
        return response.json()

    def submit_operation(self, operation: str, body, request_id: str, version: str = "v2", mock_behavior=None):
        operation_id = body.get("operation_id") or body.get("job_id")
        headers = {"X-Request-Id": request_id, "X-Idempotency-Key": operation_id}
        if mock_behavior:
            headers["X-Mock-Behavior"] = mock_behavior
        response = self.client.post(f"/{version}/operations/{operation}", json=body, headers=headers)
        assert response.status_code in {202, 409}
        return response.json()

    def get_job(self, job_id: str):
        response = self.client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        return response.json()

    def get_operation(self, operation_id: str, version: str = "v2"):
        response = self.client.get(f"/{version}/operations/{operation_id}")
        assert response.status_code == 200
        return response.json()

    def cancel_job(self, job_id: str):
        response = self.client.post(f"/v1/jobs/{job_id}/cancel")
        assert response.status_code == 200
        return response.json()

    def health(self):
        response = self.client.get("/v1/health")
        assert response.status_code == 200
        return response.json()


def test_standard_mode_reaches_formal_success(monkeypatch) -> None:
    JOBS.clear()
    monkeypatch.setenv("MOCK_LEAN_DEFAULT_DELAY_SECONDS", "0")
    monkeypatch.setattr(orchestrator_module, "LeanClient", MockLeanClientProxy)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Standard theorem",
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

    get_problem = client.get(f"/v1/problems/{problem_id}")
    assert get_problem.status_code == 200
    assert get_problem.json()["problem"]["verification_level"] == "formal"

    events = client.get(f"/v1/problems/{problem_id}/events")
    assert events.status_code == 200
    stage_names = [event["stage"] for event in events.json()["events"]]
    assert "lean.job_submitted" in stage_names


def test_selection_waits_for_all_assembly_checks(monkeypatch) -> None:
    JOBS.clear()
    monkeypatch.setenv("MOCK_LEAN_DEFAULT_DELAY_SECONDS", "0")
    monkeypatch.setattr(orchestrator_module, "LeanClient", MockLeanClientProxy)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Assembly race theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": False}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    run1 = client.post(f"/v1/problems/{problem_id}/run")
    assert run1.status_code == 200
    run2 = client.post(f"/v1/problems/{problem_id}/run")
    assert run2.status_code == 200

    after_first_assembly = client.get(f"/v1/problems/{problem_id}")
    assert after_first_assembly.status_code == 200
    first_active = after_first_assembly.json()["problem"]["active_decomposition_id"]
    if first_active is not None:
        return

    check = after_first_assembly
    for _ in range(10):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        check = client.get(f"/v1/problems/{problem_id}")
        assert check.status_code == 200
        if check.json()["problem"]["active_decomposition_id"] is not None:
            break

    assert check.json()["problem"]["active_decomposition_id"] is not None
