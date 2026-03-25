from __future__ import annotations

from nl_engine.services.agents import AgentService


def test_agent1_accepts_bare_semantic_sketch_output() -> None:
    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {
            "variables": [{"name": "n", "type": "int", "domain": "integers"}],
            "quantifier_order": [{"quantifier": "forall", "variable": "n"}],
            "domain_restrictions": ["n is integer"],
            "witness_dependencies": [{"witness": "N", "depends_on": ["k", "d"]}],
            "normalized_claim": "For all n, n = n.",
        }

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]
    out = service.semantic_sketch("For all n, n = n.", "problems/p/inputs")

    assert out.status == "completed"
    assert out.statement_nl_received == "For all n, n = n."
    assert out.semantic_sketch.normalized_claim
    assert all(isinstance(item, str) for item in out.semantic_sketch.quantifier_order)
    assert all(isinstance(item, str) for item in out.semantic_sketch.witness_dependencies)


def test_agent1_accepts_full_envelope_and_normalizes_lists() -> None:
    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {
            "status": "completed",
            "statement_nl_received": "x",
            "semantic_sketch": {
                "variables": [],
                "quantifier_order": ["forall x"],
                "domain_restrictions": ["x in X"],
                "witness_dependencies": [{"witness": "y", "depends_on": ["x"]}],
                "normalized_claim": "forall x, ...",
            },
            "implicit_assumptions_surfaced": [{"assumption": "X nonempty"}],
            "ambiguities": [{"note": "none"}],
        }

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]
    out = service.semantic_sketch("x", "problems/p/inputs")

    assert out.status == "completed"
    assert out.statement_nl_received == "x"
    assert all(isinstance(item, str) for item in out.implicit_assumptions_surfaced)
    assert all(isinstance(item, str) for item in out.ambiguities)
    assert all(isinstance(item, str) for item in out.semantic_sketch.witness_dependencies)
