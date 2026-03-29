from __future__ import annotations

from pathlib import Path

from nl_engine.domain.contracts import Agent2Input
from nl_engine.services.agents import AgentService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _prompt_text(name: str) -> str:
    return (REPO_ROOT / "prompts" / name / "system.txt").read_text()


def test_agent2_prompt_mentions_context_items() -> None:
    prompt = _prompt_text("agent2_decomposer")

    assert '"context_items": [' in prompt
    assert "context_items` should mirror `shared_context`" in prompt


def test_agent4_prompt_declares_structured_citations_and_counterexample_flag() -> None:
    prompt = _prompt_text("agent4_lemma_solver")

    assert '"counterexample_flag": {' in prompt
    assert '"citations": [' in prompt
    assert '"citation_kind": "definition | trusted_decl | claim"' in prompt
    assert '"item_id": "string"' in prompt
    assert '"label": "string"' in prompt
    assert '"detail": "string or null"' in prompt
    assert '"cited_text": "string or null"' in prompt
    assert "citations must be a list of OBJECTS, never strings." in prompt


def test_agent5_prompt_declares_counterexample_status() -> None:
    prompt = _prompt_text("agent5_lemma_vetter")

    assert '"counterexample_status": "accepted | rejected | undetermined | null"' in prompt
    assert 'If vetting_mode = "counterexample"' in prompt
    assert 'If vetting_mode = "proof" and the submitted proof is empty' in prompt
    assert 'you MUST actively test small admissible examples and edge cases' in prompt


def test_agent2_retry_prompt_surfaces_hard_negative_constraints() -> None:
    service = AgentService.__new__(AgentService)
    service._prompt = lambda agent_key: "base prompt"

    prompt = service._agent2_retry_prompt_override(
        Agent2Input(
            theorem_nl="For all n, n = n",
            root_semantic_sketch={},
            shared_context=[],
            num_candidates=1,
            previous_attempt_summaries=[
                {
                    "strategy_summary": "bad split",
                    "hard_negative_constraints": [
                        "Do not reuse the previous decomposition pattern around child lemma L1.",
                    ],
                    "false_lemma_findings": [
                        {
                            "local_id": "L1",
                            "candidate_counterexample": "n = 1 violates the child claim",
                        }
                    ],
                    "invalidating_counterexample": {
                        "counterexample_text": "n = 1 violates the child claim",
                        "summary": "fails immediately",
                    },
                }
            ],
            trusted_context_summaries=[],
        )
    )

    assert prompt is not None
    assert "HARD NEGATIVE CONSTRAINTS FROM PREVIOUS FALSE ATTEMPTS" in prompt
    assert "n = 1 violates the child claim" in prompt
    assert "Do not reuse the previous decomposition pattern" in prompt
