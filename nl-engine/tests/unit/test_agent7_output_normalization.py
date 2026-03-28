from __future__ import annotations

from nl_engine.domain.contracts import Agent7Input
from nl_engine.services.agents import AgentService


def test_agent7_normalizes_textual_context_and_assembly_steps() -> None:
    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {
            "decision": "split",
            "strategy_summary": "Split the parent proof into two reusable facts.",
            "shared_context": ["floor facts"],
            "context_items": ["reassembly note"],
            "lemmas": [
                {
                    "local_id": "L1",
                    "statement_nl": "For every real number x, floor(x) is the greatest integer less than or equal to x.",
                    "semantic_sketch": {"normalized_claim": "floor(x) is the greatest integer <= x."},
                    "role_in_assembly": "identify the floor characterization",
                    "lemma_relation_to_parent": "bottleneck",
                    "formalization_cost_estimate": 0.2,
                    "self_check_true": True,
                    "self_check_notes": "matches the parent proof",
                    "proof_nl": "This is the defining property of floor.",
                }
            ],
            "assembly_plan": {
                "steps": [
                    "Apply L1 to identify floor(x) as the greatest integer <= x.",
                    "Conclude floor(x) <= x.",
                ],
                "proof_skeleton_nl": "",
                "final_step_yields_exact_root": True,
            },
            "parent_reassembly_explanation": "Apply the child lemma and read off the desired inequality.",
            "confidence": 0.8,
            "summary": "The split keeps the original proof structure.",
        }

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]
    output = service.split_existing_proof(
        Agent7Input(
            lemma_id="lem_floor",
            parent_statement_nl="For every real number x, floor(x) <= x.",
            parent_semantic_sketch={},
            parent_proof_nl="By the floor characterization, floor(x) <= x.",
        ),
        "problems/p/split_existing_proof",
    )

    assert output.status == "completed"
    assert output.shared_context[0]["content"] == "floor facts"
    assert output.context_items[0]["content"] == "reassembly note"
    assert [step["derives"] for step in output.assembly_plan.steps] == [
        "Apply L1 to identify floor(x) as the greatest integer <= x.",
        "Conclude floor(x) <= x.",
    ]
    assert output.assembly_plan.proof_skeleton_nl == (
        "Apply L1 to identify floor(x) as the greatest integer <= x.\n"
        "Conclude floor(x) <= x."
    )
