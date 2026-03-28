from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Any, Callable, Type

from pydantic import BaseModel

from nl_engine.api.run_state import is_problem_stop_requested
from nl_engine.artifacts.store import ArtifactStore
from nl_engine.domain.contracts import Agent1Input, Agent1Output, Agent2Input, Agent2Output, Agent3Input, Agent3Output, Agent4Input, Agent4Output, Agent5Input, Agent5Output, Agent6Input, Agent6Output, Agent7Input, Agent7Output, Agent8Input, Agent8Output
from nl_engine.services.agents import AgentExecutionError, AgentService
from nl_engine.settings import get_settings
from nl_engine.observability.metrics import MetricsExporter
from nl_engine.workers.contracts import WorkerJob, WorkerResult


class WorkerFacade:
    """Idempotent in-process worker boundary keyed by `job_id`.

    This mirrors the idempotency rule in docs/nl_engine.tex while allowing
    local development without Pub/Sub services.
    """

    def __init__(
        self,
        store: ArtifactStore | None = None,
        agent_service_factory: Type[AgentService] | None = None,
        agent_service_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.store = store or ArtifactStore()
        self.agent_service_factory = agent_service_factory
        self.agent_service_kwargs = agent_service_kwargs or {}
        settings = get_settings()
        worker_kinds = [
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
        self._max_pending = settings.worker_default_max_pending
        self._pending: dict[str, int] = {kind: 0 for kind in worker_kinds}
        self._lock = threading.Lock()
        self._semaphores = {
            kind: threading.BoundedSemaphore(value=max(1, settings.worker_default_max_concurrency))
            for kind in worker_kinds
        }
        self._background_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.worker_default_max_concurrency),
            thread_name_prefix="nl_engine_worker",
        )
        self._background_futures: dict[str, Future[Any]] = {}
        self._background_lock = threading.Lock()
        self.metrics = MetricsExporter()

    def _agent_service(self):
        factory = self.agent_service_factory or AgentService
        try:
            return factory(**self.agent_service_kwargs)
        except TypeError:
            # Test doubles may not accept extra kwargs.
            return factory()

    def _result_key(self, job_id: str) -> str:
        return f"worker_jobs/{job_id}/result.json"

    def _run_idempotent(self, job: WorkerJob, fn: Callable[[], BaseModel | dict[str, Any]]) -> WorkerResult:
        def _raise_if_stopped() -> None:
            if not is_problem_stop_requested(job.problem_id):
                return
            agent_key = {
                "root_semantic_sketch": "agent1",
                "decomposition_generation": "agent2",
                "decomposition_vetting": "agent3",
                "lemma_solver": "agent4",
                "lemma_vetter": "agent5",
                "proof_split_generation": "agent7",
                "proof_split_vetting": "agent8",
            }.get(job.worker_kind, job.worker_kind)
            raise AgentExecutionError(
                agent_key=agent_key,
                error_class="interrupted",
                message="worker execution interrupted by local reset",
                artifact_prefix=f"worker_jobs/{job.job_id}",
            )

        key = self._result_key(job.job_id)
        if self.store.exists(key):
            cached = self.store.load_json(key)
            cached_result = WorkerResult.model_validate(cached)
            if cached_result.status == "failed":
                cached_error = cached_result.error or {}
                error_class = str(cached_error.get("error_class") or "infrastructure")
                if error_class == "infrastructure_transient":
                    # Retryable transport failures must not permanently poison this job id.
                    try:
                        self.store.delete(key)
                    except OSError:
                        pass
                else:
                    raise AgentExecutionError(
                        agent_key=str(cached_error.get("agent_key") or job.worker_kind),
                        error_class=error_class,
                        message=str(cached_error.get("message") or f"worker job {job.job_id} failed"),
                        artifact_prefix=f"worker_jobs/{job.job_id}",
                    )
            else:
                return cached_result

        sem = self._semaphores[job.worker_kind]
        with self._lock:
            if self._pending[job.worker_kind] >= self._max_pending:
                self.metrics.emit("worker_queue_backlog", float(self._pending[job.worker_kind]), {"worker_kind": job.worker_kind})
                raise RuntimeError(f"worker queue backpressure for kind={job.worker_kind}")
            self._pending[job.worker_kind] += 1
            self.metrics.emit("worker_queue_backlog", float(self._pending[job.worker_kind]), {"worker_kind": job.worker_kind})

        acquired = sem.acquire(blocking=False)
        if not acquired:
            with self._lock:
                self._pending[job.worker_kind] -= 1
                self.metrics.emit("worker_queue_backlog", float(self._pending[job.worker_kind]), {"worker_kind": job.worker_kind})
            raise RuntimeError(f"worker concurrency exhausted for kind={job.worker_kind}")

        try:
            _raise_if_stopped()
            raw = fn()
            _raise_if_stopped()
            if isinstance(raw, BaseModel):
                output = raw.model_dump()
            else:
                output = raw
            result = WorkerResult(
                job_id=job.job_id,
                worker_kind=job.worker_kind,
                status="completed",
                output=output,
                error=None,
            )
        except Exception as exc:  # pragma: no cover - pass-through failure capture
            retryable_transient = isinstance(exc, AgentExecutionError) and exc.error_class == "infrastructure_transient"
            error_payload: dict[str, Any] = {
                "message": str(exc),
                "class": type(exc).__name__,
            }
            if isinstance(exc, AgentExecutionError):
                error_payload["error_class"] = exc.error_class
                error_payload["agent_key"] = exc.agent_key
            result = WorkerResult(
                job_id=job.job_id,
                worker_kind=job.worker_kind,
                status="failed",
                output=None,
                error=error_payload,
            )
            if not retryable_transient:
                self.store.save_json(key, result.model_dump())
            raise
        finally:
            sem.release()
            with self._lock:
                self._pending[job.worker_kind] -= 1
                self.metrics.emit("worker_queue_backlog", float(self._pending[job.worker_kind]), {"worker_kind": job.worker_kind})

        self.store.save_json(key, result.model_dump())
        return result

    def run_root_semantic_sketch(self, job: WorkerJob, payload: Agent1Input, artifact_prefix: str) -> Agent1Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.semantic_sketch(payload.statement_nl, artifact_prefix),
        )
        return Agent1Output.model_validate(result.output or {})

    def run_decomposition_generation(self, job: WorkerJob, payload: Agent2Input, artifact_prefix: str, *, override_key: str | None = None) -> Agent2Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.decompose(payload, artifact_prefix, override_key=override_key),
        )
        return Agent2Output.model_validate(result.output or {})

    def submit_decomposition_generation(
        self,
        job: WorkerJob,
        payload: Agent2Input,
        artifact_prefix: str,
    ) -> Future[Any]:
        with self._background_lock:
            existing = self._background_futures.get(job.job_id)
            if existing is not None:
                if not existing.done():
                    return existing
                del self._background_futures[job.job_id]

            future = self._background_executor.submit(
                self.run_decomposition_generation,
                job,
                payload,
                artifact_prefix,
            )
            self._background_futures[job.job_id] = future
            return future

    def get_background_future(self, job_id: str) -> Future[Any] | None:
        with self._background_lock:
            return self._background_futures.get(job_id)

    def clear_background_future(self, job_id: str) -> None:
        with self._background_lock:
            self._background_futures.pop(job_id, None)

    def run_decomposition_vetting(self, job: WorkerJob, payload: Agent3Input, artifact_prefix: str) -> Agent3Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.vet_decomposition(payload, artifact_prefix),
        )
        return Agent3Output.model_validate(result.output or {})

    def run_lemma_solver(self, job: WorkerJob, payload: Agent4Input, artifact_prefix: str, *, override_key: str | None = None) -> Agent4Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.solve_lemma(payload, artifact_prefix, override_key=override_key),
        )
        return Agent4Output.model_validate(result.output or {})

    def run_lemma_vetter(self, job: WorkerJob, payload: Agent5Input, artifact_prefix: str) -> Agent5Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.vet_lemma_proof(payload, artifact_prefix),
        )
        return Agent5Output.model_validate(result.output or {})

    def run_final_check(self, job: WorkerJob, payload: Agent6Input, artifact_prefix: str) -> Agent6Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.final_check(payload, artifact_prefix),
        )
        return Agent6Output.model_validate(result.output or {})

    def run_proof_split_generation(self, job: WorkerJob, payload: Agent7Input, artifact_prefix: str) -> Agent7Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.split_existing_proof(payload, artifact_prefix),
        )
        return Agent7Output.model_validate(result.output or {})

    def run_proof_split_vetting(self, job: WorkerJob, payload: Agent8Input, artifact_prefix: str) -> Agent8Output:
        service = self._agent_service()
        llm_overrides = getattr(job, "llm_overrides", None)
        if llm_overrides is not None and hasattr(service, "set_llm_overrides"):
            service.set_llm_overrides(llm_overrides)
        if hasattr(service, "set_runtime_context"):
            service.set_runtime_context(worker_job_id=job.job_id, execution_id=job.execution_id)
        result = self._run_idempotent(
            job,
            lambda: service.vet_split_existing_proof(payload, artifact_prefix),
        )
        return Agent8Output.model_validate(result.output or {})
