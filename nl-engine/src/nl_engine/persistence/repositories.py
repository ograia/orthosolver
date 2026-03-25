"""File-based repository implementations, replacing SQLAlchemy queries."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import re
from typing import Any

from nl_engine.domain.models import (
    AssemblyPlanORM,
    CounterexampleORM,
    DecompositionORM,
    EventORM,
    FailureReportORM,
    LeanJobORM,
    LeanResultORM,
    LemmaORM,
    LemmaProofAttemptORM,
    LlmUsageRecordORM,
    ProblemExecutionORM,
    ProblemORM,
    ProofDependencyCheckORM,
    ProofGraphEdgeORM,
    ProofGraphNodeORM,
    ProofGraphORM,
    RequestRecordORM,
    RunCostRollupORM,
    TheoremORM,
    TrustedContextORM,
    VetterReportORM,
    WorkerJobORM,
)
from nl_engine.persistence.db import FileStore


def _stamp_for_create(row: Any) -> Any:
    now = datetime.now(UTC)
    if hasattr(row, "updated_at"):
        row.updated_at = now
    if hasattr(row, "last_activity_at"):
        row.last_activity_at = now
    return row


def _stamp_for_save(row: Any) -> Any:
    now = datetime.now(UTC)
    if hasattr(row, "updated_at"):
        row.updated_at = now
    if hasattr(row, "last_activity_at"):
        row.last_activity_at = now
    return row


class ProblemRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _path(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "problem.json"

    def create(self, problem: ProblemORM) -> ProblemORM:
        _stamp_for_create(problem)
        with self.store.lock_for(problem.problem_id):
            self.store.atomic_write(self._path(problem.problem_id), problem.model_dump(mode="json"))
        self.store.update_index(problem.problem_id, problem.title, problem.status, str(problem.created_at))
        return problem

    def save(self, problem: ProblemORM) -> ProblemORM:
        _stamp_for_save(problem)
        with self.store.lock_for(problem.problem_id):
            self.store.atomic_write(self._path(problem.problem_id), problem.model_dump(mode="json"))
        self.store.update_index_status(problem.problem_id, problem.status)
        return problem

    def get(self, problem_id: str) -> ProblemORM | None:
        data = self.store.read_json(self._path(problem_id))
        if data is None:
            return None
        return ProblemORM.model_validate(data)

    def touch(self, problem: ProblemORM) -> None:
        problem.updated_at = datetime.now(UTC)
        self.save(problem)


class ProblemExecutionRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "execution"

    def _path(self, problem_id: str, execution_id: str):
        return self._dir(problem_id) / f"{execution_id}.json"

    def create(self, execution: ProblemExecutionORM) -> ProblemExecutionORM:
        _stamp_for_create(execution)
        with self.store.lock_for(execution.problem_id):
            self.store.atomic_write(self._path(execution.problem_id, execution.execution_id), execution.model_dump(mode="json"))
        return execution

    def save(self, execution: ProblemExecutionORM) -> ProblemExecutionORM:
        _stamp_for_save(execution)
        with self.store.lock_for(execution.problem_id):
            self.store.atomic_write(self._path(execution.problem_id, execution.execution_id), execution.model_dump(mode="json"))
        return execution

    def get(self, execution_id: str) -> ProblemExecutionORM | None:
        for pid in self.store.list_problem_ids():
            p = self._path(pid, execution_id)
            data = self.store.read_json(p)
            if data is not None:
                return ProblemExecutionORM.model_validate(data)
        return None

    def get_latest_for_problem(self, problem_id: str) -> ProblemExecutionORM | None:
        rows = self._list_all(problem_id)
        if not rows:
            return None
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[0]

    def get_active_for_problem(self, problem_id: str) -> ProblemExecutionORM | None:
        active_statuses = {"queued", "running", "waiting", "cancel_requested"}
        rows = self._list_all(problem_id)
        active = [r for r in rows if r.status in active_statuses]
        if not active:
            return None
        active.sort(key=lambda r: r.created_at, reverse=True)
        return active[0]

    def list_by_problem(self, problem_id: str) -> list[ProblemExecutionORM]:
        rows = self._list_all(problem_id)
        rows.sort(key=lambda r: r.created_at)
        return rows

    def _list_all(self, problem_id: str) -> list[ProblemExecutionORM]:
        dicts = self.store.glob_read(self._dir(problem_id))
        return [ProblemExecutionORM.model_validate(d) for d in dicts]

    def claim_next(self, worker_id: str, now: datetime, lease_seconds: int) -> ProblemExecutionORM | None:
        lease_deadline = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        for pid in sorted(self.store.list_problem_ids()):
            with self.store.lock_for(pid):
                for data in self.store.glob_read(self._dir(pid)):
                    row = ProblemExecutionORM.model_validate(data)
                    if row.desired_state != "running":
                        continue
                    eligible = False
                    if row.status == "queued":
                        eligible = True
                    elif row.status == "waiting" and (row.wake_requested_at is None or row.wake_requested_at <= now):
                        eligible = True
                    elif row.status == "running" and row.lease_expires_at is not None and row.lease_expires_at < now:
                        eligible = True
                    if not eligible:
                        continue
                    row.status = "running"
                    row.lease_owner = worker_id
                    row.lease_expires_at = lease_deadline
                    row.updated_at = now
                    if row.started_at is None:
                        row.started_at = now
                    self.store.atomic_write(self._path(pid, row.execution_id), row.model_dump(mode="json"))
                    return row
        return None

    def renew_lease(self, execution_id: str, lease_seconds: int) -> ProblemExecutionORM | None:
        row = self.get(execution_id)
        if row is None:
            return None
        now = datetime.now(UTC)
        row.lease_expires_at = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        row.updated_at = now
        self.save(row)
        return row

    def mark_waiting(self, execution_id: str, *, current_stage: str | None, blocking_kind: str, blocking_ref_id: str | None, wake_requested_at: datetime | None) -> ProblemExecutionORM | None:
        row = self.get(execution_id)
        if row is None:
            return None
        row.status = "waiting"
        row.current_stage = current_stage
        row.blocking_kind = blocking_kind
        row.blocking_ref_id = blocking_ref_id
        row.wake_requested_at = wake_requested_at
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = datetime.now(UTC)
        self.save(row)
        return row

    def wake(self, execution_id: str, *, current_stage: str | None = None) -> ProblemExecutionORM | None:
        row = self.get(execution_id)
        if row is None:
            return None
        if row.status not in {"succeeded", "failed", "cancelled"}:
            row.status = "queued"
        if current_stage is not None:
            row.current_stage = current_stage
        row.blocking_kind = "none"
        row.blocking_ref_id = None
        row.wake_requested_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        self.save(row)
        return row

    def complete(self, execution_id: str, *, status: str, current_stage: str | None, blocking_kind: str = "none", blocking_ref_id: str | None = None, last_error_payload: dict | None = None) -> ProblemExecutionORM | None:
        row = self.get(execution_id)
        if row is None:
            return None
        now = datetime.now(UTC)
        row.status = status
        row.current_stage = current_stage
        row.blocking_kind = blocking_kind
        row.blocking_ref_id = blocking_ref_id
        row.last_error_payload = last_error_payload
        row.completed_at = now
        row.lease_owner = None
        row.lease_expires_at = None
        row.wake_requested_at = None
        row.updated_at = now
        self.save(row)
        return row

    def release(self, execution_id: str, *, current_stage: str | None = None) -> ProblemExecutionORM | None:
        row = self.get(execution_id)
        if row is None:
            return None
        row.lease_owner = None
        row.lease_expires_at = None
        if current_stage is not None:
            row.current_stage = current_stage
        row.updated_at = datetime.now(UTC)
        self.save(row)
        return row


class RequestRecordRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "request_records"

    def _path(self, problem_id: str, record_id: str):
        return self._dir(problem_id) / f"{record_id}.json"

    def create(self, record: RequestRecordORM) -> RequestRecordORM:
        with self.store.lock_for(record.problem_id):
            self.store.atomic_write(self._path(record.problem_id, record.request_record_id), record.model_dump(mode="json"))
        return record

    def get(self, request_record_id: str) -> RequestRecordORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, request_record_id))
            if data is not None:
                return RequestRecordORM.model_validate(data)
        return None

    def upsert(
        self,
        request_record_id: str,
        *,
        problem_id: str,
        execution_id: str | None,
        worker_job_id: str | None,
        source: str,
        target_id: str | None,
        status: str,
        response_id: str | None = None,
        error_class: str | None = None,
        summary: str | None = None,
        llm_model: str | None = None,
        llm_reasoning_effort: str | None = None,
        llm_text_verbosity: str | None = None,
        llm_timeout_seconds: int | None = None,
        request_artifact_key: str | None = None,
        response_artifact_key: str | None = None,
    ) -> RequestRecordORM:
        now = datetime.now(UTC)
        with self.store.lock_for(problem_id):
            existing_data = self.store.read_json(self._path(problem_id, request_record_id))
            if existing_data:
                row = RequestRecordORM.model_validate(existing_data)
                row.execution_id = execution_id
                row.worker_job_id = worker_job_id
                row.source = source
                row.target_id = target_id
                row.status = status
                row.response_id = response_id
                row.error_class = error_class
                row.summary = summary
                if llm_model is not None:
                    row.llm_model = llm_model
                if llm_reasoning_effort is not None:
                    row.llm_reasoning_effort = llm_reasoning_effort
                if llm_text_verbosity is not None:
                    row.llm_text_verbosity = llm_text_verbosity
                if llm_timeout_seconds is not None:
                    row.llm_timeout_seconds = llm_timeout_seconds
                row.request_artifact_key = request_artifact_key
                row.response_artifact_key = response_artifact_key
                row.updated_at = now
            else:
                row = RequestRecordORM(
                    request_record_id=request_record_id,
                    problem_id=problem_id,
                    execution_id=execution_id,
                    worker_job_id=worker_job_id,
                    source=source,
                    target_id=target_id,
                    status=status,
                    response_id=response_id,
                    error_class=error_class,
                    summary=summary,
                    llm_model=llm_model,
                    llm_reasoning_effort=llm_reasoning_effort,
                    llm_text_verbosity=llm_text_verbosity,
                    llm_timeout_seconds=llm_timeout_seconds,
                    request_artifact_key=request_artifact_key,
                    response_artifact_key=response_artifact_key,
                    created_at=now,
                    updated_at=now,
                )
            self.store.atomic_write(self._path(problem_id, request_record_id), row.model_dump(mode="json"))
        return row

    def list_by_problem(self, problem_id: str, *, source: str | None = None, limit: int = 1000) -> list[RequestRecordORM]:
        rows = [RequestRecordORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        if source is not None:
            rows = [r for r in rows if r.source == source]
        rows.sort(key=lambda r: r.created_at)
        return rows[:limit]

    def mark_superseded(
        self,
        problem_id: str,
        *,
        execution_ids: set[str] | None = None,
        worker_job_ids: set[str] | None = None,
    ) -> int:
        execution_ids = {item for item in (execution_ids or set()) if item}
        worker_job_ids = {item for item in (worker_job_ids or set()) if item}
        if not execution_ids and not worker_job_ids:
            return 0
        touched = 0
        now = datetime.now(UTC)
        with self.store.lock_for(problem_id):
            for data in self.store.glob_read(self._dir(problem_id)):
                row = RequestRecordORM.model_validate(data)
                if row.status not in {"queued", "started", "pending"}:
                    continue
                if row.execution_id not in execution_ids and row.worker_job_id not in worker_job_ids:
                    continue
                row.status = "superseded"
                row.updated_at = now
                self.store.atomic_write(self._path(problem_id, row.request_record_id), row.model_dump(mode="json"))
                touched += 1
        return touched


class TheoremRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _path(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "theorem.json"

    def create(self, theorem: TheoremORM) -> TheoremORM:
        with self.store.lock_for(theorem.problem_id):
            self.store.atomic_write(self._path(theorem.problem_id), theorem.model_dump(mode="json"))
        return theorem

    def save(self, theorem: TheoremORM) -> TheoremORM:
        return self.create(theorem)

    def get(self, theorem_id: str) -> TheoremORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid))
            if data and data.get("theorem_id") == theorem_id:
                return TheoremORM.model_validate(data)
        return None

    def get_root_for_problem(self, problem_id: str) -> TheoremORM | None:
        data = self.store.read_json(self._path(problem_id))
        if data is None:
            return None
        return TheoremORM.model_validate(data)


class DecompositionRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "decompositions"

    def _path(self, problem_id: str, decomposition_id: str):
        return self._dir(problem_id) / f"{decomposition_id}.json"

    def create(self, decomposition: DecompositionORM) -> DecompositionORM:
        _stamp_for_create(decomposition)
        with self.store.lock_for(decomposition.problem_id):
            self.store.atomic_write(self._path(decomposition.problem_id, decomposition.decomposition_id), decomposition.model_dump(mode="json"))
        return decomposition

    def save(self, decomposition: DecompositionORM) -> DecompositionORM:
        _stamp_for_save(decomposition)
        return self.create(decomposition)

    def create_many(self, decompositions: list[DecompositionORM]) -> None:
        for d in decompositions:
            self.create(d)

    def get(self, decomposition_id: str) -> DecompositionORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, decomposition_id))
            if data is not None:
                return DecompositionORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[DecompositionORM]:
        rows = [DecompositionORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda r: r.created_at)
        return rows

    def list_by_node(self, problem_id: str, node_id: str) -> list[DecompositionORM]:
        return [d for d in self.list_by_problem(problem_id) if d.node_id == node_id]

    def list_by_logical_id(self, problem_id: str, logical_decomposition_id: str) -> list[DecompositionORM]:
        rows = [
            d
            for d in self.list_by_problem(problem_id)
            if str(d.logical_decomposition_id or "").strip() == str(logical_decomposition_id).strip()
        ]
        rows.sort(key=lambda r: (r.revision_number, r.created_at))
        return rows

    def list_active_candidates(self, problem_id: str) -> list[DecompositionORM]:
        return [d for d in self.list_by_problem(problem_id) if d.llm_vetting_status == "accepted" and d.lean_assembly_status in {"success", "skipped"}]


class AssemblyPlanRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "assembly_plans"

    def _path(self, problem_id: str, plan_id: str):
        return self._dir(problem_id) / f"{plan_id}.json"

    def create(self, plan: AssemblyPlanORM) -> AssemblyPlanORM:
        with self.store.lock_for(plan.problem_id):
            self.store.atomic_write(self._path(plan.problem_id, plan.assembly_plan_id), plan.model_dump(mode="json"))
        return plan

    def get(self, assembly_plan_id: str) -> AssemblyPlanORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, assembly_plan_id))
            if data is not None:
                return AssemblyPlanORM.model_validate(data)
        return None


class LemmaRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "lemmas"

    def _path(self, problem_id: str, lemma_id: str):
        return self._dir(problem_id) / f"{lemma_id}.json"

    def create(self, lemma: LemmaORM) -> LemmaORM:
        _stamp_for_create(lemma)
        with self.store.lock_for(lemma.problem_id):
            self.store.atomic_write(self._path(lemma.problem_id, lemma.lemma_id), lemma.model_dump(mode="json"))
        return lemma

    def save(self, lemma: LemmaORM) -> LemmaORM:
        _stamp_for_save(lemma)
        return self.create(lemma)

    def create_many(self, lemmas: list[LemmaORM]) -> None:
        for lem in lemmas:
            self.create(lem)

    def get(self, lemma_id: str) -> LemmaORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, lemma_id))
            if data is not None:
                return LemmaORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[LemmaORM]:
        rows = [LemmaORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda r: r.created_at)
        return rows

    def list_by_parent(self, problem_id: str, parent_id: str) -> list[LemmaORM]:
        return [lem for lem in self.list_by_problem(problem_id) if lem.parent_id == parent_id]

    def list_open_for_decomposition(self, problem_id: str, lemma_ids: list[str]) -> list[LemmaORM]:
        if not lemma_ids:
            return []
        ids_set = set(lemma_ids)
        return [lem for lem in self.list_by_problem(problem_id) if lem.lemma_id in ids_set and lem.routing_status != "done"]

    def count_by_status(self, problem_id: str) -> dict[str, Any]:
        lemmas = self.list_by_problem(problem_id)
        proof_counter: Counter[str] = Counter(lem.proof_status for lem in lemmas)
        routing_counter: Counter[str] = Counter(lem.routing_status for lem in lemmas)
        return {"proof_status": dict(proof_counter), "routing_status": dict(routing_counter), "total": len(lemmas)}


class VetterReportRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "vetter_reports"

    def _path(self, problem_id: str, report_id: str):
        return self._dir(problem_id) / f"{report_id}.json"

    def create(self, report: VetterReportORM) -> VetterReportORM:
        with self.store.lock_for(report.problem_id):
            self.store.atomic_write(self._path(report.problem_id, report.report_id), report.model_dump(mode="json"))
        return report

    def get(self, report_id: str) -> VetterReportORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, report_id))
            if data is not None:
                return VetterReportORM.model_validate(data)
        return None

    def latest_for_target(self, problem_id: str, target_id: str) -> VetterReportORM | None:
        rows = [VetterReportORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id)) if d.get("target_id") == target_id]
        if not rows:
            return None
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[0]


class CounterexampleRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "counterexamples"

    def _path(self, problem_id: str, counterexample_id: str):
        return self._dir(problem_id) / f"{counterexample_id}.json"

    def create(self, row: CounterexampleORM) -> CounterexampleORM:
        _stamp_for_create(row)
        with self.store.lock_for(row.problem_id):
            self.store.atomic_write(self._path(row.problem_id, row.counterexample_id), row.model_dump(mode="json"))
        return row

    def save(self, row: CounterexampleORM) -> CounterexampleORM:
        _stamp_for_save(row)
        with self.store.lock_for(row.problem_id):
            self.store.atomic_write(self._path(row.problem_id, row.counterexample_id), row.model_dump(mode="json"))
        return row

    def get(self, counterexample_id: str) -> CounterexampleORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, counterexample_id))
            if data is not None:
                return CounterexampleORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[CounterexampleORM]:
        rows = [CounterexampleORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda r: (r.created_at, r.counterexample_id))
        return rows

    def list_by_lemma(self, problem_id: str, lemma_id: str) -> list[CounterexampleORM]:
        return [row for row in self.list_by_problem(problem_id) if row.lemma_id == lemma_id]


class LemmaProofAttemptRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "proof_attempts"

    def _path(self, problem_id: str, proof_attempt_id: str):
        return self._dir(problem_id) / f"{proof_attempt_id}.json"

    def create(self, row: LemmaProofAttemptORM) -> LemmaProofAttemptORM:
        _stamp_for_create(row)
        with self.store.lock_for(row.problem_id):
            self.store.atomic_write(self._path(row.problem_id, row.proof_attempt_id), row.model_dump(mode="json"))
        return row

    def save(self, row: LemmaProofAttemptORM) -> LemmaProofAttemptORM:
        _stamp_for_save(row)
        with self.store.lock_for(row.problem_id):
            self.store.atomic_write(self._path(row.problem_id, row.proof_attempt_id), row.model_dump(mode="json"))
        return row

    def get(self, proof_attempt_id: str) -> LemmaProofAttemptORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, proof_attempt_id))
            if data is not None:
                return LemmaProofAttemptORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[LemmaProofAttemptORM]:
        rows = [LemmaProofAttemptORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda r: (r.lemma_id, r.attempt_number, r.created_at))
        return rows

    def list_by_lemma(self, problem_id: str, lemma_id: str) -> list[LemmaProofAttemptORM]:
        rows = [row for row in self.list_by_problem(problem_id) if row.lemma_id == lemma_id]
        rows.sort(key=lambda r: (r.attempt_number, r.created_at))
        return rows

    def get_by_lemma_attempt(
        self,
        problem_id: str,
        lemma_id: str,
        attempt_number: int,
    ) -> LemmaProofAttemptORM | None:
        rows = [
            row
            for row in self.list_by_lemma(problem_id, lemma_id)
            if int(row.attempt_number) == int(attempt_number)
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[0]


class LeanJobRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "lean_jobs"

    def _path(self, problem_id: str, job_id: str):
        return self._dir(problem_id) / f"{job_id}.json"

    def create_if_absent(self, job: LeanJobORM) -> LeanJobORM:
        with self.store.lock_for(job.problem_id):
            p = self._path(job.problem_id, job.job_id)
            existing = self.store.read_json(p)
            if existing is not None:
                return LeanJobORM.model_validate(existing)
            self.store.atomic_write(p, job.model_dump(mode="json"))
        return job

    def save(self, job: LeanJobORM) -> LeanJobORM:
        with self.store.lock_for(job.problem_id):
            self.store.atomic_write(self._path(job.problem_id, job.job_id), job.model_dump(mode="json"))
        return job

    def get(self, job_id: str) -> LeanJobORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, job_id))
            if data is not None:
                return LeanJobORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[LeanJobORM]:
        rows = [LeanJobORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda r: r.created_at)
        return rows

    def list_by_target(self, problem_id: str, target_id: str) -> list[LeanJobORM]:
        return [j for j in self.list_by_problem(problem_id) if j.target_id == target_id]

    def list_non_terminal(self, problem_id: str) -> list[LeanJobORM]:
        return [j for j in self.list_by_problem(problem_id) if j.status in {"queued", "running"}]

    def count_by_mode_status(self, problem_id: str) -> dict[str, dict[str, int]]:
        jobs = self.list_by_problem(problem_id)
        matrix: dict[str, dict[str, int]] = {}
        for job in jobs:
            matrix.setdefault(job.mode, {})
            matrix[job.mode][job.status] = matrix[job.mode].get(job.status, 0) + 1
        return matrix


class LeanResultRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "lean_results"

    def _path(self, problem_id: str, result_id: str):
        return self._dir(problem_id) / f"{result_id}.json"

    def create(self, result: LeanResultORM) -> LeanResultORM:
        # Need problem_id from the parent LeanJob.
        # LeanResultORM doesn't have problem_id directly; look up the job.
        # For simplicity, store in all problem dirs by scanning for the job.
        for pid in self.store.list_problem_ids():
            job_path = self.store.problem_dir(pid) / "lean_jobs" / f"{result.job_id}.json"
            if job_path.exists():
                with self.store.lock_for(pid):
                    self.store.atomic_write(self._path(pid, result.result_id), result.model_dump(mode="json"))
                return result
        # Fallback: if job not found, still save (shouldn't happen in practice)
        return result

    def get(self, result_id: str) -> LeanResultORM | None:
        if result_id is None:
            return None
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, result_id))
            if data is not None:
                return LeanResultORM.model_validate(data)
        return None

    def get_for_job(self, job_id: str) -> LeanResultORM | None:
        for pid in self.store.list_problem_ids():
            for data in self.store.glob_read(self._dir(pid)):
                if data.get("job_id") == job_id:
                    return LeanResultORM.model_validate(data)
        return None


class TrustedContextRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _path(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "trusted_context.json"

    def create_if_absent(
        self,
        problem_id: str,
        decl_name: str,
        lean_code: str,
        source_lemma_id: str,
        source_job_id: str,
        *,
        proof_graph_id: str | None = None,
        context_scope: str = "problem_external",
    ) -> TrustedContextORM:
        with self.store.lock_for(problem_id):
            items = self._load(problem_id)
            for item in items:
                if (
                    item.get("decl_name") == decl_name
                    and item.get("proof_graph_id") == proof_graph_id
                    and item.get("context_scope", "problem_external") == context_scope
                ):
                    return TrustedContextORM.model_validate(item)
            new_id = len(items) + 1
            obj = TrustedContextORM(
                id=new_id,
                problem_id=problem_id,
                proof_graph_id=proof_graph_id,
                context_scope=context_scope,
                decl_name=decl_name,
                lean_code=lean_code,
                source_lemma_id=source_lemma_id,
                source_job_id=source_job_id,
            )
            items.append(obj.model_dump(mode="json"))
            self.store.atomic_write(self._path(problem_id), items)
            return obj

    def list_by_problem(self, problem_id: str) -> list[TrustedContextORM]:
        return [TrustedContextORM.model_validate(d) for d in self._load(problem_id)]

    def list_for_graph(
        self,
        problem_id: str,
        *,
        proof_graph_id: str | None,
        include_problem_external: bool = True,
    ) -> list[TrustedContextORM]:
        rows = self.list_by_problem(problem_id)
        allowed: list[TrustedContextORM] = []
        for row in rows:
            if row.context_scope == "problem_external":
                if include_problem_external:
                    allowed.append(row)
                continue
            if row.context_scope == "graph_local" and row.proof_graph_id == proof_graph_id:
                allowed.append(row)
        return allowed

    def _load(self, problem_id: str) -> list[dict[str, Any]]:
        data = self.store.read_json(self._path(problem_id))
        if isinstance(data, list):
            return data
        return []


class ProofGraphRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "proof_graphs"

    def _path(self, problem_id: str, proof_graph_id: str):
        return self._dir(problem_id) / f"{proof_graph_id}.json"

    def create(self, row: ProofGraphORM) -> ProofGraphORM:
        with self.store.lock_for(row.problem_id):
            self.store.atomic_write(self._path(row.problem_id, row.proof_graph_id), row.model_dump(mode="json"))
        return row

    def save(self, row: ProofGraphORM) -> ProofGraphORM:
        row.updated_at = datetime.now(UTC)
        return self.create(row)

    def get(self, proof_graph_id: str) -> ProofGraphORM | None:
        for child in self.store.root.iterdir():
            if not child.is_dir():
                continue
            data = self.store.read_json(self._path(child.name, proof_graph_id))
            if data is not None:
                return ProofGraphORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[ProofGraphORM]:
        rows = [ProofGraphORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda row: row.created_at)
        return rows


class ProofGraphNodeRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "proof_graph_nodes"

    def _path(self, problem_id: str, graph_node_id: str):
        return self._dir(problem_id) / f"{graph_node_id}.json"

    def create(self, row: ProofGraphNodeORM) -> ProofGraphNodeORM:
        problem_id = self._problem_id_for_graph(row.proof_graph_id)
        if problem_id is None:
            raise ValueError(f"unknown proof graph: {row.proof_graph_id}")
        with self.store.lock_for(problem_id):
            self.store.atomic_write(self._path(problem_id, row.graph_node_id), row.model_dump(mode="json"))
        return row

    def save(self, row: ProofGraphNodeORM) -> ProofGraphNodeORM:
        row.updated_at = datetime.now(UTC)
        return self.create(row)

    def get(self, graph_node_id: str) -> ProofGraphNodeORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, graph_node_id))
            if data is not None:
                return ProofGraphNodeORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str) -> list[ProofGraphNodeORM]:
        rows = [ProofGraphNodeORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda row: row.created_at)
        return rows

    def list_by_graph(self, proof_graph_id: str) -> list[ProofGraphNodeORM]:
        problem_id = self._problem_id_for_graph(proof_graph_id)
        if problem_id is None:
            return []
        return [row for row in self.list_by_problem(problem_id) if row.proof_graph_id == proof_graph_id]

    def find_by_owner(self, proof_graph_id: str, *, owner_kind: str, owner_id: str, node_kind: str | None = None) -> ProofGraphNodeORM | None:
        rows = [
            row
            for row in self.list_by_graph(proof_graph_id)
            if row.owner_kind == owner_kind and row.owner_id == owner_id and (node_kind is None or row.node_kind == node_kind)
        ]
        if not rows:
            return None
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[0]

    def _problem_id_for_graph(self, proof_graph_id: str) -> str | None:
        graph_repo = ProofGraphRepository(self.store)
        row = graph_repo.get(proof_graph_id)
        return None if row is None else row.problem_id


class ProofGraphEdgeRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "proof_graph_edges"

    def _path(self, problem_id: str, edge_id: str):
        return self._dir(problem_id) / f"{edge_id}.json"

    def create(self, row: ProofGraphEdgeORM) -> ProofGraphEdgeORM:
        problem_id = self._problem_id_for_graph(row.proof_graph_id)
        if problem_id is None:
            raise ValueError(f"unknown proof graph: {row.proof_graph_id}")
        with self.store.lock_for(problem_id):
            self.store.atomic_write(self._path(problem_id, row.edge_id), row.model_dump(mode="json"))
        return row

    def list_by_problem(self, problem_id: str) -> list[ProofGraphEdgeORM]:
        rows = [ProofGraphEdgeORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda row: row.created_at)
        return rows

    def list_by_graph(self, proof_graph_id: str) -> list[ProofGraphEdgeORM]:
        problem_id = self._problem_id_for_graph(proof_graph_id)
        if problem_id is None:
            return []
        return [row for row in self.list_by_problem(problem_id) if row.proof_graph_id == proof_graph_id]

    def _problem_id_for_graph(self, proof_graph_id: str) -> str | None:
        graph_repo = ProofGraphRepository(self.store)
        row = graph_repo.get(proof_graph_id)
        return None if row is None else row.problem_id


class ProofDependencyCheckRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "proof_dependency_checks"

    def _path(self, problem_id: str, dependency_check_id: str):
        return self._dir(problem_id) / f"{dependency_check_id}.json"

    def create(self, row: ProofDependencyCheckORM) -> ProofDependencyCheckORM:
        problem_id = self._problem_id_for_graph(row.proof_graph_id)
        if problem_id is None:
            raise ValueError(f"unknown proof graph: {row.proof_graph_id}")
        with self.store.lock_for(problem_id):
            self.store.atomic_write(self._path(problem_id, row.dependency_check_id), row.model_dump(mode="json"))
        return row

    def list_by_problem(self, problem_id: str) -> list[ProofDependencyCheckORM]:
        rows = [ProofDependencyCheckORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        rows.sort(key=lambda row: row.created_at)
        return rows

    def list_by_graph(self, proof_graph_id: str) -> list[ProofDependencyCheckORM]:
        problem_id = self._problem_id_for_graph(proof_graph_id)
        if problem_id is None:
            return []
        return [row for row in self.list_by_problem(problem_id) if row.proof_graph_id == proof_graph_id]

    def latest_for_target(self, proof_graph_id: str, *, target_node_id: str, artifact_kind: str | None = None) -> ProofDependencyCheckORM | None:
        rows = [
            row
            for row in self.list_by_graph(proof_graph_id)
            if row.target_node_id == target_node_id and (artifact_kind is None or row.artifact_kind == artifact_kind)
        ]
        if not rows:
            return None
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[0]

    def _problem_id_for_graph(self, proof_graph_id: str) -> str | None:
        graph_repo = ProofGraphRepository(self.store)
        row = graph_repo.get(proof_graph_id)
        return None if row is None else row.problem_id


class FailureReportRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _path(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "failure_report.json"

    def create_or_replace(self, report: FailureReportORM) -> FailureReportORM:
        with self.store.lock_for(report.problem_id):
            self.store.atomic_write(self._path(report.problem_id), report.model_dump(mode="json"))
        return report

    def get_by_problem(self, problem_id: str) -> FailureReportORM | None:
        data = self.store.read_json(self._path(problem_id))
        if data is None:
            return None
        return FailureReportORM.model_validate(data)


class EventRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _path(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "events.jsonl"

    def append(self, problem_id: str, stage: str, old_status: str | None, new_status: str | None, *, target_node_id: str | None = None, worker_job_id: str | None = None, reason: str | None = None) -> EventORM:
        with self.store.lock_for(problem_id):
            event_id = self.store.next_event_id(problem_id)
            event = EventORM(
                event_id=event_id,
                problem_id=problem_id,
                target_node_id=target_node_id,
                stage=stage,
                old_status=old_status,
                new_status=new_status,
                worker_job_id=worker_job_id,
                reason=reason,
            )
            self.store.append_jsonl(self._path(problem_id), event.model_dump(mode="json"))
        return event

    def list_for_problem(self, problem_id: str, after_event_id: int | None = None, limit: int = 100) -> list[EventORM]:
        rows = [EventORM.model_validate(d) for d in self.store.read_jsonl(self._path(problem_id))]
        if after_event_id is not None:
            rows = [r for r in rows if r.event_id > after_event_id]
        rows.sort(key=lambda r: r.event_id)
        return rows[:limit]

    def latest_for_problem(self, problem_id: str) -> EventORM | None:
        rows = self.store.read_jsonl(self._path(problem_id))
        if not rows:
            return None
        return EventORM.model_validate(rows[-1])


class WorkerJobRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _dir(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "worker_jobs"

    def _path(self, problem_id: str, job_id: str):
        return self._dir(problem_id) / f"{job_id}.json"

    def enqueue_if_absent(self, job: WorkerJobORM) -> WorkerJobORM:
        with self.store.lock_for(job.problem_id):
            p = self._path(job.problem_id, job.worker_job_id)
            existing = self.store.read_json(p)
            if existing is not None:
                return WorkerJobORM.model_validate(existing)
            self.store.atomic_write(p, job.model_dump(mode="json"))
        return job

    def save(self, job: WorkerJobORM) -> WorkerJobORM:
        with self.store.lock_for(job.problem_id):
            self.store.atomic_write(self._path(job.problem_id, job.worker_job_id), job.model_dump(mode="json"))
        return job

    def get(self, worker_job_id: str) -> WorkerJobORM | None:
        for pid in self.store.list_problem_ids():
            data = self.store.read_json(self._path(pid, worker_job_id))
            if data is not None:
                return WorkerJobORM.model_validate(data)
        return None

    def list_by_problem(self, problem_id: str, *, worker_kind: str | None = None) -> list[WorkerJobORM]:
        rows = [WorkerJobORM.model_validate(d) for d in self.store.glob_read(self._dir(problem_id))]
        if worker_kind is not None:
            rows = [r for r in rows if r.worker_kind == worker_kind]
        rows.sort(key=lambda r: r.created_at)
        return rows

    @staticmethod
    def _job_generation(row: WorkerJobORM) -> int:
        try:
            return int(row.continuation_generation)
        except Exception:
            return 0

    @staticmethod
    def _job_attempt_number(row: WorkerJobORM) -> int:
        try:
            if row.attempt_number is not None:
                return int(row.attempt_number)
        except Exception:
            pass
        if isinstance(row.payload, dict):
            raw = row.payload.get("attempt_number")
            try:
                if raw is not None:
                    return int(raw)
            except Exception:
                pass
        match = re.search(r"_(?:solve|vet)_(\d+)$", row.worker_job_id)
        if match is not None:
            try:
                return int(match.group(1))
            except Exception:
                pass
        return 0

    def claim_next(self, worker_kind: str, worker_id: str, now: datetime, lease_seconds: int, *, problem_id: str | None = None) -> WorkerJobORM | None:
        lease_deadline = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        pids = [problem_id] if problem_id else sorted(self.store.list_problem_ids())
        for pid in pids:
            with self.store.lock_for(pid):
                for data in self.store.glob_read(self._dir(pid)):
                    row = WorkerJobORM.model_validate(data)
                    if row.worker_kind != worker_kind:
                        continue
                    if row.status == "superseded" or row.superseded_at is not None:
                        continue
                    if row.attempt_count >= row.max_attempts:
                        continue
                    eligible = False
                    if row.status == "queued":
                        eligible = True
                    elif row.status == "running" and row.lease_expires_at is not None and row.lease_expires_at < now:
                        eligible = True
                    if not eligible:
                        continue
                    row.status = "running"
                    row.lease_owner = worker_id
                    row.lease_expires_at = lease_deadline
                    # Clear stale terminal metadata when reclaiming a retryable job.
                    row.error_payload = None
                    row.attempt_count += 1
                    row.updated_at = now
                    self.store.atomic_write(self._path(pid, row.worker_job_id), row.model_dump(mode="json"))
                    return row
        return None

    def complete(self, worker_job_id: str, result_payload: dict) -> WorkerJobORM | None:
        row = self.get(worker_job_id)
        if row is None:
            return None
        if row.superseded_at is not None:
            row.status = "superseded"
        else:
            row.status = "completed"
        row.result_payload = result_payload
        row.error_payload = None
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = datetime.now(UTC)
        self.save(row)
        return row

    def release(self, worker_job_id: str, *, status: str = "queued") -> WorkerJobORM | None:
        row = self.get(worker_job_id)
        if row is None:
            return None
        row.status = status
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = datetime.now(UTC)
        self.save(row)
        return row

    def renew_lease(self, worker_job_id: str, lease_seconds: int) -> WorkerJobORM | None:
        row = self.get(worker_job_id)
        if row is None:
            return None
        if row.status != "running":
            return row
        now = datetime.now(UTC)
        row.lease_expires_at = datetime.fromtimestamp(now.timestamp() + lease_seconds, tz=UTC)
        row.updated_at = now
        self.save(row)
        return row

    def fail(self, worker_job_id: str, error_payload: dict) -> WorkerJobORM | None:
        row = self.get(worker_job_id)
        if row is None:
            return None
        if row.superseded_at is not None:
            row.status = "superseded"
            row.error_payload = error_payload
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = datetime.now(UTC)
            self.save(row)
            return row
        error_class = str(error_payload.get("error_class") or "")
        retryable = error_class == "infrastructure_transient"
        if retryable:
            row.status = "failed" if row.attempt_count >= row.max_attempts else "queued"
        else:
            row.status = "failed"
            if row.attempt_count < row.max_attempts:
                row.attempt_count = row.max_attempts
        row.error_payload = error_payload
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = datetime.now(UTC)
        self.save(row)
        return row

    def consume_terminal(self, worker_job_id: str, *, execution_id: str | None = None) -> tuple[WorkerJobORM | None, bool]:
        row = self.get(worker_job_id)
        if row is None:
            return None, False
        if row.status == "superseded" or row.superseded_at is not None:
            return row, False
        if row.status not in {"completed", "failed"}:
            return row, False
        with self.store.lock_for(row.problem_id):
            latest = self.store.read_json(self._path(row.problem_id, worker_job_id))
            if latest is None:
                return None, False
            current = WorkerJobORM.model_validate(latest)
            if current.status == "superseded" or current.superseded_at is not None:
                return current, False
            if current.status not in {"completed", "failed"}:
                return current, False
            if current.controller_consumed_at is not None:
                return current, False
            current.controller_consumed_at = datetime.now(UTC)
            if execution_id and execution_id.strip():
                current.controller_consumed_by_execution_id = execution_id.strip()
            current.updated_at = datetime.now(UTC)
            self.store.atomic_write(self._path(row.problem_id, worker_job_id), current.model_dump(mode="json"))
            return current, True

    def requeue_expired(self, worker_kind: str, now: datetime) -> int:
        count = 0
        for pid in self.store.list_problem_ids():
            with self.store.lock_for(pid):
                for data in self.store.glob_read(self._dir(pid)):
                    row = WorkerJobORM.model_validate(data)
                    if row.worker_kind != worker_kind or row.status != "running":
                        continue
                    if row.status == "superseded" or row.superseded_at is not None:
                        continue
                    if row.lease_expires_at is None or row.lease_expires_at >= now:
                        continue
                    row.status = "queued" if row.attempt_count < row.max_attempts else "failed"
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.updated_at = now
                    self.store.atomic_write(self._path(pid, row.worker_job_id), row.model_dump(mode="json"))
                    count += 1
        return count

    def supersede_older_inflight(
        self,
        problem_id: str,
        *,
        min_generation: int,
        superseded_by_execution_id: str | None,
        reason: str,
    ) -> list[WorkerJobORM]:
        updated: list[WorkerJobORM] = []
        now = datetime.now(UTC)
        with self.store.lock_for(problem_id):
            for data in self.store.glob_read(self._dir(problem_id)):
                row = WorkerJobORM.model_validate(data)
                if row.status not in {"queued", "running"}:
                    continue
                if row.superseded_at is not None:
                    continue
                if self._job_generation(row) >= int(min_generation):
                    continue
                row.status = "superseded"
                row.superseded_at = now
                row.superseded_by_execution_id = superseded_by_execution_id
                row.supersede_reason = reason
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                self.store.atomic_write(self._path(problem_id, row.worker_job_id), row.model_dump(mode="json"))
                updated.append(row)
        return updated

    def find_by_target_request_attempt(
        self,
        *,
        problem_id: str,
        target_id: str,
        request_source: str,
        attempt_number: int,
        continuation_generation: int,
        statuses: set[str] | None = None,
    ) -> WorkerJobORM | None:
        rows = [
            row
            for row in self.list_by_problem(problem_id)
            if row.target_id == target_id
            and row.request_source == request_source
            and self._job_attempt_number(row) == int(attempt_number)
            and self._job_generation(row) == int(continuation_generation)
            and row.superseded_at is None
        ]
        if statuses is not None:
            rows = [row for row in rows if row.status in statuses]
        if not rows:
            return None
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[0]


class LlmUsageRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def create(self, usage: LlmUsageRecordORM) -> LlmUsageRecordORM:
        pid = usage.problem_id or "_global"
        with self.store.lock_for(pid):
            self.store.append_jsonl(self.store.problem_dir(pid) / "llm_usage.jsonl", usage.model_dump(mode="json"))
        return usage

    def list_by_problem(self, problem_id: str) -> list[LlmUsageRecordORM]:
        return [LlmUsageRecordORM.model_validate(d) for d in self.store.read_jsonl(self.store.problem_dir(problem_id) / "llm_usage.jsonl")]


class RunCostRollupRepository:
    def __init__(self, store: FileStore):
        self.store = store

    def _path(self, problem_id: str):
        return self.store.problem_dir(problem_id) / "cost_rollup.json"

    def upsert_daily_rollup(self, *, problem_id: str, rollup_date: str, input_tokens: int, output_tokens: int, estimated_cost_usd: float) -> RunCostRollupORM:
        with self.store.lock_for(problem_id):
            data = self.store.read_json(self._path(problem_id))
            rollups: dict[str, Any] = data if isinstance(data, dict) else {}
            existing = rollups.get(rollup_date)
            if existing:
                row = RunCostRollupORM.model_validate(existing)
                row.total_input_tokens += input_tokens
                row.total_output_tokens += output_tokens
                row.total_estimated_cost_usd += estimated_cost_usd
                row.updated_at = datetime.now(UTC)
            else:
                row = RunCostRollupORM(
                    rollup_id=f"rollup_{problem_id}_{rollup_date}",
                    problem_id=problem_id,
                    rollup_date=rollup_date,
                    total_input_tokens=input_tokens,
                    total_output_tokens=output_tokens,
                    total_estimated_cost_usd=estimated_cost_usd,
                )
            rollups[rollup_date] = row.model_dump(mode="json")
            self.store.atomic_write(self._path(problem_id), rollups)
        return row

    def list_by_problem(self, problem_id: str) -> list[RunCostRollupORM]:
        data = self.store.read_json(self._path(problem_id))
        if not isinstance(data, dict):
            return []
        return [RunCostRollupORM.model_validate(v) for v in data.values()]
