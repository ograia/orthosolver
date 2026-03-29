from __future__ import annotations

from types import SimpleNamespace

from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.contracts import Agent2Input
from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.enums import ControllerStatus, ProofStatus, RoutingStatus
from nl_engine.domain.models import DecompositionCandidateORM, DecompositionORM, LemmaORM, ProblemORM


class _LemmaRepo:
    def save(self, lemma: LemmaORM) -> LemmaORM:
        return lemma

    def get(self, lemma_id: str) -> LemmaORM | None:
        return None


def test_child_decomposition_frontier_retries_while_slots_remain() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.lemmas = _LemmaRepo()
    orch.decompositions = SimpleNamespace(
        list_by_node=lambda problem_id, node_id: [
            DecompositionORM(
                decomposition_id="dec_failed",
                problem_id=problem_id,
                node_id=node_id,
                node_kind="lemma",
                llm_vetting_status="accepted",
                controller_status=ControllerStatus.FAILED.value,
            )
        ]
    )
    orch.decomposition_candidates = SimpleNamespace(list_by_node=lambda problem_id, node_id: [])
    orch.event_logger = SimpleNamespace(transition=lambda *args, **kwargs: None)
    orch._select_active_decomposition_for_node = lambda *args, **kwargs: False
    orch._remaining_decomposition_slots = lambda *args, **kwargs: 4
    orch._decompose_current_lemma_result = lambda *args, **kwargs: (
        orch._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND,
        "no accepted decomposition candidates",
    )
    orch._find_owner_decomposition = lambda *args, **kwargs: None
    orch._handle_lemma_terminal_failure = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not terminal fail"))
    orch._mark_failed = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not fail problem"))

    problem = ProblemORM(problem_id="prob_test")
    lemma = LemmaORM(
        lemma_id="lem_test",
        problem_id=problem.problem_id,
        parent_id="thm_root",
        parent_kind="theorem",
        statement_nl="n = n",
        proof_status=ProofStatus.PROOF_FLAWED.value,
        routing_status=RoutingStatus.RETRY_SOLVER.value,
    )

    changed = orch._process_child_decomposition_for_lemma(problem, None, lemma, ProblemConfig())

    assert changed is True
    assert lemma.proof_status == ProofStatus.PROOF_FLAWED.value
    assert lemma.routing_status == RoutingStatus.DECOMPOSE_FURTHER.value
    assert lemma.next_action == "retry_decomposition"


def test_false_invalidated_child_decomposition_is_detected_from_child_failure_origin() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.lemmas = _LemmaRepo()
    orch.decompositions = SimpleNamespace(
        list_by_node=lambda problem_id, node_id: [
            DecompositionORM(
                decomposition_id="dec_failed_false",
                problem_id=problem_id,
                node_id=node_id,
                node_kind="lemma",
                llm_vetting_status="accepted",
                controller_status=ControllerStatus.FAILED.value,
                failure_origin="child_lemma_false",
                failure_reason="child lemma is false",
            )
        ]
    )
    orch.decomposition_candidates = SimpleNamespace(list_by_node=lambda problem_id, node_id: [])
    orch.event_logger = SimpleNamespace(transition=lambda *args, **kwargs: None)
    orch._select_active_decomposition_for_node = lambda *args, **kwargs: False
    called: dict[str, str] = {}

    def invalidate(problem: ProblemORM, lemma: LemmaORM, cfg: ProblemConfig, *, reason: str, counterexample_id: str | None = None) -> None:
        called["lemma_id"] = lemma.lemma_id
        called["reason"] = reason

    orch._invalidate_parent_decomposition = invalidate

    problem = ProblemORM(problem_id="prob_test")
    lemma = LemmaORM(
        lemma_id="lem_false_child",
        problem_id=problem.problem_id,
        parent_id="dec_parent",
        parent_kind="decomposition",
        statement_nl="False statement",
    )

    changed = orch._process_child_decomposition_for_lemma(problem, None, lemma, ProblemConfig())

    assert changed is True
    assert called == {"lemma_id": "lem_false_child", "reason": "child lemma is false"}


def test_pending_accepted_child_candidate_blocks_solver_fallback() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.lemmas = _LemmaRepo()
    orch.decompositions = SimpleNamespace(list_by_node=lambda problem_id, node_id: [])
    orch.decomposition_candidates = SimpleNamespace(
        list_by_node=lambda problem_id, node_id: [
            DecompositionCandidateORM(
                candidate_id="cand_wait",
                problem_id=problem_id,
                node_id=node_id,
                node_kind="lemma",
                llm_vetting_status="accepted",
            )
        ]
    )
    orch.event_logger = SimpleNamespace(transition=lambda *args, **kwargs: None)
    orch._select_active_decomposition_for_node = lambda *args, **kwargs: False

    problem = ProblemORM(problem_id="prob_test")
    lemma = LemmaORM(
        lemma_id="lem_wait",
        problem_id=problem.problem_id,
        parent_id="dec_parent",
        parent_kind="decomposition",
        statement_nl="Need child wait",
        proof_status=ProofStatus.PROOF_FLAWED.value,
        routing_status=RoutingStatus.RETRY_SOLVER.value,
    )

    changed = orch._process_child_decomposition_for_lemma(problem, None, lemma, ProblemConfig())

    assert changed is True
    assert lemma.routing_status == RoutingStatus.BLOCKED.value
    assert lemma.next_action == "wait_on_child_decomposition"


def test_build_decomposition_generation_payload_includes_ancestor_shared_context() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    root_lemma = LemmaORM(
        lemma_id="lem_parent",
        problem_id="prob_ctx",
        parent_id="dec_root",
        parent_kind="decomposition",
        statement_nl="Parent statement",
    )
    child_lemma = LemmaORM(
        lemma_id="lem_child",
        problem_id="prob_ctx",
        parent_id="dec_child",
        parent_kind="decomposition",
        statement_nl="Child statement",
    )
    lemma_rows = {
        "lem_parent": root_lemma,
        "lem_child": child_lemma,
    }
    decomp_rows = {
        "dec_child": DecompositionORM(
            decomposition_id="dec_child",
            problem_id="prob_ctx",
            node_id="lem_parent",
            node_kind="lemma",
            shared_context=[
                {"kind": "definition", "label": "h_n", "content": "child definition"},
                {"kind": "definition", "label": "g_n", "content": "shared definition"},
            ],
        ),
        "dec_root": DecompositionORM(
            decomposition_id="dec_root",
            problem_id="prob_ctx",
            node_id="thm_root",
            node_kind="theorem",
            shared_context=[
                {"kind": "definition", "label": "g_n", "content": "shared definition"},
                {"kind": "notation", "label": "S", "content": "root notation"},
            ],
        ),
    }
    orch.lemmas = SimpleNamespace(get=lambda lemma_id: lemma_rows.get(lemma_id))
    orch.decompositions = SimpleNamespace(get=lambda decomp_id: decomp_rows.get(decomp_id))
    orch.proof_graphs = SimpleNamespace(normalize_definition_context=lambda rows: list(rows))
    orch._previous_attempt_summaries = lambda *args, **kwargs: []
    orch._trusted_context_summaries = lambda *args, **kwargs: []

    payload = orch._build_decomposition_generation_payload(
        problem=ProblemORM(problem_id="prob_ctx"),
        node_id="lem_child",
        theorem_nl="Child statement",
        theorem_semantic_sketch={},
        num_candidates=1,
    )

    assert isinstance(payload, Agent2Input)
    assert payload.shared_context == [
        {"kind": "definition", "label": "h_n", "content": "child definition"},
        {"kind": "definition", "label": "g_n", "content": "shared definition"},
        {"kind": "notation", "label": "S", "content": "root notation"},
    ]


def test_waiting_on_child_frontier_helper_covers_solver_blocking_actions() -> None:
    lemma = LemmaORM(
        lemma_id="lem_guard",
        problem_id="prob_guard",
        parent_id="dec_parent",
        parent_kind="decomposition",
        statement_nl="guard",
    )

    assert Orchestrator._lemma_waiting_on_child_decomposition_frontier(lemma) is False

    for next_action in {
        "process_child_decomposition",
        "wait_on_child_decomposition",
        "retry_decomposition",
        "evaluate_child_decompositions",
    }:
        lemma.next_action = next_action
        assert Orchestrator._lemma_waiting_on_child_decomposition_frontier(lemma) is True


def test_previous_attempt_summaries_include_false_lemma_counterexample_constraints() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.decompositions = SimpleNamespace(
        list_by_node=lambda problem_id, node_id: [
            DecompositionORM(
                decomposition_id="dec_false",
                problem_id=problem_id,
                node_id=node_id,
                node_kind="lemma",
                strategy_summary="bad split",
                llm_vetting_status="fatal",
                controller_status=ControllerStatus.FAILED.value,
                invalidated_by_lemma_id="lem_false_child",
                invalidated_by_counterexample_id="cex_1",
                failure_origin="child_lemma_false",
                failure_reason="child lemma contradicted by n=1",
            )
        ]
    )
    orch.counterexamples = SimpleNamespace(
        get=lambda counterexample_id: SimpleNamespace(
            counterexample_id=counterexample_id,
            lemma_id="lem_false_child",
            parent_decomposition_id="dec_false",
            status="accepted",
            counterexample_text="n = 1 violates the child claim",
            summary="fails on the smallest nontrivial case",
            confidence=0.98,
            source_agent="agent5",
            source_attempt_number=2,
            accepted_by_report_id="vet_false",
            rejected_by_report_id=None,
        )
    )
    orch.artifacts = SimpleNamespace(exists=lambda key: False, load_json=lambda key: None)
    orch._reconstruct_agent2_candidate = lambda *args, **kwargs: {
        "lemmas": [
            {"local_id": "L1", "lemma_id": "lem_false_child", "statement_nl": "bad child"},
        ]
    }
    orch._load_decomposition_vetter_output = lambda *args, **kwargs: {
        "decision": "fatal",
        "summary": "contains a false child",
        "fixes_required": ["remove the false child"],
        "coverage_check": {"missing_coverage": [], "disguised_difficulty": []},
        "drift_assessment": {"drift_severity": "none"},
        "lemma_findings": [
            {
                "local_id": "L1",
                "statement_status": "false",
                "evidence": "counterexample found at n=1",
                "counterexample": "n = 1 violates the child claim",
            }
        ],
    }

    summaries = orch._previous_attempt_summaries("prob_test", "lem_parent")

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["invalidated_by_lemma_id"] == "lem_false_child"
    assert summary["invalidated_by_counterexample_id"] == "cex_1"
    assert summary["invalidating_counterexample"]["counterexample_text"] == "n = 1 violates the child claim"
    assert summary["false_lemma_findings"][0]["lemma_id"] == "lem_false_child"
    assert summary["false_lemma_findings"][0]["candidate_counterexample"] == "n = 1 violates the child claim"
    assert any("Do not reuse" in item for item in summary["hard_negative_constraints"])
