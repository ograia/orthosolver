from types import SimpleNamespace

from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.contracts import Agent5Output


def _agent5_output(
    *,
    proof_status: str,
    recommended_action: str,
    detailed_findings: list[dict[str, str]],
) -> Agent5Output:
    return Agent5Output(
        status="completed",
        lemma_id="lem_test",
        statement_status="plausible",
        proof_status=proof_status,
        drift_assessment={
            "drift_detected": False,
            "drift_severity": "none",
            "drift_description": None,
            "drift_type": None,
        },
        recommended_action=recommended_action,
        confidence=0.9,
        reason="test",
        feedback_for_solver=None,
        candidate_counterexample=None,
        detailed_findings=detailed_findings,
    )


def test_max_finding_severity_accepts_legacy_and_current_labels() -> None:
    assert Orchestrator._max_finding_severity([{"severity": "low"}]) == "low"
    assert Orchestrator._max_finding_severity([{"severity": "minor"}]) == "low"
    assert Orchestrator._max_finding_severity([{"severity": "medium"}]) == "medium"
    assert Orchestrator._max_finding_severity([{"severity": "fatal"}]) == "high"
    assert Orchestrator._max_finding_severity([{"severity": "critical"}]) == "high"
    assert Orchestrator._max_finding_severity([{"severity": "minor"}, {"severity": "high"}]) == "high"


def test_solver_feedback_synthesizes_from_description_with_code_and_location() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    report = SimpleNamespace(
        feedback_for_solver=None,
        detailed_findings=[
            {
                "description": "Missing induction base case.",
                "severity": "fatal",
                "code": "LEMMA_BASE_CASE",
                "location": "line 14",
            },
            {
                "finding": "Clarify notation in case split.",
                "severity": "minor",
                "location": "Case 2",
            },
        ],
        reason="fallback",
    )

    feedback = orch._solver_feedback_from_report(report)
    assert feedback is not None
    assert "Missing induction base case." in feedback
    assert "Clarify notation in case split." in feedback
    assert "[LEMMA_BASE_CASE]" in feedback
    assert "@ line 14" in feedback


def test_localized_gap_with_fatal_findings_stays_high_and_retries_solver() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    current = _agent5_output(
        proof_status="localized_gap",
        recommended_action="retry_solver",
        detailed_findings=[{"severity": "fatal", "description": "Core logical bridge missing."}],
    )
    prior = SimpleNamespace(
        proof_status="complete",
        recommended_action="retry_solver",
        detailed_findings=[{"severity": "high", "finding": "Prior major issue still unresolved."}],
    )

    assert orch._current_vetter_issue_severity(current) == "high"
    route, override = orch._apply_vetter_route_override(
        base_route="retry_solver",
        vet=current,
        prior_report=prior,
    )
    assert route == "retry_solver"
    assert override == "high_severity_retry"
