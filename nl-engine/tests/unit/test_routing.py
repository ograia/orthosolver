from nl_engine.domain.config import ProblemConfig
from nl_engine.routing.policy import route_lean_result, route_vetter_result


def test_vetter_major_drift_blocks() -> None:
    cfg = ProblemConfig()
    assert route_vetter_result("plausible", "complete", "major", cfg) == "blocked"


def test_vetter_complete_routes_to_lean() -> None:
    cfg = ProblemConfig()
    assert route_vetter_result("plausible", "complete", None, cfg) == "send_to_lean"


def test_vetter_suspect_routes_decompose() -> None:
    cfg = ProblemConfig()
    assert route_vetter_result("suspect", "complete", None, cfg) == "decompose_further"


def test_lean_routing_classes() -> None:
    cfg = ProblemConfig()
    assert route_lean_result("repairable", "syntax", cfg) == "retry_lean_only"
    assert route_lean_result("repairable", "tactic_failure", cfg) == "retry_nl_proof"
    assert route_lean_result("fatal", "false_lemma_suspected", cfg) == "check_statement_plausibility"
