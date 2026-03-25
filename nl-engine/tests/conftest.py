from __future__ import annotations

import os
from typing import Any

import pytest

from nl_engine.domain.contracts import (
    Agent1Output,
    Agent2AssemblyPlan,
    Agent2Candidate,
    Agent2Lemma,
    Agent2Output,
    Agent3Output,
    Agent6Output,
    Agent4Output,
    Agent5Output,
    SemanticSketch,
)


class DeterministicTestAgentService:
    """Offline-safe deterministic agent used in tests when no OpenAI key exists."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def set_llm_overrides(self, llm_overrides) -> None:
        pass

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str) -> Agent1Output:
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=SemanticSketch(
                variables=[],
                quantifier_order=[],
                domain_restrictions=[],
                witness_dependencies=[],
                normalized_claim=statement_nl,
            ),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix: str, **kwargs) -> Agent2Output:
        candidates: list[Agent2Candidate] = []
        num_candidates = max(1, payload.num_candidates)
        for idx in range(num_candidates):
            local_id = f"L{idx + 1}"
            sketch = SemanticSketch(
                variables=[],
                quantifier_order=[],
                domain_restrictions=[],
                witness_dependencies=[],
                normalized_claim=payload.theorem_nl,
            )
            candidate = Agent2Candidate(
                candidate_index=idx,
                strategy_summary=f"deterministic strategy {idx + 1}",
                shared_context=[],
                lemmas=[
                    Agent2Lemma(
                        local_id=local_id,
                        statement_nl=f"{payload.theorem_nl} (lemma {idx + 1})",
                        semantic_sketch=sketch,
                        role_in_assembly="direct",
                        formalization_cost_estimate=0.2 + (0.1 * idx),
                        self_check_true=True,
                        self_check_notes="deterministic",
                    )
                ],
                assembly_plan=Agent2AssemblyPlan(
                    steps=[
                        {
                            "step_id": "A1",
                            "uses_lemmas": [local_id],
                            "uses_prior_steps": [],
                            "derives": payload.theorem_nl,
                            "is_trivial": True,
                            "trivial_justification": "lemma directly yields theorem",
                        }
                    ],
                    proof_skeleton_nl=f"Apply {local_id}.",
                    final_step_yields_exact_root=True,
                ),
                formalization_cost_estimate_total=0.2 + (0.1 * idx),
                drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
            )
            candidates.append(candidate)
        return Agent2Output(status="completed", candidates=candidates)

    def vet_decomposition(self, payload, artifact_prefix: str) -> Agent3Output:
        lemma_findings: list[dict[str, Any]] = []
        for lemma in payload.decomposition.get("lemmas", []):
            lemma_findings.append(
                {
                    "local_id": lemma.get("local_id", "L1"),
                    "statement_status": "plausible",
                    "evidence": "deterministic acceptance",
                    "counterexample": None,
                }
            )
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="deterministic acceptance",
            lemma_findings=lemma_findings,
            assembly_check={
                "verdict": "valid",
                "details": "deterministic",
                "hidden_steps_found": [],
                "final_step_matches_root": True,
            },
            coverage_check={"redundant_lemmas": [], "missing_coverage": [], "disguised_difficulty": []},
            drift_assessment={
                "drift_detected": False,
                "drift_severity": "none",
                "per_lemma_drift": [],
                "assembly_conclusion_matches_root": True,
            },
            formalization_risk="low",
            fixes_required=[],
            fatal_reason=None,
        )

    def solve_lemma(self, payload, artifact_prefix: str, **kwargs) -> Agent4Output:
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl=f"Proof of {payload.statement_nl}.",
            proof_summary="deterministic proof",
            self_report={
                "confidence": 0.9,
                "suspected_gaps": [],
                "used_external_facts": [],
                "every_step_justified": True,
                "proves_exactly_the_statement": True,
            },
            stuck_point=None,
            candidate_counterexample=None,
            addressed_previous_feedback=payload.previous_feedback,
        )

    def vet_lemma_proof(self, payload, artifact_prefix: str) -> Agent5Output:
        return Agent5Output(
            status="completed",
            lemma_id=payload.lemma_id,
            statement_status="plausible",
            proof_status="complete",
            drift_assessment={
                "drift_detected": False,
                "drift_severity": "none",
                "drift_description": None,
                "drift_type": None,
            },
            recommended_action="send_to_lean",
            confidence=0.95,
            reason="deterministic acceptance",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )

    def final_check(self, payload, artifact_prefix: str) -> Agent6Output:
        return Agent6Output(
            status="completed",
            verdict="approved",
            confidence=0.99,
            summary="deterministic approval",
            decomposition_findings=[],
            assembly_findings=[],
            lemma_findings=[],
        )


@pytest.fixture(autouse=True)
def _patch_agents_when_offline(monkeypatch):
    if os.getenv("OPENAI_API_KEY"):
        return

    from nl_engine.api import main as api_main
    from nl_engine.controller import orchestrator as orchestrator_module
    from nl_engine.execution import runtime as runtime_module
    from nl_engine.workers import facade as workers_facade

    monkeypatch.setattr(api_main, "AgentService", DeterministicTestAgentService)
    monkeypatch.setattr(orchestrator_module, "AgentService", DeterministicTestAgentService)
    monkeypatch.setattr(runtime_module, "AgentService", DeterministicTestAgentService)
    monkeypatch.setattr(workers_facade, "AgentService", DeterministicTestAgentService)


@pytest.fixture(autouse=True)
def _isolate_test_state(tmp_path):
    """Ensure each test starts with a clean data store and no stale supervisor."""
    from nl_engine.execution.runtime import stop_embedded_supervisor
    from nl_engine.api.run_state import reset_all_run_state
    from nl_engine.persistence.db import FileStore, reset_file_store
    from nl_engine.settings import get_settings

    stop_embedded_supervisor()
    reset_all_run_state()
    os.environ["NL_ENGINE_NO_AUTO_SUPERVISOR"] = "1"

    # Isolate data store to a temporary directory per test
    test_store = FileStore(str(tmp_path / "data"))
    reset_file_store(test_store)

    # Also isolate artifact store
    artifact_dir = str(tmp_path / ".artifacts")
    os.makedirs(artifact_dir, exist_ok=True)
    settings = get_settings()
    original_artifact_dir = settings.artifact_store_dir
    settings.artifact_store_dir = artifact_dir

    original_data_dir = settings.data_dir
    settings.data_dir = str(tmp_path / "data")

    yield

    stop_embedded_supervisor()
    reset_all_run_state()
    reset_file_store(None)
    settings.artifact_store_dir = original_artifact_dir
    settings.data_dir = original_data_dir
