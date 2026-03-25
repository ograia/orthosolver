from __future__ import annotations

import json
from pathlib import Path

import pytest

from nl_engine.domain.contracts import Agent4Input
from nl_engine.services.agents import AgentExecutionError, AgentService
from nl_engine.settings import get_settings


def test_agent4_validation_error_is_persisted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    get_settings.cache_clear()

    service = AgentService()

    def _fake_run_json_agent(**kwargs):
        return {}

    service._run_json_agent = _fake_run_json_agent  # type: ignore[method-assign]

    with pytest.raises(AgentExecutionError) as exc_info:
        service.solve_lemma(
            Agent4Input(
                lemma_id="lem_1",
                statement_nl="For all n, n = n",
                semantic_sketch={},
                root_theorem_nl="For all n, n = n",
                root_semantic_sketch={},
                role_in_assembly="direct",
                shared_context=[],
                trusted_context_summaries=[],
                previous_feedback=None,
                attempt_number=1,
            ),
            "problems/p/lemmas/lem_1",
        )

    assert exc_info.value.error_class == "invalid_agent_output"
    payload = json.loads((tmp_path / "problems/p/lemmas/lem_1/agent4_validation_error.json").read_text())
    assert payload["error_class"] == "invalid_agent_output"
    assert payload["exception_type"]
    assert "normalized_payload" in payload

    get_settings.cache_clear()
