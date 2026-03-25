from fastapi.testclient import TestClient

from mock_lean.main import JOBS, app


def test_mock_lean_submit_and_get_success() -> None:
    JOBS.clear()
    client = TestClient(app)
    body = {
        "job_id": "job_1",
        "problem_id": "prob_1",
        "target_id": "lem_1",
        "target_kind": "lemma",
        "mode": "formalize_lemma",
        "lean_image_tag": "x",
        "payload": {},
    }
    sub = client.post("/v1/jobs", json=body)
    assert sub.status_code == 202

    # First poll can be running.
    first = client.get("/v1/jobs/job_1")
    assert first.status_code == 200
    assert first.json()["status"] in {"running", "success"}

    # Force instant completion for deterministic assertion.
    JOBS["job_1"]["delay_seconds"] = 0
    res = client.get("/v1/jobs/job_1")
    assert res.status_code == 200
    assert res.json()["status"] == "success"


def test_mock_lean_forced_fatal() -> None:
    JOBS.clear()
    client = TestClient(app)
    body = {
        "job_id": "job_2",
        "problem_id": "prob_2",
        "target_id": "lem_2",
        "target_kind": "lemma",
        "mode": "formalize_lemma",
        "lean_image_tag": "x",
        "payload": {},
    }
    sub = client.post("/v1/jobs", json=body, headers={"X-Mock-Behavior": "fatal/major_proof_gap"})
    assert sub.status_code == 202

    JOBS["job_2"]["delay_seconds"] = 0
    res = client.get("/v1/jobs/job_2")
    assert res.status_code == 200
    assert res.json()["status"] == "fatal"
    assert res.json()["result"]["error_class"] == "major_proof_gap"
