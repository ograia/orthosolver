from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.workers import facade as workers_facade
from nl_engine.domain.contracts import Agent4Output, Agent5Output
from nl_engine.persistence.db import SessionLocal
from nl_engine.persistence.repositories import EventRepository, ProblemRepository
from tests.conftest import DeterministicTestAgentService


class DecomposeFirstAgent(DeterministicTestAgentService):
    vet_calls = 0

    def vet_lemma_proof(self, payload, artifact_prefix):
        DecomposeFirstAgent.vet_calls += 1
        if DecomposeFirstAgent.vet_calls <= 2:
            out = Agent5Output(
                status="completed",
                lemma_id=payload.lemma_id,
                statement_status="plausible",
                proof_status="major_gap",
                drift_assessment={
                    "drift_detected": False,
                    "drift_severity": "none",
                    "drift_description": None,
                    "drift_type": None,
                },
                recommended_action="decompose_current_lemma",
                confidence=0.9,
                reason="high-severity gap",
                feedback_for_solver="decompose",
                candidate_counterexample=None,
                detailed_findings=[
                    {
                        "finding": "critical logical jump remains unresolved",
                        "severity": "high",
                        "location": "core argument",
                    }
                ],
            )
            return out
        return Agent5Output(
            status="completed",
            lemma_id=payload.lemma_id,
            statement_status="plausible",
            proof_status="complete",
            drift_assessment={
                "drift_detected": False,
                "drift_severity": "none",
                "drift_description": None,
                "drift_type": None,
            },
            recommended_action="send_to_lean",
            confidence=0.95,
            reason="accepted after decomposition",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )


class MediumRetryAgent(DeterministicTestAgentService):
    vet_calls = 0

    def vet_lemma_proof(self, payload, artifact_prefix):
        MediumRetryAgent.vet_calls += 1
        if MediumRetryAgent.vet_calls == 1:
            return Agent5Output(
                status="completed",
                lemma_id=payload.lemma_id,
                statement_status="plausible",
                proof_status="complete",
                drift_assessment={
                    "drift_detected": False,
                    "drift_severity": "none",
                    "drift_description": None,
                    "drift_type": None,
                },
                recommended_action="send_to_lean",
                confidence=0.86,
                reason="proof is mostly correct but needs cleanup",
                feedback_for_solver="Clarify the case split and remove ambiguous notation.",
                candidate_counterexample=None,
                detailed_findings=[
                    {
                        "finding": "Case 2 wording is ambiguous and should be rewritten.",
                        "severity": "medium",
                        "location": "Case 2",
                    }
                ],
            )
        return Agent5Output(
            status="completed",
            lemma_id=payload.lemma_id,
            statement_status="plausible",
            proof_status="complete",
            drift_assessment={
                "drift_detected": False,
                "drift_severity": "none",
                "drift_description": None,
                "drift_type": None,
            },
            recommended_action="send_to_lean",
            confidence=0.96,
            reason="revised proof accepted",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[
                {
                    "finding": "No outstanding issues.",
                    "severity": "low",
                    "location": None,
                }
            ],
        )


class ThreeSolverFailuresThenDecomposeAgent(DeterministicTestAgentService):
    failed_lemma_id: str | None = None
    failure_counts: dict[str, int] = {}

    def solve_lemma(self, payload, artifact_prefix: str, **kwargs) -> Agent4Output:
        if self.__class__.failed_lemma_id is None:
            self.__class__.failed_lemma_id = payload.lemma_id
        if payload.lemma_id == self.__class__.failed_lemma_id:
            count = self.__class__.failure_counts.get(payload.lemma_id, 0) + 1
            self.__class__.failure_counts[payload.lemma_id] = count
            if count <= 3:
                return Agent4Output(
                    status="failed",
                    lemma_id=payload.lemma_id,
                    attempt_number=payload.attempt_number,
                    proof_nl=None,
                    proof_summary=f"stuck attempt {count}",
                    self_report={
                        "confidence": 0.2,
                        "suspected_gaps": ["missing bridge"],
                        "used_external_facts": [],
                        "every_step_justified": False,
                        "proves_exactly_the_statement": False,
                    },
                    stuck_point="missing bridge",
                    candidate_counterexample=None,
                    addressed_previous_feedback=payload.previous_feedback,
                )
        return super().solve_lemma(payload, artifact_prefix, **kwargs)


def test_recursive_lemma_decomposition_path(monkeypatch) -> None:
    DecomposeFirstAgent.vet_calls = 0
    monkeypatch.setattr(api_main, "AgentService", DecomposeFirstAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", DecomposeFirstAgent)
    monkeypatch.setattr(workers_facade, "AgentService", DecomposeFirstAgent)

    client = TestClient(app)
    payload = {
        "title": "Recursive decomposition theorem",
        "statement_nl": "For all n, n = n",
        "config": {
            "mode": {"nl_only_mode": True},
            "lemma_solving": {"max_solver_retries_per_lemma": 2, "max_total_lemma_nodes": 128},
        },
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(80):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded"

    events = client.get(f"/v1/problems/{problem_id}/events")
    assert events.status_code == 200
    stages = [event["stage"] for event in events.json()["events"]]
    assert "lemma.decomposed" in stages

    tree = client.get(f"/v1/problems/{problem_id}/tree")
    assert tree.status_code == 200
    root = tree.json()["root"]
    assert root["children"], "expected at least one root lemma"
    assert any(child.get("children") for child in root["children"]), "expected recursive lemma subtree"


def test_medium_vetter_findings_retry_solver_without_decomposition(monkeypatch) -> None:
    MediumRetryAgent.vet_calls = 0
    monkeypatch.setattr(api_main, "AgentService", MediumRetryAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", MediumRetryAgent)
    monkeypatch.setattr(workers_facade, "AgentService", MediumRetryAgent)

    client = TestClient(app)
    payload = {
        "title": "Medium findings retry theorem",
        "statement_nl": "For all n, n = n",
        "config": {
            "mode": {"nl_only_mode": True},
            "lemma_solving": {"max_solver_retries_per_lemma": 1, "max_total_lemma_nodes": 128},
        },
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(60):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded"
    assert MediumRetryAgent.vet_calls >= 2

    events = client.get(f"/v1/problems/{problem_id}/events")
    assert events.status_code == 200
    stages = [event["stage"] for event in events.json()["events"]]
    assert "lemma.decomposed" not in stages
    assert "lemma.vetter_route" in stages

    tree = client.get(f"/v1/problems/{problem_id}/tree")
    assert tree.status_code == 200
    root = tree.json()["root"]
    assert root["children"], "expected at least one root lemma"
    assert not any(child.get("children") for child in root["children"]), "did not expect recursive decomposition"


def test_solver_cap_decomposes_immediately_when_third_fatal_is_consumed(monkeypatch) -> None:
    ThreeSolverFailuresThenDecomposeAgent.failed_lemma_id = None
    ThreeSolverFailuresThenDecomposeAgent.failure_counts = {}
    monkeypatch.setattr(api_main, "AgentService", ThreeSolverFailuresThenDecomposeAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", ThreeSolverFailuresThenDecomposeAgent)
    monkeypatch.setattr(workers_facade, "AgentService", ThreeSolverFailuresThenDecomposeAgent)

    client = TestClient(app)
    payload = {
        "title": "Immediate solver cap escalation",
        "statement_nl": "For all n, n = n",
        "config": {
            "mode": {"nl_only_mode": True},
            "lemma_solving": {
                "max_consecutive_fatal_rejections_per_lemma": 3,
                "max_total_lemma_nodes": 128,
            },
        },
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    root_lemma_id = None
    saw_immediate_decomposition = False
    terminal = None
    for _ in range(80):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot?events_limit=200")
        assert snapshot.status_code == 200
        body = snapshot.json()
        if root_lemma_id is None and body["lemmas"]:
            root_lemma_id = body["lemmas"][0]["lemma_id"]
        if root_lemma_id is not None:
            row = body["lemma_by_id"][root_lemma_id]
            if row["solver_attempt_count"] >= 3:
                assert row["routing_status"] != "retry_solver"
                assert row["decomposition_count"] > 0
                saw_immediate_decomposition = True
        if terminal in {"succeeded", "failed"}:
            break

    assert saw_immediate_decomposition is True
    assert terminal == "succeeded"

    events = client.get(f"/v1/problems/{problem_id}/events")
    assert events.status_code == 200
    stages = [event["stage"] for event in events.json()["events"]]
    assert "lemma.decomposed" in stages

def test_global_timeout_config_is_not_enforced_anymore() -> None:
    client = TestClient(app)
    payload = {
        "title": "Timeout theorem",
        "statement_nl": "For all x, x = x",
        "config": {
            "mode": {"nl_only_mode": True},
            "global": {
                "global_timeout_seconds": 1,
                "timeout_grace_period_seconds": 120,
                "on_timeout_behavior": "graceful_shutdown",
            },
        },
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    with SessionLocal() as db:
        problem = ProblemRepository(db).get(problem_id)
        assert problem is not None
        problem.created_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()

    run = client.post(f"/v1/problems/{problem_id}/run")
    assert run.status_code == 200
    assert run.json()["status"] != "failed"

    terminal = run.json()["status"]
    if terminal not in {"succeeded", "failed"}:
        for _ in range(60):
            tick = client.post(f"/v1/problems/{problem_id}/run")
            assert tick.status_code == 200
            terminal = tick.json()["status"]
            if terminal in {"succeeded", "failed"}:
                break

    assert terminal == "succeeded"

    with SessionLocal() as db:
        event_rows = EventRepository(db).list_for_problem(problem_id, limit=200)
        assert not any(row.stage == "problem.timeout_grace" for row in event_rows)
