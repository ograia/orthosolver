from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.domain.enums import ControllerStatus, NodeKind, ProofStatus, RoutingStatus
from nl_engine.domain.models import CounterexampleORM, DecompositionORM, FailureReportORM, LemmaORM
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import (
    CounterexampleRepository,
    DecompositionRepository,
    EventRepository,
    FailureReportRepository,
    LemmaRepository,
    ProblemRepository,
    TheoremRepository,
)


def test_resume_after_infra_failure_uses_latest_terminal_failure_only() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "latest failure only", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    event_repo = EventRepository(store)
    problem = problem_repo.get(problem_id)
    assert problem is not None
    problem.status = "failed"
    problem_repo.save(problem)

    event_repo.append(
        problem_id,
        "problem.failed",
        "running",
        "failed",
        reason="infrastructure failure count (1) exceeded threshold (1)",
    )
    event_repo.append(
        problem_id,
        "problem.failed",
        "running",
        "failed",
        reason="solver retry cap reached",
    )

    response = client.post(f"/v1/debug/problems/{problem_id}/resume-after-infrastructure-failure")
    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "not_infrastructure_failure"


def test_public_resume_sets_branch_anchor_and_reactivates_owner_decomposition() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "resume anchor", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]
    root_theorem_id = create.json()["root_theorem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    theorem_repo = TheoremRepository(store)
    decomp_repo = DecompositionRepository(store)
    lemma_repo = LemmaRepository(store)
    failure_repo = FailureReportRepository(store)

    lemma_id = "lem_resume_anchor"
    decomp_id = "dec_resume_anchor"

    lemma_repo.create(
        LemmaORM(
            lemma_id=lemma_id,
            problem_id=problem_id,
            parent_id=root_theorem_id,
            parent_kind=NodeKind.THEOREM.value,
            kind=NodeKind.LEMMA.value,
            depth=1,
            statement_nl="stuck lemma",
            statement_semantic_sketch={"normalized_claim": "stuck lemma"},
            proof_status=ProofStatus.PROOF_EXHAUSTED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            solver_attempt_count=3,
            consecutive_fatal_rejections=3,
            minor_rejection_count=2,
        )
    )

    decomp_repo.create(
        DecompositionORM(
            decomposition_id=decomp_id,
            problem_id=problem_id,
            node_id=root_theorem_id,
            node_kind=NodeKind.THEOREM.value,
            strategy_summary="anchor branch",
            lemma_ids=[lemma_id],
            llm_vetting_status="accepted",
            lean_assembly_status="skipped",
            controller_status=ControllerStatus.FAILED.value,
        )
    )

    problem = problem_repo.get(problem_id)
    theorem = theorem_repo.get(root_theorem_id)
    assert problem is not None
    assert theorem is not None
    problem.status = "failed"
    problem.active_decomposition_id = decomp_id
    problem.failure_report_artifact_id = f"problems/{problem_id}/failure_report.manual.json"
    problem_repo.save(problem)
    theorem.status = "failed"
    theorem.active_decomposition_id = decomp_id
    theorem_repo.save(theorem)

    failure_repo.create_or_replace(
        FailureReportORM(
            failure_report_id="fail_resume_anchor",
            problem_id=problem_id,
            failure_reason="proof_exhausted",
            terminal_lemma_id=lemma_id,
            terminal_error_message="solver retry cap reached",
            partial_tree={},
            trusted_context_at_failure=[],
            all_decomposition_attempts=[],
            routing_log_summary=[],
        )
    )

    resume = client.post(f"/v1/problems/{problem_id}/resume")
    assert resume.status_code == 200
    payload = resume.json()
    assert payload["status"] == "running"

    refreshed_problem = problem_repo.get(problem_id)
    refreshed_lemma = lemma_repo.get(lemma_id)
    refreshed_decomp = decomp_repo.get(decomp_id)
    assert refreshed_problem is not None
    assert refreshed_lemma is not None
    assert refreshed_decomp is not None
    assert refreshed_problem.resume_anchor_lemma_id == lemma_id
    assert refreshed_problem.resume_anchor_owner_decomposition_id == decomp_id
    assert refreshed_problem.active_decomposition_id == decomp_id
    assert refreshed_decomp.controller_status == ControllerStatus.ACTIVE.value
    assert refreshed_lemma.proof_status == ProofStatus.PROOF_FLAWED.value
    assert refreshed_lemma.routing_status == RoutingStatus.RETRY_SOLVER.value
    assert refreshed_lemma.consecutive_fatal_rejections == 0
    assert refreshed_lemma.minor_rejection_count == 0

    events = client.get(f"/v1/problems/{problem_id}/events?limit=200")
    assert events.status_code == 200
    rows = events.json()["events"]
    assert any(row["stage"] == "problem.resume_anchor_set" for row in rows)
    resumed_event = next(row for row in rows if row["stage"] == "problem.resumed")
    assert "route=resume" in (resumed_event.get("reason") or "")


def test_continue_button_on_paused_false_root_child_sets_branch_anchor() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "paused false branch", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]
    root_theorem_id = create.json()["root_theorem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    theorem_repo = TheoremRepository(store)
    decomp_repo = DecompositionRepository(store)
    lemma_repo = LemmaRepository(store)
    counterexample_repo = CounterexampleRepository(store)
    event_repo = EventRepository(store)

    anchor_lemma_id = "lem_parent_resume"
    false_lemma_id = "lem_false_leaf"
    owner_decomp_id = "dec_owner_for_anchor"
    root_decomp_id = "dec_failed_root"

    lemma_repo.create(
        LemmaORM(
            lemma_id=anchor_lemma_id,
            problem_id=problem_id,
            parent_id="lem_upper",
            parent_kind=NodeKind.LEMMA.value,
            kind=NodeKind.LEMMA.value,
            depth=2,
            statement_nl="repair this parent lemma",
            statement_semantic_sketch={"normalized_claim": "repair this parent lemma"},
            proof_status=ProofStatus.FAILED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            statement_status="false",
            truth_status="false",
            next_action="invalidate_parent_decomposition",
        )
    )
    lemma_repo.create(
        LemmaORM(
            lemma_id=false_lemma_id,
            problem_id=problem_id,
            parent_id=anchor_lemma_id,
            parent_kind=NodeKind.LEMMA.value,
            kind=NodeKind.LEMMA.value,
            depth=3,
            statement_nl="actually false child lemma",
            statement_semantic_sketch={"normalized_claim": "actually false child lemma"},
            proof_status=ProofStatus.FAILED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            statement_status="false",
            truth_status="false",
            counterexample_status="accepted",
            active_counterexample_id="cex_resume_branch",
            next_action="invalidate_parent_decomposition",
        )
    )
    counterexample_repo.create(
        CounterexampleORM(
            counterexample_id="cex_resume_branch",
            problem_id=problem_id,
            lemma_id=false_lemma_id,
            source_agent="agent5",
            status="accepted",
            statement_fingerprint="fp_resume_branch",
            counterexample_text="n = 1",
            summary="false leaf",
        )
    )
    decomp_repo.create(
        DecompositionORM(
            decomposition_id=owner_decomp_id,
            problem_id=problem_id,
            node_id="lem_upper",
            node_kind=NodeKind.LEMMA.value,
            strategy_summary="contains the parent anchor lemma",
            lemma_ids=[anchor_lemma_id],
            llm_vetting_status="accepted",
            lean_assembly_status="skipped",
            controller_status=ControllerStatus.FAILED.value,
            failure_origin="child_lemma_false",
            invalidated_by_lemma_id=anchor_lemma_id,
        )
    )
    decomp_repo.create(
        DecompositionORM(
            decomposition_id=root_decomp_id,
            problem_id=problem_id,
            node_id=root_theorem_id,
            node_kind=NodeKind.THEOREM.value,
            strategy_summary="failed root path",
            lemma_ids=["lem_root_false"],
            llm_vetting_status="accepted",
            lean_assembly_status="skipped",
            controller_status=ControllerStatus.FAILED.value,
            failure_origin="child_lemma_false",
        )
    )

    problem = problem_repo.get(problem_id)
    theorem = theorem_repo.get(root_theorem_id)
    assert problem is not None
    assert theorem is not None
    problem.status = "paused"
    problem.active_decomposition_id = None
    problem_repo.save(problem)
    theorem.active_decomposition_id = None
    theorem_repo.save(theorem)
    event_repo.append(
        problem_id,
        "problem.paused_for_false_root_child",
        "running",
        "paused",
        target_node_id="lem_root_false",
        reason="descendant branch collapsed to root",
    )

    continued = client.post(
        f"/v1/problems/{problem_id}/start",
        headers={"X-Debug-Run-Trigger": "continue_button"},
    )
    assert continued.status_code == 200

    refreshed_problem = problem_repo.get(problem_id)
    refreshed_anchor = lemma_repo.get(anchor_lemma_id)
    refreshed_owner = decomp_repo.get(owner_decomp_id)
    assert refreshed_problem is not None
    assert refreshed_anchor is not None
    assert refreshed_owner is not None
    assert refreshed_problem.resume_anchor_lemma_id == anchor_lemma_id
    assert refreshed_problem.resume_anchor_owner_decomposition_id == owner_decomp_id
    assert refreshed_problem.active_decomposition_id == owner_decomp_id
    assert refreshed_owner.controller_status == ControllerStatus.ACTIVE.value
    assert refreshed_anchor.proof_status == ProofStatus.PROOF_FLAWED.value
    assert refreshed_anchor.routing_status == RoutingStatus.DECOMPOSE_FURTHER.value
    assert refreshed_anchor.next_action == "retry_decomposition"


def test_lemma_decomposition_exhaustion_fails_problem(monkeypatch) -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "decomposition exhaustion",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "decomposition": {"max_decompositions_per_failed_lemma": 2},
                "lemma_solving": {"max_consecutive_fatal_rejections_per_lemma": 1},
            },
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]
    root_theorem_id = create.json()["root_theorem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    theorem_repo = TheoremRepository(store)
    decomp_repo = DecompositionRepository(store)
    lemma_repo = LemmaRepository(store)

    lemma_id = "lem_decomp_exhaust"
    decomp_id = "dec_decomp_exhaust"

    lemma_repo.create(
        LemmaORM(
            lemma_id=lemma_id,
            problem_id=problem_id,
            parent_id=root_theorem_id,
            parent_kind=NodeKind.THEOREM.value,
            kind=NodeKind.LEMMA.value,
            depth=1,
            statement_nl="stuck lemma",
            statement_semantic_sketch={"normalized_claim": "stuck lemma"},
            proof_status=ProofStatus.PROOF_FLAWED.value,
            routing_status=RoutingStatus.RETRY_SOLVER.value,
            solver_attempt_count=4,
            consecutive_fatal_rejections=1,
            minor_rejection_count=0,
        )
    )

    decomp_repo.create(
        DecompositionORM(
            decomposition_id=decomp_id,
            problem_id=problem_id,
            node_id=root_theorem_id,
            node_kind=NodeKind.THEOREM.value,
            strategy_summary="buffer path",
            lemma_ids=[lemma_id],
            llm_vetting_status="accepted",
            lean_assembly_status="skipped",
            controller_status=ControllerStatus.ACTIVE.value,
        )
    )

    problem = problem_repo.get(problem_id)
    theorem = theorem_repo.get(root_theorem_id)
    assert problem is not None
    assert theorem is not None
    problem.status = "running"
    problem.active_decomposition_id = decomp_id
    problem_repo.save(problem)
    theorem.statement_semantic_sketch = {"normalized_claim": "For all n, n = n"}
    theorem.status = "open"
    theorem.active_decomposition_id = decomp_id
    theorem_repo.save(theorem)

    monkeypatch.setattr(
        orchestrator_module.Orchestrator,
        "_lemma_rejection_cap_reason",
        lambda *_args, **_kwargs: "max consecutive fatal rejections reached (1)",
    )
    monkeypatch.setattr(
        orchestrator_module.Orchestrator,
        "_decompose_current_lemma_result",
        lambda self, _problem, _lemma, _cfg: (
            self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND,
            "no accepted decomposition candidates",
        ),
    )
    remaining = {"slots": 2}

    def fake_remaining_slots(self, _problem_id, node_id, _node_kind, _cfg):
        if node_id != lemma_id:
            return 1
        current = remaining["slots"]
        remaining["slots"] = max(0, current - 1)
        return current

    monkeypatch.setattr(orchestrator_module.Orchestrator, "_remaining_decomposition_slots", fake_remaining_slots)

    orchestrator = orchestrator_module.Orchestrator(store)
    for _ in range(4):
        orchestrator.run_once(problem_id)

    events = EventRepository(store).list_for_problem(problem_id, limit=1000)
    stages = [row.stage for row in events]
    assert "lemma.decomposition_attempt_rejected" in stages


def test_blocked_exhausted_lemma_retries_decomposition_when_slots_remain(monkeypatch) -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "blocked exhausted retry",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]
    root_theorem_id = create.json()["root_theorem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    theorem_repo = TheoremRepository(store)
    decomp_repo = DecompositionRepository(store)
    lemma_repo = LemmaRepository(store)

    lemma_id = "lem_blocked_exhausted_retry"
    decomp_id = "dec_blocked_exhausted_retry"

    lemma_repo.create(
        LemmaORM(
            lemma_id=lemma_id,
            problem_id=problem_id,
            parent_id=root_theorem_id,
            parent_kind=NodeKind.THEOREM.value,
            kind=NodeKind.LEMMA.value,
            depth=1,
            statement_nl="stuck lemma",
            statement_semantic_sketch={"normalized_claim": "stuck lemma"},
            proof_status=ProofStatus.PROOF_EXHAUSTED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            solver_attempt_count=3,
            consecutive_fatal_rejections=3,
            minor_rejection_count=0,
        )
    )

    decomp_repo.create(
        DecompositionORM(
            decomposition_id=decomp_id,
            problem_id=problem_id,
            node_id=root_theorem_id,
            node_kind=NodeKind.THEOREM.value,
            strategy_summary="active parent decomposition",
            lemma_ids=[lemma_id],
            llm_vetting_status="accepted",
            lean_assembly_status="skipped",
            controller_status=ControllerStatus.ACTIVE.value,
        )
    )

    problem = problem_repo.get(problem_id)
    theorem = theorem_repo.get(root_theorem_id)
    assert problem is not None
    assert theorem is not None
    problem.status = "running"
    problem.active_decomposition_id = decomp_id
    problem_repo.save(problem)
    theorem.status = "open"
    theorem.statement_semantic_sketch = {"normalized_claim": "For all n, n = n"}
    theorem.active_decomposition_id = decomp_id
    theorem_repo.save(theorem)

    retry_called = {"count": 0}

    def fake_decompose_current_lemma(self, _problem, _lemma, _cfg):
        retry_called["count"] += 1
        return True

    def fake_decompose_current_lemma_result(self, _problem, _lemma, _cfg):
        retry_called["count"] += 1
        return self._DECOMPOSE_OUTCOME_CHILD_PENDING, "mocked decomposition"

    monkeypatch.setattr(orchestrator_module.Orchestrator, "_decompose_current_lemma", fake_decompose_current_lemma)
    monkeypatch.setattr(
        orchestrator_module.Orchestrator,
        "_decompose_current_lemma_result",
        fake_decompose_current_lemma_result,
    )

    orchestrator = orchestrator_module.Orchestrator(store)
    result = orchestrator.run_once(problem_id)
    assert result.status == "running"
    assert retry_called["count"] >= 1

    events = EventRepository(store).list_for_problem(problem_id, limit=500)
    assert any(event.stage == "lemma.decomposition_retry_scheduled" for event in events)


def test_dead_frontier_fails_running_problem_with_explicit_reason(monkeypatch) -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "dead frontier",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]
    root_theorem_id = create.json()["root_theorem_id"]

    store = get_file_store()
    problem_repo = ProblemRepository(store)
    theorem_repo = TheoremRepository(store)
    decomp_repo = DecompositionRepository(store)
    lemma_repo = LemmaRepository(store)
    failure_repo = FailureReportRepository(store)

    lemma_id = "lem_dead_frontier"
    decomp_id = "dec_dead_frontier"

    lemma_repo.create(
        LemmaORM(
            lemma_id=lemma_id,
            problem_id=problem_id,
            parent_id=root_theorem_id,
            parent_kind=NodeKind.THEOREM.value,
            kind=NodeKind.LEMMA.value,
            depth=1,
            statement_nl="stuck lemma",
            statement_semantic_sketch={"normalized_claim": "stuck lemma"},
            proof_status=ProofStatus.PROOF_EXHAUSTED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            solver_attempt_count=3,
            consecutive_fatal_rejections=3,
            minor_rejection_count=0,
        )
    )

    decomp_repo.create(
        DecompositionORM(
            decomposition_id=decomp_id,
            problem_id=problem_id,
            node_id=root_theorem_id,
            node_kind=NodeKind.THEOREM.value,
            strategy_summary="active parent decomposition",
            lemma_ids=[lemma_id],
            llm_vetting_status="accepted",
            lean_assembly_status="skipped",
            controller_status=ControllerStatus.ACTIVE.value,
        )
    )

    problem = problem_repo.get(problem_id)
    theorem = theorem_repo.get(root_theorem_id)
    assert problem is not None
    assert theorem is not None
    problem.status = "running"
    problem.active_decomposition_id = decomp_id
    problem_repo.save(problem)
    theorem.status = "open"
    theorem.statement_semantic_sketch = {"normalized_claim": "For all n, n = n"}
    theorem.active_decomposition_id = decomp_id
    theorem_repo.save(theorem)

    monkeypatch.setattr(
        orchestrator_module.Orchestrator,
        "_remaining_decomposition_slots",
        lambda *_args, **_kwargs: 0,
    )

    orchestrator = orchestrator_module.Orchestrator(store)
    result = orchestrator.run_once(problem_id)
    assert result.status == "failed"

    failure = failure_repo.get_by_problem(problem_id)
    assert failure is not None
    assert failure.failure_reason == "dead_frontier"

    events = EventRepository(store).list_for_problem(problem_id, limit=500)
    assert any(event.stage == "problem.dead_frontier_detected" for event in events)
