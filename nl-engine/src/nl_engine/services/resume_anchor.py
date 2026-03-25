from __future__ import annotations

from typing import Tuple

from nl_engine.domain.enums import ControllerStatus, ProofStatus, RoutingStatus
from nl_engine.domain.models import ProblemORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
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
