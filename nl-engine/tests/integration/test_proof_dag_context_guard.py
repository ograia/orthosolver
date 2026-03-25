from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import WorkerJobRepository


def test_solver_payload_uses_dependency_manifest_and_hides_root_claim(monkeypatch) -> None:
    from nl_engine.api import main as api_main
    from nl_engine.controller import orchestrator as orchestrator_module
    from nl_engine.workers import facade as workers_facade
    from tests.integration.test_decomposition_pipeline_progress import CountingPipelineAgent

    monkeypatch.setattr(api_main, "AgentService", CountingPipelineAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", CountingPipelineAgent)
    monkeypatch.setattr(workers_facade, "AgentService", CountingPipelineAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Proof DAG context guard",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"parallel_root_decompositions_n": 1, "parallel_root_take_k": 1},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    for _ in range(8):
        response = client.post(f"/v1/problems/{problem_id}/run")
        assert response.status_code == 200

    store = get_file_store()
    worker_repo = WorkerJobRepository(store)
    solver_jobs = worker_repo.list_by_problem(problem_id, worker_kind="lemma_solver")
    assert solver_jobs
    payload = solver_jobs[0].payload
    assert payload["root_theorem_nl"] == ""
    assert payload["shared_context"] == []
    assert isinstance(payload.get("definition_context"), list)
    assert isinstance(payload.get("allowed_dependency_manifest"), list)
    assert isinstance(payload.get("forbidden_claims"), list)
    assert any(item.get("label") == "root_theorem_claim" for item in payload["forbidden_claims"])

    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot?events_limit=50")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["problem"]["dependency_verification_level"] == "shadow"
    assert len(body["proof_graphs"]) >= 1
    assert len(body["proof_graph_nodes"]) >= 1
