from __future__ import annotations

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
from nl_engine.workers import facade as workers_facade


def _sketch(statement_nl: str) -> SemanticSketch:
    return SemanticSketch(
        variables=[],
        quantifier_order=[],
        domain_restrictions=[],
        witness_dependencies=[],
        normalized_claim=statement_nl,
    )


class LocalizedRepairAgentService:
    final_check_calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str) -> Agent1Output:
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=_sketch(statement_nl),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix: str, **kwargs) -> Agent2Output:
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="single localized-repair strategy",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl=f"{payload.theorem_nl} supporting lemma",
                            semantic_sketch=_sketch(f"{payload.theorem_nl} supporting lemma"),
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
                    drift_self_check={
                        "all_lemmas_consistent_with_root_sketch": True,
                        "inconsistencies_noted": [],
                    },
                )
            ],
        )

    def vet_decomposition(self, payload, artifact_prefix: str) -> Agent3Output:
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[{"local_id": "L1", "statement_status": "plausible", "evidence": "ok", "counterexample": None}],
            assembly_check={
                "verdict": "valid",
                "details": "valid",
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

    def solve_lemma(self, payload, artifact_prefix: str, **kwargs) -> Agent4Output:
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl=f"Attempt {payload.attempt_number} proof of {payload.statement_nl}.",
            proof_summary="localized repair proof",
            self_report={
                "confidence": 0.9,
                "suspected_gaps": [],
                "used_external_facts": [],
                "every_step_justified": True,
                "proves_exactly_the_statement": True,
            },
            stuck_point=None,
            candidate_counterexample=None,
            addressed_previous_feedback=payload.previous_feedback,
        )

    def vet_lemma_proof(self, payload, artifact_prefix: str) -> Agent5Output:
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

    def final_check(self, payload, artifact_prefix: str) -> Agent6Output:
        self.__class__.final_check_calls += 1
        lemma_id = payload.proof_bundle["children"][0]["node_id"]
        if self.__class__.final_check_calls == 1:
            return Agent6Output(
                status="completed",
                verdict="assembly_issue",
                confidence=0.9,
                summary="one lemma proof needs a localized retry",
                decomposition_findings=[],
                assembly_findings=[
                    {
                        "target_scope": "lemma",
                        "target_id": lemma_id,
                        "severity": "fatal",
                        "description": "Localized repair needed for direct lemma proof packaging.",
                        "recommended_action": "retry_solver",
                    }
                ],
                lemma_findings=[],
            )
        return Agent6Output(
            status="completed",
            verdict="approved",
            confidence=0.99,
            summary="approved after localized repair",
            decomposition_findings=[],
            assembly_findings=[],
            lemma_findings=[],
        )


def test_agent6_localized_failure_retries_same_root_branch(monkeypatch) -> None:
    LocalizedRepairAgentService.final_check_calls = 0
    monkeypatch.setattr(api_main, "AgentService", LocalizedRepairAgentService)
    monkeypatch.setattr(orchestrator_module, "AgentService", LocalizedRepairAgentService)
    monkeypatch.setattr(workers_facade, "AgentService", LocalizedRepairAgentService)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "localized repair",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    status = "created"
    for _ in range(60):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        status = run.json()["status"]
        if status in {"succeeded", "failed"}:
            break

    assert status == "succeeded"
    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert len(body["decompositions"]) == 1
    assert body["problem"]["active_decomposition_id"] == body["decompositions"][0]["decomposition_id"]
    assert body["final_proof"] is not None
    event_stages = [row["stage"] for row in body["events"]]
    assert "final_check.localized_repair_requested" in event_stages
    assert LocalizedRepairAgentService.final_check_calls >= 2
