from __future__ import annotations

from nl_engine.domain.config import ProblemConfig


def route_vetter_result(statement_status: str, proof_status: str, drift_level: str | None, config: ProblemConfig) -> str:
    """Route NL vetter output according to docs/nl_engine.tex routing table."""
    if drift_level == "major" and config.drift.major_drift_blocks_progress:
        return "blocked"
    if statement_status == "false":
        return "flag_suspected_false"
    if statement_status == "suspect":
        return "decompose_further"
    if statement_status == "plausible" and proof_status == "complete":
        return "send_to_lean"
    if statement_status == "plausible" and proof_status in {"localized_gap"}:
        return "retry_solver"
    if statement_status == "plausible" and proof_status in {"major_gap", "wrong_strategy"}:
        return "decompose_further"
    return "retry_solver"


def route_lean_result(
    status: str,
    error_class: str | None,
    config: ProblemConfig,
    issue_kind: str | None = None,
) -> str:
    """Route Lean job terminal result according to docs/nl_engine.tex."""
    if status == "success":
        return "done"

    if issue_kind == "lean_issue":
        if error_class == "assembly_composition_failure":
            return "retry_assembly_plan"
        return "retry_lean_only"

    if issue_kind == "proof_issue":
        if error_class == "false_lemma_suspected":
            return "check_statement_plausibility"
        if error_class in config.routing.repairable_nl_loop_classes:
            return "retry_nl_proof"
        return "decompose_further"

    if error_class in config.routing.repairable_lean_only_classes:
        return "retry_lean_only"

    if error_class in config.routing.repairable_nl_loop_classes:
        return "retry_nl_proof"

    if error_class == "false_lemma_suspected":
        return "check_statement_plausibility"

    if error_class == "assembly_composition_failure":
        return "retry_assembly_plan"

    if error_class == "bad_statement_translation":
        return "retry_lean_only"

    return "decompose_further"
