from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.domain.contracts import Agent4Output
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import WorkerJobRepository
from tests.conftest import DeterministicTestAgentService


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
    assert payload["root_semantic_sketch"] == {}
    assert payload["shared_context"] == []
    assert isinstance(payload.get("definition_context"), list)
    assert isinstance(payload.get("allowed_dependency_manifest"), list)
    assert isinstance(payload.get("forbidden_claims"), list)
    assert not any(item.get("label") == "root_semantic_sketch" for item in payload["allowed_dependency_manifest"])
    assert any(item.get("label") == "root_theorem_claim" for item in payload["forbidden_claims"])

    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot?events_limit=50")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["problem"]["dependency_verification_level"] == "shadow"
    assert len(body["proof_graphs"]) >= 1
    assert len(body["proof_graph_nodes"]) >= 1


class RootContextLeakAgent(DeterministicTestAgentService):
    def solve_lemma(self, payload, artifact_prefix: str, **kwargs) -> Agent4Output:  # noqa: ANN001
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl="Applying the allowed dependency root_semantic_sketch completes the proof.",
            proof_summary="illegally cites theorem context",
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


def test_solver_root_context_reference_is_rejected_before_vetter(monkeypatch) -> None:
    from nl_engine.api import main as api_main
    from nl_engine.controller import orchestrator as orchestrator_module
    from nl_engine.workers import facade as workers_facade

    monkeypatch.setattr(api_main, "AgentService", RootContextLeakAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", RootContextLeakAgent)
    monkeypatch.setattr(workers_facade, "AgentService", RootContextLeakAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Proof DAG descendant root leak",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"parallel_root_decompositions_n": 1, "parallel_root_take_k": 1},
                "lemma_solving": {"max_solver_retries_per_lemma": 1},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    saw_dependency_violation = False
    for _ in range(20):
        response = client.post(f"/v1/problems/{problem_id}/run")
        assert response.status_code == 200
        events = client.get(f"/v1/problems/{problem_id}/events")
        assert events.status_code == 200
        stages = [event["stage"] for event in events.json()["events"]]
        if "lemma.dependency_violation" in stages:
            saw_dependency_violation = True
            break

    assert saw_dependency_violation is True

    store = get_file_store()
    worker_repo = WorkerJobRepository(store)
    solver_jobs = worker_repo.list_by_problem(problem_id, worker_kind="lemma_solver")
    assert solver_jobs
    assert worker_repo.list_by_problem(problem_id, worker_kind="lemma_vetter") == []
