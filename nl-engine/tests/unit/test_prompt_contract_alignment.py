from __future__ import annotations

from pathlib import Path


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
