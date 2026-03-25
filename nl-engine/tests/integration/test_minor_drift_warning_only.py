"""Tests that minor_drift_adds_warning_only config is respected.

Bug report: The config field drift.minor_drift_adds_warning_only is
defined in DriftConfig but never consulted by the orchestrator's vetting
logic.  Decompositions with a vetter decision of "minor_fix" are always
marked rejected_minor / controller_status=FAILED, regardless of this
config setting.

When minor_drift_adds_warning_only=True, a "minor_fix" vetter decision
should still accept the decomposition (with a drift warning event), not
reject it.
"""

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
from nl_engine.services.agents import AgentService as BaseAgentService
from nl_engine.workers import facade as workers_facade


def _make_sketch(statement_nl: str) -> SemanticSketch:
    return SemanticSketch(
        variables=[],
        quantifier_order=[],
        domain_restrictions=[],
        witness_dependencies=[],
        normalized_claim=statement_nl,
    )


class MinorFixVetterAgent(BaseAgentService):
    """Agent whose vetter always returns decision='minor_fix' with minor
    drift severity.  Used to verify that the config setting
    minor_drift_adds_warning_only=True causes the decomposition to be
    accepted despite the minor_fix vetter decision.
    """

    def __init__(self, *args, **kwargs):
        pass

    def set_llm_overrides(self, llm_overrides) -> None:
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=_make_sketch(statement_nl),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix, **kwargs):
        sketch = _make_sketch(payload.theorem_nl)
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="minor-fix candidate",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl=f"{payload.theorem_nl} helper",
                            semantic_sketch=sketch,
                            role_in_assembly="direct",
                            formalization_cost_estimate=0.1,
                            self_check_true=True,
                            self_check_notes="ok",
                        )
                    ],
                    assembly_plan=Agent2AssemblyPlan(
                        steps=[{
                            "step_id": "A1",
                            "uses_lemmas": ["L1"],
                            "uses_prior_steps": [],
                            "derives": payload.theorem_nl,
                            "is_trivial": True,
                            "trivial_justification": "direct",
                        }],
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

    def vet_decomposition(self, payload, artifact_prefix):
        """Returns minor_fix decision with minor drift — the key scenario."""
        return Agent3Output(
            status="completed",
            decision="minor_fix",
            summary="Minor formalism issues; mathematically sound.",
            lemma_findings=[
                {
                    "local_id": lemma.get("local_id", "L1"),
                    "statement_status": "plausible",
                    "evidence": "ok",
                    "counterexample": None,
                }
                for lemma in payload.decomposition.get("lemmas", [])
            ],
            assembly_check={
                "verdict": "valid",
                "details": "ok",
                "hidden_steps_found": [],
                "final_step_matches_root": True,
            },
            coverage_check={"redundant_lemmas": [], "missing_coverage": [], "disguised_difficulty": []},
            drift_assessment={
                "drift_detected": True,
                "drift_severity": "minor",
                "per_lemma_drift": [],
                "assembly_conclusion_matches_root": True,
            },
            formalization_risk="low",
            fixes_required=["Step A4 should explicitly state the induction hypothesis."],
            fatal_reason=None,
        )

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl=f"Proof for {payload.statement_nl}",
            proof_summary="proof",
            self_report={
                "confidence": 0.9,
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
            summary="deterministic approval",
            decomposition_findings=[],
            assembly_findings=[],
            lemma_findings=[],
        )


def test_minor_fix_accepted_when_warning_only_enabled(monkeypatch) -> None:
    """When minor_drift_adds_warning_only=True (the default), a decomposition
    with vetter decision='minor_fix' should be ACCEPTED with a drift warning,
    not rejected.  The problem should succeed.
    """
    monkeypatch.setattr(api_main, "AgentService", MinorFixVetterAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", MinorFixVetterAgent)
    monkeypatch.setattr(workers_facade, "AgentService", MinorFixVetterAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Minor drift warning only",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "drift": {"minor_drift_adds_warning_only": True},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(40):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded", (
        "Problem failed but should have succeeded — minor_fix decomposition "
        "should be accepted when minor_drift_adds_warning_only=True."
    )

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stages = [e["stage"] for e in events.json()["events"]]
    # Should see a drift warning but the decomposition should be accepted
    assert "decomposition.drift_warning" in stages
    assert "decomposition.generated" in stages
    # The generated event should show accepted, not rejected_minor
    gen_events = [e for e in events.json()["events"] if e["stage"] == "decomposition.generated"]
    assert len(gen_events) >= 1
    assert gen_events[0]["new_status"] == "accepted"


def test_minor_fix_rejected_when_warning_only_disabled(monkeypatch) -> None:
    """When minor_drift_adds_warning_only=False, a decomposition with vetter
    decision='minor_fix' should be REJECTED as before.
    """
    monkeypatch.setattr(api_main, "AgentService", MinorFixVetterAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", MinorFixVetterAgent)
    monkeypatch.setattr(workers_facade, "AgentService", MinorFixVetterAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Minor drift rejected",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "drift": {"minor_drift_adds_warning_only": False},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(40):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "failed"

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    gen_events = [e for e in events.json()["events"] if e["stage"] == "decomposition.generated"]
    assert len(gen_events) >= 1
    assert gen_events[0]["new_status"] == "rejected_minor"
