from __future__ import annotations

from datetime import UTC, datetime
import io
from pathlib import Path
import re
import time
from typing import Any
import zipfile

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from nl_engine.persistence.db import FileStore

from nl_engine.api.deps import get_db
from nl_engine.api.run_state import clear_stop_all_runs, has_any_running_problem, is_problem_running, request_problem_stop, request_stop_all_runs
from nl_engine.artifacts.store import ArtifactStore
from nl_engine.artifacts.browser import ArtifactBrowser, ArtifactEntry, ArtifactSecurityError
from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.contracts import (
    DebugArtifactContentResponse,
    DebugArtifactItem,
    DebugArtifactsResponse,
    DebugCleanupResponse,
    DebugExecutionItem,
    DebugExecutionListResponse,
    DebugLlmUsageStage,
    DebugLlmUsageSummaryResponse,
    DebugNodeGraph,
    DebugNodeGraphEdge,
    DebugNodeGraphNode,
    DebugProblemCreateTemplateResponse,
    DebugProblemInputJsonResponse,
    DebugProblemItem,
    DebugProblemSnapshotResponse,
    DebugProblemsListResponse,
    DebugRequestLogEntry,
    DebugRequestLogResponse,
    EventItem,
    TreeNode,
)
from nl_engine.domain.enums import NodeKind, ProblemStatus, ProofStatus, RoutingStatus
from nl_engine.domain.models import EventORM, LlmUsageRecordORM, ProblemORM, TheoremORM, WorkerJobORM
from nl_engine.execution.runtime import start_embedded_supervisor_if_enabled
from nl_engine.observability.costs import cached_input_tokens_from_raw_usage
from nl_engine.persistence.repositories import (
    AssemblyPlanRepository,
    CounterexampleRepository,
    DecompositionRepository,
    EventRepository,
    FailureReportRepository,
    LeanJobRepository,
    LeanResultRepository,
    LlmUsageRepository,
    LemmaRepository,
    LemmaProofAttemptRepository,
    ProblemExecutionRepository,
    ProblemRepository,
    ProofDependencyCheckRepository,
    ProofGraphEdgeRepository,
    ProofGraphNodeRepository,
    ProofGraphRepository,
    RequestRecordRepository,
    TheoremRepository,
    TrustedContextRepository,
    VetterReportRepository,
    WorkerJobRepository,
)
from nl_engine.services.debug_cleaner import DebugDataCleaner
from nl_engine.services.executions import ProblemExecutionService, execution_summary
from nl_engine.services.ids import new_id
from nl_engine.services.resume_anchor import apply_resume_anchor
from nl_engine.settings import get_settings

router = APIRouter(prefix="/v1/debug", tags=["debug"])

DEBUG_STATIC_DIR = Path(__file__).resolve().parent / "static" / "debug"


def register_debug_ui(app: FastAPI) -> None:
    app.mount("/debug/static", StaticFiles(directory=str(DEBUG_STATIC_DIR)), name="debug-static")

    @app.get("/debug", include_in_schema=False)
    def debug_index() -> FileResponse:
        index = DEBUG_STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(
                status_code=404,
                detail={"code": "debug_ui_not_found", "message": "debug UI is not available", "details": {}},
            )
        return FileResponse(index)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _raise_api_error(status_code: int, *, code: str, message: str, details: dict | None = None) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message, "details": details or {}})


def _tree_summary(db: FileStore, problem_id: str, root_theorem_id: str | None) -> dict[str, Any]:
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
        return TreeNode(id=lemma.lemma_id, kind=NodeKind.LEMMA.value, status=lemma.proof_status, children=children)

    root_children = [build_lemma_node(child.lemma_id) for child in by_parent.get(theorem.theorem_id, [])]
    return TreeNode(id=theorem.theorem_id, kind=NodeKind.THEOREM.value, status=theorem.status, children=root_children)


def _to_event_item(row: EventORM) -> EventItem:
    return EventItem(
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


def _tail_events(db: FileStore, problem_id: str, limit: int) -> list[EventORM]:
    all_events = EventRepository(db).list_for_problem(problem_id, limit=10_000)
    # Return the last `limit` events (same semantics as ORDER BY desc + LIMIT + reverse)
    return all_events[-limit:]


def _build_node_graph(
    problem_id: str,
    theorem: TheoremORM | None,
    lemmas_payload: list[dict[str, Any]],
    logical_decompositions_payload: list[dict[str, Any]],
    lean_jobs_payload: list[dict[str, Any]],
    *,
    visible_lemma_ids: set[str] | None = None,
) -> DebugNodeGraph:
    nodes: list[DebugNodeGraphNode] = []
    edges: list[DebugNodeGraphEdge] = []

    if theorem:
        nodes.append(
            DebugNodeGraphNode(
                id=theorem.theorem_id,
                kind="theorem",
                label=f"theorem: {theorem.theorem_id}",
                status=theorem.status,
                parent_id=None,
                metadata={"problem_id": problem_id},
            )
        )

    for dec in logical_decompositions_payload:
        nodes.append(
            DebugNodeGraphNode(
                id=dec["logical_decomposition_id"],
                kind="decomposition",
                label=f"decomposition: {dec['logical_decomposition_id']}",
                status=dec["controller_status"],
                parent_id=dec["node_id"],
                metadata={
                    "node_id": dec["node_id"],
                    "node_kind": dec["node_kind"],
                    "llm_vetting_status": dec["llm_vetting_status"],
                    "lean_assembly_status": dec["lean_assembly_status"],
                    "revision_count": dec.get("revision_count", 1),
                    "current_revision_id": dec.get("current_revision_id"),
                    "current_revision_number": dec.get("current_revision_number"),
                },
            )
        )
        edges.append(DebugNodeGraphEdge(from_id=dec["node_id"], to=dec["logical_decomposition_id"], relation="decomposed_into"))

    for lemma in lemmas_payload:
        if visible_lemma_ids is not None and lemma["lemma_id"] not in visible_lemma_ids:
            continue
        nodes.append(
            DebugNodeGraphNode(
                id=lemma["lemma_id"],
                kind="lemma",
                label=f"lemma: {lemma['lemma_id']}",
                status=lemma["proof_status"],
                parent_id=lemma["parent_id"],
                metadata={
                    "routing_status": lemma["routing_status"],
                    "statement_status": lemma["statement_status"],
                    "solver_attempt_count": lemma["solver_attempt_count"],
                },
            )
        )
        edges.append(DebugNodeGraphEdge(from_id=lemma["parent_id"], to=lemma["lemma_id"], relation="contains"))

    for job in lean_jobs_payload:
        nodes.append(
            DebugNodeGraphNode(
                id=job["job_id"],
                kind="lean_job",
                label=f"lean_job: {job.get('operation') or job['mode']}",
                status=job["status"],
                parent_id=job["target_id"],
                metadata={
                    "mode": job["mode"],
                    "operation": job.get("operation"),
                    "target_id": job["target_id"],
                    "target_kind": job["target_kind"],
                    "issue_kind": job.get("issue_kind"),
                    "confidence": job.get("confidence"),
                    "fatality": job.get("fatality"),
                },
            )
        )
        edges.append(DebugNodeGraphEdge(from_id=job["target_id"], to=job["job_id"], relation="formalization_job"))

    # Add final_check nodes as children of decompositions
    for dec in logical_decompositions_payload:
        fc_job_id = dec.get("final_check_job_id")
        if fc_job_id:
            fc_data = dec.get("agent6_final_check") or {}
            fc_output = fc_data.get("output") or {}
            fc_verdict = fc_output.get("verdict", "pending")
            fc_status = "succeeded" if fc_verdict == "approved" else "failed" if fc_verdict in ("decomposition_issue", "assembly_issue") else "running" if fc_verdict == "pending" else "warning"
            nodes.append(
                DebugNodeGraphNode(
                    id=fc_job_id,
                    kind="final_check",
                    label="final_vetter",
                    status=fc_status,
                    parent_id=dec["logical_decomposition_id"],
                    metadata={
                        "verdict": fc_verdict,
                        "final_check_passed": dec.get("final_check_passed", False),
                    },
                )
            )
            edges.append(DebugNodeGraphEdge(from_id=dec["logical_decomposition_id"], to=fc_job_id, relation="final_check"))

    return DebugNodeGraph(nodes=nodes, edges=edges)


def _compute_lemma_visibility(
    lemmas_payload: list[dict[str, Any]],
    decompositions_payload: list[dict[str, Any]],
) -> tuple[dict[str, str | None], list[str], list[str]]:
    lemma_ids = [row["lemma_id"] for row in lemmas_payload]
    owner_by_lemma: dict[str, str | None] = {lemma_id: None for lemma_id in lemma_ids}
    visible_lemma_ids: set[str] = set()

    for dec in decompositions_payload:
        dec_id = dec["decomposition_id"]
        dec_is_accepted = dec.get("llm_vetting_status") == "accepted"
        for lemma_id in dec.get("lemma_ids", []):
            if lemma_id not in owner_by_lemma:
                owner_by_lemma[lemma_id] = dec_id
            elif owner_by_lemma[lemma_id] is None or dec_is_accepted:
                owner_by_lemma[lemma_id] = dec_id
            if dec_is_accepted:
                visible_lemma_ids.add(lemma_id)

    visible = sorted(lemma_id for lemma_id in lemma_ids if lemma_id in visible_lemma_ids)
    hidden = sorted(lemma_id for lemma_id in lemma_ids if lemma_id not in visible_lemma_ids)
    return owner_by_lemma, visible, hidden


def _logical_decomposition_sort_key(row: dict[str, Any]) -> tuple[int, int, float]:
    status = str(row.get("controller_status") or "")
    status_rank = {
        "active": 5,
        "standby": 4,
        "pending": 3,
        "succeeded": 2,
        "failed": 1,
    }.get(status, 0)
    revision_number = int(row.get("revision_number") or 1)
    created_at = row.get("created_at")
    created_ts = created_at.timestamp() if isinstance(created_at, datetime) else 0.0
    return (status_rank, revision_number, created_ts)


def _build_logical_decompositions(decomposition_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in decomposition_rows:
        logical_id = str(row.get("logical_decomposition_id") or row.get("decomposition_id") or "").strip()
        if not logical_id:
            continue
        grouped.setdefault(logical_id, []).append(row)

    logical_rows: list[dict[str, Any]] = []
    for logical_id, rows in grouped.items():
        rows.sort(key=_logical_decomposition_sort_key, reverse=True)
        current = rows[0]
        logical_rows.append(
            {
                "logical_decomposition_id": logical_id,
                "node_id": current.get("node_id"),
                "node_kind": current.get("node_kind"),
                "current_revision_id": current.get("decomposition_id"),
                "current_revision_number": current.get("revision_number", 1),
                "revision_count": len(rows),
                "controller_status": current.get("controller_status"),
                "llm_vetting_status": current.get("llm_vetting_status"),
                "lean_assembly_status": current.get("lean_assembly_status"),
                "lean_run_dir": current.get("lean_run_dir"),
                "lean_v2_track_id": current.get("lean_v2_track_id"),
                "lean_v2_lemma_handles": current.get("lean_v2_lemma_handles"),
                "lean_v2_prepare_status": current.get("lean_v2_prepare_status"),
                "lemma_ids": current.get("lemma_ids", []),
                "strategy_summary": current.get("strategy_summary"),
                "proof_bundle_artifact_id": current.get("proof_bundle_artifact_id"),
                "proof_bundle": current.get("proof_bundle"),
                "current_revision": current,
                "revisions": rows,
                "final_check_job_id": current.get("final_check_job_id"),
                "final_check_passed": current.get("final_check_passed", False),
                "agent6_final_check": current.get("agent6_final_check"),
                "created_at": current.get("created_at"),
                "updated_at": current.get("updated_at"),
            }
        )
    logical_rows.sort(key=lambda row: _logical_decomposition_sort_key(row["current_revision"]), reverse=True)
    return logical_rows, {row["logical_decomposition_id"]: row for row in logical_rows}


def _build_nl_only_final_output(
    *,
    problem: ProblemORM,
    theorem: TheoremORM | None,
    decompositions_payload: list[dict[str, Any]],
    lemmas_payload: list[dict[str, Any]],
    visible_lemma_ids: list[str],
) -> dict[str, Any] | None:
    if not problem.nl_only_mode or problem.status != "succeeded" or theorem is None:
        return None

    selected_decomposition: dict[str, Any] | None = None
    if problem.active_decomposition_id:
        selected_decomposition = next(
            (row for row in decompositions_payload if row["decomposition_id"] == problem.active_decomposition_id),
            None,
        )
    if selected_decomposition is None:
        selected_decomposition = next(
            (
                row
                for row in decompositions_payload
                if row.get("llm_vetting_status") == "accepted"
                and row.get("controller_status") in {"active", "succeeded", "standby"}
            ),
            None,
        )

    visible_set = set(visible_lemma_ids)
    output_lemmas = []
    for lemma in lemmas_payload:
        if lemma["lemma_id"] not in visible_set:
            continue
        output_lemmas.append(
            {
                "lemma_id": lemma["lemma_id"],
                "statement_nl": lemma["statement_nl"],
                "semantic_sketch": lemma["statement_semantic_sketch"],
                "proof_status": lemma["proof_status"],
                "routing_status": lemma["routing_status"],
                "proof_nl": lemma["latest_nl_proof"],
                "role_in_parent": lemma["role_in_parent"],
            }
        )

    return {
        "problem_id": problem.problem_id,
        "title": problem.title,
        "verification_level": problem.verification_level,
        "root_theorem": {
            "theorem_id": theorem.theorem_id,
            "statement_nl": theorem.statement_nl,
            "semantic_sketch": theorem.statement_semantic_sketch,
        },
        "selected_decomposition": (
            {
                "decomposition_id": selected_decomposition["decomposition_id"],
                "strategy_summary": selected_decomposition["strategy_summary"],
                "controller_status": selected_decomposition["controller_status"],
                "llm_vetting_status": selected_decomposition["llm_vetting_status"],
                "assembly_plan": selected_decomposition.get("assembly_plan"),
            }
            if selected_decomposition
            else None
        ),
        "lemmas": output_lemmas,
        "all_visible_lemmas_nl_accepted": all(lemma.get("proof_status") == "nl_accepted" for lemma in output_lemmas),
    }


def _load_optional_json_artifact(artifacts: ArtifactStore, artifact_key: str | None) -> dict[str, Any] | None:
    key = str(artifact_key or "").strip()
    if not key or not artifacts.exists(key):
        return None
    try:
        payload = artifacts.load_json(key)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _root_track_decomposition_ids(
    *,
    problem: ProblemORM,
    theorem: TheoremORM | None,
    decompositions_payload: list[dict[str, Any]],
) -> list[str]:
    if theorem is None:
        return []

    cfg = ProblemConfig.model_validate(problem.config)
    take_k = max(1, min(cfg.decomposition.parallel_root_take_k, cfg.decomposition.parallel_root_decompositions_n))
    ready: list[dict[str, Any]] = []
    for row in decompositions_payload:
        if row.get("node_id") != theorem.theorem_id:
            continue
        if row.get("llm_vetting_status") != "accepted":
            continue
        if row.get("controller_status") == "failed":
            continue
        if problem.nl_only_mode and row.get("lean_assembly_status") == "skipped":
            ready.append(row)
        if not problem.nl_only_mode and row.get("lean_assembly_status") == "success":
            ready.append(row)

    ready.sort(key=lambda row: (row.get("created_at"), row.get("decomposition_id")))
    selected = [str(row["decomposition_id"]) for row in ready[:take_k]]
    if problem.active_decomposition_id and problem.active_decomposition_id not in selected:
        selected = [problem.active_decomposition_id, *selected][:take_k]
    return selected


def _request_source_from_key(key: str) -> tuple[str | None, str | None]:
    if key.endswith("/api/problem_create_request.json"):
        return "api_create", None
    if "/api/start_requests/" in key and key.endswith(".request.json"):
        return "api_start", None
    if "/api/pause_requests/" in key and key.endswith(".request.json"):
        return "api_pause", None
    if "/api/resume_requests/" in key and key.endswith(".request.json"):
        return "api_resume", None
    if "/api/run_requests/" in key and key.endswith(".request.json"):
        return "api_run", None
    for agent in ("agent1", "agent2", "agent3", "agent4", "agent5", "agent6"):
        if key.endswith(f"/{agent}_input.json"):
            return agent, None
    if "/lean_jobs/" in key and key.endswith("/request.json"):
        return "lean", None
    return None, None


def _request_summary(source: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return source
    if source == "api_create":
        title = str(payload.get("title", "")).strip()
        statement = str(payload.get("statement_nl", "")).strip()
        return f"title={title[:80]} statement={statement[:120]}"
    if source == "api_run":
        run_request_id = str(payload.get("run_request_id", "")).strip()
        trigger = str(payload.get("trigger", "unspecified")).strip() or "unspecified"
        if run_request_id:
            return f"run_request_id={run_request_id} trigger={trigger}"
        return f"trigger={trigger}"
    if source == "api_start":
        request_id = str(payload.get("request_id", "")).strip()
        trigger = str(payload.get("trigger", "manual")).strip() or "manual"
        if request_id:
            return f"request_id={request_id} trigger={trigger}"
        return f"trigger={trigger}"
    if source == "api_pause":
        request_id = str(payload.get("request_id", "")).strip()
        trigger = str(payload.get("trigger", "manual_pause")).strip() or "manual_pause"
        if request_id:
            return f"request_id={request_id} trigger={trigger}"
        return f"trigger={trigger}"
    if source == "api_resume":
        request_id = str(payload.get("request_id", "")).strip()
        trigger = str(payload.get("trigger", "manual_resume")).strip() or "manual_resume"
        if request_id:
            return f"request_id={request_id} trigger={trigger}"
        return f"trigger={trigger}"
    if source.startswith("agent"):
        lemma_id = payload.get("lemma_id")
        theorem = payload.get("theorem_nl")
        if lemma_id:
            return f"lemma_id={lemma_id}"
        if theorem:
            return f"theorem={str(theorem)[:100]}"
        return source
    if source == "lean":
        mode = payload.get("mode")
        target = payload.get("target_id")
        return f"mode={mode} target={target}"
    return source


def _request_completion_status(
    *,
    browser: ArtifactBrowser,
    source: str,
    artifact_key: str,
    problem_id: str,
    key_set: set[str],
    problem_running: bool,
) -> tuple[str, str | None]:
    def _has_key(key: str) -> bool:
        return key in key_set or browser.exists(key)

    if source == "api_create":
        response_key = f"problems/{problem_id}/api/problem_create_response.json"
        if _has_key(response_key):
            return "completed", "create response artifact present"
        return ("pending", "create request received; response pending") if problem_running else ("failed", "create response artifact missing")

    if source in {"api_start", "api_pause", "api_resume"}:
        response_key = artifact_key.replace(".request.json", ".response.json")
        if source == "api_start":
            source_label = "start"
        elif source == "api_pause":
            source_label = "pause"
        else:
            source_label = "resume"
        if _has_key(response_key):
            try:
                response_payload = browser.read(response_key).content
            except Exception:
                return "completed", f"{source_label} response artifact present"
            if isinstance(response_payload, dict):
                if response_payload.get("ok") is False:
                    err = response_payload.get("error")
                    if isinstance(err, dict):
                        code = str(err.get("code", "error"))
                        message = str(err.get("message", "")).strip()
                        detail = f"{source_label} failed: {code}"
                        if message:
                            detail = f"{detail} ({message})"
                        return "failed", detail
                    return "failed", f"{source_label} response recorded an error"
                if "error" in response_payload and response_payload.get("ok") is not True:
                    return "failed", f"{source_label} response contains error payload"
            return "completed", f"{source_label} response artifact present"
        if source == "api_start":
            return ("pending", "execution start pending") if problem_running else ("failed", "start response artifact missing")
        if source == "api_pause":
            return ("pending", "pause pending") if problem_running else ("failed", "pause response artifact missing")
        return ("pending", "resume pending") if problem_running else ("failed", "resume response artifact missing")

    if source == "api_run":
        response_key = artifact_key.replace(".request.json", ".response.json")
        if _has_key(response_key):
            try:
                response_payload = browser.read(response_key).content
            except Exception:
                return "completed", "run response artifact present"
            if isinstance(response_payload, dict):
                if response_payload.get("status") == "in_progress":
                    return ("pending", "compatibility run request still waiting on execution") if problem_running else ("failed", "compatibility run request interrupted before completion")
                if response_payload.get("ok") is False:
                    err = response_payload.get("error")
                    if isinstance(err, dict):
                        code = str(err.get("code", "error"))
                        message = str(err.get("message", "")).strip()
                        detail = f"run failed: {code}"
                        if message:
                            detail = f"{detail} ({message})"
                        return "failed", detail
                    return "failed", "run response recorded an error"
                if "error" in response_payload and response_payload.get("status") is None:
                    return "failed", "run response contains error payload"
            return "completed", "run response artifact present"
        downstream_prefixes = (
            f"problems/{problem_id}/decomposer",
            f"problems/{problem_id}/decomposition_vetter",
            f"problems/{problem_id}/lemmas",
            f"problems/{problem_id}/lean_jobs",
        )
        for downstream_prefix in downstream_prefixes:
            if browser.list_files(prefix=downstream_prefix, limit=1):
                return "unknown", "run response artifact missing (downstream artifacts indicate legacy run)"
        return ("pending", "compatibility run request still waiting on execution") if problem_running else ("failed", "run response artifact missing")

    if source.startswith("agent"):
        prefix = artifact_key.rsplit("/", 1)[0]
        parsed_prefix = f"{prefix}/{source}_parsed_output_attempt_"
        raw_prefix = f"{prefix}/{source}_raw_output_attempt_"
        parse_error_key = f"{prefix}/{source}_parse_error.json"
        validation_error_key = f"{prefix}/{source}_validation_error.json"
        request_error_prefix = f"{prefix}/{source}_request_error_attempt_"
        request_state_prefix = f"{prefix}/{source}_request_state_attempt_"

        local_keys = {row.artifact_key for row in browser.list_files(prefix=prefix, limit=200)}
        has_parsed = any(key.startswith(parsed_prefix) and key.endswith(".json") for key in local_keys)
        has_raw = any(key.startswith(raw_prefix) and key.endswith(".txt") for key in local_keys)
        has_parse_error = parse_error_key in local_keys
        has_validation_error = validation_error_key in local_keys
        has_request_error = any(key.startswith(request_error_prefix) and key.endswith(".json") for key in local_keys)
        state_keys = sorted(
            [key for key in local_keys if key.startswith(request_state_prefix) and key.endswith(".json")],
            key=lambda item: item,
        )
        latest_state: str | None = None
        latest_state_payload: dict[str, Any] = {}
        if state_keys:
            try:
                loaded_state_payload = browser.read(state_keys[-1]).content
                if isinstance(loaded_state_payload, dict):
                    latest_state_payload = dict(loaded_state_payload)
                    latest_state = str(loaded_state_payload.get("status", "")).strip().lower() or None
            except Exception:
                latest_state = None
                latest_state_payload = {}
        worker_result_status: str | None = None
        worker_error_detail: str | None = None
        worker_job_id = latest_state_payload.get("worker_job_id")
        if isinstance(worker_job_id, str) and worker_job_id.strip():
            worker_result_key = f"worker_jobs/{worker_job_id.strip()}/result.json"
            if _has_key(worker_result_key):
                try:
                    worker_result_payload = browser.read(worker_result_key).content
                    if isinstance(worker_result_payload, dict):
                        status_value = str(worker_result_payload.get("status", "")).strip().lower()
                        worker_result_status = status_value or None
                        if worker_result_status == "failed":
                            error_payload = worker_result_payload.get("error")
                            if isinstance(error_payload, dict):
                                error_class = str(error_payload.get("error_class") or "infrastructure").strip() or "infrastructure"
                                error_message = str(error_payload.get("message") or "").strip()
                                worker_error_detail = (
                                    f"worker failed ({error_class}): {error_message}"
                                    if error_message
                                    else f"worker failed ({error_class})"
                                )
                            else:
                                worker_error_detail = "worker failed"
                except Exception:
                    worker_result_status = None
                    worker_error_detail = None

        timeout_detail = ""
        timeout_value = latest_state_payload.get("timeout_seconds")
        if isinstance(timeout_value, (int, float)):
            timeout_detail = f" (timeout={int(timeout_value)}s)"
        if has_validation_error:
            return "failed", "agent output failed schema validation"
        if has_parsed or has_raw:
            return "completed", f"agent output artifact present{timeout_detail}"
        if worker_result_status == "completed":
            return "completed", f"worker result artifact present{timeout_detail}"
        if worker_result_status == "failed":
            return "failed", worker_error_detail or f"worker result failed{timeout_detail}"
        if latest_state == "completed":
            return "completed", f"agent request completed{timeout_detail}"
        if latest_state == "started":
            is_root_agent2_track = source == "agent2" and "/decomposer/" in artifact_key
            if problem_running or is_root_agent2_track:
                return "pending", f"agent request in progress{timeout_detail}"
            return "failed", f"agent request interrupted{timeout_detail}"
        if latest_state == "queued":
            return "pending", f"agent request queued{timeout_detail}"
        if latest_state == "deferred":
            message = str(latest_state_payload.get("message", "")).strip()
            detail = f"agent request continuing after join timeout{timeout_detail}"
            if message:
                detail = f"{detail}: {message}"
            return "pending", detail
        if latest_state == "failed":
            message = str(latest_state_payload.get("message", "")).strip()
            detail = f"agent request failed{timeout_detail}"
            if message:
                detail = f"{detail}: {message}"
            return "failed", detail
        if has_request_error or has_parse_error:
            return "failed", "agent produced error artifact"
        return ("pending", "waiting for agent output") if problem_running else ("failed", "agent output artifact missing")

    if source == "lean":
        result_key = artifact_key.replace("/request.json", "/result.json")
        if _has_key(result_key):
            return "completed", "lean result artifact present"
        return "pending", "lean result pending"

    return "unknown", None


def _epoch_seconds(value: datetime) -> float:
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _apply_usage_to_entry(entry: DebugRequestLogEntry, usage: LlmUsageRecordORM) -> None:
    input_tokens = int(usage.input_tokens or 0)
    output_tokens = int(usage.output_tokens or 0)
    cached_input_tokens = cached_input_tokens_from_raw_usage(usage.raw_usage)
    cost = float(usage.estimated_cost_usd or 0.0)

    entry.llm_call_count += 1
    entry.llm_input_tokens += input_tokens
    entry.llm_cached_input_tokens += cached_input_tokens
    entry.llm_output_tokens += output_tokens
    entry.llm_total_tokens += input_tokens + output_tokens
    entry.llm_estimated_cost_usd = round(entry.llm_estimated_cost_usd + cost, 8)
    if usage.model and usage.model not in entry.llm_models:
        entry.llm_models.append(usage.model)


def _agent_request_worker_job_id(browser: ArtifactBrowser, artifact_key: str) -> str | None:
    prefix = artifact_key.rsplit("/", 1)[0]
    state_rows = browser.list_files(prefix=prefix, limit=200)
    state_keys = sorted(
        [
            row.artifact_key
            for row in state_rows
            if "_request_state_attempt_" in row.artifact_key and row.artifact_key.endswith(".json")
        ],
        key=lambda item: item,
    )
    for state_key in reversed(state_keys):
        try:
            payload = browser.read(state_key).content
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        worker_job_id = payload.get("worker_job_id")
        if isinstance(worker_job_id, str) and worker_job_id.strip():
            return worker_job_id.strip()
    return None


def _latest_agent_request_state_payload(browser: ArtifactBrowser, artifact_key: str) -> dict[str, Any] | None:
    prefix = artifact_key.rsplit("/", 1)[0]
    state_rows = browser.list_files(prefix=prefix, limit=200)
    state_keys = sorted(
        [
            row.artifact_key
            for row in state_rows
            if "_request_state_attempt_" in row.artifact_key and row.artifact_key.endswith(".json")
        ],
        key=lambda item: item,
    )
    for state_key in reversed(state_keys):
        try:
            payload = browser.read(state_key).content
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _agent_request_runtime_config_from_artifact(
    browser: ArtifactBrowser,
    artifact_key: str | None,
) -> tuple[str | None, str | None, str | None, int | None]:
    key = str(artifact_key or "").strip()
    if not key:
        return None, None, None, None
    payload = _latest_agent_request_state_payload(browser, key)
    if not isinstance(payload, dict):
        return None, None, None, None

    model = payload.get("model")
    reasoning_effort = payload.get("reasoning_effort")
    text_verbosity = payload.get("text_verbosity")
    timeout_seconds = payload.get("timeout_seconds")

    parsed_model = model.strip() if isinstance(model, str) and model.strip() else None
    parsed_reasoning = reasoning_effort.strip() if isinstance(reasoning_effort, str) and reasoning_effort.strip() else None
    parsed_verbosity = text_verbosity.strip() if isinstance(text_verbosity, str) and text_verbosity.strip() else None
    try:
        parsed_timeout = int(timeout_seconds) if timeout_seconds is not None else None
    except Exception:
        parsed_timeout = None

    return parsed_model, parsed_reasoning, parsed_verbosity, parsed_timeout


def _request_error_context_from_artifact(
    browser: ArtifactBrowser,
    response_artifact_key: str | None,
) -> tuple[str | None, str | None]:
    if not response_artifact_key:
        return None, None
    if not browser.exists(response_artifact_key):
        return None, None
    try:
        content = browser.read(response_artifact_key).content
    except Exception:
        return None, None
    if not isinstance(content, dict):
        return None, None

    response_id = content.get("response_id")
    if isinstance(response_id, str) and response_id.strip():
        parsed_response_id = response_id.strip()
    else:
        parsed_response_id = None

    provider_terminal_status = content.get("provider_terminal_status")
    if isinstance(provider_terminal_status, str) and provider_terminal_status.strip():
        parsed_terminal_status = provider_terminal_status.strip()
    else:
        parsed_terminal_status = None

    message = str(content.get("message") or "")
    if parsed_response_id is None:
        match = re.search(r"Background response\s+([^\s]+)", message)
        if match is not None:
            parsed_response_id = match.group(1).strip() or None
    if parsed_terminal_status is None:
        match = re.search(r"terminal status:\s*([A-Za-z0-9_-]+)", message)
        if match is not None:
            parsed_terminal_status = match.group(1).strip() or None

    return parsed_response_id, parsed_terminal_status


def _attach_llm_usage_to_request_entries(
    entries: list[DebugRequestLogEntry],
    usage_rows: list[LlmUsageRecordORM],
) -> None:
    if not entries:
        return

    matched_usage_ids: set[str] = set()
    by_worker_job_id: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if entry.worker_job_id:
            by_worker_job_id[entry.worker_job_id] = index

    for usage in usage_rows:
        if not usage.worker_job_id:
            continue
        entry_index = by_worker_job_id.get(usage.worker_job_id)
        if entry_index is None:
            continue
        _apply_usage_to_entry(entries[entry_index], usage)
        matched_usage_ids.add(usage.usage_id)

    per_stage_entry_indexes: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.source in {"agent1", "agent2", "agent3", "agent4", "agent5"}:
            per_stage_entry_indexes.setdefault(entry.source, []).append(index)

    if not per_stage_entry_indexes:
        return

    per_stage_usage: dict[str, list[LlmUsageRecordORM]] = {}
    for usage in usage_rows:
        per_stage_usage.setdefault(usage.stage, []).append(usage)

    for stage, entry_indexes in per_stage_entry_indexes.items():
        stage_usage = [row for row in per_stage_usage.get(stage, []) if row.usage_id not in matched_usage_ids]
        if not stage_usage:
            continue

        entry_indexes.sort(key=lambda idx: (_epoch_seconds(entries[idx].timestamp), entries[idx].artifact_key))
        stage_usage.sort(key=lambda row: _epoch_seconds(row.created_at))

        bucket = 0
        for usage in stage_usage:
            while (
                bucket + 1 < len(entry_indexes)
                and _epoch_seconds(usage.created_at) >= _epoch_seconds(entries[entry_indexes[bucket + 1]].timestamp)
            ):
                bucket += 1

            _apply_usage_to_entry(entries[entry_indexes[bucket]], usage)


def _build_llm_usage_summary(
    usage_rows: list[LlmUsageRecordORM],
) -> tuple[dict[str, float | int], list[DebugLlmUsageStage]]:
    totals: dict[str, float | int] = {
        "call_count": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    by_stage: dict[str, dict[str, Any]] = {}

    for row in usage_rows:
        stage = row.stage
        bucket = by_stage.setdefault(
            stage,
            {
                "stage": stage,
                "call_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "models": set(),
            },
        )
        input_tokens = int(row.input_tokens or 0)
        output_tokens = int(row.output_tokens or 0)
        cached_input_tokens = cached_input_tokens_from_raw_usage(row.raw_usage)
        cost = float(row.estimated_cost_usd or 0.0)

        bucket["call_count"] += 1
        bucket["input_tokens"] += input_tokens
        bucket["cached_input_tokens"] += cached_input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["total_tokens"] += input_tokens + output_tokens
        bucket["estimated_cost_usd"] += cost
        if row.model:
            bucket["models"].add(row.model)

        totals["call_count"] = int(totals["call_count"]) + 1
        totals["input_tokens"] = int(totals["input_tokens"]) + input_tokens
        totals["cached_input_tokens"] = int(totals["cached_input_tokens"]) + cached_input_tokens
        totals["output_tokens"] = int(totals["output_tokens"]) + output_tokens
        totals["total_tokens"] = int(totals["total_tokens"]) + input_tokens + output_tokens
        totals["estimated_cost_usd"] = float(totals["estimated_cost_usd"]) + cost

    totals["estimated_cost_usd"] = round(float(totals["estimated_cost_usd"]), 8)

    stage_rows = [
        DebugLlmUsageStage(
            stage=stage,
            call_count=int(values["call_count"]),
            input_tokens=int(values["input_tokens"]),
            cached_input_tokens=int(values["cached_input_tokens"]),
            output_tokens=int(values["output_tokens"]),
            total_tokens=int(values["total_tokens"]),
            estimated_cost_usd=round(float(values["estimated_cost_usd"]), 8),
            models=sorted(values["models"]),
        )
        for stage, values in sorted(by_stage.items(), key=lambda item: item[0])
    ]
    return totals, stage_rows


def _collect_request_log_entries(
    db: FileStore,
    browser: ArtifactBrowser,
    problem_id: str,
    *,
    source_filter: str | None,
    limit: int,
) -> list[DebugRequestLogEntry]:
    request_records = RequestRecordRepository(db).list_by_problem(problem_id, source=source_filter, limit=max(1000, limit * 5))
    rows: list[DebugRequestLogEntry] = []
    seen_artifact_keys: set[str] = set()
    scan_limit = max(2000, limit * 10)
    files = browser.list_files(prefix=f"problems/{problem_id}", limit=scan_limit)
    key_set = {row.artifact_key for row in files}
    problem_running = is_problem_running(problem_id) or ProblemExecutionRepository(db).get_active_for_problem(problem_id) is not None
    if request_records:
        for index, record in enumerate(request_records[-limit:]):
            completion_status = "pending"
            completion_detail: str | None = record.error_class
            response_id: str | None = record.response_id
            provider_terminal_status: str | None = None
            llm_model: str | None = record.llm_model
            llm_reasoning_effort: str | None = record.llm_reasoning_effort
            llm_text_verbosity: str | None = record.llm_text_verbosity
            llm_timeout_seconds: int | None = record.llm_timeout_seconds
            if record.status in {"completed", "succeeded"}:
                completion_status = "completed"
            elif record.status in {"failed", "cancelled"}:
                completion_status = "failed"
            elif record.status == "superseded":
                completion_status = "failed"
                completion_detail = "superseded by newer continuation generation"
            elif record.request_artifact_key and record.source.startswith("agent"):
                artifact_status, artifact_detail = _request_completion_status(
                    browser=browser,
                    source=record.source,
                    artifact_key=record.request_artifact_key,
                    problem_id=problem_id,
                    key_set=key_set,
                    problem_running=problem_running,
                )
                if artifact_status in {"completed", "failed"}:
                    completion_status = artifact_status
                    completion_detail = artifact_detail
            artifact_key = record.request_artifact_key or record.response_artifact_key or f"request_records/{record.request_record_id}"
            size_bytes = 0
            if artifact_key and browser.exists(artifact_key):
                try:
                    size_bytes = browser.read(artifact_key).size_bytes
                except Exception:
                    size_bytes = 0
            parsed_response_id, parsed_terminal_status = _request_error_context_from_artifact(
                browser,
                record.response_artifact_key,
            )
            if record.source.startswith("agent"):
                artifact_model, artifact_reasoning, artifact_verbosity, artifact_timeout = _agent_request_runtime_config_from_artifact(
                    browser,
                    record.request_artifact_key or artifact_key,
                )
                if llm_model is None:
                    llm_model = artifact_model
                if llm_reasoning_effort is None:
                    llm_reasoning_effort = artifact_reasoning
                if llm_text_verbosity is None:
                    llm_text_verbosity = artifact_verbosity
                if llm_timeout_seconds is None:
                    llm_timeout_seconds = artifact_timeout
            if parsed_response_id:
                response_id = parsed_response_id
            provider_terminal_status = parsed_terminal_status
            if artifact_key:
                seen_artifact_keys.add(artifact_key)
            rows.append(
                DebugRequestLogEntry(
                    entry_id=f"reqlog_db_{index}",
                    timestamp=record.updated_at,
                    source=record.source,
                    target_id=record.target_id,
                    worker_job_id=record.worker_job_id,
                    artifact_key=artifact_key,
                    summary=record.summary or record.source,
                    completion_status=completion_status,  # type: ignore[arg-type]
                    completion_detail=completion_detail,
                    response_id=response_id,
                    provider_terminal_status=provider_terminal_status,
                    llm_model=llm_model,
                    llm_reasoning_effort=llm_reasoning_effort,
                    llm_text_verbosity=llm_text_verbosity,
                    llm_timeout_seconds=llm_timeout_seconds,
                    size_bytes=size_bytes,
                )
            )

    index = len(rows)
    scan_source_filter = source_filter

    for file_entry in files:
        source, _ = _request_source_from_key(file_entry.artifact_key)
        if not source:
            continue
        if scan_source_filter and source != scan_source_filter:
            continue
        if file_entry.artifact_key in seen_artifact_keys:
            continue

        target_id: str | None = None
        worker_job_id: str | None = None
        parts = file_entry.artifact_key.split("/")
        if len(parts) >= 2 and parts[0] == "problems":
            target_id = parts[1]

        if "/lemmas/" in file_entry.artifact_key:
            if "lemmas" in parts:
                idx = parts.index("lemmas")
                if idx + 1 < len(parts):
                    target_id = parts[idx + 1]
        if "/lean_jobs/" in file_entry.artifact_key:
            if "lean_jobs" in parts:
                idx = parts.index("lean_jobs")
                if idx + 1 < len(parts):
                    target_id = parts[idx + 1]
                    worker_job_id = parts[idx + 1]

        summary = source
        try:
            artifact_payload = browser.read(file_entry.artifact_key)
            summary = _request_summary(source, artifact_payload.content)
            if source == "lean" and isinstance(artifact_payload.content, dict):
                target_id = target_id or artifact_payload.content.get("target_id")
            if source == "api_run" and isinstance(artifact_payload.content, dict):
                target_id = artifact_payload.content.get("problem_id") or target_id
        except Exception:
            pass

        if source in {"agent2", "agent3", "agent4", "agent5"}:
            worker_job_id = _agent_request_worker_job_id(browser, file_entry.artifact_key)
        llm_model, llm_reasoning_effort, llm_text_verbosity, llm_timeout_seconds = (
            _agent_request_runtime_config_from_artifact(browser, file_entry.artifact_key)
            if source.startswith("agent")
            else (None, None, None, None)
        )

        completion_status, completion_detail = _request_completion_status(
            browser=browser,
            source=source,
            artifact_key=file_entry.artifact_key,
            problem_id=problem_id,
            key_set=key_set,
            problem_running=problem_running,
        )

        rows.append(
            DebugRequestLogEntry(
                entry_id=f"reqlog_{index}",
                timestamp=file_entry.modified_at,
                source=source,
                target_id=target_id,
                worker_job_id=worker_job_id,
                artifact_key=file_entry.artifact_key,
                summary=summary,
                completion_status=completion_status,
                completion_detail=completion_detail,
                response_id=None,
                provider_terminal_status=None,
                llm_model=llm_model,
                llm_reasoning_effort=llm_reasoning_effort,
                llm_text_verbosity=llm_text_verbosity,
                llm_timeout_seconds=llm_timeout_seconds,
                size_bytes=file_entry.size_bytes,
            )
        )
        index += 1

    def _sort_timestamp(value: datetime) -> float:
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized.timestamp()

    rows.sort(key=lambda row: (_sort_timestamp(row.timestamp), row.artifact_key))
    rows = rows[-limit:]
    usage_rows = [row for row in LlmUsageRepository(db).list_by_problem(problem_id) if row.provider == "openai"]
    _attach_llm_usage_to_request_entries(rows, usage_rows)
    return rows


def _reconcile_incomplete_run_requests(problem_id: str, browser: ArtifactBrowser) -> tuple[int, int]:
    request_prefix = f"problems/{problem_id}/api/run_requests"
    request_rows = [row for row in browser.list_files(prefix=request_prefix, limit=5000) if row.artifact_key.endswith(".request.json")]
    if not request_rows:
        return 0, 0

    running = is_problem_running(problem_id)
    store = ArtifactStore()
    scanned = 0
    repaired = 0
    for request_row in request_rows:
        scanned += 1
        response_key = request_row.artifact_key.replace(".request.json", ".response.json")
        if browser.exists(response_key):
            try:
                payload = browser.read(response_key).content
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("ok") is None and payload.get("status") == "in_progress" and not running:
                repaired += 1
                store.save_json(
                    response_key,
                    {
                        "ok": False,
                        "status": "failed",
                        "status_code": 500,
                        "problem_id": problem_id,
                        "request_artifact": request_row.artifact_key,
                        "server_time": _now_utc().isoformat(),
                        "error": {
                            "code": "run_interrupted",
                            "message": "compatibility run request interrupted before completion",
                            "details": {
                                "reason": "in_progress response artifact remained without active run lock",
                            },
                        },
                    },
                )
            continue
        if running:
            continue
        repaired += 1
        request_payload = None
        try:
            content = browser.read(request_row.artifact_key).content
            if isinstance(content, dict):
                request_payload = content
        except Exception:
            request_payload = None
        store.save_json(
            response_key,
            {
                "ok": False,
                "status": "failed",
                "status_code": 500,
                "problem_id": problem_id,
                "request_artifact": request_row.artifact_key,
                "run_request_id": request_payload.get("run_request_id") if isinstance(request_payload, dict) else None,
                "trigger": request_payload.get("trigger") if isinstance(request_payload, dict) else None,
                "server_time": _now_utc().isoformat(),
                "error": {
                    "code": "run_interrupted",
                            "message": "compatibility run request interrupted before completion",
                    "details": {
                        "reason": "missing run response artifact and no active run lock",
                    },
                },
            },
        )
    return scanned, repaired


def _is_infrastructure_failure_problem(problem_id: str, db: FileStore) -> bool:
    failure = FailureReportRepository(db).get_by_problem(problem_id)
    if failure is not None:
        terminal_class = str(failure.terminal_error_class or "").strip().lower()
        terminal_message = str(failure.terminal_error_message or "").lower()
        if "infrastructure" in terminal_class or terminal_class in {"timeout", "infrastructure_transient"}:
            return True
        if "infrastructure failure count" in terminal_message:
            return True
        return False

    events = EventRepository(db).list_for_problem(problem_id, limit=5000)
    for event in reversed(events):
        if event.stage != "problem.failed":
            continue
        reason = str(event.reason or "").lower()
        return "infrastructure failure count" in reason or "infrastructure" in reason
    return False


def _promote_legacy_solver_retry_state(problem_id: str, db: FileStore) -> tuple[str | None, str | None]:
    request_rows = RequestRecordRepository(db).list_by_problem(problem_id, source="agent4", limit=5000)
    failed_agent4_rows = [
        row
        for row in request_rows
        if row.status == "failed"
        and row.target_id
        and isinstance(row.error_class, str)
        and row.error_class.startswith("infrastructure")
    ]
    if not failed_agent4_rows:
        return None, None

    latest = failed_agent4_rows[-1]
    lemma_id = str(latest.target_id or "").strip()
    if not lemma_id:
        return None, None

    lemma_repo = LemmaRepository(db)
    lemma = lemma_repo.get(lemma_id)
    if lemma is None:
        return None, None

    attempt_from_artifact = 0
    request_artifact_key = str(latest.request_artifact_key or "")
    match = re.search(r"/solver_attempt_(\d+)/", request_artifact_key)
    if match is not None:
        try:
            attempt_from_artifact = int(match.group(1))
        except Exception:
            attempt_from_artifact = 0
    if attempt_from_artifact > 0:
        lemma.solver_attempt_count = max(lemma.solver_attempt_count, attempt_from_artifact)

    if lemma.routing_status != RoutingStatus.DONE.value:
        lemma.proof_status = ProofStatus.PROOF_FLAWED.value
        lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
    lemma_repo.save(lemma)

    worker_jobs = WorkerJobRepository(db)
    problem = ProblemRepository(db).get(problem_id)
    continuation_generation = int(problem.continuation_generation or 0) if problem is not None else 0
    existing_solver_rows = [
        row
        for row in worker_jobs.list_by_problem(problem_id, worker_kind="lemma_solver")
        if row.target_id == lemma_id
        and row.superseded_at is None
        and int(row.continuation_generation or 0) == continuation_generation
    ]
    if existing_solver_rows:
        return lemma_id, None

    next_attempt = max(1, lemma.solver_attempt_count + 1)
    new_job_id = f"wrk_{lemma.lemma_id}_g{continuation_generation}_solve_{next_attempt}"
    if worker_jobs.get(new_job_id) is not None:
        return lemma_id, None

    request_payload: dict[str, Any] = {}
    browser = ArtifactBrowser(get_settings().artifact_store_dir)
    if latest.request_artifact_key and browser.exists(latest.request_artifact_key):
        try:
            payload_candidate = browser.read(latest.request_artifact_key).content
            if isinstance(payload_candidate, dict):
                request_payload = dict(payload_candidate)
        except Exception:
            request_payload = {}

    theorem = TheoremRepository(db).get(problem.root_theorem_id) if problem and problem.root_theorem_id else None
    if not isinstance(request_payload.get("lemma_id"), str):
        request_payload["lemma_id"] = lemma.lemma_id
    if not isinstance(request_payload.get("statement_nl"), str):
        request_payload["statement_nl"] = lemma.statement_nl
    if not isinstance(request_payload.get("semantic_sketch"), dict):
        request_payload["semantic_sketch"] = dict(lemma.statement_semantic_sketch or {})
    request_payload["root_theorem_nl"] = ""
    if not isinstance(request_payload.get("root_semantic_sketch"), dict):
        request_payload["root_semantic_sketch"] = (
            dict(theorem.statement_semantic_sketch or {}) if theorem is not None else {}
        )
    if not isinstance(request_payload.get("role_in_assembly"), str):
        request_payload["role_in_assembly"] = lemma.role_in_parent or ""
    if not isinstance(request_payload.get("shared_context"), list):
        request_payload["shared_context"] = []
    if not isinstance(request_payload.get("definition_context"), list):
        request_payload["definition_context"] = []
    if not isinstance(request_payload.get("trusted_context_summaries"), list):
        request_payload["trusted_context_summaries"] = []
    if not isinstance(request_payload.get("allowed_dependency_manifest"), list):
        request_payload["allowed_dependency_manifest"] = []
    if not isinstance(request_payload.get("forbidden_claims"), list):
        request_payload["forbidden_claims"] = []
    request_payload["previous_proof_nl"] = lemma.latest_nl_proof
    if "previous_feedback" not in request_payload:
        request_payload["previous_feedback"] = None
    request_payload["attempt_number"] = next_attempt

    worker_jobs.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id=new_job_id,
            problem_id=problem_id,
            worker_kind="lemma_solver",
            status="queued",
            target_id=lemma.lemma_id,
            target_kind="lemma",
            execution_id=None,
            continuation_generation=continuation_generation,
            attempt_number=next_attempt,
            artifact_prefix=f"problems/{problem_id}/lemmas/{lemma.lemma_id}/solver_attempt_{next_attempt}",
            handler_key="agent4_lemma_solver",
            request_source="lemma_solver",
            payload=request_payload,
            llm_override_key="agent4_first" if next_attempt == 1 else None,
        )
    )
    return lemma_id, new_job_id


@router.get("/problem-create-template", response_model=DebugProblemCreateTemplateResponse)
def get_problem_create_template() -> DebugProblemCreateTemplateResponse:
    cfg = ProblemConfig()
    template = {
        "title": "",
        "statement_nl": "",
        "statement_lean": None,
        "imports": ["Mathlib"],
        "lean_image_tag": "Orthosolver-lean-4.18.0-mathlib-v4.18.0",
        "initial_trusted_context": [],
        # Guided form exposes the simplified user-facing config surface.
        "config": {
            "mode": {
                "nl_only_mode": cfg.mode.nl_only_mode,
                "lean_mode": cfg.mode.lean_mode,
                "lean": cfg.mode.lean.model_dump(by_alias=True),
            },
            "budget": {
                "max_estimated_cost_usd_per_problem": cfg.budget.max_estimated_cost_usd_per_problem,
                "max_estimated_cost_usd_per_lemma": cfg.budget.max_estimated_cost_usd_per_lemma,
            },
            "decomposition": {
                "parallel_root_decompositions_n": cfg.decomposition.parallel_root_decompositions_n,
                "parallel_root_take_k": cfg.decomposition.parallel_root_take_k,
                "root_solutions_required_for_termination": cfg.decomposition.root_solutions_required_for_termination,
                "max_consecutive_fatal_rejections_per_node": cfg.decomposition.max_consecutive_fatal_rejections_per_node,
                "max_decompositions_per_failed_lemma": cfg.decomposition.max_decompositions_per_failed_lemma,
            },
            "lemma_solving": {
                "max_consecutive_fatal_rejections_per_lemma": cfg.lemma_solving.max_consecutive_fatal_rejections_per_lemma,
                "max_minor_rejections_per_lemma": cfg.lemma_solving.max_minor_rejections_per_lemma,
                "max_total_lemma_nodes": cfg.lemma_solving.max_total_lemma_nodes,
                "max_solver_attempts_per_lemma_total": cfg.lemma_solving.max_solver_attempts_per_lemma_total,
                "max_consecutive_infrastructure_failures_per_lemma": cfg.lemma_solving.max_consecutive_infrastructure_failures_per_lemma,
                "max_solver_series_wall_clock_seconds_per_lemma": cfg.lemma_solving.max_solver_series_wall_clock_seconds_per_lemma,
            },
            "lean_engine": {
                "model": cfg.lean_engine.model,
                "max_repair_rounds": cfg.lean_engine.max_repair_rounds,
                "max_lean_jobs_per_lemma": cfg.lean_engine.max_lean_jobs_per_lemma,
                "repair_context_token_budget": cfg.lean_engine.repair_context_token_budget,
                "assemble_root_repair_rounds": cfg.lean_engine.assemble_root_repair_rounds,
                "assembly_check_timeout_seconds": cfg.lean_engine.assembly_check_timeout_seconds,
                "lean_job_timeout_seconds": cfg.lean_engine.lean_job_timeout_seconds,
                "plausibility_check_timeout_seconds": cfg.lean_engine.plausibility_check_timeout_seconds,
                "assemble_root_timeout_seconds": cfg.lean_engine.assemble_root_timeout_seconds,
            },
            "final_check": {
                "fail_problem_on_fatal": cfg.final_check.fail_problem_on_fatal,
            },
            "llm": cfg.llm.model_dump(by_alias=True),
        },
    }
    return DebugProblemCreateTemplateResponse(request_id=new_id("req"), server_time=_now_utc(), template=template)


@router.get("/problems", response_model=DebugProblemsListResponse)
def list_debug_problems(
    limit: int = Query(default=50, ge=1, le=500),
    db: FileStore = Depends(get_db),
) -> DebugProblemsListResponse:
    index = db.read_index()
    problems: list[DebugProblemItem] = []
    for pid in index:
        p = ProblemRepository(db).get(pid)
        if p:
            problems.append(
                DebugProblemItem(
                    problem_id=p.problem_id,
                    title=p.title,
                    status=p.status,
                    verification_level=p.verification_level,
                    nl_only_mode=p.nl_only_mode,
                    lean_mode=not p.nl_only_mode,
                    root_theorem_id=p.root_theorem_id,
                    active_decomposition_id=p.active_decomposition_id,
                    standby_decomposition_id=p.standby_decomposition_id,
                    created_at=p.created_at,
                    updated_at=p.updated_at,
                )
            )
    # Sort by created_at descending, take the first `limit`
    problems.sort(key=lambda item: item.created_at, reverse=True)
    problems = problems[:limit]
    return DebugProblemsListResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problems=problems,
    )


@router.get("/problems/{problem_id}/input-json", response_model=DebugProblemInputJsonResponse)
def get_problem_input_json(
    problem_id: str,
    db: FileStore = Depends(get_db),
) -> DebugProblemInputJsonResponse:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")

    browser = ArtifactBrowser(get_settings().artifact_store_dir)
    artifact_key: str | None = None

    request_rows = RequestRecordRepository(db).list_by_problem(problem_id, source="api_create", limit=100)
    for row in request_rows:
        key = str(row.request_artifact_key or "").strip()
        if key:
            artifact_key = key
            break

    if artifact_key is None:
        fallback_key = f"problems/{problem_id}/api/problem_create_request.json"
        if browser.exists(fallback_key):
            artifact_key = fallback_key

    if artifact_key is None:
        _raise_api_error(
            404,
            code="problem_input_not_found",
            message="problem create-request JSON artifact not found",
        )

    try:
        artifact = browser.read(artifact_key)
    except FileNotFoundError:
        _raise_api_error(
            404,
            code="problem_input_not_found",
            message="problem create-request JSON artifact not found",
        )
    except ArtifactSecurityError as exc:
        _raise_api_error(400, code="invalid_artifact_key", message=str(exc))

    if not isinstance(artifact.content, dict):
        _raise_api_error(
            409,
            code="invalid_problem_input_artifact",
            message="problem create-request artifact is not a JSON object",
        )

    return DebugProblemInputJsonResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problem_id=problem_id,
        artifact_key=artifact.artifact_key,
        input_json=artifact.content,
    )


@router.get("/problems/{problem_id}/snapshot", response_model=DebugProblemSnapshotResponse)
def get_debug_problem_snapshot(
    problem_id: str,
    events_limit: int = Query(default=250, ge=1, le=5000),
    db: FileStore = Depends(get_db),
) -> DebugProblemSnapshotResponse:
    problems = ProblemRepository(db)
    theorems = TheoremRepository(db)
    lemmas = LemmaRepository(db)
    decompositions = DecompositionRepository(db)
    assembly_plans = AssemblyPlanRepository(db)
    lean_jobs = LeanJobRepository(db)
    trusted_context = TrustedContextRepository(db)
    proof_graphs = ProofGraphRepository(db)
    proof_graph_nodes = ProofGraphNodeRepository(db)
    proof_graph_edges = ProofGraphEdgeRepository(db)
    proof_dependency_checks = ProofDependencyCheckRepository(db)
    vetter_reports = VetterReportRepository(db)

    problem = problems.get(problem_id)
    if not problem:
        _raise_api_error(404, code="problem_not_found", message="problem not found")

    theorem = theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
    root_tree = _build_problem_tree(db, problem_id, theorem) if theorem else None

    lemma_rows = lemmas.list_by_problem(problem_id)
    decomposition_rows = decompositions.list_by_problem(problem_id)
    counterexamples = CounterexampleRepository(db)
    proof_attempts = LemmaProofAttemptRepository(db)
    job_rows = lean_jobs.list_by_problem(problem_id)
    event_rows = _tail_events(db, problem_id, events_limit)
    event_items = [_to_event_item(row) for row in event_rows]

    artifact_refs: set[str] = set()
    if problem.failure_report_artifact_id:
        artifact_refs.add(problem.failure_report_artifact_id)
    if problem.running_final_proof_artifact_id:
        artifact_refs.add(problem.running_final_proof_artifact_id)
    if problem.final_proof_artifact_id:
        artifact_refs.add(problem.final_proof_artifact_id)
    if theorem:
        artifact_refs.update(theorem.artifact_ids or [])
    for event in event_rows:
        if event.worker_job_id:
            artifact_refs.add(f"worker_jobs/{event.worker_job_id}/result.json")

    artifacts = ArtifactStore()
    running_final_proof = _load_optional_json_artifact(artifacts, problem.running_final_proof_artifact_id)
    final_proof = _load_optional_json_artifact(artifacts, problem.final_proof_artifact_id)
    if running_final_proof is None:
        running_final_proof = final_proof
    legacy_reconstructed_key = f"problems/{problem_id}/proof_bundles/root/legacy_reconstructed.json"
    legacy_reconstructed_proof = _load_optional_json_artifact(artifacts, legacy_reconstructed_key)
    if legacy_reconstructed_proof is not None:
        artifact_refs.add(legacy_reconstructed_key)

    lean_jobs_payload: list[dict[str, Any]] = []
    for job in job_rows:
        if job.request_artifact_id:
            artifact_refs.add(job.request_artifact_id)
        if job.result_artifact_id:
            artifact_refs.add(job.result_artifact_id)

        result_row = LeanResultRepository(db).get_for_job(job.job_id)
        if result_row and result_row.lean_code_artifact_id:
            artifact_refs.add(result_row.lean_code_artifact_id)
        if result_row and result_row.compiler_log_artifact_id:
            artifact_refs.add(result_row.compiler_log_artifact_id)

        lean_jobs_payload.append(
            {
                "job_id": job.job_id,
                "target_id": job.target_id,
                "target_kind": job.target_kind,
                "mode": job.mode,
                "operation": job.operation,
                "status": job.status,
                "attempt_index": job.attempt_index,
                "progress_snapshot": job.progress_snapshot,
                "issue_kind": job.issue_kind,
                "confidence": job.confidence,
                "fatality": job.fatality,
                "last_error": job.last_error,
                "request_artifact_id": job.request_artifact_id,
                "result_artifact_id": job.result_artifact_id,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "result": (
                    {
                        "result_id": result_row.result_id,
                        "status": result_row.status,
                        "error_class": result_row.error_class,
                        "issue_kind": result_row.issue_kind,
                        "confidence": result_row.confidence,
                        "fatality": result_row.fatality,
                        "error_scope": result_row.error_scope,
                        "error_message": result_row.error_message,
                        "progress_snapshot": result_row.progress_snapshot,
                        "diagnostics": result_row.diagnostics,
                        "decl_name": result_row.decl_name,
                        "artifact_index": result_row.artifact_index,
                        "recommended_next_step": result_row.recommended_next_step,
                        "routing_confidence": result_row.routing_confidence,
                    }
                    if result_row
                    else None
                ),
            }
        )

    lemmas_payload: list[dict[str, Any]] = []
    for lemma in lemma_rows:
        latest_report = vetter_reports.get(lemma.latest_vetter_report_id) if lemma.latest_vetter_report_id else None
        latest_lean_result = LeanResultRepository(db).get(lemma.latest_lean_result_id) if lemma.latest_lean_result_id else None
        active_counterexample = counterexamples.get(lemma.active_counterexample_id) if lemma.active_counterexample_id else None
        proof_attempt_rows = proof_attempts.list_by_lemma(problem_id, lemma.lemma_id)
        if lemma.proof_bundle_artifact_id:
            artifact_refs.add(lemma.proof_bundle_artifact_id)
        for attempt in proof_attempt_rows:
            if attempt.artifact_key:
                artifact_refs.add(attempt.artifact_key)
        lemmas_payload.append(
            {
                "lemma_id": lemma.lemma_id,
                "proof_graph_id": lemma.proof_graph_id,
                "claim_node_id": lemma.claim_node_id,
                "parent_id": lemma.parent_id,
                "parent_kind": lemma.parent_kind,
                "kind": lemma.kind,
                "depth": lemma.depth,
                "statement_nl": lemma.statement_nl,
                "statement_semantic_sketch": lemma.statement_semantic_sketch,
                "role_in_parent": lemma.role_in_parent,
                "assembly_step_ids": lemma.assembly_step_ids,
                "statement_status": lemma.statement_status,
                "truth_status": lemma.truth_status,
                "proof_status": lemma.proof_status,
                "routing_status": lemma.routing_status,
                "counterexample_status": lemma.counterexample_status,
                "active_counterexample_id": lemma.active_counterexample_id,
                "dependency_status": lemma.dependency_status,
                "last_dependency_check_id": lemma.last_dependency_check_id,
                "latest_nl_proof": lemma.latest_nl_proof,
                "proof_bundle_artifact_id": lemma.proof_bundle_artifact_id,
                "proof_bundle": _load_optional_json_artifact(artifacts, lemma.proof_bundle_artifact_id),
                "solver_attempt_count": lemma.solver_attempt_count,
                "consecutive_fatal_rejections": lemma.consecutive_fatal_rejections,
                "minor_rejection_count": lemma.minor_rejection_count,
                "decomposition_count": lemma.decomposition_count,
                "decomposition_round_count": lemma.decomposition_round_count,
                "lean_attempt_count": lemma.lean_attempt_count,
                "lean_identical_fatal_count": lemma.lean_identical_fatal_count,
                "consecutive_infrastructure_failures": lemma.consecutive_infrastructure_failures,
                "solver_series_started_at": lemma.solver_series_started_at,
                "next_action": lemma.next_action,
                "last_terminal_worker_result": lemma.last_terminal_worker_result,
                "last_transition_reason": lemma.last_transition_reason,
                "latest_vetter_report_id": lemma.latest_vetter_report_id,
                "latest_lean_result_id": lemma.latest_lean_result_id,
                "latest_lean_issue_class": lemma.latest_lean_issue_class,
                "latest_lean_issue_kind": lemma.latest_lean_issue_kind,
                "created_at": lemma.created_at,
                "updated_at": lemma.updated_at,
                "last_activity_at": lemma.last_activity_at,
                "active_counterexample": (
                    {
                        "counterexample_id": active_counterexample.counterexample_id,
                        "status": active_counterexample.status,
                        "source_agent": active_counterexample.source_agent,
                        "source_worker_job_id": active_counterexample.source_worker_job_id,
                        "source_attempt_number": active_counterexample.source_attempt_number,
                        "counterexample_text": active_counterexample.counterexample_text,
                        "summary": active_counterexample.summary,
                        "confidence": active_counterexample.confidence,
                        "accepted_by_report_id": active_counterexample.accepted_by_report_id,
                        "rejected_by_report_id": active_counterexample.rejected_by_report_id,
                        "created_at": active_counterexample.created_at,
                        "updated_at": active_counterexample.updated_at,
                    }
                    if active_counterexample
                    else None
                ),
                "proof_attempts": [
                    {
                        "proof_attempt_id": attempt.proof_attempt_id,
                        "attempt_number": attempt.attempt_number,
                        "solver_worker_job_id": attempt.solver_worker_job_id,
                        "solver_status": attempt.solver_status,
                        "solver_summary": attempt.solver_summary,
                        "solver_artifact_prefix": attempt.solver_artifact_prefix,
                        "proof_nl": attempt.proof_nl,
                        "solver_candidate_counterexample": attempt.solver_candidate_counterexample,
                        "counterexample_record_id": attempt.counterexample_record_id,
                        "counterexample_vetter_worker_job_id": attempt.counterexample_vetter_worker_job_id,
                        "counterexample_vetter_status": attempt.counterexample_vetter_status,
                        "vetter_worker_job_id": attempt.vetter_worker_job_id,
                        "vetter_report_id": attempt.vetter_report_id,
                        "vetter_statement_status": attempt.vetter_statement_status,
                        "vetter_proof_status": attempt.vetter_proof_status,
                        "vetter_reason": attempt.vetter_reason,
                        "terminal_disposition": attempt.terminal_disposition,
                        "artifact_key": attempt.artifact_key,
                        "artifact": _load_optional_json_artifact(artifacts, attempt.artifact_key),
                        "created_at": attempt.created_at,
                        "updated_at": attempt.updated_at,
                    }
                    for attempt in proof_attempt_rows
                ],
                "latest_vetter_report": (
                    {
                        "report_id": latest_report.report_id,
                        "statement_status": latest_report.statement_status,
                        "proof_status": latest_report.proof_status,
                        "drift_assessment": latest_report.drift_assessment,
                        "recommended_action": latest_report.recommended_action,
                        "reason": latest_report.reason,
                        "confidence": latest_report.confidence,
                        "feedback_for_solver": latest_report.feedback_for_solver,
                        "detailed_findings": latest_report.detailed_findings,
                        "created_at": latest_report.created_at,
                    }
                    if latest_report
                    else None
                ),
                "latest_lean_result": (
                    {
                        "result_id": latest_lean_result.result_id,
                        "status": latest_lean_result.status,
                        "error_class": latest_lean_result.error_class,
                        "issue_kind": latest_lean_result.issue_kind,
                        "confidence": latest_lean_result.confidence,
                        "fatality": latest_lean_result.fatality,
                        "error_scope": latest_lean_result.error_scope,
                        "error_message": latest_lean_result.error_message,
                        "progress_snapshot": latest_lean_result.progress_snapshot,
                        "decl_name": latest_lean_result.decl_name,
                        "artifact_index": latest_lean_result.artifact_index,
                        "recommended_next_step": latest_lean_result.recommended_next_step,
                        "routing_confidence": latest_lean_result.routing_confidence,
                    }
                    if latest_lean_result
                    else None
                ),
            }
        )

    def _load_final_check_data(art_store: ArtifactStore, pid: str, dec_row) -> dict[str, Any] | None:
        if not dec_row.final_check_job_id:
            return None
        result: dict[str, Any] = {}
        # Load Agent 6 output from worker result
        worker_key = f"worker_jobs/{dec_row.final_check_job_id}/result.json"
        if art_store.exists(worker_key):
            try:
                worker_payload = art_store.load_json(worker_key)
                if isinstance(worker_payload, dict):
                    result["output"] = worker_payload.get("output")
            except Exception:
                pass
        # Load Agent 6 input from artifact prefix
        input_key = f"problems/{pid}/final_check/{dec_row.decomposition_id}/agent6_input.json"
        if art_store.exists(input_key):
            try:
                result["input"] = art_store.load_json(input_key)
            except Exception:
                pass
        return result if result else None

    decompositions_payload: list[dict[str, Any]] = []
    for row in decomposition_rows:
        plan = assembly_plans.get(row.assembly_plan_id) if row.assembly_plan_id else None
        if row.proof_bundle_artifact_id:
            artifact_refs.add(row.proof_bundle_artifact_id)

        # Load Agent 3 vetter output from artifacts.
        agent3_vetter_output: dict[str, Any] | None = None
        worker_key = f"worker_jobs/wrk_{row.decomposition_id}_vet/result.json"
        if artifacts.exists(worker_key):
            try:
                worker_payload = artifacts.load_json(worker_key)
                if isinstance(worker_payload, dict):
                    agent3_vetter_output = worker_payload.get("output")
            except Exception:
                pass
        if agent3_vetter_output is None:
            for attempt in (2, 1):
                parsed_key = f"problems/{problem_id}/decomposition_vetter/{row.decomposition_id}/agent3_parsed_output_attempt_{attempt}.json"
                if artifacts.exists(parsed_key):
                    try:
                        agent3_vetter_output = artifacts.load_json(parsed_key)
                        break
                    except Exception:
                        continue

        # Load Agent 2 candidate (lemma statements) from agent3 input artifact.
        agent2_candidate: dict[str, Any] | None = None
        agent3_input_key = f"problems/{problem_id}/decomposition_vetter/{row.decomposition_id}/agent3_input.json"
        if artifacts.exists(agent3_input_key):
            try:
                agent3_input = artifacts.load_json(agent3_input_key)
                if isinstance(agent3_input, dict):
                    agent2_candidate = agent3_input.get("decomposition")
            except Exception:
                pass

        decompositions_payload.append(
            {
                "decomposition_id": row.decomposition_id,
                "logical_decomposition_id": row.logical_decomposition_id or row.decomposition_id,
                "revision_number": row.revision_number,
                "node_id": row.node_id,
                "node_kind": row.node_kind,
                "strategy_summary": row.strategy_summary,
                "shared_context": row.shared_context,
                "lemma_ids": row.lemma_ids,
                "formalization_cost_estimate": row.formalization_cost_estimate,
                "llm_vetting_status": row.llm_vetting_status,
                "lean_assembly_status": row.lean_assembly_status,
                "controller_status": row.controller_status,
                "proof_graph_id": row.proof_graph_id,
                "dependency_status": row.dependency_status,
                "equivalence_risk": row.equivalence_risk,
                "pinned_statement_signatures": row.pinned_statement_signatures,
                "lean_run_dir": row.lean_run_dir,
                "lean_v2_track_id": row.lean_v2_track_id,
                "lean_v2_lemma_handles": row.lean_v2_lemma_handles,
                "lean_v2_prepare_status": row.lean_v2_prepare_status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "assembly_plan": (
                    {
                        "assembly_plan_id": plan.assembly_plan_id,
                        "steps": plan.steps,
                        "proof_skeleton_nl": plan.proof_skeleton_nl,
                        "is_trivially_composable": plan.is_trivially_composable,
                    }
                    if plan
                    else None
                ),
                "agent2_candidate": agent2_candidate,
                "agent3_vetter_output": agent3_vetter_output,
                "final_check_passed": row.final_check_passed,
                "final_check_job_id": row.final_check_job_id,
                "invalidated_by_lemma_id": row.invalidated_by_lemma_id,
                "invalidated_by_counterexample_id": row.invalidated_by_counterexample_id,
                "proof_bundle_artifact_id": row.proof_bundle_artifact_id,
                "proof_bundle": _load_optional_json_artifact(artifacts, row.proof_bundle_artifact_id),
                "agent6_final_check": _load_final_check_data(artifacts, problem_id, row),
            }
        )

    context_rows = trusted_context.list_by_problem(problem_id)
    context_payload = [
        {
            "id": row.id,
            "proof_graph_id": row.proof_graph_id,
            "context_scope": row.context_scope,
            "decl_name": row.decl_name,
            "lean_code": row.lean_code,
            "source_lemma_id": row.source_lemma_id,
            "source_job_id": row.source_job_id,
            "created_at": row.created_at,
        }
        for row in context_rows
    ]
    proof_graph_rows = [row.model_dump(mode="json") for row in proof_graphs.list_by_problem(problem_id)]
    proof_graph_node_rows = [row.model_dump(mode="json") for row in proof_graph_nodes.list_by_problem(problem_id)]
    proof_graph_edge_rows = [row.model_dump(mode="json") for row in proof_graph_edges.list_by_problem(problem_id)]
    proof_dependency_check_rows = [row.model_dump(mode="json") for row in proof_dependency_checks.list_by_problem(problem_id)]
    usage_rows = [row for row in LlmUsageRepository(db).list_by_problem(problem_id) if row.provider == "openai"]
    usage_totals, usage_by_stage = _build_llm_usage_summary(usage_rows)
    cfg = ProblemConfig.model_validate(problem.config)
    problem_spend = float(usage_totals.get("estimated_cost_usd", 0.0) or 0.0)
    problem_budget = cfg.budget.max_estimated_cost_usd_per_problem
    remaining_problem_budget = None if problem_budget is None else round(float(problem_budget) - problem_spend, 8)

    lemma_by_id = {row["lemma_id"]: row for row in lemmas_payload}
    decomposition_by_id = {row["decomposition_id"]: row for row in decompositions_payload}
    logical_decompositions_payload, logical_decomposition_by_id = _build_logical_decompositions(decompositions_payload)
    lean_job_by_id = {row["job_id"]: row for row in lean_jobs_payload}
    final_check_by_id: dict[str, Any] = {}
    for dec_row in decompositions_payload:
        fc_job_id = dec_row.get("final_check_job_id")
        if fc_job_id:
            final_check_by_id[fc_job_id] = {
                "job_id": fc_job_id,
                "decomposition_id": dec_row["decomposition_id"],
                "final_check_passed": dec_row.get("final_check_passed", False),
                "agent6_final_check": dec_row.get("agent6_final_check"),
            }
    lemma_owner_decomposition, visible_lemma_ids, hidden_candidate_lemma_ids = _compute_lemma_visibility(
        lemmas_payload,
        decompositions_payload,
    )
    node_graph = _build_node_graph(
        problem_id,
        theorem,
        lemmas_payload,
        logical_decompositions_payload,
        lean_jobs_payload,
        visible_lemma_ids=set(visible_lemma_ids),
    )
    root_track_decomposition_ids = _root_track_decomposition_ids(
        problem=problem,
        theorem=theorem,
        decompositions_payload=decompositions_payload,
    )
    nl_only_final_output = _build_nl_only_final_output(
        problem=problem,
        theorem=theorem,
        decompositions_payload=decompositions_payload,
        lemmas_payload=lemmas_payload,
        visible_lemma_ids=visible_lemma_ids,
    )
    if nl_only_final_output is not None and final_proof is not None:
        nl_only_final_output["proof_bundle"] = final_proof

    return DebugProblemSnapshotResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problem_id=problem.problem_id,
        problem={
            "problem_id": problem.problem_id,
            "title": problem.title,
            "status": problem.status,
            "verification_level": problem.verification_level,
            "nl_only_mode": problem.nl_only_mode,
            "lean_mode": not problem.nl_only_mode,
            "root_theorem_id": problem.root_theorem_id,
            "active_decomposition_id": problem.active_decomposition_id,
            "standby_decomposition_id": problem.standby_decomposition_id,
            "active_proof_graph_id": problem.active_proof_graph_id,
            "dependency_verification_level": problem.dependency_verification_level,
            "resume_anchor_lemma_id": problem.resume_anchor_lemma_id,
            "resume_anchor_owner_decomposition_id": problem.resume_anchor_owner_decomposition_id,
            "failure_report_artifact_id": problem.failure_report_artifact_id,
            "running_final_proof_artifact_id": problem.running_final_proof_artifact_id,
            "final_proof_artifact_id": problem.final_proof_artifact_id,
            "budget_guardrails": cfg.budget.model_dump(),
            "llm_profile": cfg.llm.model_dump(exclude_none=True),
            "cost_summary": {
                "estimated_cost_usd": round(problem_spend, 8),
                "problem_budget_usd": problem_budget,
                "remaining_problem_budget_usd": remaining_problem_budget,
                "openai_call_count": int(usage_totals.get("call_count", 0) or 0),
                "by_stage": [row.model_dump() for row in usage_by_stage],
            },
            "created_at": problem.created_at,
            "updated_at": problem.updated_at,
        },
        root_theorem=(
            {
                "theorem_id": theorem.theorem_id,
                "statement_nl": theorem.statement_nl,
                "statement_lean": theorem.statement_lean,
                "statement_semantic_sketch": theorem.statement_semantic_sketch,
                "status": theorem.status,
                "active_decomposition_id": theorem.active_decomposition_id,
                "final_decl_name": theorem.final_decl_name,
                "artifact_ids": theorem.artifact_ids,
                "created_at": theorem.created_at,
                "updated_at": theorem.updated_at,
            }
            if theorem
            else None
        ),
        tree_summary=_tree_summary(db, problem.problem_id, problem.root_theorem_id),
        root_tree=root_tree,
        node_graph=node_graph,
        root_track_decomposition_ids=root_track_decomposition_ids,
        visible_lemma_ids=visible_lemma_ids,
        hidden_candidate_lemma_ids=hidden_candidate_lemma_ids,
        lemma_owner_decomposition=lemma_owner_decomposition,
        running_final_proof=running_final_proof,
        final_proof=final_proof,
        legacy_reconstructed_proof=legacy_reconstructed_proof,
        nl_only_final_output=nl_only_final_output,
        lemma_by_id=lemma_by_id,
        decomposition_by_id=decomposition_by_id,
        logical_decomposition_by_id=logical_decomposition_by_id,
        lean_job_by_id=lean_job_by_id,
        final_check_by_id=final_check_by_id,
        lemmas=lemmas_payload,
        decompositions=decompositions_payload,
        logical_decompositions=logical_decompositions_payload,
        lean_jobs=lean_jobs_payload,
        trusted_context=context_payload,
        proof_graphs=proof_graph_rows,
        proof_graph_nodes=proof_graph_node_rows,
        proof_graph_edges=proof_graph_edge_rows,
        proof_dependency_checks=proof_dependency_check_rows,
        events=event_items,
        artifact_refs=sorted(artifact_refs),
    )


@router.get("/problems/{problem_id}/request-log", response_model=DebugRequestLogResponse)
def get_problem_request_log(
    problem_id: str,
    source: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: FileStore = Depends(get_db),
) -> DebugRequestLogResponse:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")
    if source and source not in {"api_create", "api_start", "api_pause", "api_resume", "api_run", "agent1", "agent2", "agent3", "agent4", "agent5", "agent6", "lean"}:
        _raise_api_error(400, code="invalid_source_filter", message="invalid request-log source filter")

    browser = ArtifactBrowser(get_settings().artifact_store_dir)
    entries = _collect_request_log_entries(db, browser, problem_id, source_filter=source, limit=limit)
    return DebugRequestLogResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problem_id=problem_id,
        source_filter=source,
        count=len(entries),
        entries=entries,
    )


@router.get("/problems/{problem_id}/executions", response_model=DebugExecutionListResponse)
def get_problem_executions(problem_id: str, db: FileStore = Depends(get_db)) -> DebugExecutionListResponse:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")
    rows = ProblemExecutionRepository(db).list_by_problem(problem_id)
    return DebugExecutionListResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problem_id=problem_id,
        count=len(rows),
        executions=[
            DebugExecutionItem(
                execution_id=row.execution_id,
                problem_id=row.problem_id,
                continuation_generation=int(row.continuation_generation or 0),
                status=row.status,
                desired_state=row.desired_state,
                current_stage=row.current_stage,
                blocking_kind=row.blocking_kind,
                blocking_ref_id=row.blocking_ref_id,
                lease_owner=row.lease_owner,
                lease_expires_at=row.lease_expires_at,
                wake_requested_at=row.wake_requested_at,
                trigger_source=row.trigger_source,
                created_at=row.created_at,
                updated_at=row.updated_at,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ],
    )


@router.post("/problems/{problem_id}/reconcile-incomplete-runs")
def reconcile_incomplete_runs(
    problem_id: str,
    db: FileStore = Depends(get_db),
) -> dict[str, Any]:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")
    browser = ArtifactBrowser(get_settings().artifact_store_dir)
    scanned, reconciled = _reconcile_incomplete_run_requests(problem_id, browser)
    return {
        "request_id": new_id("req"),
        "server_time": _now_utc(),
        "problem_id": problem_id,
        "scanned_run_requests": scanned,
        "reconciled_run_requests": reconciled,
        "problem_running": is_problem_running(problem_id),
    }


@router.post("/problems/{problem_id}/resume-after-infrastructure-failure")
def resume_after_infrastructure_failure(
    problem_id: str,
    db: FileStore = Depends(get_db),
) -> dict[str, Any]:
    problem_repo = ProblemRepository(db)
    problem = problem_repo.get(problem_id)
    if not problem:
        _raise_api_error(404, code="problem_not_found", message="problem not found")
    if problem.status != ProblemStatus.FAILED.value:
        _raise_api_error(
            409,
            code="invalid_problem_state",
            message=f"can only resume failed problems; current status is '{problem.status}'",
        )
    if not _is_infrastructure_failure_problem(problem_id, db):
        _raise_api_error(
            409,
            code="not_infrastructure_failure",
            message="problem is failed but not due to infrastructure",
        )
    if not problem.root_theorem_id:
        _raise_api_error(409, code="invalid_problem_state", message="problem has no root theorem")

    theorem = TheoremRepository(db).get(problem.root_theorem_id)
    if theorem is None:
        _raise_api_error(409, code="invalid_problem_state", message="root theorem not found")

    resumed_lemma_id, legacy_enqueued_worker_job_id = _promote_legacy_solver_retry_state(problem_id, db)
    unresolved_infra_incidents = sum(
        1
        for row in WorkerJobRepository(db).list_by_problem(problem_id)
        if row.status == "failed"
        and row.controller_consumed_at is None
        and str((row.error_payload or {}).get("error_class") or "").strip().lower()
        in {"infrastructure", "infrastructure_transient", "timeout"}
    )
    previous_failure_report_id = problem.failure_report_artifact_id
    previous_status = problem.status
    problem.status = ProblemStatus.RUNNING.value
    problem.failure_report_artifact_id = None
    problem.infrastructure_failure_count = unresolved_infra_incidents
    apply_resume_anchor(problem, db, route="infra_resume")
    problem_repo.save(problem)

    if theorem.status == "failed":
        theorem.status = "open"
        TheoremRepository(db).save(theorem)

    EventRepository(db).append(
        problem_id,
        "problem.resumed_after_infrastructure_failure",
        previous_status,
        ProblemStatus.RUNNING.value,
        target_node_id=problem.root_theorem_id,
        reason=(
            "route=infra_resume; "
            "trigger=debug_resume_after_infrastructure_failure; "
            f"previous_failure_report={previous_failure_report_id}; "
            f"resumed_lemma_id={resumed_lemma_id}; "
            f"legacy_enqueued_worker_job_id={legacy_enqueued_worker_job_id}"
        ),
    )

    execution = ProblemExecutionService(db).start_fresh_continuation(
        problem_id,
        trigger_source="debug_resume_after_infrastructure_failure",
        supersede_reason="debug_resume_after_infrastructure_failure",
        set_problem_running=True,
    )
    start_embedded_supervisor_if_enabled()
    execution_payload = execution_summary(execution)
    return {
        "request_id": new_id("req"),
        "server_time": _now_utc(),
        "problem_id": problem_id,
        "status": problem.status,
        "previous_failure_report_id": previous_failure_report_id,
        "resumed_lemma_id": resumed_lemma_id,
        "legacy_enqueued_worker_job_id": legacy_enqueued_worker_job_id,
        "execution": execution_payload.model_dump() if execution_payload else None,
    }


@router.get("/problems/{problem_id}/llm-usage", response_model=DebugLlmUsageSummaryResponse)
def get_problem_llm_usage(
    problem_id: str,
    db: FileStore = Depends(get_db),
) -> DebugLlmUsageSummaryResponse:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")

    usage_rows = [row for row in LlmUsageRepository(db).list_by_problem(problem_id) if row.provider == "openai"]
    totals, by_stage = _build_llm_usage_summary(usage_rows)
    settings = get_settings()
    pricing_usd_per_1m = {
        "input": round(settings.openai_price_input_per_1k * 1000.0, 6),
        "cached_input": round(settings.openai_price_cached_input_per_1k * 1000.0, 6),
        "output": round(settings.openai_price_output_per_1k * 1000.0, 6),
    }

    return DebugLlmUsageSummaryResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problem_id=problem_id,
        pricing_usd_per_1m=pricing_usd_per_1m,
        totals=totals,
        by_stage=by_stage,
    )


@router.get("/problems/{problem_id}/artifacts", response_model=DebugArtifactsResponse)
def list_problem_artifacts(
    problem_id: str,
    prefix: str | None = Query(default=None),
    include_worker_jobs: bool = Query(default=True),
    limit: int = Query(default=500, ge=1, le=5000),
    db: FileStore = Depends(get_db),
) -> DebugArtifactsResponse:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")

    browser = ArtifactBrowser(get_settings().artifact_store_dir)
    prefix_to_use = prefix or f"problems/{problem_id}"

    try:
        prefix_entries = browser.list_files(prefix=prefix_to_use, limit=limit)
    except ArtifactSecurityError as exc:
        _raise_api_error(400, code="invalid_artifact_prefix", message=str(exc))

    merged: dict[str, DebugArtifactItem] = {
        row.artifact_key: DebugArtifactItem(
            artifact_key=row.artifact_key,
            size_bytes=row.size_bytes,
            modified_at=row.modified_at,
            content_type=row.content_type,
            source="prefix",
        )
        for row in prefix_entries
    }

    if include_worker_jobs:
        all_events = EventRepository(db).list_for_problem(problem_id, limit=10_000)
        worker_job_ids_set = {e.worker_job_id for e in all_events if e.worker_job_id}
        for worker_job_id in sorted(worker_job_ids_set):
            worker_key = f"worker_jobs/{worker_job_id}/result.json"
            if not browser.exists(worker_key):
                continue
            worker_rows: list[ArtifactEntry] = browser.list_files(prefix=worker_key, limit=1)
            for row in worker_rows:
                merged[row.artifact_key] = DebugArtifactItem(
                    artifact_key=row.artifact_key,
                    size_bytes=row.size_bytes,
                    modified_at=row.modified_at,
                    content_type=row.content_type,
                    source="worker_job",
                )

    artifacts = sorted(merged.values(), key=lambda item: item.artifact_key)[:limit]
    return DebugArtifactsResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        problem_id=problem_id,
        prefix=prefix_to_use,
        include_worker_jobs=include_worker_jobs,
        count=len(artifacts),
        artifacts=artifacts,
    )


@router.get("/artifacts/{artifact_key:path}", response_model=DebugArtifactContentResponse)
def get_artifact_content(
    artifact_key: str,
    db: FileStore = Depends(get_db),  # noqa: ARG001
) -> DebugArtifactContentResponse:
    browser = ArtifactBrowser(get_settings().artifact_store_dir)
    try:
        artifact = browser.read(artifact_key)
    except ArtifactSecurityError as exc:
        _raise_api_error(400, code="invalid_artifact_key", message=str(exc))
    except FileNotFoundError:
        _raise_api_error(404, code="artifact_not_found", message="artifact not found")

    return DebugArtifactContentResponse(
        request_id=new_id("req"),
        server_time=_now_utc(),
        artifact_key=artifact.artifact_key,
        format=artifact.format,
        content=artifact.content,
        size_bytes=artifact.size_bytes,
        content_type=artifact.content_type,
    )


@router.get("/problems/{problem_id}/download-logs")
def download_problem_logs(
    problem_id: str,
    db: FileStore = Depends(get_db),
) -> StreamingResponse:
    """Download a zip of all logs and artifacts for a problem run."""
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")

    settings = get_settings()
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Data dir: all JSON / JSONL files for this problem
        data_problem_dir = Path(settings.data_dir) / problem_id
        if data_problem_dir.exists():
            for file_path in sorted(data_problem_dir.rglob("*")):
                if file_path.is_file():
                    arc_name = f"data/{problem_id}/{file_path.relative_to(data_problem_dir)}"
                    zf.write(file_path, arc_name)

        # 2. Artifact store: everything under problems/{problem_id}/
        artifact_root = Path(settings.artifact_store_dir)
        artifact_problem_dir = artifact_root / "problems" / problem_id
        if artifact_problem_dir.exists():
            for file_path in sorted(artifact_problem_dir.rglob("*")):
                if file_path.is_file():
                    arc_name = f"artifacts/problems/{problem_id}/{file_path.relative_to(artifact_problem_dir)}"
                    zf.write(file_path, arc_name)

    buf.seek(0)
    filename = f"{problem_id}_logs.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/problems/{problem_id}", response_model=DebugCleanupResponse)
def delete_problem_debug(problem_id: str, db: FileStore = Depends(get_db)) -> DebugCleanupResponse:
    if not ProblemRepository(db).get(problem_id):
        _raise_api_error(404, code="problem_not_found", message="problem not found")

    # Cancel any active execution and signal in-flight agent calls to stop.
    execution = ProblemExecutionRepository(db).get_active_for_problem(problem_id)
    if execution is not None:
        ProblemExecutionService(db).request_cancel(problem_id)
    elif is_problem_running(problem_id):
        request_problem_stop(problem_id)

    # Wait for all in-flight work (including OpenAI calls) to drain.
    deadline = time.monotonic() + 30
    while is_problem_running(problem_id):
        if time.monotonic() > deadline:
            _raise_api_error(
                409,
                code="execution_stop_timeout",
                message="timed out waiting for in-flight work to stop",
            )
        time.sleep(0.05)

    cleaner = DebugDataCleaner(db, get_settings().artifact_store_dir)
    result = cleaner.delete_problem(problem_id)
    return DebugCleanupResponse(request_id=new_id("req"), server_time=_now_utc(), **result.as_dict())


@router.post("/reset-local", response_model=DebugCleanupResponse)
def reset_local_debug(db: FileStore = Depends(get_db)) -> DebugCleanupResponse:
    request_stop_all_runs()
    try:
        while has_any_running_problem():
            time.sleep(0.05)
        cleaner = DebugDataCleaner(db, get_settings().artifact_store_dir)
        result = cleaner.reset_all()
        return DebugCleanupResponse(request_id=new_id("req"), server_time=_now_utc(), **result.as_dict())
    finally:
        clear_stop_all_runs()
