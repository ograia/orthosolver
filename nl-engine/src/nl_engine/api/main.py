from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import logging
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from nl_engine.persistence.db import FileStore, get_file_store

from nl_engine.api.debug import register_debug_ui, router as debug_router
from nl_engine.api.deps import get_db
from nl_engine.api.run_state import acquire_problem_run, is_global_stop_active, release_problem_run
from nl_engine.artifacts.store import ArtifactStore
from nl_engine.domain.contracts import (
    EventItem,
    EventsResponse,
    FailureReportResponse,
    LeanJobStatusItem,
    LeanJobsResponse,
    LemmaLeanStatus,
    ProblemCostSummaryResponse,
    ProblemCancelResponse,
    ProblemCreateRequest,
    ProblemCreateResponse,
    ProblemExecutionResponse,
    ProblemGetResponse,
    ProblemPauseResponse,
    ProblemResumeResponse,
    ProblemRunResponse,
    ProblemStartResponse,
    ProblemSummary,
    ProblemTreeResponse,
    ProgressResponse,
    TreeNode,
)
from nl_engine.domain.enums import NodeKind, NodeStatus, ProblemStatus, VerificationLevel
from nl_engine.domain.models import ProblemORM, TheoremORM
from nl_engine.execution.runtime import (
    ExecutionDriver,
    StageWorkerRuntime,
    is_embedded_supervisor_running,
    start_embedded_supervisor_if_enabled,
    stop_embedded_supervisor,
)
from nl_engine.persistence.repositories import (
    DecompositionRepository,
    EventRepository,
    FailureReportRepository,
    LeanJobRepository,
    LeanResultRepository,
    LlmUsageRepository,
    LemmaRepository,
    ProblemExecutionRepository,
    ProblemRepository,
    RequestRecordRepository,
    RunCostRollupRepository,
    TheoremRepository,
    TrustedContextRepository,
    WorkerJobRepository,
)
from nl_engine.services.agents import AgentService
from nl_engine.services.executions import ProblemExecutionService, execution_summary
from nl_engine.services.ids import new_id
from nl_engine.services.resume_anchor import apply_resume_anchor
from nl_engine.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="Orthosolver NL Engine", version="0.2.0")
app.include_router(debug_router)
register_debug_ui(app)


@app.on_event("startup")
def _start_embedded_workers() -> None:
    # Auto-start the supervisor so queued executions are processed
    # even without active browser polling. The supervisor checks
    # is_enabled internally based on settings.
    import os
    if os.environ.get("NL_ENGINE_NO_AUTO_SUPERVISOR") != "1":
        start_embedded_supervisor_if_enabled()


@app.on_event("shutdown")
def _stop_embedded_workers() -> None:
    stop_embedded_supervisor()


def now_utc() -> datetime:
    return datetime.now(UTC)


def _error_payload(*, code: str, message: str, details: dict | None = None) -> dict:
    return {
        "request_id": new_id("req"),
        "server_time": now_utc().isoformat(),
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }


def raise_api_error(status_code: int, *, code: str, message: str, details: dict | None = None) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message, "details": details or {}})


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code", "http_error"))
        message = str(detail.get("message", "request failed"))
        details = detail.get("details") if isinstance(detail.get("details"), dict) else {}
    else:
        code = "http_error"
        message = str(detail)
        details = {}
    return JSONResponse(status_code=exc.status_code, content=_error_payload(code=code, message=message, details=details))


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_payload(code="validation_error", message="invalid request payload", details={"errors": exc.errors()}),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_error_payload(code="internal_error", message="unhandled server error", details={"exception": type(exc).__name__}),
    )


def tree_summary(db: FileStore, problem_id: str, root_theorem_id: str | None) -> dict:
    lemma_repo = LemmaRepository(db)
    lemmas = lemma_repo.list_by_problem(problem_id)
    counts = lemma_repo.count_by_status(problem_id)
    return {
        "root_theorem_id": root_theorem_id,
        "lemma_count": len(lemmas),
        "lemma_statuses": {
            "proof": {k: v for k, v in counts.get("proof_status", {}).items()},
            "routing": {k: v for k, v in counts.get("routing_status", {}).items()},
        },
    }


def _latest_execution(db: FileStore, problem_id: str):
    return ProblemExecutionRepository(db).get_latest_for_problem(problem_id)


def _await_compatibility_progress(problem_id: str, *, timeout_seconds: float = 0.35) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        store = get_file_store()
        problem = ProblemRepository(store).get(problem_id)
        execution = ProblemExecutionRepository(store).get_active_for_problem(problem_id)
        if problem and problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            return
        if execution is None:
            return
        if execution.status in {"waiting", "succeeded", "failed", "cancelled"} and problem and problem.status != ProblemStatus.CREATED.value:
            return
        time.sleep(0.02)


def _run_compatibility_pump(problem_id: str, execution_id: str, *, timeout_seconds: float = 2.0) -> None:
    # When the embedded supervisor is already driving execution and stage
    # workers, creating additional drivers here only adds SQLite write-lock
    # contention.  Fall back to polling only.
    if is_embedded_supervisor_running():
        _await_compatibility_progress(problem_id, timeout_seconds=timeout_seconds)
        return

    deadline = time.monotonic() + timeout_seconds
    worker_id = f"compat-{new_id('wrk')}"
    stage_runtime = StageWorkerRuntime()
    execution_driver = ExecutionDriver()
    while time.monotonic() < deadline:
        did_work = False
        did_work |= stage_runtime.process_next(worker_id, problem_id=problem_id)
        if acquire_problem_run(problem_id):
            try:
                did_work |= execution_driver.advance_until_blocked(execution_id)
            finally:
                release_problem_run(problem_id)
        store = get_file_store()
        problem = ProblemRepository(store).get(problem_id)
        execution = ProblemExecutionRepository(store).get(execution_id)
        if problem and problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            return
        if execution is None:
            return
        if execution.status in {"waiting", "succeeded", "failed", "cancelled"} and not did_work:
            return
        if not did_work:
            return
        time.sleep(0.01)


def _build_problem_tree(db: FileStore, problem_id: str, theorem: TheoremORM) -> TreeNode:
    lemma_rows = LemmaRepository(db).list_by_problem(problem_id)
    lemma_map = {lemma.lemma_id: lemma for lemma in lemma_rows}
    by_parent: dict[str, list] = {}
    for lemma in lemma_rows:
        by_parent.setdefault(lemma.parent_id, []).append(lemma)

    def build_lemma_node(lemma_id: str) -> TreeNode:
        lemma = lemma_map.get(lemma_id)
        if lemma is None:
            return TreeNode(id=lemma_id, kind=NodeKind.LEMMA.value, status="unknown", children=[])
        children = [build_lemma_node(child.lemma_id) for child in by_parent.get(lemma.lemma_id, [])]
        return TreeNode(
            id=lemma.lemma_id,
            kind=NodeKind.LEMMA.value,
            status=lemma.proof_status,
            children=children,
        )

    root_children = [build_lemma_node(child.lemma_id) for child in by_parent.get(theorem.theorem_id, [])]
    return TreeNode(
        id=theorem.theorem_id,
        kind=NodeKind.THEOREM.value,
        status=theorem.status,
        children=root_children,
    )


def _run_semantic_sketch_background(
    problem_id: str,
    theorem_id: str,
    statement_nl: str,
    llm_overrides: dict,
) -> None:
    """Run Agent 1 semantic sketch in a background thread and update the theorem."""
    log = logging.getLogger("nl_engine.api")
    store = get_file_store()
    try:
        agent = AgentService(db_session=store, llm_overrides=llm_overrides)

        sketch = agent.semantic_sketch(statement_nl, f"problems/{problem_id}/inputs")
        theorem = TheoremRepository(store).get(theorem_id)
        if theorem:
            theorem.statement_semantic_sketch = sketch.semantic_sketch.model_dump()
            TheoremRepository(store).save(theorem)
            EventRepository(store).append(
                problem_id, "problem.semantic_sketch_ready", None, "ready",
                target_node_id=theorem_id,
            )
            # Wake any waiting execution so it picks up the sketch immediately
            execution = ProblemExecutionRepository(store).get_active_for_problem(problem_id)
            if execution:
                ProblemExecutionRepository(store).wake(
                    execution.execution_id,
                    current_stage="problem.semantic_sketch_ready",
                )
            log.info("Semantic sketch ready for problem %s", problem_id)
    except Exception:
        log.exception("Background semantic sketch failed for problem %s", problem_id)
        # Set a minimal valid sketch so the problem doesn't hang forever.
        theorem = TheoremRepository(store).get(theorem_id)
        if theorem and not theorem.statement_semantic_sketch:
            theorem.statement_semantic_sketch = {
                "variables": [], "quantifier_order": [], "domain_restrictions": [],
                "witness_dependencies": [], "normalized_claim": statement_nl,
            }
            TheoremRepository(store).save(theorem)
        EventRepository(store).append(
            problem_id, "problem.semantic_sketch_failed", None, "failed",
            target_node_id=theorem_id,
        )
        # Wake execution even on failure so it doesn't stall
        try:
            execution = ProblemExecutionRepository(store).get_active_for_problem(problem_id)
            if execution:
                ProblemExecutionRepository(store).wake(
                    execution.execution_id,
                    current_stage="problem.semantic_sketch_failed",
                )
        except Exception:
            log.exception("Failed to wake execution after sketch failure for %s", problem_id)


@app.post("/v1/problems", response_model=ProblemCreateResponse)
def create_problem(payload: ProblemCreateRequest, db: FileStore = Depends(get_db)) -> ProblemCreateResponse:
    log = logging.getLogger("nl_engine.api")
    log.info("POST /v1/problems — creating problem: %s", payload.title[:80])
    if is_global_stop_active():
        raise_api_error(409, code="reset_in_progress", message="local reset is in progress; try again shortly")
    llm_overrides = payload.config.llm.model_dump(exclude_none=True)
    artifacts = ArtifactStore()
    problem_id = new_id("prob")
    theorem_id = new_id("thm_root")
    create_request_artifact = artifacts.save_json(
        f"problems/{problem_id}/api/problem_create_request.json",
        payload.model_dump(by_alias=True),
    )

    # Create problem and theorem records immediately with empty sketch placeholder.
    # Agent 1 runs in a background thread and updates the sketch when ready.
    problem = ProblemORM(
        problem_id=problem_id,
        status=ProblemStatus.CREATED.value,
        title=payload.title,
        input_mode="nl_only" if payload.config.mode.nl_only_mode else "both",
        root_theorem_id=theorem_id,
        active_decomposition_id=None,
        standby_decomposition_id=None,
        lean_image_tag=payload.lean_image_tag,
        config=payload.config.model_dump(by_alias=True),
        nl_only_mode=payload.config.mode.nl_only_mode,
        verification_level=VerificationLevel.NL_ONLY.value if payload.config.mode.nl_only_mode else VerificationLevel.FORMAL.value,
        dependency_verification_level="shadow",
        failure_report_artifact_id=None,
    )

    theorem = TheoremORM(
        theorem_id=theorem_id,
        problem_id=problem_id,
        kind=NodeKind.THEOREM.value,
        statement_nl=payload.statement_nl,
        statement_lean=payload.statement_lean,
        statement_semantic_sketch={},
        status=NodeStatus.OPEN.value,
        active_decomposition_id=None,
        final_decl_name=None,
        artifact_ids=[],
    )

    ProblemRepository(db).create(problem)
    TheoremRepository(db).create(theorem)

    trusted_repo = TrustedContextRepository(db)
    for item in payload.initial_trusted_context:
        decl_name = item.get("decl_name")
        lean_code = item.get("lean_code")
        if decl_name and lean_code:
            trusted_repo.create_if_absent(
                problem_id,
                decl_name,
                lean_code,
                source_lemma_id="initial",
                source_job_id="initial",
                context_scope="problem_external",
            )

    EventRepository(db).append(problem_id, "problem.created", None, ProblemStatus.CREATED.value, target_node_id=theorem_id)

    # Launch Agent 1 in background thread
    threading.Thread(
        target=_run_semantic_sketch_background,
        args=(problem_id, theorem_id, payload.statement_nl, llm_overrides),
        daemon=True,
        name=f"sketch-{problem_id}",
    ).start()

    response = ProblemCreateResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem_id,
        root_theorem_id=theorem_id,
        status="created",
    )
    create_response_artifact = artifacts.save_json(
        f"problems/{problem_id}/api/problem_create_response.json",
        response.model_dump(mode="json"),
    )
    RequestRecordRepository(db).upsert(
        f"reqrec_api_create_{problem_id}",
        problem_id=problem_id,
        execution_id=None,
        worker_job_id=None,
        source="api_create",
        target_id=problem_id,
        status="completed",
        summary=f"title={payload.title[:80]} statement={payload.statement_nl[:120]}",
        request_artifact_key=create_request_artifact,
        response_artifact_key=create_response_artifact,
    )

    log.info("POST /v1/problems — returning immediately, problem_id=%s", problem_id)
    return response


@app.get("/v1/problems/{problem_id}", response_model=ProblemGetResponse)
def get_problem(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemGetResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")

    execution = _latest_execution(db, problem_id)
    summary = ProblemSummary(
        problem_id=problem.problem_id,
        status=problem.status,
        verification_level=problem.verification_level,
        nl_only_mode=problem.nl_only_mode,
        lean_mode=not problem.nl_only_mode,
        root_theorem_id=problem.root_theorem_id,
        active_decomposition_id=problem.active_decomposition_id,
        standby_decomposition_id=problem.standby_decomposition_id,
        execution_id=execution.execution_id if execution else None,
        execution_status=execution.status if execution else None,
        current_stage=execution.current_stage if execution else None,
        blocking_kind=execution.blocking_kind if execution else None,
        blocking_ref_id=execution.blocking_ref_id if execution else None,
    )

    return ProblemGetResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem=summary,
        tree_summary=tree_summary(db, problem.problem_id, problem.root_theorem_id),
    )


def _schedule_problem_execution(
    problem_id: str,
    *,
    source: str,
    trigger: str,
    force_fresh: bool = False,
    db: FileStore,
):
    if is_global_stop_active():
        raise_api_error(409, code="reset_in_progress", message="local reset is in progress; new execution starts are blocked")
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")
    if source == "api_start" and problem.status == ProblemStatus.PAUSED.value and trigger != "continue_button":
        EventRepository(db).append(
            problem_id,
            "problem.paused_start_rejected",
            ProblemStatus.PAUSED.value,
            ProblemStatus.PAUSED.value,
            reason=f"source={source} trigger={trigger}",
        )
        raise_api_error(
            409,
            code="problem_paused_use_continue",
            message="problem is paused; use Continue to resume",
        )

    artifacts = ArtifactStore()
    req_id = new_id("start_req" if source == "api_start" else "run_req")
    request_dir = "start_requests" if source == "api_start" else "run_requests"
    run_req_artifact = f"problems/{problem_id}/api/{request_dir}/{req_id}.request.json"
    run_res_artifact = f"problems/{problem_id}/api/{request_dir}/{req_id}.response.json"
    artifacts.save_json(
        run_req_artifact,
        {
            "request_id": req_id,
            "problem_id": problem_id,
            "server_time": now_utc().isoformat(),
            "trigger": trigger,
            "source": source,
        },
    )
    request_record_id = f"reqrec_{source}_{req_id}"
    RequestRecordRepository(db).upsert(
        request_record_id,
        problem_id=problem_id,
        execution_id=None,
        worker_job_id=None,
        source=source,
        target_id=problem_id,
        status="started",
        summary=f"trigger={trigger}",
        request_artifact_key=run_req_artifact,
    )
    execution_service = ProblemExecutionService(db)
    if force_fresh and problem.status not in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
        execution = execution_service.start_fresh_continuation(
            problem_id,
            trigger_source=trigger,
            supersede_reason=f"{source}:{trigger}",
            set_problem_running=True,
        )
    else:
        execution = execution_service.ensure_running(problem_id, trigger_source=trigger)

    start_embedded_supervisor_if_enabled()
    problem = ProblemRepository(db).get(problem_id)
    return {
        "request_id": req_id,
        "request_record_id": request_record_id,
        "request_artifact": run_req_artifact,
        "response_artifact": run_res_artifact,
        "execution": execution,
        "problem": problem,
        "trigger": trigger,
        "source": source,
    }


@app.post("/v1/problems/{problem_id}/start", response_model=ProblemStartResponse)
def start_problem(
    problem_id: str,
    x_debug_run_trigger: str | None = Header(default=None, alias="X-Debug-Run-Trigger"),
    db: FileStore = Depends(get_db),
) -> ProblemStartResponse:
    trigger = (x_debug_run_trigger or "manual").strip() or "manual"
    scheduled = _schedule_problem_execution(
        problem_id,
        source="api_start",
        trigger=trigger,
        force_fresh=(trigger == "continue_button"),
        db=db,
    )
    response = ProblemStartResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem_id,
        status=scheduled["problem"].status,
        execution=execution_summary(scheduled["execution"]),
    )
    artifacts = ArtifactStore()
    artifacts.save_json(
        scheduled["response_artifact"],
        {
            "ok": True,
            **response.model_dump(mode="json"),
            "request_id_internal": scheduled["request_id"],
            "request_artifact": scheduled["request_artifact"],
            "trigger": scheduled["trigger"],
        },
    )
    RequestRecordRepository(db).upsert(
        scheduled["request_record_id"],
        problem_id=problem_id,
        execution_id=scheduled["execution"].execution_id,
        worker_job_id=None,
        source="api_start",
        target_id=problem_id,
        status="completed",
        summary=f"trigger={scheduled['trigger']}",
        request_artifact_key=scheduled["request_artifact"],
        response_artifact_key=scheduled["response_artifact"],
    )

    return response


@app.post("/v1/problems/{problem_id}/run", response_model=ProblemRunResponse)
def run_problem(
    problem_id: str,
    x_debug_run_trigger: str | None = Header(default=None, alias="X-Debug-Run-Trigger"),
    db: FileStore = Depends(get_db),
) -> ProblemRunResponse:
    log = logging.getLogger("nl_engine.api")
    log.info("POST /v1/problems/%s/run", problem_id)
    scheduled = _schedule_problem_execution(
        problem_id,
        source="api_run",
        trigger=(x_debug_run_trigger or "unspecified").strip() or "unspecified",
        db=db,
    )
    _run_compatibility_pump(problem_id, scheduled["execution"].execution_id)
    _await_compatibility_progress(problem_id)
    scheduled["problem"] = ProblemRepository(db).get(problem_id)
    scheduled["execution"] = ProblemExecutionRepository(db).get(scheduled["execution"].execution_id)
    response = ProblemRunResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        status=scheduled["problem"].status,
        execution_id=scheduled["execution"].execution_id,
    )
    artifacts = ArtifactStore()
    artifacts.save_json(
        scheduled["response_artifact"],
        {
            "ok": True,
            **response.model_dump(mode="json"),
            "request_id_internal": scheduled["request_id"],
            "problem_id": problem_id,
            "request_artifact": scheduled["request_artifact"],
            "trigger": scheduled["trigger"],
            "deprecated": True,
        },
    )
    RequestRecordRepository(db).upsert(
        scheduled["request_record_id"],
        problem_id=problem_id,
        execution_id=scheduled["execution"].execution_id,
        worker_job_id=None,
        source="api_run",
        target_id=problem_id,
        status="completed",
        summary=f"trigger={scheduled['trigger']}",
        request_artifact_key=scheduled["request_artifact"],
        response_artifact_key=scheduled["response_artifact"],
    )

    return response


@app.get("/v1/problems/{problem_id}/execution", response_model=ProblemExecutionResponse)
def get_problem_execution(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemExecutionResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")
    execution = _latest_execution(db, problem_id)
    return ProblemExecutionResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem_id,
        execution=execution_summary(execution),
    )


@app.post("/v1/problems/{problem_id}/cancel", response_model=ProblemCancelResponse)
def cancel_problem(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemCancelResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")
    execution = ProblemExecutionService(db).request_cancel(problem_id)

    return ProblemCancelResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem_id,
        execution=execution_summary(execution),
    )


@app.post("/v1/problems/{problem_id}/pause", response_model=ProblemPauseResponse)
def pause_problem(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemPauseResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")
    if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
        raise_api_error(
            409,
            code="invalid_problem_state",
            message=f"can only pause non-terminal problems; current status is '{problem.status}'",
        )
    artifacts = ArtifactStore()
    req_id = new_id("pause_req")
    pause_req_artifact = f"problems/{problem_id}/api/pause_requests/{req_id}.request.json"
    pause_res_artifact = f"problems/{problem_id}/api/pause_requests/{req_id}.response.json"
    trigger = "manual_pause"
    artifacts.save_json(
        pause_req_artifact,
        {
            "request_id": req_id,
            "problem_id": problem_id,
            "server_time": now_utc().isoformat(),
            "trigger": trigger,
            "source": "api_pause",
        },
    )
    request_record_id = f"reqrec_api_pause_{req_id}"
    RequestRecordRepository(db).upsert(
        request_record_id,
        problem_id=problem_id,
        execution_id=None,
        worker_job_id=None,
        source="api_pause",
        target_id=problem_id,
        status="started",
        summary=f"trigger={trigger}",
        request_artifact_key=pause_req_artifact,
    )
    try:
        execution = ProblemExecutionService(db).request_pause(problem_id)
        refreshed = ProblemRepository(db).get(problem_id)
        response = ProblemPauseResponse(
            request_id=new_id("req"),
            server_time=now_utc(),
            problem_id=problem_id,
            status=(refreshed.status if refreshed is not None else ProblemStatus.PAUSED.value),
            execution=execution_summary(execution),
        )
        artifacts.save_json(
            pause_res_artifact,
            {
                "ok": True,
                **response.model_dump(mode="json"),
                "request_id_internal": req_id,
                "request_artifact": pause_req_artifact,
                "trigger": trigger,
            },
        )
        RequestRecordRepository(db).upsert(
            request_record_id,
            problem_id=problem_id,
            execution_id=execution.execution_id if execution else None,
            worker_job_id=None,
            source="api_pause",
            target_id=problem_id,
            status="completed",
            summary=f"trigger={trigger}",
            request_artifact_key=pause_req_artifact,
            response_artifact_key=pause_res_artifact,
        )
        return response
    except HTTPException:
        RequestRecordRepository(db).upsert(
            request_record_id,
            problem_id=problem_id,
            execution_id=None,
            worker_job_id=None,
            source="api_pause",
            target_id=problem_id,
            status="failed",
            summary=f"trigger={trigger}",
            request_artifact_key=pause_req_artifact,
            response_artifact_key=pause_res_artifact,
            error_class="api_error",
        )
        raise


@app.post("/v1/problems/{problem_id}/resume", response_model=ProblemResumeResponse)
def resume_problem(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemResumeResponse:
    if is_global_stop_active():
        raise_api_error(409, code="reset_in_progress", message="local reset is in progress; try again shortly")

    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")

    artifacts = ArtifactStore()
    req_id = new_id("resume_req")
    resume_req_artifact = f"problems/{problem_id}/api/resume_requests/{req_id}.request.json"
    resume_res_artifact = f"problems/{problem_id}/api/resume_requests/{req_id}.response.json"
    trigger = "manual_resume"
    artifacts.save_json(
        resume_req_artifact,
        {
            "request_id": req_id,
            "problem_id": problem_id,
            "server_time": now_utc().isoformat(),
            "trigger": trigger,
            "source": "api_resume",
        },
    )
    request_record_id = f"reqrec_api_resume_{req_id}"
    RequestRecordRepository(db).upsert(
        request_record_id,
        problem_id=problem_id,
        execution_id=None,
        worker_job_id=None,
        source="api_resume",
        target_id=problem_id,
        status="started",
        summary=f"trigger={trigger}",
        request_artifact_key=resume_req_artifact,
    )

    try:
        if problem.status != ProblemStatus.FAILED.value:
            raise_api_error(
                409,
                code="invalid_problem_state",
                message=f"can only resume failed problems; current status is '{problem.status}'",
            )

        if not problem.root_theorem_id:
            raise_api_error(409, code="invalid_problem_state", message="problem has no root theorem")
        theorem = TheoremRepository(db).get(problem.root_theorem_id)
        if not theorem:
            raise_api_error(409, code="invalid_problem_state", message="root theorem not found")

        previous_failure_report_id = problem.failure_report_artifact_id

        # Reset problem state for resumption.
        problem.status = ProblemStatus.RUNNING.value
        problem.failure_report_artifact_id = None
        problem.infrastructure_failure_count = 0
        apply_resume_anchor(problem, db, route="resume")
        ProblemRepository(db).save(problem)

        # Reset root theorem status so orchestrator re-enters the processing loop.
        if theorem.status == NodeStatus.FAILED.value:
            theorem.status = NodeStatus.OPEN.value
            TheoremRepository(db).save(theorem)

        # Clear stale cached worker failures so superseded runs never poison
        # fresh continuation attempts.
        for worker_row in WorkerJobRepository(db).list_by_problem(problem_id):
            result_key = f"worker_jobs/{worker_row.worker_job_id}/result.json"
            if not artifacts.exists(result_key):
                continue
            try:
                cached = artifacts.load_json(result_key)
                if cached.get("status") == "failed":
                    (artifacts.root / result_key).unlink(missing_ok=True)
            except Exception:
                pass

        # Log the resume event.
        EventRepository(db).append(
            problem_id,
            "problem.resumed",
            ProblemStatus.FAILED.value,
            ProblemStatus.RUNNING.value,
            target_node_id=problem.root_theorem_id,
            reason=f"route=resume; resumed from failure; previous_failure_report={previous_failure_report_id}",
        )

        # Create a fresh continuation execution and supersede stale in-flight work.
        execution = ProblemExecutionService(db).start_fresh_continuation(
            problem_id,
            trigger_source="api_resume",
            supersede_reason="api_resume",
            set_problem_running=True,
        )
        start_embedded_supervisor_if_enabled()

        response = ProblemResumeResponse(
            request_id=new_id("req"),
            server_time=now_utc(),
            problem_id=problem_id,
            status=problem.status,
            previous_failure_report_id=previous_failure_report_id,
            execution=execution_summary(execution),
        )
        artifacts.save_json(
            resume_res_artifact,
            {
                "ok": True,
                **response.model_dump(mode="json"),
                "request_id_internal": req_id,
                "request_artifact": resume_req_artifact,
                "trigger": trigger,
            },
        )
        RequestRecordRepository(db).upsert(
            request_record_id,
            problem_id=problem_id,
            execution_id=execution.execution_id,
            worker_job_id=None,
            source="api_resume",
            target_id=problem_id,
            status="completed",
            summary=f"trigger={trigger}",
            request_artifact_key=resume_req_artifact,
            response_artifact_key=resume_res_artifact,
        )
        return response
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code", "http_error"))
            message = str(detail.get("message", "request failed"))
            details = detail.get("details") if isinstance(detail.get("details"), dict) else {}
        else:
            code = "http_error"
            message = str(detail)
            details = {}
        artifacts.save_json(
            resume_res_artifact,
            {
                "ok": False,
                "status": "failed",
                "status_code": exc.status_code,
                "request_id_internal": req_id,
                "problem_id": problem_id,
                "request_artifact": resume_req_artifact,
                "trigger": trigger,
                "error": {"code": code, "message": message, "details": details},
            },
        )
        RequestRecordRepository(db).upsert(
            request_record_id,
            problem_id=problem_id,
            execution_id=None,
            worker_job_id=None,
            source="api_resume",
            target_id=problem_id,
            status="failed",
            summary=f"trigger={trigger}",
            error_class=code,
            request_artifact_key=resume_req_artifact,
            response_artifact_key=resume_res_artifact,
        )
        raise
    except Exception as exc:
        error_class = type(exc).__name__
        artifacts.save_json(
            resume_res_artifact,
            {
                "ok": False,
                "status": "failed",
                "status_code": 500,
                "request_id_internal": req_id,
                "problem_id": problem_id,
                "request_artifact": resume_req_artifact,
                "trigger": trigger,
                "error": {
                    "code": "internal_error",
                    "message": "unhandled server error",
                    "details": {"exception": error_class},
                },
            },
        )
        RequestRecordRepository(db).upsert(
            request_record_id,
            problem_id=problem_id,
            execution_id=None,
            worker_job_id=None,
            source="api_resume",
            target_id=problem_id,
            status="failed",
            summary=f"trigger={trigger}",
            error_class=error_class,
            request_artifact_key=resume_req_artifact,
            response_artifact_key=resume_res_artifact,
        )
        raise


@app.get("/v1/problems/{problem_id}/tree", response_model=ProblemTreeResponse)
def get_tree(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemTreeResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")
    if not problem.root_theorem_id:
        raise_api_error(409, code="invalid_problem_state", message="problem has no root theorem")

    theorem = TheoremRepository(db).get(problem.root_theorem_id)
    if not theorem:
        raise_api_error(404, code="root_theorem_not_found", message="root theorem not found")

    root_node = _build_problem_tree(db, problem_id, theorem)

    return ProblemTreeResponse(request_id=new_id("req"), server_time=now_utc(), root=root_node)


@app.get("/v1/problems/{problem_id}/failure-report", response_model=FailureReportResponse)
def get_failure_report(problem_id: str, db: FileStore = Depends(get_db)) -> FailureReportResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")
    if problem.status != ProblemStatus.FAILED.value:
        raise_api_error(409, code="failure_report_unavailable", message="problem is not failed")

    failure = FailureReportRepository(db).get_by_problem(problem_id)
    if not failure:
        raise_api_error(404, code="failure_report_not_found", message="failure report not found")

    return FailureReportResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        failure_report={
            "failure_report_id": failure.failure_report_id,
            "problem_id": failure.problem_id,
            "failure_reason": failure.failure_reason,
            "terminal_lemma_id": failure.terminal_lemma_id,
            "terminal_error_class": failure.terminal_error_class,
            "terminal_error_message": failure.terminal_error_message,
            "partial_tree": failure.partial_tree,
            "trusted_context_at_failure": failure.trusted_context_at_failure,
            "all_decomposition_attempts": failure.all_decomposition_attempts,
            "routing_log_summary": failure.routing_log_summary,
            "created_at": failure.created_at,
        },
    )


@app.get("/v1/problems/{problem_id}/progress", response_model=ProgressResponse)
def get_progress(problem_id: str, db: FileStore = Depends(get_db)) -> ProgressResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")

    event_repo = EventRepository(db)
    latest_event = event_repo.latest_for_problem(problem_id)
    decomp_repo = DecompositionRepository(db)
    lemma_repo = LemmaRepository(db)
    lean_jobs_repo = LeanJobRepository(db)
    lean_results_repo = LeanResultRepository(db)
    execution = _latest_execution(db, problem_id)
    active_decomposition = decomp_repo.get(problem.active_decomposition_id) if problem.active_decomposition_id else None
    standby_decomposition = decomp_repo.get(problem.standby_decomposition_id) if problem.standby_decomposition_id else None

    latest_blocking_reason = None
    recent_events = event_repo.list_for_problem(problem_id, limit=500)
    for event in reversed(recent_events):
        if event.stage in {"lemma.terminal_failure", "problem.failed"}:
            latest_blocking_reason = event.reason
            break
        if event.new_status in {"blocked", "failed", "proof_exhausted"}:
            latest_blocking_reason = event.reason
            break

    resume_anchor_lemma_id = problem.resume_anchor_lemma_id
    resume_anchor_owner_decomposition_id = problem.resume_anchor_owner_decomposition_id

    status_map = {
        "queued": "pending",
        "running": "compiling",
        "success": "success",
        "repairable": "error",
        "fatal": "error",
        "cancelled": "cancelled",
    }
    latest_jobs = lean_jobs_repo.latest_by_lemma(problem_id)
    per_lemma_lean_status: list[LemmaLeanStatus] = []
    for lemma in lemma_repo.list_by_problem(problem_id):
        job = latest_jobs.get(lemma.lemma_id)
        if not job:
            continue
        result = lean_results_repo.get_for_job(job.job_id)
        per_lemma_lean_status.append(
            LemmaLeanStatus(
                lemma_id=lemma.lemma_id,
                status=status_map.get(job.status, "pending"),
                error_class=result.error_class if result else None,
                issue_kind=(result.issue_kind if result else None) or job.issue_kind,
                attempt_index=job.attempt_index,
                job_id=job.job_id,
                confidence=(result.confidence if result and result.confidence is not None else job.confidence),
                fatality=(result.fatality if result and result.fatality is not None else job.fatality),
            )
        )
    per_lemma_lean_status.sort(key=lambda row: row.lemma_id)

    return ProgressResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem.problem_id,
        status=problem.status,
        verification_level=problem.verification_level,
        lean_mode=not problem.nl_only_mode,
        execution_id=execution.execution_id if execution else None,
        execution_status=execution.status if execution else None,
        execution_desired_state=execution.desired_state if execution else None,
        active_decomposition_id=problem.active_decomposition_id,
        active_decomposition_status=active_decomposition.controller_status if active_decomposition else None,
        standby_decomposition_id=problem.standby_decomposition_id,
        standby_decomposition_status=standby_decomposition.controller_status if standby_decomposition else None,
        lemma_counts=lemma_repo.count_by_status(problem_id),
        lean_job_counts=lean_jobs_repo.count_by_mode_status(problem_id),
        per_lemma_lean_status=per_lemma_lean_status,
        lean_v2_track_id=active_decomposition.lean_v2_track_id if active_decomposition else None,
        current_stage=execution.current_stage if execution and execution.current_stage else (latest_event.stage if latest_event else None),
        blocking_kind=execution.blocking_kind if execution else None,
        blocking_ref_id=execution.blocking_ref_id if execution else None,
        latest_reason=latest_event.reason if latest_event else None,
        latest_blocking_reason=latest_blocking_reason,
        last_event_id=latest_event.event_id if latest_event else None,
        resume_anchor_lemma_id=resume_anchor_lemma_id,
        resume_anchor_owner_decomposition_id=resume_anchor_owner_decomposition_id,
    )


@app.get("/v1/problems/{problem_id}/lean-jobs", response_model=LeanJobsResponse)
def get_lean_jobs(problem_id: str, db: FileStore = Depends(get_db)) -> LeanJobsResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")

    jobs_repo = LeanJobRepository(db)
    results_repo = LeanResultRepository(db)

    rows: list[LeanJobStatusItem] = []
    for job in jobs_repo.list_by_problem(problem_id):
        result = results_repo.get_for_job(job.job_id)
        rows.append(
            LeanJobStatusItem(
                job_id=job.job_id,
                target_id=job.target_id,
                target_kind=job.target_kind,
                mode=job.mode,
                operation=job.operation,
                status=job.status,
                attempt_index=job.attempt_index,
                progress_snapshot=(result.progress_snapshot if result and result.progress_snapshot is not None else job.progress_snapshot),
                issue_kind=(result.issue_kind if result else None) or job.issue_kind,
                confidence=(result.confidence if result and result.confidence is not None else job.confidence),
                fatality=(result.fatality if result and result.fatality is not None else job.fatality),
                error_class=result.error_class if result else None,
                error_message=result.error_message if result else None,
                recommended_next_step=result.recommended_next_step if result else None,
                request_artifact_id=job.request_artifact_id,
                result_artifact_id=job.result_artifact_id,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
        )

    return LeanJobsResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem_id,
        count=len(rows),
        jobs=rows,
    )


@app.get("/v1/problems/{problem_id}/events", response_model=EventsResponse)
def get_events(
    problem_id: str,
    after_event_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: FileStore = Depends(get_db),
) -> EventsResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")

    rows = EventRepository(db).list_for_problem(problem_id, after_event_id=after_event_id, limit=limit)
    return EventsResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        events=[
            EventItem(
                event_id=row.event_id,
                problem_id=row.problem_id,
                target_node_id=row.target_node_id,
                stage=row.stage,
                old_status=row.old_status,
                new_status=row.new_status,
                worker_job_id=row.worker_job_id,
                reason=row.reason,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


@app.get("/v1/problems/{problem_id}/cost", response_model=ProblemCostSummaryResponse)
def get_problem_cost(problem_id: str, db: FileStore = Depends(get_db)) -> ProblemCostSummaryResponse:
    problem = ProblemRepository(db).get(problem_id)
    if not problem:
        raise_api_error(404, code="problem_not_found", message="problem not found")

    usage_rows = LlmUsageRepository(db).list_by_problem(problem_id)
    rollup_rows = RunCostRollupRepository(db).list_by_problem(problem_id)
    total_input = int(sum(row.total_input_tokens for row in rollup_rows))
    total_output = int(sum(row.total_output_tokens for row in rollup_rows))
    total_cost = float(sum(row.total_estimated_cost_usd for row in rollup_rows))

    by_stage: dict[str, dict[str, float]] = {}
    for row in usage_rows:
        stage = row.stage
        by_stage.setdefault(stage, {"input_tokens": 0.0, "output_tokens": 0.0, "estimated_cost_usd": 0.0})
        by_stage[stage]["input_tokens"] += row.input_tokens
        by_stage[stage]["output_tokens"] += row.output_tokens
        by_stage[stage]["estimated_cost_usd"] += row.estimated_cost_usd

    return ProblemCostSummaryResponse(
        request_id=new_id("req"),
        server_time=now_utc(),
        problem_id=problem_id,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_estimated_cost_usd=round(total_cost, 8),
        by_stage=by_stage,
    )


@app.get("/v1/problems/{problem_id}/events/stream")
async def stream_events(
    problem_id: str,
    after_event_id: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    store = get_file_store()
    if not ProblemRepository(store).get(problem_id):
        raise_api_error(404, code="problem_not_found", message="problem not found")

    start_after = after_event_id
    if last_event_id is not None:
        try:
            start_after = int(last_event_id)
        except ValueError:
            start_after = after_event_id

    settings = get_settings()

    async def generator():
        cursor = start_after
        since_heartbeat = 0

        while True:
            store = get_file_store()
            rows = EventRepository(store).list_for_problem(problem_id, after_event_id=cursor, limit=200)
            problem = ProblemRepository(store).get(problem_id)

            if rows:
                for row in rows:
                    payload = {
                        "event_id": row.event_id,
                        "problem_id": row.problem_id,
                        "target_node_id": row.target_node_id,
                        "stage": row.stage,
                        "old_status": row.old_status,
                        "new_status": row.new_status,
                        "worker_job_id": row.worker_job_id,
                        "reason": row.reason,
                        "created_at": row.created_at.isoformat(),
                    }
                    if row.stage in {"problem.succeeded", "problem.failed"}:
                        event_name = "terminal"
                    elif row.stage == "lean.result_classified":
                        event_name = "lean_classified"
                    elif row.stage.startswith("lean.") or row.stage.startswith("lean_v2."):
                        event_name = "lean"
                    else:
                        event_name = "event"
                    yield f"id: {row.event_id}\nevent: {event_name}\ndata: {json.dumps(payload)}\n\n"
                    cursor = row.event_id
                    since_heartbeat = 0

                    if event_name == "terminal":
                        return
            else:
                since_heartbeat += 1
                if since_heartbeat >= settings.sse_heartbeat_seconds:
                    heartbeat_payload = {"problem_id": problem_id, "cursor": cursor}
                    yield f"event: heartbeat\ndata: {json.dumps(heartbeat_payload)}\n\n"
                    since_heartbeat = 0

                if problem and problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                    terminal_payload = {"problem_id": problem_id, "status": problem.status}
                    yield f"event: terminal\ndata: {json.dumps(terminal_payload)}\n\n"
                    return

            await asyncio.sleep(1)

    return StreamingResponse(generator(), media_type="text/event-stream")
