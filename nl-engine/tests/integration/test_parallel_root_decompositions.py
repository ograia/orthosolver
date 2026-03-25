from __future__ import annotations

from datetime import UTC, datetime
import threading
import time

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.artifacts.store import ArtifactStore
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.workers import facade as workers_facade
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
from nl_engine.services.agents import AgentService as BaseAgentService


class ParallelRootTracksAgent(BaseAgentService):
    decompose_calls = 0
    max_parallel_decompose = 0
    vet_started_while_decompose_active = False
    _active_decompose = 0
    _decompose_lock = threading.Lock()

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
        with self.__class__._decompose_lock:
            self.__class__.decompose_calls += 1
            self.__class__._active_decompose += 1
            self.__class__.max_parallel_decompose = max(
                self.__class__.max_parallel_decompose,
                self.__class__._active_decompose,
            )
        # Ensure one root track finishes first so vetting can start before the
        # slowest concurrent root decomposition returns.
        sleep_seconds = 0.05
        if "_track_2" in artifact_prefix:
            sleep_seconds = 0.25
        time.sleep(sleep_seconds)
        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim="lemma",
        )
        candidates: list[Agent2Candidate] = []
        try:
            for idx in range(max(1, payload.num_candidates)):
                local_id = f"L{idx + 1}"
                candidates.append(
                    Agent2Candidate(
                        candidate_index=idx,
                        strategy_summary=f"root strategy {idx + 1}",
                        shared_context=[],
                        lemmas=[
                            Agent2Lemma(
                                local_id=local_id,
                                statement_nl=f"Aux lemma {idx + 1}: {payload.theorem_nl}",
                                semantic_sketch=sketch,
                                role_in_assembly=f"step{idx + 1}",
                                formalization_cost_estimate=0.1 + (0.1 * idx),
                                self_check_true=True,
                                self_check_notes="parallel root test",
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
                                    "trivial_justification": "direct composition",
                                }
                            ],
                            proof_skeleton_nl=f"Apply {local_id}.",
                            final_step_yields_exact_root=True,
                        ),
                        formalization_cost_estimate_total=0.1 + (0.1 * idx),
                        drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
                    )
                )
        finally:
            with self.__class__._decompose_lock:
                self.__class__._active_decompose -= 1
        return Agent2Output(status="completed", candidates=candidates)

    def vet_decomposition(self, payload, artifact_prefix):
        with self.__class__._decompose_lock:
            if self.__class__._active_decompose > 0:
                self.__class__.vet_started_while_decompose_active = True
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
                "details": "valid",
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
            proof_nl=f"Proof of {payload.statement_nl}",
            proof_summary="proved",
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


class DelayedSecondRootTrackAgent(ParallelRootTracksAgent):
    track2_delay_seconds = 2.5

    def decompose(self, payload, artifact_prefix, **kwargs):
        with self.__class__._decompose_lock:
            self.__class__.decompose_calls += 1
            self.__class__._active_decompose += 1
            self.__class__.max_parallel_decompose = max(
                self.__class__.max_parallel_decompose,
                self.__class__._active_decompose,
            )
        sleep_seconds = 0.05
        track_label = "track1"
        if "_track_2" in artifact_prefix:
            sleep_seconds = self.__class__.track2_delay_seconds
            track_label = "track2"
        time.sleep(sleep_seconds)
        sketch = SemanticSketch(
            variables=[],
            quantifier_order=[],
            domain_restrictions=[],
            witness_dependencies=[],
            normalized_claim="lemma",
        )
        candidates = [
            Agent2Candidate(
                candidate_index=0,
                strategy_summary="root strategy",
                shared_context=[],
                lemmas=[
                    Agent2Lemma(
                        local_id="L1",
                        statement_nl=f"Aux lemma ({track_label}): {payload.theorem_nl}",
                        semantic_sketch=sketch,
                        role_in_assembly="step1",
                        formalization_cost_estimate=0.1,
                        self_check_true=True,
                        self_check_notes="parallel root delay test",
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
                            "trivial_justification": "direct composition",
                        }
                    ],
                    proof_skeleton_nl="Apply L1.",
                    final_step_yields_exact_root=True,
                ),
                formalization_cost_estimate_total=0.1,
                drift_self_check={"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []},
            )
        ]
        with self.__class__._decompose_lock:
            self.__class__._active_decompose -= 1
        return Agent2Output(status="completed", candidates=candidates)


class DelayedSecondRootTrackSlowSolveAgent(DelayedSecondRootTrackAgent):
    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        if "track2" in payload.statement_nl and payload.attempt_number == 1:
            return Agent4Output(
                status="failed",
                lemma_id=payload.lemma_id,
                attempt_number=payload.attempt_number,
                proof_nl=None,
                proof_summary="retry once before proving",
                self_report={
                    "confidence": 0.3,
                    "suspected_gaps": ["intentional staggered solve"],
                    "used_external_facts": [],
                    "every_step_justified": False,
                    "proves_exactly_the_statement": False,
                },
                stuck_point="intentional staggered solve",
                candidate_counterexample=None,
                addressed_previous_feedback=payload.previous_feedback,
            )
        return super().solve_lemma(payload, artifact_prefix)


class FatalFirstTrackWithDeferredSiblingAgent(DelayedSecondRootTrackAgent):
    track2_delay_seconds = 4.0
    vet_call_count = 0
    decompose_payloads: list[dict] = []

    def decompose(self, payload, artifact_prefix, **kwargs):
        self.__class__.decompose_payloads.append(payload.model_dump())
        return super().decompose(payload, artifact_prefix)

    def vet_decomposition(self, payload, artifact_prefix):
        self.__class__.vet_call_count += 1
        if self.__class__.vet_call_count == 1:
            return Agent3Output(
                status="completed",
                decision="fatal",
                summary="force retry after fatal vetter decision",
                lemma_findings=[
                    {
                        "local_id": lemma.get("local_id", "L1"),
                        "statement_status": "plausible",
                        "evidence": "synthetic fatal test",
                        "counterexample": None,
                    }
                    for lemma in payload.decomposition.get("lemmas", [])
                ],
                assembly_check={
                    "verdict": "valid",
                    "details": "valid",
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
                formalization_risk="high",
                fixes_required=["retry with alternate decomposition strategy"],
                fatal_reason="synthetic fatal root decomposition",
            )
        return super().vet_decomposition(payload, artifact_prefix)


class StatefulFatalFirstTrackWithDeferredSiblingAgent(FatalFirstTrackWithDeferredSiblingAgent):
    def decompose(self, payload, artifact_prefix, **kwargs):
        store = ArtifactStore()
        state_payload = {
            "agent_key": "agent2",
            "attempt": 1,
            "status": "started",
            "updated_at": datetime.now(UTC).isoformat(),
        }
        worker_job_id = getattr(self, "runtime_worker_job_id", None)
        if isinstance(worker_job_id, str) and worker_job_id.strip():
            state_payload["worker_job_id"] = worker_job_id.strip()
        store.save_json(f"{artifact_prefix}/agent2_request_state_attempt_1.json", state_payload)

        output = super().decompose(payload, artifact_prefix)

        state_payload["status"] = "completed"
        state_payload["updated_at"] = datetime.now(UTC).isoformat()
        store.save_json(f"{artifact_prefix}/agent2_request_state_attempt_1.json", state_payload)
        return output


class FirstAttemptFailsAgent(DelayedSecondRootTrackAgent):
    track2_delay_seconds = 0.05

    def solve_lemma(self, payload, artifact_prefix, **kwargs):
        if payload.attempt_number == 1:
            return Agent4Output(
                status="failed",
                lemma_id=payload.lemma_id,
                attempt_number=payload.attempt_number,
                proof_nl=None,
                proof_summary="intentional first-attempt failure",
                self_report={
                    "confidence": 0.2,
                    "suspected_gaps": ["intentional first-attempt failure"],
                    "used_external_facts": [],
                    "every_step_justified": False,
                    "proves_exactly_the_statement": False,
                },
                stuck_point="intentional first-attempt failure",
                candidate_counterexample=None,
                addressed_previous_feedback=payload.previous_feedback,
            )
        return super().solve_lemma(payload, artifact_prefix)


def test_root_parallel_take_k_processes_multiple_root_tracks(monkeypatch) -> None:
    ParallelRootTracksAgent.decompose_calls = 0
    ParallelRootTracksAgent.max_parallel_decompose = 0
    ParallelRootTracksAgent.vet_started_while_decompose_active = False
    ParallelRootTracksAgent._active_decompose = 0
    monkeypatch.setattr(api_main, "AgentService", ParallelRootTracksAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", ParallelRootTracksAgent)
    monkeypatch.setattr(workers_facade, "AgentService", ParallelRootTracksAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Parallel root tracks theorem",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 2,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    # Tick 1+ generates/vets root decomposition attempts.
    first = client.post(f"/v1/problems/{problem_id}/run")
    assert first.status_code == 200
    for _ in range(20):
        if ParallelRootTracksAgent.decompose_calls >= 2:
            break
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
    assert ParallelRootTracksAgent.decompose_calls >= 2

    # Additional ticks to allow harvest and materialization of both tracks.
    for _ in range(10):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot?events_limit=200")
        root_tracks = snapshot.json()["root_track_decomposition_ids"]
        if len(root_tracks) >= 2:
            break
    assert len(root_tracks) == 2

    # Tick 2 should process multiple root tracks (K=2) in the same tick path.
    second = client.post(f"/v1/problems/{problem_id}/run")
    assert second.status_code == 200

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    solver_events = [row for row in events.json()["events"] if row["stage"] == "lemma.solver_attempt"]
    assert len({row["target_node_id"] for row in solver_events}) >= 2


def test_root_parallel_slow_track_does_not_block_solver_progress(monkeypatch) -> None:
    DelayedSecondRootTrackAgent.decompose_calls = 0
    DelayedSecondRootTrackAgent.max_parallel_decompose = 0
    DelayedSecondRootTrackAgent.vet_started_while_decompose_active = False
    DelayedSecondRootTrackAgent._active_decompose = 0
    monkeypatch.setattr(api_main, "AgentService", DelayedSecondRootTrackAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", DelayedSecondRootTrackAgent)
    monkeypatch.setattr(workers_facade, "AgentService", DelayedSecondRootTrackAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Parallel delayed track theorem",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 2,
                    "parallel_root_join_timeout_seconds": 1,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    started = time.time()
    first = client.post(f"/v1/problems/{problem_id}/run")
    elapsed = time.time() - started
    assert first.status_code == 200
    assert elapsed < (DelayedSecondRootTrackAgent.track2_delay_seconds + 1.0)

    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
        assert events.status_code == 200
        solver_events = [row for row in events.json()["events"] if row["stage"] == "lemma.solver_attempt"]
        if len(solver_events) >= 1:
            break
    assert len(solver_events) >= 1


def test_root_fatal_vet_retry_dispatches_even_when_sibling_track_is_deferred(monkeypatch) -> None:
    FatalFirstTrackWithDeferredSiblingAgent.decompose_calls = 0
    FatalFirstTrackWithDeferredSiblingAgent.max_parallel_decompose = 0
    FatalFirstTrackWithDeferredSiblingAgent.vet_started_while_decompose_active = False
    FatalFirstTrackWithDeferredSiblingAgent._active_decompose = 0
    FatalFirstTrackWithDeferredSiblingAgent.vet_call_count = 0
    FatalFirstTrackWithDeferredSiblingAgent.decompose_payloads = []
    monkeypatch.setattr(api_main, "AgentService", FatalFirstTrackWithDeferredSiblingAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", FatalFirstTrackWithDeferredSiblingAgent)
    monkeypatch.setattr(workers_facade, "AgentService", FatalFirstTrackWithDeferredSiblingAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Fatal root retry with deferred sibling",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 1,
                    "parallel_root_join_timeout_seconds": 1,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first = client.post(f"/v1/problems/{problem_id}/run")
    assert first.status_code == 200

    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        if FatalFirstTrackWithDeferredSiblingAgent.decompose_calls >= 2 and FatalFirstTrackWithDeferredSiblingAgent.vet_call_count >= 1:
            break
    assert FatalFirstTrackWithDeferredSiblingAgent.decompose_calls >= 2
    assert FatalFirstTrackWithDeferredSiblingAgent.vet_call_count >= 1

    terminal = run.json()["status"]
    for _ in range(16):
        if terminal in {"succeeded", "failed"}:
            break
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
    assert terminal == "succeeded"


def test_root_deferred_sibling_counts_against_parallel_dispatch_budget(monkeypatch) -> None:
    StatefulFatalFirstTrackWithDeferredSiblingAgent.decompose_calls = 0
    StatefulFatalFirstTrackWithDeferredSiblingAgent.max_parallel_decompose = 0
    StatefulFatalFirstTrackWithDeferredSiblingAgent.vet_started_while_decompose_active = False
    StatefulFatalFirstTrackWithDeferredSiblingAgent._active_decompose = 0
    StatefulFatalFirstTrackWithDeferredSiblingAgent.vet_call_count = 0
    StatefulFatalFirstTrackWithDeferredSiblingAgent.decompose_payloads = []
    monkeypatch.setattr(api_main, "AgentService", StatefulFatalFirstTrackWithDeferredSiblingAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", StatefulFatalFirstTrackWithDeferredSiblingAgent)
    monkeypatch.setattr(workers_facade, "AgentService", StatefulFatalFirstTrackWithDeferredSiblingAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Deferred root budget accounting",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 2,
                    "parallel_root_join_timeout_seconds": 1,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first = client.post(f"/v1/problems/{problem_id}/run")
    assert first.status_code == 200
    for _ in range(20):
        if StatefulFatalFirstTrackWithDeferredSiblingAgent.decompose_calls >= 2:
            break
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
    assert StatefulFatalFirstTrackWithDeferredSiblingAgent.decompose_calls >= 2

    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        if StatefulFatalFirstTrackWithDeferredSiblingAgent.decompose_calls >= 3:
            break
    assert StatefulFatalFirstTrackWithDeferredSiblingAgent.decompose_calls >= 3


def test_root_solution_threshold_requires_m_solutions(monkeypatch) -> None:
    FirstAttemptFailsAgent.decompose_calls = 0
    FirstAttemptFailsAgent.max_parallel_decompose = 0
    FirstAttemptFailsAgent.vet_started_while_decompose_active = False
    FirstAttemptFailsAgent._active_decompose = 0
    monkeypatch.setattr(api_main, "AgentService", FirstAttemptFailsAgent)
    monkeypatch.setattr(orchestrator_module, "AgentService", FirstAttemptFailsAgent)
    monkeypatch.setattr(workers_facade, "AgentService", FirstAttemptFailsAgent)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Parallel m-solution theorem",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {
                    "parallel_root_decompositions_n": 2,
                    "parallel_root_take_k": 2,
                    "parallel_root_join_timeout_seconds": 1,
                    "root_solutions_required_for_termination": 2,
                },
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    first = client.post(f"/v1/problems/{problem_id}/run")
    assert first.status_code == 200
    second = client.post(f"/v1/problems/{problem_id}/run")
    assert second.status_code == 200
    assert second.json()["status"] in {"created", "running", "succeeded"}

    terminal = "running"
    for _ in range(30):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded"
