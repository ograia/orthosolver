from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


WorkerKind = Literal[
    "decomposition_generation",
    "decomposition_vetting",
    "lemma_solver",
    "lemma_vetter",
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


class WorkerResult(BaseModel):
    job_id: str
    worker_kind: WorkerKind
    status: Literal["completed", "failed"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
