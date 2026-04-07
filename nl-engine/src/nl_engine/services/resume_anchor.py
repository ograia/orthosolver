from __future__ import annotations

from typing import Tuple

from nl_engine.domain.enums import ControllerStatus, NodeKind, ProofStatus, RoutingStatus, StatementStatus
from nl_engine.domain.models import ProblemORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
    CounterexampleRepository,
    DecompositionRepository,
    EventRepository,
    FailureReportRepository,
    LemmaRepository,
    TheoremRepository,
)


def _find_owner_decomposition_id(problem_id: str, lemma_id: str, db: FileStore) -> str | None:
    rows = DecompositionRepository(db).list_by_problem(problem_id)
    for row in reversed(rows):
        if lemma_id in (row.lemma_ids or []):
            return row.decomposition_id
    return None


def derive_resume_anchor(problem_id: str, db: FileStore) -> Tuple[str | None, str | None]:
    failure = FailureReportRepository(db).get_by_problem(problem_id)
    if failure is None:
        return None, None

    lemma_id = str(failure.terminal_lemma_id or "").strip()
    if not lemma_id:
        return None, None

    owner_decomposition_id = _find_owner_decomposition_id(problem_id, lemma_id, db)
    return lemma_id, owner_decomposition_id


def apply_resume_anchor(problem: ProblemORM, db: FileStore, *, route: str) -> tuple[str | None, str | None]:
    previous_anchor = problem.resume_anchor_lemma_id
    lemma_id, owner_decomposition_id = derive_resume_anchor(problem.problem_id, db)

    problem.resume_anchor_lemma_id = lemma_id
    problem.resume_anchor_owner_decomposition_id = owner_decomposition_id

    if not lemma_id:
        return None, None

    lemmas = LemmaRepository(db)
    lemma = lemmas.get(lemma_id)
    if lemma is not None and lemma.routing_status != RoutingStatus.DONE.value:
        if lemma.proof_status in {ProofStatus.PROOF_EXHAUSTED.value, ProofStatus.FAILED.value}:
            lemma.proof_status = ProofStatus.PROOF_FLAWED.value
        lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
        lemma.consecutive_fatal_rejections = 0
        lemma.minor_rejection_count = 0
        lemmas.save(lemma)

    if owner_decomposition_id:
        decompositions = DecompositionRepository(db)
        owner = decompositions.get(owner_decomposition_id)
        if owner is not None and owner.problem_id == problem.problem_id:
            if owner.controller_status == ControllerStatus.FAILED.value:
                owner.controller_status = ControllerStatus.ACTIVE.value
                decompositions.save(owner)
            problem.active_decomposition_id = owner.decomposition_id
            if problem.standby_decomposition_id == owner.decomposition_id:
                problem.standby_decomposition_id = None
            if owner.node_kind == "theorem" and problem.root_theorem_id:
                theorem = TheoremRepository(db).get(problem.root_theorem_id)
                if theorem is not None:
                    theorem.active_decomposition_id = owner.decomposition_id
                    TheoremRepository(db).save(theorem)

    EventRepository(db).append(
        problem.problem_id,
        "problem.resume_anchor_set",
        previous_anchor,
        lemma_id,
        target_node_id=lemma_id,
        reason=f"route={route}; owner_decomposition_id={owner_decomposition_id or '-'}",
    )
    return lemma_id, owner_decomposition_id


def derive_paused_false_branch_anchor(problem: ProblemORM, db: FileStore) -> tuple[str | None, str | None, str | None]:
    recent_events = EventRepository(db).list_for_problem(problem.problem_id, limit=10_000)
    if not any(row.stage == "problem.paused_for_false_root_child" for row in recent_events):
        return None, None, None

    counterexamples = [
        row
        for row in CounterexampleRepository(db).list_by_problem(problem.problem_id)
        if row.status == "accepted"
    ]
    if not counterexamples:
        return None, None, None

    latest_counterexample = counterexamples[-1]
    false_lemma = LemmaRepository(db).get(latest_counterexample.lemma_id)
    if false_lemma is None or false_lemma.problem_id != problem.problem_id:
        return None, None, None
    if false_lemma.parent_kind != NodeKind.LEMMA.value:
        return None, None, false_lemma.lemma_id

    anchor_lemma_id = str(false_lemma.parent_id or "").strip()
    if not anchor_lemma_id:
        return None, None, false_lemma.lemma_id

    owner_decomposition_id = _find_owner_decomposition_id(problem.problem_id, anchor_lemma_id, db)
    return anchor_lemma_id, owner_decomposition_id, false_lemma.lemma_id


def apply_paused_false_branch_anchor(problem: ProblemORM, db: FileStore, *, route: str) -> tuple[str | None, str | None]:
    previous_anchor = problem.resume_anchor_lemma_id
    lemma_id, owner_decomposition_id, false_lemma_id = derive_paused_false_branch_anchor(problem, db)

    problem.resume_anchor_lemma_id = lemma_id
    problem.resume_anchor_owner_decomposition_id = owner_decomposition_id

    if not lemma_id:
        return None, None

    lemmas = LemmaRepository(db)
    lemma = lemmas.get(lemma_id)
    if lemma is not None and lemma.problem_id == problem.problem_id and lemma.routing_status != RoutingStatus.DONE.value:
        if lemma.proof_status in {ProofStatus.PROOF_EXHAUSTED.value, ProofStatus.FAILED.value}:
            lemma.proof_status = ProofStatus.PROOF_FLAWED.value
        lemma.routing_status = RoutingStatus.DECOMPOSE_FURTHER.value
        lemma.next_action = "retry_decomposition"
        lemma.consecutive_fatal_rejections = 0
        lemma.minor_rejection_count = 0
        if not lemma.active_counterexample_id:
            lemma.truth_status = "unknown"
            lemma.statement_status = StatementStatus.UNVETTED.value
        lemmas.save(lemma)

    if owner_decomposition_id:
        decompositions = DecompositionRepository(db)
        owner = decompositions.get(owner_decomposition_id)
        if owner is not None and owner.problem_id == problem.problem_id:
            if owner.controller_status == ControllerStatus.FAILED.value:
                owner.controller_status = ControllerStatus.ACTIVE.value
                decompositions.save(owner)
            problem.active_decomposition_id = owner.decomposition_id
            if problem.standby_decomposition_id == owner.decomposition_id:
                problem.standby_decomposition_id = None
            if owner.node_kind == NodeKind.THEOREM.value and problem.root_theorem_id:
                theorem = TheoremRepository(db).get(problem.root_theorem_id)
                if theorem is not None:
                    theorem.active_decomposition_id = owner.decomposition_id
                    TheoremRepository(db).save(theorem)

    EventRepository(db).append(
        problem.problem_id,
        "problem.resume_anchor_set",
        previous_anchor,
        lemma_id,
        target_node_id=lemma_id,
        reason=(
            f"route={route}; "
            f"owner_decomposition_id={owner_decomposition_id or '-'}; "
            f"false_lemma_id={false_lemma_id or '-'}"
        ),
    )
    return lemma_id, owner_decomposition_id
