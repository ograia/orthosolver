from __future__ import annotations

from datetime import UTC, datetime

from nl_engine.api.run_state import request_problem_stop
from nl_engine.domain.contracts import ProblemExecutionSummary
from nl_engine.domain.enums import ExecutionDesiredState, ExecutionStatus, ProblemStatus
from nl_engine.domain.models import ProblemExecutionORM
from nl_engine.lean_client.sessions import LeanSessionManager
from nl_engine.observability.events import EventLogger
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
    ProblemExecutionRepository,
    ProblemRepository,
    RequestRecordRepository,
    WorkerJobRepository,
)
from nl_engine.services.agents import AgentService
from nl_engine.services.ids import new_id


def execution_summary(row: ProblemExecutionORM | None) -> ProblemExecutionSummary | None:
    if row is None:
        return None
    return ProblemExecutionSummary(
        execution_id=row.execution_id,
        problem_id=row.problem_id,
        continuation_generation=int(row.continuation_generation or 0),
        status=row.status,
        desired_state=row.desired_state,
        current_stage=row.current_stage,
        blocking_kind=row.blocking_kind,
        blocking_ref_id=row.blocking_ref_id,
        trigger_source=row.trigger_source,
        wake_requested_at=row.wake_requested_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


class ProblemExecutionService:
    def __init__(self, store: FileStore) -> None:
        self.store = store
        self.executions = ProblemExecutionRepository(store)
        self.problems = ProblemRepository(store)
        self.worker_jobs = WorkerJobRepository(store)
        self.request_records = RequestRecordRepository(store)
        self.events = EventLogger(store)

    @staticmethod
    def _problem_generation(problem_status_row) -> int:
        try:
            return int(problem_status_row.continuation_generation)
        except Exception:
            return 0

    def _create_execution(
        self,
        *,
        problem_id: str,
        continuation_generation: int,
        trigger_source: str,
    ) -> ProblemExecutionORM:
        now = datetime.now(UTC)
        row = ProblemExecutionORM(
            execution_id=new_id("exec"),
            problem_id=problem_id,
            continuation_generation=max(0, int(continuation_generation)),
            status=ExecutionStatus.QUEUED.value,
            desired_state=ExecutionDesiredState.RUNNING.value,
            current_stage="execution.queued",
            blocking_kind="none",
            blocking_ref_id=None,
            wake_requested_at=now,
            trigger_source=trigger_source,
        )
        self.executions.create(row)
        self.events.transition(
            problem_id,
            "execution.created",
            None,
            row.status,
            reason=f"trigger={trigger_source}; generation={row.continuation_generation}",
        )
        return row

    def _active_executions(self, problem_id: str) -> list[ProblemExecutionORM]:
        active_statuses = {
            ExecutionStatus.QUEUED.value,
            ExecutionStatus.RUNNING.value,
            ExecutionStatus.WAITING.value,
            ExecutionStatus.CANCEL_REQUESTED.value,
        }
        return [
            row
            for row in self.executions.list_by_problem(problem_id)
            if row.status in active_statuses
        ]

    def _cancel_provider_responses_for_superseded_refs(
        self,
        *,
        problem_id: str,
        execution_ids: set[str],
        worker_job_ids: set[str],
    ) -> None:
        if not execution_ids and not worker_job_ids:
            return
        try:
            rows = self.request_records.list_by_problem(problem_id, limit=50_000)
        except Exception:
            return
        for row in rows:
            if row.status not in {"queued", "started", "pending"}:
                continue
            if row.execution_id not in execution_ids and row.worker_job_id not in worker_job_ids:
                continue
            response_id = str(row.response_id or "").strip()
            if not response_id:
                continue
            try:
                AgentService.best_effort_cancel_response(response_id)
            except Exception:
                continue

    def ensure_running(self, problem_id: str, *, trigger_source: str) -> ProblemExecutionORM:
        problem = self.problems.get(problem_id)
        if problem is None:
            raise ValueError("problem not found")
        if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            latest = self.executions.get_latest_for_problem(problem_id)
            if latest is not None:
                return latest
        existing = self.executions.get_active_for_problem(problem_id)
        now = datetime.now(UTC)
        if existing is not None:
            existing.desired_state = ExecutionDesiredState.RUNNING.value
            if existing.status in {ExecutionStatus.WAITING.value, ExecutionStatus.CANCEL_REQUESTED.value}:
                existing.status = ExecutionStatus.QUEUED.value
            existing.trigger_source = trigger_source
            existing.continuation_generation = self._problem_generation(problem)
            existing.wake_requested_at = now
            existing.updated_at = now
            self.executions.save(existing)
            return existing

        return self._create_execution(
            problem_id=problem_id,
            continuation_generation=self._problem_generation(problem),
            trigger_source=trigger_source,
        )

    def start_fresh_continuation(
        self,
        problem_id: str,
        *,
        trigger_source: str,
        supersede_reason: str,
        set_problem_running: bool = True,
    ) -> ProblemExecutionORM:
        problem = self.problems.get(problem_id)
        if problem is None:
            raise ValueError("problem not found")

        active_rows = self._active_executions(problem_id)
        active_execution_ids = {row.execution_id for row in active_rows}
        LeanSessionManager(self.store).cancel_problem_lean_work(
            problem_id,
            reason=supersede_reason,
            terminate_session=True,
            clear_metadata=True,
        )

        problem.continuation_generation = self._problem_generation(problem) + 1
        if set_problem_running:
            problem.status = ProblemStatus.RUNNING.value
        self.problems.save(problem)

        # Create new execution first so superseded jobs can reference it.
        fresh_execution = self._create_execution(
            problem_id=problem_id,
            continuation_generation=problem.continuation_generation,
            trigger_source=trigger_source,
        )

        superseded_jobs = self.worker_jobs.supersede_older_inflight(
            problem_id,
            min_generation=problem.continuation_generation,
            superseded_by_execution_id=fresh_execution.execution_id,
            reason=supersede_reason,
        )
        superseded_worker_job_ids = {row.worker_job_id for row in superseded_jobs}

        for row in active_rows:
            self.executions.complete(
                row.execution_id,
                status=ExecutionStatus.CANCELLED.value,
                current_stage=f"execution.superseded:{supersede_reason}",
            )

        self.request_records.mark_superseded(
            problem_id,
            execution_ids=active_execution_ids,
            worker_job_ids=superseded_worker_job_ids,
        )
        self._cancel_provider_responses_for_superseded_refs(
            problem_id=problem_id,
            execution_ids=active_execution_ids,
            worker_job_ids=superseded_worker_job_ids,
        )
        return fresh_execution

    def request_cancel(self, problem_id: str) -> ProblemExecutionORM | None:
        LeanSessionManager(self.store).cancel_problem_lean_work(
            problem_id,
            reason="api cancel requested",
            terminate_session=True,
            clear_metadata=True,
        )
        row = self.executions.get_active_for_problem(problem_id)
        if row is None:
            return self.executions.get_latest_for_problem(problem_id)
        previous = row.status
        row.desired_state = ExecutionDesiredState.STOPPED.value
        row.status = ExecutionStatus.CANCEL_REQUESTED.value
        row.wake_requested_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        self.executions.save(row)
        self.events.transition(
            problem_id,
            "execution.cancel_requested",
            previous,
            row.status,
            reason="api cancel requested",
        )
        request_problem_stop(problem_id)
        return row

    def request_pause(self, problem_id: str) -> ProblemExecutionORM | None:
        problem = self.problems.get(problem_id)
        if problem is None:
            return None
        if problem.status not in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            old = problem.status
            problem.status = ProblemStatus.PAUSED.value
            self.problems.save(problem)
            self.events.transition(
                problem_id,
                "problem.paused",
                old,
                problem.status,
                reason="api pause requested",
            )
        LeanSessionManager(self.store).cancel_problem_lean_work(
            problem_id,
            reason="api pause requested",
            terminate_session=True,
            clear_metadata=True,
        )
        row = self.executions.get_active_for_problem(problem_id)
        if row is None:
            return self.executions.get_latest_for_problem(problem_id)
        previous = row.status
        row.desired_state = ExecutionDesiredState.STOPPED.value
        row.status = ExecutionStatus.CANCEL_REQUESTED.value
        row.wake_requested_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        self.executions.save(row)
        self.events.transition(
            problem_id,
            "execution.pause_requested",
            previous,
            row.status,
            reason="api pause requested",
        )
        request_problem_stop(problem_id)
        # Re-assert paused after execution state mutation so pause remains
        # authoritative even under concurrent stale controller commits.
        latest_problem = self.problems.get(problem_id)
        if latest_problem is not None and latest_problem.status not in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            if latest_problem.status != ProblemStatus.PAUSED.value:
                latest_problem.status = ProblemStatus.PAUSED.value
                self.problems.save(latest_problem)
        return row
