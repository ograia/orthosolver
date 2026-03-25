from __future__ import annotations

from nl_engine.domain.contracts import Agent3Output, Agent5Output
from nl_engine.services.agents import AgentService


def test_agent3_check_style_output_normalizes_to_contract() -> None:
    service = AgentService()
    raw = {
        "check_1_individual_lemma_validity": {
            "L1": {"status": "TRUE", "reason": "looks valid"},
            "L2": {"status": "SUSPECT", "reason": "might have a gap"},
        },
        "check_2_assembly_skeleton_completeness": {
            "status": "COMPLETE",
            "reason": "skeleton closes the proof",
            "minor_notes": ["optional tightening"],
        },
        "check_3_coverage_and_redundancy": {
            "redundant_lemmas": [],
            "missing_pieces": [],
            "lemmas_as_hard_as_original": [],
            "overall_assessment": "good coverage",
        },
        "check_4_semantic_drift": {
            "L1": {"drift": "MINOR", "reason": "equivalent restatement"},
            "L2": {"drift": "MINOR", "reason": "equivalent restatement"},
        },
        "overall_verdict": {"soundness": "PLAUSIBLY_CORRECT", "main_issues": []},
    }

    normalized = service._normalize_agent3_output(raw)
    output = Agent3Output.model_validate(normalized)

    assert output.status == "completed"
    assert output.decision == "minor_fix"
    assert output.assembly_check["verdict"] == "valid"
    assert output.drift_assessment["drift_severity"] == "minor"
    assert len(output.lemma_findings) == 2


def test_agent3_and_agent5_contracts_coerce_string_findings_lists() -> None:
    agent3 = Agent3Output.model_validate(
        {
            "status": "completed",
            "decision": "fatal",
            "summary": "needs cleanup",
            "lemma_findings": ["lemma L1 is suspect"],
            "assembly_check": {},
            "coverage_check": {},
            "drift_assessment": {},
            "formalization_risk": "medium",
            "fixes_required": [],
            "fatal_reason": "fatal",
            "equivalence_findings": ["stronger than parent"],
            "context_purity_findings": ["used undeclared fact"],
        }
    )
    assert agent3.lemma_findings == [{"finding": "lemma L1 is suspect"}]
    assert agent3.equivalence_findings == [{"finding": "stronger than parent"}]
    assert agent3.context_purity_findings == [{"finding": "used undeclared fact"}]

    agent5 = Agent5Output.model_validate(
        {
            "status": "completed",
            "lemma_id": "lem_test",
            "statement_status": "plausible",
            "proof_status": "localized_gap",
            "drift_assessment": {},
            "recommended_action": "retry_solver",
            "confidence": 0.8,
            "reason": "retry",
            "feedback_for_solver": None,
            "candidate_counterexample": None,
            "detailed_findings": ["bridge step missing"],
            "undeclared_citation_findings": ["citation missing"],
            "forbidden_dependency_findings": ["used parent claim"],
        }
    )
    assert agent5.detailed_findings == [{"finding": "bridge step missing"}]
    assert agent5.undeclared_citation_findings == [{"finding": "citation missing"}]
    assert agent5.forbidden_dependency_findings == [{"finding": "used parent claim"}]
