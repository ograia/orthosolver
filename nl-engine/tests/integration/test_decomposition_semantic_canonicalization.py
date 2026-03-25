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
    SemanticSketch,
)
from nl_engine.services.agents import AgentService as BaseAgentService
from nl_engine.workers import facade as workers_facade


class CanonicalizedSemanticAgent(BaseAgentService):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        pass

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=SemanticSketch(
                variables=[{"name": "x", "type": "object", "domain": "universe", "role": "bound variable"}],
                quantifier_order=["for all x"],
                domain_restrictions=["x is any object"],
                witness_dependencies=[],
                normalized_claim=f"A1::{statement_nl}",
            ),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix, **kwargs):
        # Intentionally provide a different semantic sketch here. The controller
        # should canonicalize via Agent1 before sending to decomposition vetting.
        raw_semantic = SemanticSketch(
            variables=[{"name": "x", "type": "object", "domain": "universe", "role": "bound variable"}],
            quantifier_order=["for all x"],
            domain_restrictions=["x is any object"],
            witness_dependencies=[],
            normalized_claim="AGENT2_RAW",
        )
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="single-lemma identity",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl="For all x, x = x",
                            semantic_sketch=raw_semantic,
                            role_in_assembly="directly yields theorem",
                            formalization_cost_estimate=0.1,
                            self_check_true=True,
                            self_check_notes="identity",
                        )
                    ],
                    assembly_plan=Agent2AssemblyPlan(
                        steps=[
                            {
                                "step_id": "A1",
                                "uses_lemmas": ["L1"],
                                "uses_prior_steps": [],
                                "derives": "For all x, x = x",
                                "is_trivial": True,
                                "trivial_justification": "identity lemma",
                            }
                        ],
                        proof_skeleton_nl="By L1, done.",
                        final_step_yields_exact_root=True,
                    ),
                    formalization_cost_estimate_total=0.1,
                    drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
                )
            ],
        )

    def vet_decomposition(self, payload, artifact_prefix):
        lemma_rows = payload.decomposition.get("lemmas", [])
        assert lemma_rows, "expected at least one lemma in decomposition payload"
        normalized_claim = lemma_rows[0].get("semantic_sketch", {}).get("normalized_claim")
        assert str(normalized_claim).startswith("A1::"), "expected Agent1-canonicalized semantic sketch"
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[{"local_id": "L1", "statement_status": "plausible", "evidence": "ok", "counterexample": None}],
            assembly_check={"verdict": "valid", "details": "ok", "hidden_steps_found": [], "final_step_matches_root": True},
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
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl="Trivial by reflexivity.",
            proof_summary="identity",
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
            confidence=0.99,
            reason="accepted",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )


def test_decomposition_vetter_receives_agent1_canonicalized_semantics(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", CanonicalizedSemanticAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", CanonicalizedSemanticAgent)
    monkeypatch.setattr(workers_facade, "AgentService", CanonicalizedSemanticAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Canonicalized semantics test",
            "statement_nl": "For all x, x = x",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(15):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    # Root-equivalent child lemmas are now intentionally rejected as fatal
    # decomposition outcomes after canonicalization/vetting.
    assert terminal == "failed"
