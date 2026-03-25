from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.workers import facade as workers_facade
from nl_engine.domain.contracts import (
    Agent2AssemblyPlan,
    Agent2Candidate,
    Agent2Lemma,
    Agent2Output,
    Agent3Output,
    SemanticSketch,
)
from nl_engine.persistence.db import SessionLocal
from nl_engine.persistence.repositories import DecompositionRepository, LemmaRepository
from tests.conftest import DeterministicTestAgentService


class RejectThenAcceptDecompositionAgent(DeterministicTestAgentService):
    decompose_payloads: list[dict] = []
    vet_call_count = 0

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__()

    def decompose(self, payload, artifact_prefix: str, **kwargs) -> Agent2Output:
        self.__class__.decompose_payloads.append(payload.model_dump())
        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim=payload.theorem_nl,
        )
        candidate = Agent2Candidate(
            candidate_index=0,
            strategy_summary=(
                "retry strategy addresses prior vetter feedback"
                if payload.previous_attempt_summaries
                else "initial strategy"
            ),
            shared_context=[],
            lemmas=[
                Agent2Lemma(
                    local_id="L1",
                    statement_nl=f"{payload.theorem_nl} (candidate lemma)",
                    semantic_sketch=sketch,
                    role_in_assembly="direct",
                    formalization_cost_estimate=0.2,
                    self_check_true=True,
                    self_check_notes="deterministic",
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
                        "trivial_justification": "direct application",
                    }
                ],
                proof_skeleton_nl="Apply L1.",
                final_step_yields_exact_root=True,
            ),
            formalization_cost_estimate_total=0.2,
            drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
        )
        return Agent2Output(status="completed", candidates=[candidate])

    def vet_decomposition(self, payload, artifact_prefix: str) -> Agent3Output:
        self.__class__.vet_call_count += 1
        if self.__class__.vet_call_count == 1:
            return Agent3Output(
                status="completed",
                decision="minor_fix",
                summary="initial candidate rejected due to self-containment issue",
                lemma_findings=[
                    {
                        "local_id": "L1",
                        "statement_status": "plausible",
                        "evidence": "plausible",
                        "counterexample": None,
                    }
                ],
                assembly_check={
                    "verdict": "valid",
                    "details": "valid but needs refinement",
                    "hidden_steps_found": [],
                    "final_step_matches_root": True,
                },
                coverage_check={
                    "redundant_lemmas": [],
                    "missing_coverage": ["make lemma self-contained"],
                    "disguised_difficulty": [],
                },
                drift_assessment={
                    "drift_detected": False,
                    "drift_severity": "none",
                    "per_lemma_drift": [],
                    "assembly_conclusion_matches_root": True,
                },
                formalization_risk="low",
                fixes_required=["inline missing definition in lemma statement"],
                fatal_reason=None,
            )
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="retry candidate accepted",
            lemma_findings=[
                {
                    "local_id": "L1",
                    "statement_status": "plausible",
                    "evidence": "accepted",
                    "counterexample": None,
                }
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


def _run_to_terminal(client: TestClient, problem_id: str, max_ticks: int = 120) -> str:
    status = "created"
    for _ in range(max_ticks):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        status = run.json()["status"]
        if status in {"succeeded", "failed"}:
            break
    return status


def test_rejected_decomposition_lemmas_hidden_and_retry_feedback_propagates(monkeypatch) -> None:
    RejectThenAcceptDecompositionAgent.decompose_payloads = []
    RejectThenAcceptDecompositionAgent.vet_call_count = 0
    monkeypatch.setattr(api_main, "AgentService", RejectThenAcceptDecompositionAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", RejectThenAcceptDecompositionAgent)
    monkeypatch.setattr(workers_facade, "AgentService", RejectThenAcceptDecompositionAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
                "title": "lifecycle cleanup",
                "statement_nl": "For all n, n = n",
                "config": {
                    "mode": {"nl_only_mode": True},
                    "decomposition": {"parallel_root_decompositions_n": 1, "parallel_root_take_k": 1},
                },
            },
        )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first_tick = client.post(f"/v1/problems/{problem_id}/run")
    assert first_tick.status_code == 200

    for _ in range(20):
        with SessionLocal() as db:
            dec_rows = DecompositionRepository(db).list_by_problem(problem_id)
        if len(dec_rows) >= 1:
            break
        client.post(f"/v1/problems/{problem_id}/run")
    assert len(dec_rows) >= 1

    for _ in range(20):
        if len(RejectThenAcceptDecompositionAgent.decompose_payloads) >= 2:
            break
        next_tick = client.post(f"/v1/problems/{problem_id}/run")
        assert next_tick.status_code == 200

    assert len(RejectThenAcceptDecompositionAgent.decompose_payloads) >= 2
    second_decompose = RejectThenAcceptDecompositionAgent.decompose_payloads[1]
    previous = second_decompose.get("previous_attempt_summaries", [])
    assert previous
    assert previous[0].get("decision") == "minor_fix"
    assert "inline missing definition in lemma statement" in (previous[0].get("fixes_required") or [])
    assert "make lemma self-contained" in (previous[0].get("missing_coverage") or [])

    with SessionLocal() as db:
        dec_rows = DecompositionRepository(db).list_by_problem(problem_id)
        rejected_minor = [row for row in dec_rows if row.llm_vetting_status == "rejected_minor"]
        assert rejected_minor
        accepted = [row for row in dec_rows if row.llm_vetting_status == "accepted"]
        assert accepted
        assert accepted[0].lemma_ids
        lem_rows = LemmaRepository(db).list_by_problem(problem_id)
        assert len(lem_rows) == len(accepted[0].lemma_ids)

    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    accepted_ids = set()
    for dec in body["decompositions"]:
        if dec["llm_vetting_status"] == "accepted":
            accepted_ids.update(dec["lemma_ids"])
    assert set(body["visible_lemma_ids"]) == accepted_ids
    assert all(hidden_id not in accepted_ids for hidden_id in body["hidden_candidate_lemma_ids"])

    terminal = _run_to_terminal(client, problem_id)
    assert terminal == "succeeded"
    final_snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot")
    assert final_snapshot.status_code == 200
    final_output = final_snapshot.json()["nl_only_final_output"]
    assert final_output is not None
    assert final_output["root_theorem"]["statement_nl"] == "For all n, n = n"
    assert isinstance(final_output["lemmas"], list)
