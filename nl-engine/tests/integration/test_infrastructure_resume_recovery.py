from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.domain.contracts import (
    Agent1Output,
    Agent2AssemblyPlan,
    Agent2Candidate,
    Agent2Lemma,
    Agent2Output,
    Agent3Output,
    Agent4Output,
    Agent5Output,
    Agent6Output,
    SemanticSketch,
)
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import ProblemRepository, TheoremRepository, WorkerJobRepository
from nl_engine.services.agents import AgentExecutionError, AgentService as BaseAgentService
from nl_engine.workers import facade as workers_facade


class InfraThenRecoverAgent(BaseAgentService):
    solve_call_counts: dict[int, int] = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def set_llm_overrides(self, llm_overrides) -> None:
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=SemanticSketch(
                variables=[],
                quantifier_order=[],
                domain_restrictions=[],
                witness_dependencies=[],
                normalized_claim=statement_nl,
            ),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix, **kwargs):
        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim=payload.theorem_nl,
        )
        candidate = Agent2Candidate(
            candidate_index=0,
            strategy_summary="single-lemma",
            shared_context=[],
            lemmas=[
                Agent2Lemma(
                    local_id="L1",
                    statement_nl=f"{payload.theorem_nl} lemma",
                    semantic_sketch=sketch,
                    role_in_assembly="direct",
                    formalization_cost_estimate=0.1,
                    self_check_true=True,
                    self_check_notes="ok",
                )
            ],
            assembly_plan=Agent2AssemblyPlan(
                steps=[
                    {
                        "step_id": "A1",
                        "uses_lemmas": ["L1"],
                        "uses_prior_steps": [],
                        "derives": payload.theorem_nl,
                        "is_trivial": True,
                        "trivial_justification": "direct",
                    }
                ],
                proof_skeleton_nl="Apply L1.",
                final_step_yields_exact_root=True,
            ),
            formalization_cost_estimate_total=0.1,
            drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
        )
        return Agent2Output(status="completed", candidates=[candidate])

    def vet_decomposition(self, payload, artifact_prefix):
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[
                {
                    "local_id": "L1",
                    "statement_status": "plausible",
                    "evidence": "ok",
                    "counterexample": None,
                }
            ],
            assembly_check={
                "verdict": "valid",
                "details": "ok",
                "hidden_steps_found": [],
                "final_step_matches_root": True,
            },
            coverage_check={"redundant_lemmas": [], "missing_coverage": [], "disguised_difficulty": []},
            drift_assessment={
                "drift_detected": False,
                "drift_severity": "none",
                "per_lemma_drift": [],
                "assembly_conclusion_matches_root": True,
            },
            formalization_risk="low",
            fixes_required=[],
            fatal_reason=None,
        )

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        attempt = int(payload.attempt_number)
        self.__class__.solve_call_counts[attempt] = self.__class__.solve_call_counts.get(attempt, 0) + 1
        if attempt == 1:
            return Agent4Output(
                status="failed",
                lemma_id=payload.lemma_id,
                attempt_number=attempt,
                proof_nl=None,
                proof_summary="no proof yet",
                self_report={
                    "confidence": 0.2,
                    "suspected_gaps": ["missing detail"],
                    "used_external_facts": [],
                    "every_step_justified": False,
                    "proves_exactly_the_statement": False,
                },
                stuck_point="needs another attempt",
                candidate_counterexample=None,
                addressed_previous_feedback=payload.previous_feedback,
            )
        if attempt == 2:
            raise AgentExecutionError(
                agent_key="agent4",
                error_class="infrastructure_transient",
                message=(
                    "OpenAI request failed for agent4: RuntimeError: "
                    "Background response resp_test_legacy for agent4 reached terminal status: failed"
                ),
                artifact_prefix=artifact_prefix,
            )
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=attempt,
            proof_nl=f"Proof for {payload.statement_nl}",
            proof_summary="proved after infra recovery",
            self_report={
                "confidence": 0.95,
                "suspected_gaps": [],
                "used_external_facts": [],
                "every_step_justified": True,
                "proves_exactly_the_statement": True,
            },
            stuck_point=None,
            candidate_counterexample=None,
            addressed_previous_feedback=payload.previous_feedback,
        )

    def vet_lemma_proof(self, payload, artifact_prefix):
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
            reason="accepted",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )

    def final_check(self, payload, artifact_prefix):
        return Agent6Output(
            status="completed",
            verdict="approved",
            confidence=0.99,
            summary="approved",
            decomposition_findings=[],
            assembly_findings=[],
            lemma_findings=[],
        )


def _run_until_status(client: TestClient, problem_id: str, wanted: set[str], max_ticks: int = 120) -> str:
    status = "created"
    for _ in range(max_ticks):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        status = run.json()["status"]
        if status in wanted:
            return status
    return status


def test_resume_after_infrastructure_failure_preserves_run_and_recovers(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", InfraThenRecoverAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", InfraThenRecoverAgent)
    monkeypatch.setattr(workers_facade, "AgentService", InfraThenRecoverAgent)
    InfraThenRecoverAgent.solve_call_counts = {}

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "infra resume recovery",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "lemma_solving": {
                    "max_infrastructure_failures": 1,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = _run_until_status(client, problem_id, {"failed"}, max_ticks=120)
    assert terminal == "failed"

    failure_report = client.get(f"/v1/problems/{problem_id}/failure-report")
    assert failure_report.status_code == 200
    failure_payload = failure_report.json()["failure_report"]
    assert failure_payload["terminal_error_class"] in {"infrastructure", "infrastructure_transient", "timeout"}
    assert "infrastructure failure count (1)" in (failure_payload["terminal_error_message"] or "")

    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot")
    assert snapshot.status_code == 200
    failure_artifact = snapshot.json()["problem"]["failure_report_artifact_id"]
    assert isinstance(failure_artifact, str) and failure_artifact
    encoded_failure_artifact = "/".join(quote(part, safe="") for part in failure_artifact.split("/"))
    failure_artifact_before_resume = client.get(f"/v1/debug/artifacts/{encoded_failure_artifact}")
    assert failure_artifact_before_resume.status_code == 200

    store = get_file_store()
    jobs_before = WorkerJobRepository(store).list_by_problem(problem_id, worker_kind="lemma_solver")
    failed_solver_jobs = [row for row in jobs_before if row.status == "failed"]
    assert failed_solver_jobs
    assert len([row for row in failed_solver_jobs if row.controller_consumed_at is not None]) >= 1

    resume = client.post(f"/v1/debug/problems/{problem_id}/resume-after-infrastructure-failure")
    assert resume.status_code == 200
    resume_payload = resume.json()
    assert resume_payload["problem_id"] == problem_id
    assert resume_payload["status"] == "running"
    assert resume_payload["previous_failure_report_id"] == failure_artifact

    # Original failure artifact must remain readable after resume.
    failure_artifact_after_resume = client.get(f"/v1/debug/artifacts/{encoded_failure_artifact}")
    assert failure_artifact_after_resume.status_code == 200

    recovered = _run_until_status(client, problem_id, {"succeeded", "failed"}, max_ticks=160)
    assert recovered == "succeeded"

    jobs_after = WorkerJobRepository(get_file_store()).list_by_problem(problem_id, worker_kind="lemma_solver")
    assert any(row.worker_job_id.endswith("_solve_3") and row.status == "completed" for row in jobs_after)

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stages = [row["stage"] for row in events.json()["events"]]
    assert "problem.resumed_after_infrastructure_failure" in stages

    problem_row = ProblemRepository(get_file_store()).get(problem_id)
    assert problem_row is not None
    assert problem_row.status == "succeeded"


def test_resume_after_infrastructure_failure_refuses_non_failed_problem() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "resume-refusal", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    response = client.post(f"/v1/debug/problems/{problem_id}/resume-after-infrastructure-failure")
    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "invalid_problem_state"


def test_public_resume_succeeds_for_failed_non_infrastructure_problem_and_records_resume_artifacts() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "public resume success", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    payload = create.json()
    problem_id = payload["problem_id"]
    theorem_id = payload["root_theorem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    theorem_repo = TheoremRepository(store)
    problem_row = problem_repo.get(problem_id)
    theorem_row = theorem_repo.get(theorem_id)
    assert problem_row is not None
    assert theorem_row is not None

    problem_row.status = "failed"
    problem_row.failure_report_artifact_id = f"problems/{problem_id}/failure_report.manual.json"
    problem_repo.save(problem_row)
    theorem_row.status = "failed"
    theorem_repo.save(theorem_row)

    resume = client.post(f"/v1/problems/{problem_id}/resume")
    assert resume.status_code == 200
    resume_payload = resume.json()
    assert resume_payload["problem_id"] == problem_id
    assert resume_payload["status"] == "running"
    assert resume_payload["previous_failure_report_id"] == f"problems/{problem_id}/failure_report.manual.json"
    assert resume_payload["execution"]["trigger_source"] == "api_resume"
    assert resume_payload["execution"]["execution_id"]

    request_log = client.get(f"/v1/debug/problems/{problem_id}/request-log?source=api_resume&limit=100")
    assert request_log.status_code == 200
    entries = request_log.json()["entries"]
    assert entries
    assert any(entry["completion_status"] == "completed" for entry in entries)
    assert any("trigger=manual_resume" in (entry.get("summary") or "") for entry in entries)

    artifacts = client.get(
        f"/v1/debug/problems/{problem_id}/artifacts"
        f"?prefix=problems/{problem_id}/api/resume_requests&include_worker_jobs=false&limit=200"
    )
    assert artifacts.status_code == 200
    keys = [row["artifact_key"] for row in artifacts.json()["artifacts"]]
    assert any(key.endswith(".request.json") for key in keys)
    assert any(key.endswith(".response.json") for key in keys)
    response_key = next(key for key in keys if key.endswith(".response.json"))
    encoded_response_key = "/".join(quote(part, safe="") for part in response_key.split("/"))
    response_artifact = client.get(f"/v1/debug/artifacts/{encoded_response_key}")
    assert response_artifact.status_code == 200
    response_content = response_artifact.json()["content"]
    assert response_content["ok"] is True


def test_public_resume_refuses_non_failed_problem() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "public resume refusal", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    response = client.post(f"/v1/problems/{problem_id}/resume")
    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "invalid_problem_state"
