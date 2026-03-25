from __future__ import annotations
from fastapi.testclient import TestClient

from nl_engine.api.main import app
from tools.evaluator.run_eval import EvalCase, _build_summary, _to_markdown, run_case


def test_evaluator_case_and_report_generation() -> None:
    case = EvalCase(
        case_id="eval_case_1",
        title="Evaluator theorem",
        statement_nl="For all n, n = n",
        mode="nl_only",
        expected_terminal_status="succeeded",
        expected_verification_level="nl_only",
        expected_failure_reason=None,
        routing_invariants=["problem.start", "problem.succeeded"],
        config_overrides={},
    )
    with TestClient(app) as client:
        result = run_case(client, case, tick_limit=40, poll_sleep=0.0, database_url=None)
    assert result["passed"] is True
    summary = _build_summary([result])
    assert summary["total_cases"] == 1
    md = _to_markdown([result], summary)
    assert "Orthos Evaluator Report" in md
