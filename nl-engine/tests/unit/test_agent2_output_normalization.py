from __future__ import annotations

from nl_engine.domain.contracts import Agent2Input, Agent2Output
from nl_engine.services.agents import AgentService


def test_agent2_accepts_alternate_candidate_and_lemma_keys() -> None:
    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {
            "candidates": [
                {
                    "strategy": "alternate strategy key",
                    "lemmas": [
                        {
                            "name": "L_alt",
                            "statement": "For all n, n = n",
                            "semantic_sketch": {"normalized_claim": "For all n, n = n"},
                            "formalization_cost": 0.15,
                        }
                    ],
                    "assembly_plan": [
                        "By L_alt, derive the root theorem immediately.",
                    ],
                }
            ]
        }

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]
    out = service.decompose(
        Agent2Input(theorem_nl="For all n, n = n", root_semantic_sketch={}),
        "problems/p/decomposer",
    )

    assert out.status == "completed"
    assert len(out.candidates) == 1
    candidate = out.candidates[0]
    assert candidate.candidate_index == 0
    assert candidate.strategy_summary == "alternate strategy key"
    assert len(candidate.lemmas) == 1
    lemma = candidate.lemmas[0]
    assert lemma.local_id == "L_alt"
    assert lemma.statement_nl == "For all n, n = n"
    assert lemma.formalization_cost_estimate == 0.15
    assert lemma.self_check_true is True
    assert candidate.assembly_plan.final_step_yields_exact_root is True
    assert len(candidate.assembly_plan.steps) == 1


def test_agent2_defaults_missing_fields_without_validation_error() -> None:
    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {"candidates": [{}]}

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]
    out = service.decompose(
        Agent2Input(theorem_nl="Root theorem", root_semantic_sketch={}),
        "problems/p/decomposer",
    )

    assert out.status == "completed"
    assert len(out.candidates) == 1
    candidate = out.candidates[0]
    assert candidate.strategy_summary == ""
    assert candidate.formalization_cost_estimate_total >= 0.0


def test_agent2_normalizes_shared_context_and_candidate_index() -> None:
    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {
            "candidates": [
                {
                    "candidate_index": "7",
                    "strategy_summary": "strategy",
                    "shared_context": ["ctx A", {"name": "ctx_b", "value": "ctx B"}],
                    "lemmas": [
                        {
                            "local_id": "L1",
                            "statement_nl": "For all n, n = n",
                            "semantic_sketch": {"normalized_claim": "For all n, n = n"},
                            "role_in_assembly": "direct",
                            "formalization_cost_estimate": 0.1,
                            "self_check_true": True,
                            "self_check_notes": "ok",
                        }
                    ],
                    "assembly_plan": {"steps": [], "proof_skeleton_nl": "", "final_step_yields_exact_root": True},
                    "formalization_cost_estimate_total": 0.1,
                    "drift_self_check": {},
                }
            ]
        }

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]
    out = service.decompose(
        Agent2Input(theorem_nl="For all n, n = n", root_semantic_sketch={}),
        "problems/p/decomposer",
    )

    candidate = out.candidates[0]
    assert candidate.candidate_index == 7
    assert candidate.shared_context[0]["label"] == "context_1"
    assert candidate.shared_context[0]["content"] == "ctx A"
    assert candidate.shared_context[1]["label"] == "ctx_b"
    assert candidate.shared_context[1]["content"] == "ctx B"


def test_normalize_semantic_sketch_coerces_variable_strings_to_objects() -> None:
    service = AgentService()
    sketch = service._normalize_semantic_sketch(
        {
            "variables": ["x", "y"],
            "quantifier_order": ["for all x"],
            "domain_restrictions": ["x is an integer"],
            "witness_dependencies": ["y depends on x"],
            "normalized_claim": "sample claim",
        }
    )
    assert sketch["variables"] == [{"name": "x"}, {"name": "y"}]


def test_agent2_normalization_accepts_string_variable_lists() -> None:
    service = AgentService()
    raw = {
        "candidates": [
            {
                "candidate_index": 0,
                "strategy_summary": "test strategy",
                "shared_context": [],
                "lemmas": [
                    {
                        "local_id": "L1",
                        "statement_nl": "For all x, x = x",
                        "semantic_sketch": {
                            "variables": ["x"],
                            "quantifier_order": ["for all x"],
                            "domain_restrictions": ["x is any object"],
                            "witness_dependencies": [],
                            "normalized_claim": "for all x, x = x",
                        },
                        "role_in_assembly": "direct",
                        "formalization_cost_estimate": 0.1,
                        "self_check_true": True,
                        "self_check_notes": "basic identity",
                    }
                ],
                "assembly_plan": {
                    "steps": [
                        {
                            "step_id": "A1",
                            "uses_lemmas": ["L1"],
                            "uses_prior_steps": [],
                            "derives": "For all x, x = x",
                            "is_trivial": True,
                            "trivial_justification": "identity lemma",
                        }
                    ],
                    "proof_skeleton_nl": "By L1 we are done.",
                    "final_step_yields_exact_root": True,
                },
                "formalization_cost_estimate_total": 0.1,
                "drift_self_check": {
                    "all_lemmas_consistent_with_root_sketch": True,
                    "inconsistencies_noted": [],
                },
            }
        ]
    }

    normalized = service._normalize_agent2_output(raw, theorem_nl="For all x, x = x")
    output = Agent2Output.model_validate(normalized)
    assert output.candidates[0].lemmas[0].semantic_sketch.variables == [{"name": "x"}]
