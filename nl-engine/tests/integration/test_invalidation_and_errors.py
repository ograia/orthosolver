from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.domain.contracts import (
    Agent1Output,
    Agent2AssemblyPlan,
    Agent2Candidate,
    Agent2Lemma,
    Agent2Output,
    Agent3Output,
    Agent4Output,
    Agent5Output,
    SemanticSketch,
)
from nl_engine.services.agents import AgentExecutionError, AgentService as BaseAgentService
from nl_engine.workers import facade as workers_facade


class NLInvalidationAgent(BaseAgentService):
    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
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

    def decompose(self, payload, artifact_prefix, **kwargs):
        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim=payload.theorem_nl,
        )
        candidates: list[Agent2Candidate] = []
        for idx in range(2):
            local_id = f"L{idx + 1}"
            candidates.append(
                Agent2Candidate(
                    candidate_index=idx,
                    strategy_summary=f"candidate-{idx + 1}",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id=local_id,
                            statement_nl=f"{payload.theorem_nl} variant {idx + 1}",
                            semantic_sketch=sketch,
                            role_in_assembly="direct",
                            formalization_cost_estimate=0.1 + idx,
                            self_check_true=True,
                            self_check_notes="test",
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
                                "trivial_justification": "direct",
                            }
                        ],
                        proof_skeleton_nl="direct",
                        final_step_yields_exact_root=True,
                    ),
                    formalization_cost_estimate_total=0.1 + idx,
                    drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
                )
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
            assembly_check={"verdict": "valid", "details": "ok", "hidden_steps_found": [], "final_step_matches_root": True},
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
        if "variant 1" in payload.statement_nl:
            return Agent5Output(
                status="completed",
                lemma_id=payload.lemma_id,
                statement_status="false",
                proof_status="wrong_strategy",
                drift_assessment={
                    "drift_detected": False,
                    "drift_severity": "none",
                    "drift_description": None,
                    "drift_type": None,
                },
                recommended_action="flag_suspected_false",
                confidence=0.99,
                reason="forced false",
                feedback_for_solver=None,
                candidate_counterexample="x=0",
                detailed_findings=[],
            )
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


class ParseFailureAgent(BaseAgentService):
    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
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

    def decompose(self, payload, artifact_prefix, **kwargs):
        raise AgentExecutionError(
            agent_key="agent2",
            error_class="infrastructure",
            message="synthetic parse failure",
            artifact_prefix=artifact_prefix,
            parse_error_artifact=f"{artifact_prefix}/agent2_parse_error.json",
        )


class TransientDecomposeAgent(BaseAgentService):
    decompose_calls = 0
    previous_attempt_lengths: list[int] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def set_llm_overrides(self, llm_overrides) -> None:  # noqa: ANN001
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
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

    def decompose(self, payload, artifact_prefix, **kwargs):
        self.__class__.decompose_calls += 1
        self.__class__.previous_attempt_lengths.append(len(payload.previous_attempt_summaries or []))
        if self.__class__.decompose_calls == 1:
            raise AgentExecutionError(
                agent_key="agent2",
                error_class="infrastructure_transient",
                message="synthetic transient connection error",
                artifact_prefix=artifact_prefix,
            )

        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim=payload.theorem_nl,
        )
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="transient retry strategy",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl=f"{payload.theorem_nl} helper",
                            semantic_sketch=sketch,
                            role_in_assembly="direct",
                            formalization_cost_estimate=0.1,
                            self_check_true=True,
                            self_check_notes="ok",
                        )
                    ],
                    assembly_plan=Agent2AssemblyPlan(
                        steps=[
                            {
                                "step_id": "A1",
                                "uses_lemmas": ["L1"],
                                "uses_prior_steps": [],
                                "derives": payload.theorem_nl,
                                "is_trivial": True,
                                "trivial_justification": "direct",
                            }
                        ],
                        proof_skeleton_nl="Apply L1.",
                        final_step_yields_exact_root=True,
                    ),
                    formalization_cost_estimate_total=0.1,
                    drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
                )
            ],
        )

    def vet_decomposition(self, payload, artifact_prefix):
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[{"local_id": "L1", "statement_status": "plausible"}],
            assembly_check={"verdict": "valid", "details": "ok", "hidden_steps_found": [], "final_step_matches_root": True},
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


class ParallelRootOneWinnerAgent(BaseAgentService):
    decompose_calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def set_llm_overrides(self, llm_overrides) -> None:  # noqa: ANN001
        return None

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str):
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

    def decompose(self, payload, artifact_prefix, **kwargs):
        self.__class__.decompose_calls += 1
        if "_track_1" not in artifact_prefix:
            raise AgentExecutionError(
                agent_key="agent2",
                error_class="infrastructure_transient",
                message="synthetic transient connection error",
                artifact_prefix=artifact_prefix,
            )

        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim=payload.theorem_nl,
        )
        return Agent2Output(
            status="completed",
            candidates=[
                Agent2Candidate(
                    candidate_index=0,
                    strategy_summary="single surviving root track",
                    shared_context=[],
                    lemmas=[
                        Agent2Lemma(
                            local_id="L1",
                            statement_nl=f"{payload.theorem_nl} helper",
                            semantic_sketch=sketch,
                            role_in_assembly="direct",
                            formalization_cost_estimate=0.1,
                            self_check_true=True,
                            self_check_notes="ok",
                        )
                    ],
                    assembly_plan=Agent2AssemblyPlan(
                        steps=[
                            {
                                "step_id": "A1",
                                "uses_lemmas": ["L1"],
                                "uses_prior_steps": [],
                                "derives": payload.theorem_nl,
                                "is_trivial": True,
                                "trivial_justification": "direct",
                            }
                        ],
                        proof_skeleton_nl="Apply L1.",
                        final_step_yields_exact_root=True,
                    ),
                    formalization_cost_estimate_total=0.1,
                    drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
                )
            ],
        )

    def vet_decomposition(self, payload, artifact_prefix):
        return Agent3Output(
            status="completed",
            decision="accepted",
            summary="accepted",
            lemma_findings=[{"local_id": "L1", "statement_status": "plausible"}],
            assembly_check={"verdict": "valid", "details": "ok", "hidden_steps_found": [], "final_step_matches_root": True},
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


def test_nl_only_false_lemma_invalidates_and_pauses_for_root_review(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", NLInvalidationAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", NLInvalidationAgent)
    monkeypatch.setattr(workers_facade, "AgentService", NLInvalidationAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Invalidate parent decomposition",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"parallel_root_decompositions_n": 2, "parallel_root_take_k": 2},
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
        if terminal in {"succeeded", "failed", "paused"}:
            break

    assert terminal == "paused"

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stage_names = [event["stage"] for event in events.json()["events"]]
    assert "decomposition.invalidated" in stage_names
    assert "problem.paused_for_false_root_child" in stage_names
    assert "decomposition.promoted" not in stage_names


def test_api_error_envelope_and_infrastructure_failure(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", ParseFailureAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", ParseFailureAgent)
    monkeypatch.setattr(workers_facade, "AgentService", ParseFailureAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Parse failure theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    # create can succeed because semantic sketch runs before decomposition in this test harness.
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    run = client.post(f"/v1/problems/{problem_id}/run")
    assert run.status_code == 200
    terminal = run.json()["status"]
    for _ in range(20):
        if terminal == "failed":
            break
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
    assert terminal == "failed"

    failure = client.get(f"/v1/problems/{problem_id}/failure-report")
    assert failure.status_code == 200
    assert failure.json()["failure_report"]["terminal_error_class"] == "infrastructure"

    missing = client.get("/v1/problems/does-not-exist")
    assert missing.status_code == 404
    body = missing.json()
    assert "request_id" in body
    assert "server_time" in body
    assert body["error"]["code"] == "problem_not_found"


def test_transient_decomposition_error_retries_without_failing_problem(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", TransientDecomposeAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", TransientDecomposeAgent)
    monkeypatch.setattr(workers_facade, "AgentService", TransientDecomposeAgent)
    TransientDecomposeAgent.decompose_calls = 0
    TransientDecomposeAgent.previous_attempt_lengths = []

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Transient decomposition retry",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"parallel_root_decompositions_n": 1, "parallel_root_take_k": 1},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first_run = client.post(f"/v1/problems/{problem_id}/run")
    assert first_run.status_code == 200

    terminal = "running"
    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded"
    assert TransientDecomposeAgent.decompose_calls >= 2
    assert TransientDecomposeAgent.previous_attempt_lengths[:2] == [0, 0]

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stages = [row["stage"] for row in events.json()["events"]]
    assert "agent.infrastructure_fatal" not in stages
    assert "problem.failed" not in stages


def test_parallel_root_winner_does_not_emit_retry_for_loser_transport_failures(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", ParallelRootOneWinnerAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", ParallelRootOneWinnerAgent)
    monkeypatch.setattr(workers_facade, "AgentService", ParallelRootOneWinnerAgent)
    ParallelRootOneWinnerAgent.decompose_calls = 0

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Parallel root winner",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"parallel_root_decompositions_n": 3, "parallel_root_take_k": 1},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first_run = client.post(f"/v1/problems/{problem_id}/run")
    assert first_run.status_code == 200
    for _ in range(20):
        if ParallelRootOneWinnerAgent.decompose_calls >= 3:
            break
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
    assert ParallelRootOneWinnerAgent.decompose_calls >= 3

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    stages = [row["stage"] for row in events.json()["events"]]
    assert "decomposition.generated" in stages
    assert "agent.infrastructure_retry" not in stages
