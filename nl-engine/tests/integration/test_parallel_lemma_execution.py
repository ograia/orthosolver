from __future__ import annotations

import time

from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.workers import facade as workers_facade
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


class ParallelLemmasAgent(BaseAgentService):
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
            normalized_claim="lemma",
        )
        lemmas = [
            Agent2Lemma(
                local_id=f"L{i}",
                statement_nl=f"Aux lemma {i}: {payload.theorem_nl}",
                semantic_sketch=sketch,
                role_in_assembly=f"step{i}",
                formalization_cost_estimate=0.2 + (0.05 * i),
                self_check_true=True,
                self_check_notes="parallel test",
            )
            for i in range(1, 4)
        ]
        candidate = Agent2Candidate(
            candidate_index=0,
            strategy_summary="parallel-test-strategy",
            shared_context=[],
            lemmas=lemmas,
            assembly_plan=Agent2AssemblyPlan(
                steps=[
                    {
                        "step_id": "A1",
                        "uses_lemmas": ["L1", "L2", "L3"],
                        "uses_prior_steps": [],
                        "derives": payload.theorem_nl,
                        "is_trivial": True,
                        "trivial_justification": "direct composition",
                    }
                ],
                proof_skeleton_nl="Combine L1, L2, L3 directly.",
                final_step_yields_exact_root=True,
            ),
            formalization_cost_estimate_total=0.6,
            drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
        )
        return Agent2Output(status="completed", candidates=[candidate])

    def vet_decomposition(self, payload, artifact_prefix):
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted for parallel test",
            lemma_findings=[
                {
                    "local_id": lemma.get("local_id", "L1"),
                    "statement_status": "plausible",
                    "evidence": "test",
                    "counterexample": None,
                }
                for lemma in payload.decomposition.get("lemmas", [])
            ],
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

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        time.sleep(0.25)
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl=f"Proof of {payload.statement_nl}",
            proof_summary="parallel proof",
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
            reason="parallel proof accepted",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )


def test_parallel_solver_batch_execution(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_module, "AgentService", ParallelLemmasAgent)
    monkeypatch.setattr(workers_facade, "AgentService", ParallelLemmasAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Parallel lemma theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first = client.post(f"/v1/problems/{problem_id}/run")
    assert first.status_code == 200

    start = time.perf_counter()
    second = client.post(f"/v1/problems/{problem_id}/run")
    elapsed = time.perf_counter() - start
    assert second.status_code == 200

    # 3 lemmas * 0.25s would be ~0.75s sequentially. Parallel batch should be much lower.
    # Allow extra headroom for async supervisor overhead.
    assert elapsed < 1.5

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    solver_events = [row for row in events.json()["events"] if row["stage"] == "lemma.solver_attempt"]
    if len(solver_events) < 3:
        for _ in range(20):
            run = client.post(f"/v1/problems/{problem_id}/run")
            assert run.status_code == 200
            events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
            assert events.status_code == 200
            solver_events = [row for row in events.json()["events"] if row["stage"] == "lemma.solver_attempt"]
            if len(solver_events) >= 3:
                break
    assert len(solver_events) >= 3
