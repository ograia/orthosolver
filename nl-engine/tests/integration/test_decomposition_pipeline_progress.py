from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api.main import app
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


class CountingPipelineAgent:
    semantic_calls: list[str] = []
    decompose_calls: int = 0
    vet_calls: int = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def set_llm_overrides(self, llm_overrides) -> None:  # noqa: ANN001
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str) -> Agent1Output:
        self.__class__.semantic_calls.append(statement_nl)
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

    def decompose(self, payload, artifact_prefix: str, **kwargs) -> Agent2Output:  # noqa: ANN001
        self.__class__.decompose_calls += 1
        lemma_sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim="lemma sketch from agent2",
        )
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="counting strategy",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl="Lemma statement",
                            semantic_sketch=lemma_sketch,
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
            ],
        )

    def vet_decomposition(self, payload, artifact_prefix: str) -> Agent3Output:  # noqa: ANN001
        self.__class__.vet_calls += 1
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[{"local_id": "L1", "statement_status": "plausible"}],
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

    def solve_lemma(self, payload, artifact_prefix: str, **kwargs) -> Agent4Output:  # noqa: ANN001
        return Agent4Output(
            status="failed",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl=None,
            proof_summary=None,
            self_report={},
            stuck_point="not used in this test",
            candidate_counterexample=None,
            addressed_previous_feedback=None,
        )

    def vet_lemma_proof(self, payload, artifact_prefix: str) -> Agent5Output:  # noqa: ANN001
        return Agent5Output(
            status="completed",
            lemma_id=payload.lemma_id,
            statement_status="plausible",
            proof_status="localized_gap",
            drift_assessment={"drift_detected": False, "drift_severity": "none"},
            recommended_action="retry_solver",
            confidence=0.1,
            reason="not used in this test",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )


def test_root_decomposition_tick_reaches_agent3_without_extra_agent1(monkeypatch) -> None:
    from nl_engine.api import main as api_main
    from nl_engine.controller import orchestrator as orchestrator_module
    from nl_engine.workers import facade as workers_facade

    CountingPipelineAgent.semantic_calls = []
    CountingPipelineAgent.decompose_calls = 0
    CountingPipelineAgent.vet_calls = 0

    monkeypatch.setattr(api_main, "AgentService", CountingPipelineAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", CountingPipelineAgent)
    monkeypatch.setattr(workers_facade, "AgentService", CountingPipelineAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Pipeline progression",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"parallel_root_decompositions_n": 1, "parallel_root_take_k": 1},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    run = client.post(f"/v1/problems/{problem_id}/run")
    assert run.status_code == 200
    assert run.json()["status"] in {"created", "running", "succeeded"}

    for _ in range(20):
        if CountingPipelineAgent.decompose_calls >= 1 and CountingPipelineAgent.vet_calls >= 1:
            break
        next_run = client.post(f"/v1/problems/{problem_id}/run")
        assert next_run.status_code == 200
    assert CountingPipelineAgent.decompose_calls >= 1
    assert CountingPipelineAgent.vet_calls >= 1
    # Root theorem sketch at create + lemma semantic canonicalization during decomposition.
    assert len(CountingPipelineAgent.semantic_calls) >= 2
    assert "Lemma statement" in CountingPipelineAgent.semantic_calls

    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot?events_limit=100")
    assert snapshot.status_code == 200
    payload = snapshot.json()
    assert len(payload["decompositions"]) >= 1
    assert len(payload["lemmas"]) >= 1
