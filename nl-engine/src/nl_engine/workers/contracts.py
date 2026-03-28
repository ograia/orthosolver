from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


WorkerKind = Literal[
    "root_semantic_sketch",
    "decomposition_generation",
    "decomposition_vetting",
    "lemma_solver",
    "lemma_vetter",
    "proof_split_generation",
    "proof_split_vetting",
    "final_check",
    "lean_dispatch",
]


class WorkerJob(BaseModel):
    job_id: str
    problem_id: str
    worker_kind: WorkerKind
    payload: dict[str, Any]
    execution_id: str | None = None
    llm_overrides: dict[str, Any] | None = None
    worker_attempt_count: int | None = None


class WorkerResult(BaseModel):
    job_id: str
    worker_kind: WorkerKind
    status: Literal["completed", "failed"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
