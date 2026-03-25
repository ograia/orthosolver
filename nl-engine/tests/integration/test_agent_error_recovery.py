"""Tests for agent error recovery — verifies that agent-level errors
(e.g. invalid JSON from Agent1 during lemma semantic sketch) do NOT
kill the entire problem but instead reject the failing decomposition
candidate and allow the orchestrator to continue trying alternatives.

Bug report: prob_20260316030120_87162b68 failed because Agent1 returned
invalid JSON for a single lemma's semantic sketch during decomposition
materialization.  Instead of rejecting that candidate and trying the
next, the exception bubbled up to run_once()'s top-level handler which
marked the entire problem as failed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.contracts import (
    Agent1Output,
    Agent2AssemblyPlan,
    Agent2Candidate,
    Agent2Lemma,
    Agent2Output,
    Agent3Output,
    Agent4Output,
    Agent5Output,
    Agent6Output,
    SemanticSketch,
)
from nl_engine.persistence.db import SessionLocal
from nl_engine.persistence.repositories import DecompositionRepository, ProblemRepository
from nl_engine.services.agents import AgentExecutionError, AgentService as BaseAgentService
from nl_engine.workers import facade as workers_facade


_SKETCH = SemanticSketch(
    variables=[],
    quantifier_order=[],
    domain_restrictions=[],
    witness_dependencies=[],
    normalized_claim="placeholder",
)


def _make_sketch(statement_nl: str) -> SemanticSketch:
    return SemanticSketch(
        variables=[],
        quantifier_order=[],
        domain_restrictions=[],
        witness_dependencies=[],
        normalized_claim=statement_nl,
    )


class Agent1FailsOnFirstLemmaSketch(BaseAgentService):
    """Agent1 fails with invalid_agent_output the first time it's asked
    for a lemma semantic sketch (during decomposition materialization),
    but succeeds on subsequent calls.  This simulates the exact failure
    from prod: a single agent parse error should NOT kill the problem.
    """

    sketch_call_count = 0

    def __init__(self, *args, **kwargs):
        pass

    def set_llm_overrides(self, llm_overrides) -> None:
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
        self.__class__.sketch_call_count += 1
        # First call is the root theorem sketch (always succeeds).
        # The first *lemma* sketch call (call #2) should fail.
        if self.__class__.sketch_call_count == 2:
            raise AgentExecutionError(
                agent_key="agent1",
                error_class="invalid_agent_output",
                message="Agent output was not valid JSON after one retry",
                artifact_prefix=artifact_prefix,
                parse_error_artifact=f"{artifact_prefix}/agent1_parse_error.json",
            )
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=_make_sketch(statement_nl),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix, **kwargs):
        sketch = _make_sketch(payload.theorem_nl)
        candidates = []
        for idx in range(max(1, payload.num_candidates)):
            local_id = f"L{idx + 1}"
            candidates.append(
                Agent2Candidate(
                    candidate_index=idx,
                    strategy_summary=f"candidate-{idx}",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id=local_id,
                            statement_nl=f"{payload.theorem_nl} lemma {idx}",
                            semantic_sketch=sketch,
                            role_in_assembly="direct",
                            formalization_cost_estimate=0.1,
                            self_check_true=True,
                            self_check_notes="ok",
                        )
                    ],
                    assembly_plan=Agent2AssemblyPlan(
                        steps=[{
                            "step_id": "A1",
                            "uses_lemmas": [local_id],
                            "uses_prior_steps": [],
                            "derives": payload.theorem_nl,
                            "is_trivial": True,
                            "trivial_justification": "direct",
                        }],
                        proof_skeleton_nl="Apply lemma.",
                        final_step_yields_exact_root=True,
                    ),
                    formalization_cost_estimate_total=0.1,
                    drift_self_check={
                        "all_lemmas_consistent_with_root_sketch": True,
                        "inconsistencies_noted": [],
                    },
                ),
            )
        return Agent2Output(status="completed", candidates=candidates)

    def vet_decomposition(self, payload, artifact_prefix):
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[
                {
                    "local_id": lemma.get("local_id", "L1"),
                    "statement_status": "plausible",
                    "evidence": "ok",
                    "counterexample": None,
                }
                for lemma in payload.decomposition.get("lemmas", [])
            ],
            assembly_check={
                "verdict": "valid",
                "details": "ok",
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

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        return Agent4Output(
            status="proved",
            lemma_id=payload.lemma_id,
            attempt_number=payload.attempt_number,
            proof_nl=f"Proof for {payload.statement_nl}",
            proof_summary="proof",
            self_report={
                "confidence": 0.9,
                "suspected_gaps": [],
                "used_external_facts": [],
                "every_step_justified": True,
                "proves_exactly_the_statement": True,
            },
            stuck_point=None,
            candidate_counterexample=None,
            addressed_previous_feedback=None,
        )

    def vet_lemma_proof(self, payload, artifact_prefix):
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
            reason="accepted",
            feedback_for_solver=None,
            candidate_counterexample=None,
            detailed_findings=[],
        )

    def final_check(self, payload, artifact_prefix):
        return Agent6Output(
            status="completed",
            verdict="approved",
            confidence=0.99,
            summary="deterministic approval",
            decomposition_findings=[],
            assembly_findings=[],
            lemma_findings=[],
        )


def test_agent1_parse_error_during_materialization_does_not_kill_problem(monkeypatch) -> None:
    """Bug 1 reproducer: When Agent1 fails with invalid_agent_output during
    lemma semantic sketch generation inside _materialize_decomposition_candidates,
    the decomposition candidate should be skipped — not the entire problem killed.

    With 2 candidates, if the first fails Agent1 parse, the second should still
    be tried and the problem should eventually succeed.
    """
    monkeypatch.setattr(api_main, "AgentService", Agent1FailsOnFirstLemmaSketch)
    monkeypatch.setattr(orchestrator_module, "AgentService", Agent1FailsOnFirstLemmaSketch)
    monkeypatch.setattr(workers_facade, "AgentService", Agent1FailsOnFirstLemmaSketch)
    Agent1FailsOnFirstLemmaSketch.sketch_call_count = 0

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Agent1 parse error recovery",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 1,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(40):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    # The problem should NOT have failed due to a single agent parse error.
    # It should have skipped the failing candidate and continued.
    assert terminal == "succeeded", (
        "Problem failed due to agent parse error on a single candidate. "
        "Expected the orchestrator to skip the failing candidate."
    )

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stages = [e["stage"] for e in events.json()["events"]]
    # Must NOT see the fatal event
    assert "agent.infrastructure_fatal" not in stages

    with SessionLocal() as db:
        dec_rows = DecompositionRepository(db).list_by_problem(problem_id)
    rejected = [row for row in dec_rows if row.failure_origin == "agent1:invalid_agent_output"]
    assert rejected
    assert rejected[0].raw_candidate_artifact_id is not None
    assert rejected[0].failure_reason is not None


class Agent1AlwaysFailsOnLemmaSketch(BaseAgentService):
    """Agent1 always fails on lemma sketches — all candidates should be
    rejected, then the problem should fail gracefully (not via
    agent.infrastructure_fatal but via proof_exhausted).
    """

    def __init__(self, *args, **kwargs):
        pass

    def set_llm_overrides(self, llm_overrides) -> None:
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
        # Root theorem sketch succeeds; lemma sketches (which have 'lemmas/' in path) fail.
        if "/lemmas/" in artifact_prefix:
            raise AgentExecutionError(
                agent_key="agent1",
                error_class="invalid_agent_output",
                message="Agent output was not valid JSON after one retry",
                artifact_prefix=artifact_prefix,
                parse_error_artifact=f"{artifact_prefix}/agent1_parse_error.json",
            )
        return Agent1Output(
            status="completed",
            statement_nl_received=statement_nl,
            semantic_sketch=_make_sketch(statement_nl),
            implicit_assumptions_surfaced=[],
            ambiguities=[],
        )

    def decompose(self, payload, artifact_prefix, **kwargs):
        sketch = _make_sketch(payload.theorem_nl)
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="only candidate",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl=f"{payload.theorem_nl} lemma",
                            semantic_sketch=sketch,
                            role_in_assembly="direct",
                            formalization_cost_estimate=0.1,
                            self_check_true=True,
                            self_check_notes="ok",
                        )
                    ],
                    assembly_plan=Agent2AssemblyPlan(
                        steps=[{
                            "step_id": "A1",
                            "uses_lemmas": ["L1"],
                            "uses_prior_steps": [],
                            "derives": payload.theorem_nl,
                            "is_trivial": True,
                            "trivial_justification": "direct",
                        }],
                        proof_skeleton_nl="Apply L1.",
                        final_step_yields_exact_root=True,
                    ),
                    formalization_cost_estimate_total=0.1,
                    drift_self_check={
                        "all_lemmas_consistent_with_root_sketch": True,
                        "inconsistencies_noted": [],
                    },
                )
            ],
        )

    def vet_decomposition(self, payload, artifact_prefix):
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[{"local_id": "L1", "statement_status": "plausible"}],
            assembly_check={
                "verdict": "valid",
                "details": "ok",
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


def test_all_candidates_fail_agent1_sketch_fails_gracefully(monkeypatch) -> None:
    """When ALL candidates fail Agent1 lemma sketch, the problem should fail
    via normal proof exhaustion, NOT via agent.infrastructure_fatal.
    """
    monkeypatch.setattr(api_main, "AgentService", Agent1AlwaysFailsOnLemmaSketch)
    monkeypatch.setattr(orchestrator_module, "AgentService", Agent1AlwaysFailsOnLemmaSketch)
    monkeypatch.setattr(workers_facade, "AgentService", Agent1AlwaysFailsOnLemmaSketch)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "All candidates fail sketch",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 1,
                    "max_consecutive_fatal_rejections_per_node": 2,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(40):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "failed"

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stages = [e["stage"] for e in events.json()["events"]]
    # Should fail via normal exhaustion, NOT via infrastructure_fatal
    assert "agent.infrastructure_fatal" not in stages


def test_agent1_failure_preserves_full_candidate_for_retry_feedback(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", Agent1FailsOnFirstLemmaSketch)
    monkeypatch.setattr(orchestrator_module, "AgentService", Agent1FailsOnFirstLemmaSketch)
    monkeypatch.setattr(workers_facade, "AgentService", Agent1FailsOnFirstLemmaSketch)
    Agent1FailsOnFirstLemmaSketch.sketch_call_count = 0

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Agent1 failure preserves candidate",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 1,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200

    with SessionLocal() as db:
        dec_rows = DecompositionRepository(db).list_by_problem(problem_id)
    failed_rows = [row for row in dec_rows if row.failure_origin == "agent1:invalid_agent_output"]
    assert failed_rows
    failed = failed_rows[0]
    assert failed.raw_candidate_artifact_id is not None

    artifact_resp = client.get(f"/v1/debug/problems/{problem_id}/artifacts?limit=2000")
    assert artifact_resp.status_code == 200
    artifact_keys = {row["artifact_key"] for row in artifact_resp.json()["artifacts"]}
    assert failed.raw_candidate_artifact_id in artifact_keys

    with SessionLocal() as db:
        problem = ProblemRepository(db).get(problem_id)
        assert problem is not None
        theorem_id = problem.root_theorem_id
        assert theorem_id is not None
        previous = Orchestrator(db)._previous_attempt_summaries(problem_id, theorem_id)

    preserved_failure = None
    for prev in previous:
        if prev.get("failure_origin") == "agent1:invalid_agent_output":
            preserved_failure = prev
            break

    assert preserved_failure is not None
    full_candidate = preserved_failure.get("full_candidate") or {}
    lemmas = full_candidate.get("lemmas") or []
    assert lemmas
    assert lemmas[0].get("statement_nl")
    assert "Retry the same candidate before replacing the decomposition strategy." in (
        preserved_failure.get("rejection_reason") or ""
    )
