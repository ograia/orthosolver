from __future__ import annotations

from types import SimpleNamespace

from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.enums import ControllerStatus, ProofStatus, RoutingStatus
from nl_engine.domain.models import DecompositionORM, LemmaORM, ProblemORM


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
                llm_vetting_status="rejected_fatal",
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
