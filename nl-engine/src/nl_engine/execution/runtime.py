from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import threading
import time
from typing import Any

from nl_engine.api.run_state import (
    acquire_problem_run,
    begin_problem_activity,
    end_problem_activity,
    is_global_stop_active,
    release_problem_run,
)
from nl_engine.artifacts.store import ArtifactStore
from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.contracts import Agent1Input, Agent2Input, Agent3Input, Agent4Input, Agent5Input, Agent6Input, Agent7Input, Agent8Input
from nl_engine.domain.enums import ExecutionDesiredState, ExecutionStatus, ProblemStatus
from nl_engine.lean_client.sessions import LeanSessionManager
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import EventRepository, LeanJobRepository, ProblemExecutionRepository, ProblemRepository, WorkerJobRepository
from nl_engine.settings import get_settings
from nl_engine.workers import WorkerFacade
from nl_engine.workers import facade as workers_facade
from nl_engine.workers.contracts import WorkerJob, WorkerResult

log = logging.getLogger("nl_engine.execution")
AgentService = workers_facade.AgentService


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _int_generation(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


class ExecutionDriver:
    def __init__(self) -> None:
        self.settings = get_settings()

    @staticmethod
    def _reconcile_expired_worker_jobs(worker_jobs: WorkerJobRepository, *, now: datetime) -> int:
        changed = 0
        for worker_kind in ("root_semantic_sketch", "decomposition_generation", "decomposition_vetting", "lemma_solver", "lemma_vetter", "proof_split_generation", "proof_split_vetting", "final_check"):
            changed += worker_jobs.requeue_expired(worker_kind, now)
        return changed

    def advance_until_blocked(self, execution_id: str) -> bool:
        store = get_file_store()
        executions = ProblemExecutionRepository(store)
        problems = ProblemRepository(store)
        worker_jobs = WorkerJobRepository(store)
        events = EventRepository(store)

        execution = executions.get(execution_id)
        if execution is None:
            return False

        # Claim queued executions so they advance
        if execution.status in (ExecutionStatus.QUEUED.value, "queued"):
            execution.status = ExecutionStatus.RUNNING.value
            if execution.started_at is None:
                execution.started_at = _now_utc()
            execution.updated_at = _now_utc()
            executions.save(execution)

        problem = problems.get(execution.problem_id)
        if problem is None:
            executions.complete(
                execution.execution_id,
                status=ExecutionStatus.FAILED.value,
                current_stage="execution.failed",
                last_error_payload={"message": "problem not found"},
            )
            return True

        if _int_generation(execution.continuation_generation) < _int_generation(problem.continuation_generation):
            executions.complete(
                execution.execution_id,
                status=ExecutionStatus.CANCELLED.value,
                current_stage="execution.superseded_by_new_generation",
                last_error_payload=None,
            )
            return True

        if problem.status == ProblemStatus.PAUSED.value:
            executions.complete(
                execution.execution_id,
                status=ExecutionStatus.CANCELLED.value,
                current_stage="execution.paused",
                last_error_payload=None,
            )
            return True

        if execution.desired_state == ExecutionDesiredState.STOPPED.value and problem.status not in {
            ProblemStatus.SUCCEEDED.value,
            ProblemStatus.FAILED.value,
        }:
            executions.complete(
                execution.execution_id,
                status=ExecutionStatus.CANCELLED.value,
                current_stage="execution.cancelled",
                last_error_payload=None,
            )
            return True

        latest_event = events.latest_for_problem(problem.problem_id)
        latest_event_id = latest_event.event_id if latest_event else 0
        execution_generation = _int_generation(execution.continuation_generation)

        for _ in range(32):
            latest_problem = problems.get(problem.problem_id)
            if latest_problem is None:
                executions.complete(
                    execution.execution_id,
                    status=ExecutionStatus.FAILED.value,
                    current_stage="execution.failed",
                    last_error_payload={"message": "problem not found"},
                )
                return True
            if _int_generation(execution.continuation_generation) < _int_generation(latest_problem.continuation_generation):
                executions.complete(
                    execution.execution_id,
                    status=ExecutionStatus.CANCELLED.value,
                    current_stage="execution.superseded_by_new_generation",
                    last_error_payload=None,
                )
                return True
            if latest_problem.status == ProblemStatus.PAUSED.value:
                executions.complete(
                    execution.execution_id,
                    status=ExecutionStatus.CANCELLED.value,
                    current_stage="execution.paused",
                    last_error_payload=None,
                )
                return True
            problem = latest_problem

            # Renew the execution lease before each tick so long-running
            # operations (e.g. batch solver) don't cause lease expiration.
            executions.renew_lease(execution_id, self.settings.worker_lease_seconds)

            # Reconcile worker leases before checking whether the execution is
            # still blocked. This prevents an expired final-attempt job from
            # remaining "running" forever and wedging the run.
            self._reconcile_expired_worker_jobs(worker_jobs, now=_now_utc())

            before_updated = problem.updated_at
            before_status = problem.status
            before_event_id = latest_event_id
            problem = Orchestrator(store, execution_id=execution.execution_id).run_once(problem.problem_id)

            latest_event = events.latest_for_problem(problem.problem_id)
            latest_event_id = latest_event.event_id if latest_event else 0
            current_stage = latest_event.stage if latest_event else execution.current_stage

            if execution.desired_state == ExecutionDesiredState.STOPPED.value and problem.status not in {
                ProblemStatus.SUCCEEDED.value,
                ProblemStatus.FAILED.value,
            }:
                executions.complete(
                    execution.execution_id,
                    status=ExecutionStatus.CANCELLED.value,
                    current_stage="execution.cancelled",
                    last_error_payload=None,
                )
                return True

            if problem.status == ProblemStatus.SUCCEEDED.value:
                executions.complete(
                    execution.execution_id,
                    status=ExecutionStatus.SUCCEEDED.value,
                    current_stage=current_stage,
                )
                return True

            if problem.status == ProblemStatus.FAILED.value:
                executions.complete(
                    execution.execution_id,
                    status=ExecutionStatus.FAILED.value,
                    current_stage=current_stage,
                )
                return True

            inflight_jobs = [
                row
                for row in worker_jobs.list_by_problem(problem.problem_id)
                if row.status in {"queued", "running"}
                and row.superseded_at is None
                and _int_generation(row.continuation_generation) == execution_generation
            ]
            if inflight_jobs:
                executions.mark_waiting(
                    execution.execution_id,
                    current_stage=current_stage,
                    blocking_kind="worker_job",
                    blocking_ref_id=inflight_jobs[0].worker_job_id,
                    wake_requested_at=_now_utc() + timedelta(seconds=float(self.settings.worker_poll_interval_seconds)),
                )
                return True

            non_terminal_lean = Orchestrator(store, execution_id=execution.execution_id).lean_jobs.list_non_terminal(problem.problem_id)
            if non_terminal_lean:
                executions.mark_waiting(
                    execution.execution_id,
                    current_stage=current_stage,
                    blocking_kind="lean_job",
                    blocking_ref_id=non_terminal_lean[0].job_id,
                    wake_requested_at=_now_utc() + timedelta(seconds=float(self.settings.worker_poll_interval_seconds)),
                )
                return True

            progressed = (
                latest_event_id != before_event_id
                or problem.updated_at != before_updated
                or problem.status != before_status
            )
            if not progressed:
                executions.mark_waiting(
                    execution.execution_id,
                    current_stage=current_stage,
                    blocking_kind="none",
                    blocking_ref_id=None,
                    wake_requested_at=_now_utc() + timedelta(seconds=float(self.settings.worker_poll_interval_seconds)),
                )
                return True

        executions.mark_waiting(
            execution.execution_id,
            current_stage="execution.yield",
            blocking_kind="none",
            blocking_ref_id=None,
            wake_requested_at=_now_utc() + timedelta(seconds=float(self.settings.worker_poll_interval_seconds)),
        )
        return True


class StageWorkerRuntime:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.facade = WorkerFacade(
            agent_service_factory=workers_facade.AgentService,
            agent_service_kwargs={
                "db_session": get_file_store(),
                "usage_persistence_mode_override": self.settings.openai_usage_persistence_mode,
            },
        )
        self.store = ArtifactStore()

    def _problem_llm_overrides(self, problem_id: str) -> dict[str, Any] | None:
        store = get_file_store()
        problem = ProblemRepository(store).get(problem_id)
        if problem is None:
            return None
        try:
            cfg = ProblemConfig.model_validate(problem.config)
        except Exception:
            return None
        return cfg.llm.model_dump(exclude_none=True)

    def _start_worker_job_lease_heartbeat(self, worker_job_id: str) -> tuple[threading.Event, threading.Thread]:
        stop_event = threading.Event()
        interval_seconds = max(0.25, min(30.0, float(self.settings.worker_lease_seconds) / 3.0))

        def _heartbeat() -> None:
            while not stop_event.wait(interval_seconds):
                try:
                    WorkerJobRepository(get_file_store()).renew_lease(
                        worker_job_id,
                        self.settings.worker_lease_seconds,
                    )
                except Exception:
                    continue

        thread = threading.Thread(
            target=_heartbeat,
            daemon=True,
            name=f"worker-lease-{worker_job_id}",
        )
        thread.start()
        return stop_event, thread

    def _dispatch(
        self,
        *,
        worker_job_id: str,
        problem_id: str,
        worker_kind: str,
        payload: dict[str, Any],
        execution_id: str | None,
        artifact_prefix: str | None,
        llm_override_key: str | None = None,
        continuation_generation: int | None = None,
        attempt_number: int | None = None,
    ) -> dict[str, Any]:
        resolved_artifact_prefix = artifact_prefix or f"worker_jobs/{worker_job_id}"
        llm_overrides = self._problem_llm_overrides(problem_id)
        job = WorkerJob(
            job_id=worker_job_id,
            problem_id=problem_id,
            worker_kind=worker_kind,
            payload=payload,
            execution_id=execution_id,
            llm_overrides=llm_overrides,
        )
        if worker_kind == "decomposition_generation":
            output = self.facade.run_decomposition_generation(job, Agent2Input.model_validate(payload), resolved_artifact_prefix, override_key=llm_override_key)
            return output.model_dump()
        if worker_kind == "root_semantic_sketch":
            output = self.facade.run_root_semantic_sketch(job, Agent1Input.model_validate(payload), resolved_artifact_prefix)
            return output.model_dump()
        if worker_kind == "decomposition_vetting":
            output = self.facade.run_decomposition_vetting(job, Agent3Input.model_validate(payload), resolved_artifact_prefix)
            return output.model_dump()
        if worker_kind == "lemma_solver":
            output = self.facade.run_lemma_solver(job, Agent4Input.model_validate(payload), resolved_artifact_prefix, override_key=llm_override_key)
            return output.model_dump()
        if worker_kind == "lemma_vetter":
            output = self.facade.run_lemma_vetter(job, Agent5Input.model_validate(payload), resolved_artifact_prefix)
            return output.model_dump()
        if worker_kind == "proof_split_generation":
            output = self.facade.run_proof_split_generation(job, Agent7Input.model_validate(payload), resolved_artifact_prefix)
            return output.model_dump()
        if worker_kind == "proof_split_vetting":
            output = self.facade.run_proof_split_vetting(job, Agent8Input.model_validate(payload), resolved_artifact_prefix)
            return output.model_dump()
        if worker_kind == "final_check":
            output = self.facade.run_final_check(job, Agent6Input.model_validate(payload), resolved_artifact_prefix)
            return output.model_dump()
        raise RuntimeError(f"unsupported worker kind: {worker_kind}")

    def process_next(self, worker_id: str, *, problem_id: str | None = None) -> bool:
        if is_global_stop_active():
            return False
        for worker_kind in ("root_semantic_sketch", "decomposition_generation", "decomposition_vetting", "lemma_solver", "lemma_vetter", "proof_split_generation", "proof_split_vetting", "final_check"):
            claimed: dict[str, Any] | None = None
            store = get_file_store()
            repo = WorkerJobRepository(store)
            row = repo.claim_next(
                worker_kind,
                worker_id,
                _now_utc(),
                lease_seconds=self.settings.worker_lease_seconds,
                problem_id=problem_id,
            )
            if row is None:
                continue
            if not begin_problem_activity(row.problem_id):
                repo.release(row.worker_job_id, status="queued")
                continue
            claimed = {
                "worker_job_id": row.worker_job_id,
                "problem_id": row.problem_id,
                "worker_kind": row.worker_kind,
                "payload": dict(row.payload) if isinstance(row.payload, dict) else {},
                "execution_id": row.execution_id,
                "continuation_generation": _int_generation(row.continuation_generation),
                "artifact_prefix": row.artifact_prefix,
                "llm_override_key": row.llm_override_key,
            }

            if claimed is None:
                continue

            lease_stop, lease_thread = self._start_worker_job_lease_heartbeat(claimed["worker_job_id"])
            try:
                output_payload = self._dispatch(**claimed)
                self.store.save_json(
                    f"worker_jobs/{claimed['worker_job_id']}/result.json",
                    WorkerResult(
                        job_id=claimed["worker_job_id"],
                        worker_kind=claimed["worker_kind"],
                        status="completed",
                        output=output_payload,
                    ).model_dump(),
                )
                store = get_file_store()
                repo = WorkerJobRepository(store)
                executions = ProblemExecutionRepository(store)
                stored_row = repo.complete(claimed["worker_job_id"], output_payload)
                if claimed["execution_id"] and stored_row is not None and stored_row.status != "superseded":
                    execution_row = executions.get(claimed["execution_id"])
                    if (
                        execution_row is not None
                        and execution_row.status not in {"succeeded", "failed", "cancelled"}
                        and _int_generation(execution_row.continuation_generation) == int(claimed["continuation_generation"])
                    ):
                        executions.wake(claimed["execution_id"], current_stage=f"worker.{claimed['worker_kind']}.completed")
            except Exception as exc:
                log.exception(
                    "Worker job %s (%s) failed: %s",
                    claimed["worker_job_id"], claimed["worker_kind"], exc,
                )
                error_payload = {
                    "message": str(exc),
                    "class": type(exc).__name__,
                }
                if hasattr(exc, "error_class"):
                    error_payload["error_class"] = getattr(exc, "error_class")
                if hasattr(exc, "agent_key"):
                    error_payload["agent_key"] = getattr(exc, "agent_key")
                try:
                    self.store.save_json(
                        f"worker_jobs/{claimed['worker_job_id']}/result.json",
                        WorkerResult(
                            job_id=claimed["worker_job_id"],
                            worker_kind=claimed["worker_kind"],
                            status="failed",
                            error=error_payload,
                        ).model_dump(),
                    )
                    store = get_file_store()
                    repo = WorkerJobRepository(store)
                    executions = ProblemExecutionRepository(store)
                    stored = repo.fail(claimed["worker_job_id"], error_payload)
                    if (
                        claimed["execution_id"]
                        and stored
                        and stored.status in {"queued", "failed"}
                        and stored.status != "superseded"
                    ):
                        execution_row = executions.get(claimed["execution_id"])
                        if (
                            execution_row is not None
                            and execution_row.status not in {"succeeded", "failed", "cancelled"}
                            and _int_generation(execution_row.continuation_generation) == int(claimed["continuation_generation"])
                        ):
                            stage = "failed" if stored.status == "failed" else "retry_queued"
                            executions.wake(claimed["execution_id"], current_stage=f"worker.{claimed['worker_kind']}.{stage}")
                except Exception:
                    log.exception(
                        "Failed to persist error for worker job %s", claimed["worker_job_id"],
                    )
            finally:
                lease_stop.set()
                lease_thread.join(timeout=1)
                end_problem_activity(claimed["problem_id"])
            return True
        return False


class WorkerSupervisor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.execution_driver = ExecutionDriver()
        self.stage_runtime = StageWorkerRuntime()

    def start(self) -> None:
        stage_workers = max(1, int(self.settings.worker_default_max_concurrency))
        expected_threads = 2 + stage_workers
        if self._threads:
            if len(self._threads) == expected_threads and all(thread.is_alive() for thread in self._threads):
                return
            self.stop()
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run_execution_loop, name="nl_engine_execution_supervisor", daemon=True),
            threading.Thread(target=self._run_reconcile_loop, name="nl_engine_reconcile_supervisor", daemon=True),
        ]
        self._threads.extend(
            threading.Thread(target=self._run_stage_loop, name=f"nl_engine_stage_supervisor_{index}", daemon=True)
            for index in range(stage_workers)
        )
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def _process_execution(self, worker_id: str) -> bool:
        if is_global_stop_active():
            return False
        store = get_file_store()
        repo = ProblemExecutionRepository(store)
        execution = repo.claim_next(
            worker_id,
            _now_utc(),
            lease_seconds=self.settings.worker_lease_seconds,
        )
        if execution is None:
            return False
        execution_id = execution.execution_id
        problem_id = execution.problem_id
        if not acquire_problem_run(problem_id):
            return False
        try:
            self.execution_driver.advance_until_blocked(execution_id)
        except Exception as exc:
            store = get_file_store()
            repo = ProblemExecutionRepository(store)
            row = repo.get(execution_id)
            if row is not None:
                error_payload = {"message": str(exc), "class": type(exc).__name__}
                repo.complete(
                    execution_id,
                    status=ExecutionStatus.FAILED.value,
                    current_stage="execution.failed",
                    last_error_payload=error_payload,
                )
        finally:
            release_problem_run(problem_id)
        return True

    def _run_execution_loop(self) -> None:
        worker_id = f"embedded-exec-{threading.get_ident()}"
        poll_sleep = max(0.05, float(self.settings.worker_poll_interval_seconds))
        while not self._stop.is_set():
            try:
                did_work = self._process_execution(worker_id)
            except Exception:
                log.exception("Execution loop error (worker=%s)", worker_id)
                did_work = False
            if not did_work:
                time.sleep(poll_sleep)

    def _reconcile_external_progress(self) -> bool:
        store = get_file_store()
        executions = ProblemExecutionRepository(store)
        worker_jobs = WorkerJobRepository(store)
        lean_jobs = LeanJobRepository(store)
        changed = False

        for problem_id in sorted(store.list_problem_ids()):
            execution = executions.get_active_for_problem(problem_id)
            if execution is None:
                continue
            problem = ProblemRepository(store).get(problem_id)
            if problem is None:
                continue

            has_unconsumed_worker = any(
                row.status in {"completed", "failed"} and row.controller_consumed_at is None
                for row in worker_jobs.list_by_problem(problem_id)
            )
            has_terminal_lean_ready = False
            orch = Orchestrator(store)
            lean_client = LeanSessionManager(store).client_for_problem(problem, create_if_missing=True)
            for job in lean_jobs.list_non_terminal(problem_id):
                try:
                    if job.remote_operation_id and hasattr(lean_client, "get_operation"):
                        try:
                            polled = lean_client.get_operation(job.remote_operation_id, version="v2")
                        except Exception:
                            polled = lean_client.get_job(job.job_id)
                    else:
                        polled = lean_client.get_job(job.job_id)
                except Exception:
                    continue
                if polled.get("status") not in {"queued", "running"}:
                    has_terminal_lean_ready = True
                    break

            if not has_unconsumed_worker and not has_terminal_lean_ready:
                continue

            if execution.status in {"waiting", "queued", "cancel_requested"}:
                executions.wake(
                    execution.execution_id,
                    current_stage="execution.external_progress_reconciled",
                )
                changed = True

        return changed

    def _run_reconcile_loop(self) -> None:
        poll_sleep = max(0.05, float(self.settings.worker_poll_interval_seconds))
        while not self._stop.is_set():
            try:
                did_work = self._reconcile_external_progress()
            except Exception:
                log.exception("Reconcile loop error")
                did_work = False
            if not did_work:
                time.sleep(poll_sleep)

    def _run_stage_loop(self) -> None:
        worker_id = f"embedded-stage-{threading.get_ident()}"
        poll_sleep = max(0.05, float(self.settings.worker_poll_interval_seconds))
        while not self._stop.is_set():
            try:
                did_work = self.stage_runtime.process_next(worker_id)
            except Exception:
                log.exception("Stage loop error (worker=%s)", worker_id)
                did_work = False
            if not did_work:
                time.sleep(poll_sleep)


_SUPERVISOR: WorkerSupervisor | None = None
_SUPERVISOR_LOCK = threading.Lock()


def get_worker_supervisor() -> WorkerSupervisor:
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            _SUPERVISOR = WorkerSupervisor()
        return _SUPERVISOR


def start_embedded_supervisor_if_enabled() -> None:
    settings = get_settings()
    if not settings.worker_enable_embedded_supervisor:
        return
    LeanSessionManager(get_file_store()).recover_active_problem_sessions()
    get_worker_supervisor().start()


def is_embedded_supervisor_running() -> bool:
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            return False
        return bool(_SUPERVISOR._threads and all(t.is_alive() for t in _SUPERVISOR._threads))


def stop_embedded_supervisor() -> None:
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            return
        _SUPERVISOR.stop()
        _SUPERVISOR = None
