from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any

from ..artifact_io import create_run_paths, load_run_paths, write_json
from ..config import RuntimeConfig, load_runtime_config
from ..lemma_phase import (
    load_pinned_lemma_signatures,
    load_trusted_manifest,
    run_lemma_formalization,
    write_trusted_manifest,
)
from ..normalize import normalize_problem_artifact_result
from ..phase03 import load_normalized_bundle, run_phase03
from ..phase04 import load_mock_candidates_dir
from ..phase06 import load_mock_root_candidates_dir, run_phase06
from ..phase07 import run_phase07
from ..result_types import FatalResult
from ..workspace import create_workspace_from_template, snapshot_workspace, write_project_mcp_config
from .store import JobRecord, JobStore

SUPPORTED_SERVICE_MODES = {
    "run_full_pipeline",
    "check_assembly",
    "prepare_track",
    "formalize_lemma",
    "split_proof_into_sublemmas",
    "assemble_root",
    "assemble_root_from_track",
    "check_statement_plausibility",
    "formalize_statement_from_nl",
    "formalize_lemma_from_nl",
    "check_root_assembly",
}

FATAL_CLASSES = {
    "major_proof_gap",
    "false_lemma_suspected",
    "bad_statement_translation",
    "assembly_invalid",
    "assembly_composition_failure",
    "unknown_fatal",
}

STATEMENT_SUSPECT_CLASSES = {
    "bad_statement_translation",
    "false_lemma_suspected",
    "statement_schema_invalid",
    "malformed_input_artifact",
}

PROOF_ISSUE_CLASSES = {
    "major_proof_gap",
    "false_lemma_suspected",
    "bad_statement_translation",
    "assembly_invalid",
    "assembly_composition_failure",
}

LEAN_ISSUE_CLASSES = {
    "syntax",
    "type_mismatch",
    "missing_import",
    "missing_library_fact",
    "tactic_failure",
    "environment_mismatch",
    "service_execution_error",
}


@dataclass(frozen=True)
class JobExecutionResult:
    status: str
    result: dict[str, Any]
    error_class: str | None = None
    message: str | None = None


class ServiceJobManager:
    def __init__(
        self,
        *,
        store: JobStore,
        default_config_path: Path | None,
        default_runtime_config: RuntimeConfig,
        max_workers: int,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be > 0")
        self._store = store
        self._default_config_path = default_config_path
        self._default_runtime_config = default_runtime_config
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: dict[str, Future[None]] = {}
        self._lock = Lock()

    def submit(self, request: dict[str, Any]) -> tuple[JobRecord, bool]:
        validated = _validate_submit_request(request)
        job_id = validated["job_id"]
        mode = validated["mode"]

        record, created = self._store.create_or_get(job_id=job_id, mode=mode, request=validated)
        if created:
            with self._lock:
                self._futures[job_id] = self._executor.submit(self._run_job, job_id)

        refreshed = self._store.get(job_id)
        if refreshed is None:
            raise RuntimeError(f"job disappeared from store: {job_id}")
        return refreshed, created

    def cancel(self, job_id: str) -> JobRecord:
        with self._lock:
            future = self._futures.get(job_id)
            if future is not None and future.cancel():
                self._futures.pop(job_id, None)

        cancelled = self._store.cancel_job(job_id)
        if cancelled is None:
            raise KeyError(job_id)
        return cancelled

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    def _run_job(self, job_id: str) -> None:
        record = self._store.get(job_id)
        if record is None:
            return
        if record.status != "queued":
            return

        self._store.mark_running(job_id)
        running = self._store.get(job_id)
        if running is None or running.status != "running":
            return

        try:
            execution = self._execute_request(running.request)
        except Exception as exc:
            message = f"service execution failed: {exc}"
            execution = JobExecutionResult(
                status="fatal",
                error_class="service_execution_error",
                message=message,
                result={
                    "status": "fatal",
                    "error_class": "service_execution_error",
                    "error_scope": "service",
                    "message": message,
                    "diagnostics": [str(exc)],
                },
            )

        result_payload = _ensure_result_metadata(
            status=execution.status,
            error_class=execution.error_class,
            result=execution.result,
            request=running.request,
        )
        normalized_error_class = execution.error_class or _safe_optional_string(result_payload.get("error_class"))
        normalized_message = execution.message or _safe_optional_string(result_payload.get("error_message"))

        latest = self._store.get(job_id)
        if latest is not None and latest.status == "cancelled":
            with self._lock:
                self._futures.pop(job_id, None)
            return

        self._store.mark_terminal(
            job_id=job_id,
            status=execution.status,
            result=result_payload,
            error_class=normalized_error_class,
            message=normalized_message,
        )

        with self._lock:
            self._futures.pop(job_id, None)

    def _execute_request(self, request: dict[str, Any]) -> JobExecutionResult:
        mode = str(request["mode"])
        operation = _optional_string(request.get("operation")) or mode
        payload = request.get("payload") or {}
        options = request.get("options") or {}
        if not isinstance(payload, dict):
            raise ValueError("request.payload must be an object")
        if not isinstance(options, dict):
            raise ValueError("request.options must be an object")

        if mode == "run_full_pipeline":
            return _execute_run_full_pipeline(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
            )
        if mode == "prepare_track":
            return _execute_prepare_track(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
                operation=operation,
            )
        if mode in {"check_assembly", "formalize_statement_from_nl", "check_statement_plausibility"}:
            return _execute_check_assembly(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
                operation=operation,
            )
        if mode in {"formalize_lemma", "formalize_lemma_from_nl"}:
            return _execute_formalize_lemma(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
                operation=operation,
            )
        if mode == "split_proof_into_sublemmas":
            return _execute_split_proof_into_sublemmas(payload=payload, operation=operation)
        if mode in {"assemble_root", "check_root_assembly", "assemble_root_from_track"}:
            return _execute_assemble_root(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
                operation=operation,
            )

        raise ValueError(f"unsupported mode: {mode}")


def _execute_run_full_pipeline(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
) -> JobExecutionResult:
    source = _resolve_source(payload)
    source_name = _optional_string(payload.get("source_name"))
    runtime_config = _resolve_runtime_config(
        options=options,
        default_config_path=default_config_path,
        default_runtime_config=default_runtime_config,
    )

    statements_text = _resolve_statements_text(payload)
    mock_candidates = _resolve_mock_candidates(payload)
    mock_equivalence = _resolve_mapping_payload(payload, "mock_equivalence_verdicts")
    mock_gap_classifications = _resolve_mapping_payload(payload, "mock_gap_classifications")
    mock_root_candidates = _resolve_mock_root_candidates(payload)

    result = run_phase07(
        source=source,
        source_name=source_name,
        runtime_config=runtime_config,
        template_dir=_optional_path(options.get("template_dir")),
        max_repair_rounds=_int_option(options, "max_repair_rounds", 2),
        max_attempts_per_lemma=_int_option(options, "max_attempts_per_lemma", 5),
        parallel_lemmas=_bool_option(options, "parallel_lemmas", False),
        lemma_workers=_int_option(options, "lemma_workers", 1),
        max_semantic_repairs=_int_option(options, "max_semantic_repairs", 1),
        max_root_attempts=_int_option(options, "max_root_attempts", 4),
        timeout_seconds=_optional_timeout_option(options, "timeout_seconds"),
        workspace_timeout_seconds=_optional_timeout_option(options, "workspace_timeout_seconds"),
        model=_optional_string(options.get("model")),
        target_lemma_id=_optional_string(options.get("target_lemma_id")),
        stop_after_semantic=_bool_option(options, "stop_after_semantic", False),
        provided_statements_text=statements_text,
        mock_candidates=mock_candidates,
        mock_equivalence_verdicts=mock_equivalence,
        mock_gap_classifications=mock_gap_classifications,
        mock_root_candidates=mock_root_candidates,
    )

    phase_payload = result.to_dict()
    final_payload = _load_optional_json_file(result.final_result_path)

    if result.status == "ok":
        return JobExecutionResult(
            status="success",
            result={
                "mode": "run_full_pipeline",
                "phase07": phase_payload,
                "final_result": final_payload,
                "integration_usage": phase_payload.get("integration_usage"),
                "integration_preflight": phase_payload.get("integration_preflight"),
                "external_backend_attempts": phase_payload.get("external_backend_attempts", []),
                "error_class": None,
            },
            error_class=None,
        )

    error_class = None
    if isinstance(final_payload, dict):
        error_class = _optional_string(final_payload.get("error_class"))
    error_class = error_class or result.fatal_error_class or "unknown_fatal"
    message = _optional_string(result.message) or f"run_full_pipeline failed with `{error_class}`"

    return JobExecutionResult(
        status="fatal",
        error_class=error_class,
        message=message,
        result={
            "mode": "run_full_pipeline",
            "phase07": phase_payload,
            "final_result": final_payload,
            "integration_usage": phase_payload.get("integration_usage"),
            "integration_preflight": phase_payload.get("integration_preflight"),
            "external_backend_attempts": phase_payload.get("external_backend_attempts", []),
            "error_class": error_class,
            "message": message,
        },
    )


def _execute_check_assembly(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
    operation: str,
) -> JobExecutionResult:
    source_name = _optional_string(payload.get("source_name"))
    runtime_config = _resolve_runtime_config(
        options=options,
        default_config_path=default_config_path,
        default_runtime_config=default_runtime_config,
    )

    source = _resolve_or_build_assembly_source(payload)

    normalized = normalize_problem_artifact_result(source, source_name=source_name)
    if isinstance(normalized, FatalResult):
        error = normalized.error
        if operation == "check_statement_plausibility":
            return JobExecutionResult(
                status="success",
                result={
                    "mode": "check_statement_plausibility",
                    "operation": operation,
                    "verdict": "suspected_false",
                    "confidence": 0.9,
                    "error_class": error.error_class,
                    "error_message": error.message,
                    "diagnostics": list(error.diagnostics),
                    "recommended_next_step": "invalidate_decomposition",
                },
                error_class=None,
            )
        return JobExecutionResult(
            status="fatal",
            error_class=error.error_class,
            message=error.message,
            result=error.to_dict(),
        )

    bundle = normalized.data
    run_paths = _initialize_run_workspace(
        bundle_dict=bundle.to_dict(),
        problem_id=bundle.problem_id,
        runtime_config=runtime_config,
        template_dir=_optional_path(options.get("template_dir")),
    )

    phase03_result = run_phase03(
        run_paths=run_paths,
        runtime_config=runtime_config,
        normalized_bundle=bundle,
        max_repair_rounds=_int_option(options, "max_repair_rounds", 2),
        timeout_seconds=_int_option(options, "timeout_seconds", 180),
        model=_optional_string(options.get("model")),
        provided_statements_text=_resolve_statements_text(payload),
    )

    pinned_statement_signatures = _extract_pinned_statement_signatures(
        run_root=phase03_result.run_root,
        problem_id=bundle.problem_id,
    )

    if operation == "check_statement_plausibility":
        if phase03_result.status == "ok":
            verdict = "plausible"
            confidence = 0.9
            evidence = "statement formalization and assembly precheck succeeded"
        else:
            error_class = phase03_result.error_class or "statement_schema_invalid"
            verdict = "suspected_false" if error_class in STATEMENT_SUSPECT_CLASSES else "plausible"
            confidence = 0.8 if verdict == "suspected_false" else 0.55
            evidence = phase03_result.message or f"phase03 failed with `{error_class}`"

        return JobExecutionResult(
            status="success",
            result={
                "mode": "check_statement_plausibility",
                "operation": operation,
                "verdict": verdict,
                "evidence": evidence,
                "confidence": confidence,
                "phase03": phase03_result.to_dict(),
                "error_class": None,
                "error_scope": None,
                "error_message": None,
                "diagnostics": list(phase03_result.diagnostics),
                "recommended_next_step": (
                    "invalidate_decomposition" if verdict == "suspected_false" else "retry_solver"
                ),
            },
            error_class=None,
        )

    if phase03_result.status == "ok":
        return JobExecutionResult(
            status="success",
            result={
                "mode": "check_assembly",
                "operation": operation,
                "phase03": phase03_result.to_dict(),
                "run_dir": str(phase03_result.run_root),
                "pinned_statement_signatures": pinned_statement_signatures,
                "compiler_ok": True,
                "decl_name": None,
                "lean_code": None,
                "error_class": None,
                "error_scope": None,
                "error_message": None,
                "diagnostics": list(phase03_result.diagnostics),
                "recommended_next_step": "accept",
                "routing_confidence": 0.95,
            },
        )

    error_class = phase03_result.error_class or "assembly_invalid"
    message = phase03_result.message or f"check_assembly failed with `{error_class}`"
    status = "fatal" if error_class in FATAL_CLASSES else "repairable"
    recommended_next_step = "decompose_current" if status == "fatal" else "retry_lean_only"
    return JobExecutionResult(
        status=status,
        error_class=error_class,
        message=message,
        result={
            "mode": "check_assembly",
            "operation": operation,
            "phase03": phase03_result.to_dict(),
            "run_dir": str(phase03_result.run_root),
            "pinned_statement_signatures": pinned_statement_signatures,
            "compiler_ok": False,
            "decl_name": None,
            "lean_code": None,
            "error_class": error_class,
            "error_scope": phase03_result.error_scope,
            "error_message": message,
            "message": message,
            "diagnostics": list(phase03_result.diagnostics),
            "recommended_next_step": recommended_next_step,
            "routing_confidence": 0.9 if status == "repairable" else 0.8,
        },
    )


def _execute_prepare_track(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
    operation: str,
) -> JobExecutionResult:
    source_name = _optional_string(payload.get("source_name"))
    runtime_config = _resolve_runtime_config(
        options=options,
        default_config_path=default_config_path,
        default_runtime_config=default_runtime_config,
    )

    source = _resolve_or_build_assembly_source(payload)
    normalized = normalize_problem_artifact_result(source, source_name=source_name)
    if isinstance(normalized, FatalResult):
        error = normalized.error
        message = error.message or "prepare_track failed before phase03"
        return JobExecutionResult(
            status="fatal",
            error_class=error.error_class,
            message=message,
            result={
                "mode": "prepare_track",
                "operation": operation,
                "compiler_ok": False,
                "error_class": error.error_class,
                "error_scope": "statement",
                "error_message": message,
                "diagnostics": list(error.diagnostics),
                "recommended_next_step": "decompose_current",
            },
        )

    bundle = normalized.data
    run_paths = _initialize_run_workspace(
        bundle_dict=bundle.to_dict(),
        problem_id=bundle.problem_id,
        runtime_config=runtime_config,
        template_dir=_optional_path(options.get("template_dir")),
    )

    phase03_result = run_phase03(
        run_paths=run_paths,
        runtime_config=runtime_config,
        normalized_bundle=bundle,
        max_repair_rounds=_int_option(options, "max_repair_rounds", 2),
        timeout_seconds=_int_option(options, "timeout_seconds", 180),
        model=_optional_string(options.get("model")),
        provided_statements_text=_resolve_statements_text(payload),
    )

    track_id = (
        _optional_string(payload.get("track_id"))
        or _optional_string(payload.get("operation_id"))
        or _optional_string(payload.get("decomposition_id"))
        or f"track_{bundle.problem_id}"
    )
    lemma_ids = [lemma.lemma_id for lemma in bundle.lemmas]
    lemma_handles = {lemma_id: f"{track_id}:{lemma_id}" for lemma_id in lemma_ids}

    manifest_path = run_paths.run_root / "track_manifest.json"
    write_json(
        manifest_path,
        {
            "track_id": track_id,
            "problem_id": bundle.problem_id,
            "run_dir": str(run_paths.run_root),
            "lemma_ids": lemma_ids,
            "lemma_handles": lemma_handles,
            "created_by": operation,
        },
    )

    pinned_statement_signatures = _extract_pinned_statement_signatures(
        run_root=run_paths.run_root,
        problem_id=bundle.problem_id,
    )
    artifact_index = _build_artifact_index(
        run_paths.run_root,
        extra_paths={
            "track_manifest": manifest_path,
            "normalized_bundle": run_paths.normalized_problem_path,
            "phase03_summary": run_paths.summaries_dir / "phase03_summary.json",
        },
    )

    if phase03_result.status == "ok":
        return JobExecutionResult(
            status="success",
            result={
                "mode": "prepare_track",
                "operation": operation,
                "phase03": phase03_result.to_dict(),
                "track_id": track_id,
                "run_dir": str(run_paths.run_root),
                "lemma_ids": lemma_ids,
                "lemma_handles": lemma_handles,
                "pinned_statement_signatures": pinned_statement_signatures,
                "compiler_ok": True,
                "error_class": None,
                "error_scope": None,
                "error_message": None,
                "diagnostics": list(phase03_result.diagnostics),
                "recommended_next_step": "formalize_lemma_from_nl",
                "routing_confidence": 0.95,
                "artifact_index": artifact_index,
            },
        )

    error_class = phase03_result.error_class or "bad_statement_translation"
    message = phase03_result.message or f"prepare_track failed with `{error_class}`"
    status = "fatal" if error_class in FATAL_CLASSES else "repairable"
    return JobExecutionResult(
        status=status,
        error_class=error_class,
        message=message,
        result={
            "mode": "prepare_track",
            "operation": operation,
            "phase03": phase03_result.to_dict(),
            "track_id": track_id,
            "run_dir": str(run_paths.run_root),
            "lemma_ids": lemma_ids,
            "lemma_handles": lemma_handles,
            "pinned_statement_signatures": pinned_statement_signatures,
            "compiler_ok": False,
            "error_class": error_class,
            "error_scope": phase03_result.error_scope,
            "error_message": message,
            "diagnostics": list(phase03_result.diagnostics),
            "recommended_next_step": "decompose_current" if status == "fatal" else "retry_lean_only",
            "routing_confidence": 0.85 if status == "repairable" else 0.75,
            "artifact_index": artifact_index,
        },
    )


def _execute_formalize_lemma(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
    operation: str,
) -> JobExecutionResult:
    lemma_id = _optional_string(payload.get("lemma_id"))
    lemma_handle = _optional_string(payload.get("lemma_handle"))
    if lemma_id is None and lemma_handle:
        if ":" in lemma_handle:
            lemma_id = lemma_handle.split(":", 1)[1].strip() or None
        else:
            lemma_id = lemma_handle
    if lemma_id is None:
        raise ValueError("payload.lemma_id (or payload.lemma_handle) is required")

    proof_nl_override = _optional_string(payload.get("proof_nl"))
    run_dir_value = payload.get("run_dir")
    if run_dir_value is None:
        run_dir_value = payload.get("track_run_dir")

    if run_dir_value is not None:
        run_dir = _optional_path(run_dir_value)
        if run_dir is None:
            raise ValueError("payload.run_dir must be a non-empty string when provided")
        run_paths = load_run_paths(run_dir)
        runtime_config = _resolve_runtime_config(
            options=options,
            default_config_path=(
                run_paths.runtime_config_path if run_paths.runtime_config_path.exists() else default_config_path
            ),
            default_runtime_config=default_runtime_config,
        )
        normalized_input_path = _optional_path(payload.get("normalized_input")) or run_paths.normalized_problem_path
        bundle = load_normalized_bundle(normalized_input_path)
    else:
        runtime_config = _resolve_runtime_config(
            options=options,
            default_config_path=default_config_path,
            default_runtime_config=default_runtime_config,
        )
        source = _build_single_lemma_source(payload)
        normalized = normalize_problem_artifact_result(source, source_name=_optional_string(payload.get("source_name")))
        if isinstance(normalized, FatalResult):
            error = normalized.error
            return JobExecutionResult(
                status="fatal",
                error_class=error.error_class,
                message=error.message,
                result={
                    "mode": "formalize_lemma",
                    "operation": operation,
                    "compiler_ok": False,
                    "decl_name": None,
                    "lean_code": None,
                    "error_class": error.error_class,
                    "error_scope": "proof",
                    "error_message": error.message,
                    "diagnostics": list(error.diagnostics),
                    "recommended_next_step": "decompose_current",
                },
            )
        bundle = normalized.data
        run_paths = _initialize_run_workspace(
            bundle_dict=bundle.to_dict(),
            problem_id=bundle.problem_id,
            runtime_config=runtime_config,
            template_dir=_optional_path(options.get("template_dir")),
        )
        phase03_result = run_phase03(
            run_paths=run_paths,
            runtime_config=runtime_config,
            normalized_bundle=bundle,
            max_repair_rounds=_int_option(options, "max_repair_rounds", 2),
            timeout_seconds=_int_option(options, "timeout_seconds", 180),
            model=_optional_string(options.get("model")),
            provided_statements_text=_resolve_statements_text(payload),
        )
        if phase03_result.status != "ok":
            error_class = phase03_result.error_class or "bad_statement_translation"
            message = phase03_result.message or f"formalize_lemma setup failed with `{error_class}`"
            status = "fatal" if error_class in FATAL_CLASSES else "repairable"
            return JobExecutionResult(
                status=status,
                error_class=error_class,
                message=message,
                result={
                    "mode": "formalize_lemma",
                    "operation": operation,
                    "phase03": phase03_result.to_dict(),
                    "run_dir": str(phase03_result.run_root),
                    "compiler_ok": False,
                    "decl_name": None,
                    "lean_code": None,
                    "error_class": error_class,
                    "error_scope": phase03_result.error_scope,
                    "error_message": message,
                    "diagnostics": list(phase03_result.diagnostics),
                    "recommended_next_step": "decompose_current" if status == "fatal" else "retry_lean_only",
                    "routing_confidence": 0.85,
                },
            )

    pinned_signatures_path = (
        _optional_path(payload.get("pinned_signatures")) or (run_paths.run_root / "pinned_signatures.json")
    )
    pinned_map = load_pinned_lemma_signatures(
        pinned_signatures_path,
        expected_problem_id=bundle.problem_id,
        expected_run_name=run_paths.run_name,
    )

    lemma = bundle.lemma_map.get(lemma_id)
    pinned = pinned_map.get(lemma_id)
    if lemma is None or pinned is None:
        message = f"missing lemma or pinned signature for `{lemma_id}`"
        return JobExecutionResult(
            status="fatal",
            error_class="malformed_input_artifact",
            message=message,
            result={
                "mode": "formalize_lemma",
                "operation": operation,
                "compiler_ok": False,
                "decl_name": None,
                "lean_code": None,
                "error_class": "malformed_input_artifact",
                "error_scope": "proof",
                "error_message": message,
                "diagnostics": [],
                "recommended_next_step": "decompose_current",
                "message": message,
            },
        )

    if proof_nl_override:
        lemma = replace(lemma, proof_nl=proof_nl_override)

    trusted_manifest_path = (
        _optional_path(payload.get("trusted_manifest")) or (run_paths.run_root / "trusted_context_manifest.json")
    )
    manifest_problem_id = run_paths.problem_id
    trusted_entries = load_trusted_manifest(trusted_manifest_path, expected_problem_id=manifest_problem_id)
    if not trusted_manifest_path.exists():
        write_trusted_manifest(trusted_manifest_path, manifest_problem_id, trusted_entries)

    mock_candidates = _resolve_single_lemma_mock_candidates(payload, lemma_id)

    lemma_result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=trusted_entries,
        manifest_path=trusted_manifest_path,
        manifest_problem_id=manifest_problem_id,
        max_attempts=_int_option(options, "max_attempts_per_lemma", 5),
        timeout_seconds=_int_option(options, "timeout_seconds", 180),
        model=_optional_string(options.get("model")),
        mock_candidates=mock_candidates,
    )

    decl_name = lemma_result.decl_name
    lean_code = _read_optional_text(lemma_result.final_success_path)

    if lemma_result.status == "ok":
        return JobExecutionResult(
            status="success",
            result={
                "mode": "formalize_lemma",
                "operation": operation,
                "lemma_handle": lemma_handle,
                "lemma": lemma_result.to_dict(),
                "run_dir": str(run_paths.run_root),
                "decl_name": decl_name,
                "lean_code": lean_code,
                "compiler_ok": True,
                "error_class": None,
                "error_scope": None,
                "error_message": None,
                "diagnostics": list(lemma_result.diagnostics),
                "recommended_next_step": "accept",
                "routing_confidence": 0.95,
            },
        )

    error_class = lemma_result.error_class or "tactic_failure"
    status = "fatal" if error_class in FATAL_CLASSES else "repairable"
    message = lemma_result.message or f"formalize_lemma failed with `{error_class}`"
    recommended_next_step = "check_statement_plausibility" if error_class == "false_lemma_suspected" else (
        "decompose_current" if status == "fatal" else "retry_lean_only"
    )

    return JobExecutionResult(
        status=status,
        error_class=error_class,
        message=message,
        result={
            "mode": "formalize_lemma",
            "operation": operation,
            "lemma_handle": lemma_handle,
            "lemma": lemma_result.to_dict(),
            "run_dir": str(run_paths.run_root),
            "decl_name": decl_name,
            "lean_code": lean_code,
            "compiler_ok": False,
            "error_class": error_class,
            "error_scope": "proof",
            "error_message": message,
            "diagnostics": list(lemma_result.diagnostics),
            "recommended_next_step": recommended_next_step,
            "routing_confidence": 0.9 if status == "repairable" else 0.8,
            "message": message,
        },
    )


def _execute_assemble_root(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
    operation: str,
) -> JobExecutionResult:
    run_dir = _optional_path(payload.get("run_dir")) or _optional_path(payload.get("track_run_dir"))
    if run_dir is None:
        raise ValueError("payload.run_dir is required")
    run_paths = load_run_paths(run_dir)

    runtime_config = _resolve_runtime_config(
        options=options,
        default_config_path=run_paths.runtime_config_path if run_paths.runtime_config_path.exists() else default_config_path,
        default_runtime_config=default_runtime_config,
    )

    normalized_input_path = _optional_path(payload.get("normalized_input")) or run_paths.normalized_problem_path
    bundle = load_normalized_bundle(normalized_input_path)

    pinned_signatures_path = (
        _optional_path(payload.get("pinned_signatures")) or (run_paths.run_root / "pinned_signatures.json")
    )
    semantic_manifest_path = _optional_path(payload.get("semantic_manifest")) or (
        run_paths.run_root / "trusted_context_semantic_manifest.json"
    )
    phase05_summary_path = _optional_path(payload.get("phase05_summary")) or (run_paths.summaries_dir / "phase05_summary.json")

    infer_dependencies = operation in {"check_root_assembly", "assemble_root_from_track"}
    if "infer_dependencies" in payload:
        raw_infer = payload.get("infer_dependencies")
        if not isinstance(raw_infer, bool):
            raise ValueError("payload.infer_dependencies must be a boolean when provided")
        infer_dependencies = raw_infer

    if infer_dependencies:
        _materialize_phase06_dependencies(
            run_paths=run_paths,
            bundle=bundle,
            payload=payload,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
        )

    phase06_result = run_phase06(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_signatures_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        max_root_attempts=_int_option(options, "max_root_attempts", 4),
        timeout_seconds=_int_option(options, "timeout_seconds", 180),
        model=_optional_string(options.get("model")),
        mock_root_candidates=_resolve_mock_root_candidates(payload),
    )

    phase_payload = phase06_result.to_dict()
    final_payload = _load_optional_json_file(phase06_result.final_result_path)

    if phase06_result.status == "ok":
        lean_code = _read_optional_text(phase06_result.root_file_path)
        return JobExecutionResult(
            status="success",
            result={
                "mode": "assemble_root",
                "operation": operation,
                "phase06": phase_payload,
                "final_result": final_payload,
                "run_dir": str(run_paths.run_root),
                "decl_name": phase06_result.root_decl_name,
                "lean_code": lean_code,
                "compiler_ok": True,
                "error_class": None,
                "error_scope": None,
                "error_message": None,
                "diagnostics": [],
                "recommended_next_step": "accept",
                "routing_confidence": 0.95,
            },
        )

    error_class = None
    if isinstance(final_payload, dict):
        error_class = _optional_string(final_payload.get("error_class"))
    error_class = error_class or phase06_result.error_class or "unknown_fatal"
    message = phase06_result.message or f"assemble_root failed with `{error_class}`"

    return JobExecutionResult(
        status="fatal",
        error_class=error_class,
        message=message,
        result={
            "mode": "assemble_root",
            "operation": operation,
            "phase06": phase_payload,
            "final_result": final_payload,
            "run_dir": str(run_paths.run_root),
            "decl_name": phase06_result.root_decl_name,
            "lean_code": None,
            "compiler_ok": False,
            "error_class": error_class,
            "error_scope": "assembly",
            "error_message": message,
            "diagnostics": [],
            "recommended_next_step": "decompose_current",
            "routing_confidence": 0.8,
            "message": message,
        },
    )


def _execute_split_proof_into_sublemmas(*, payload: dict[str, Any], operation: str) -> JobExecutionResult:
    proof_nl = _optional_string(payload.get("proof_nl")) or ""
    statement_nl = _optional_string(payload.get("statement_nl")) or ""

    chunks = [part.strip() for part in proof_nl.replace("\n", " ").split(".") if part.strip()]
    if not chunks and statement_nl:
        chunks = [statement_nl]

    sublemmas: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks[:4], start=1):
        label = chunk if len(chunk) >= 20 else f"Intermediate step {idx}: {chunk}" if chunk else f"Intermediate step {idx}"
        sublemmas.append(
            {
                "local_id": f"split_{idx}",
                "statement_nl": label,
                "proof_hint": chunk,
                "source": "auto_split",
            }
        )

    if not sublemmas:
        sublemmas = [
            {
                "local_id": "split_1",
                "statement_nl": "Derive a reusable intermediate claim from the current proof sketch.",
                "proof_hint": "auto-generated placeholder",
                "source": "auto_split",
            },
            {
                "local_id": "split_2",
                "statement_nl": "Use the intermediate claim to reduce the final goal.",
                "proof_hint": "auto-generated placeholder",
                "source": "auto_split",
            },
        ]

    return JobExecutionResult(
        status="success",
        result={
            "mode": "split_proof_into_sublemmas",
            "operation": operation,
            "input_statement_nl": statement_nl,
            "sublemmas": sublemmas,
            "split_count": len(sublemmas),
            "recommended_next_step": "retry_solver",
            "routing_confidence": 0.7,
        },
    )


def _resolve_runtime_config(
    *,
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
) -> RuntimeConfig:
    if not options:
        return default_runtime_config

    config_path = _optional_path(options.get("config")) or default_config_path
    model = _optional_string(options.get("model"))
    fallback_model = _optional_string(options.get("fallback_model"))
    artifact_root = _optional_path(options.get("artifact_root"))
    mcp_command = _optional_string(options.get("mcp_command"))
    repo_paths = options.get("repo_paths")
    if repo_paths is not None and not isinstance(repo_paths, dict):
        raise ValueError("options.repo_paths must be an object when provided")
    repo_paths = repo_paths or {}

    return load_runtime_config(
        config_path,
        model=model,
        fallback_model=fallback_model,
        artifact_root=artifact_root,
        mcp_command=mcp_command,
        repo_lean_lsp_mcp_root=_optional_path(repo_paths.get("lean_lsp_mcp")) if repo_paths else None,
        lean4_skills_root=_optional_path(repo_paths.get("lean4_skills")) if repo_paths else None,
    )


def _initialize_run_workspace(
    *,
    bundle_dict: dict[str, Any],
    problem_id: str,
    runtime_config: RuntimeConfig,
    template_dir: Path | None,
):
    run_paths = create_run_paths(problem_id, artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(
        run_paths.workspace_dir,
        template_dir=template_dir,
        runtime_config=runtime_config,
    )
    if runtime_config.mcp.enabled:
        write_project_mcp_config(
            run_paths.workspace_dir,
            runtime_config,
            mcp_log_dir=run_paths.run_root / ".mcp_logs",
        )
    write_json(run_paths.runtime_config_path, runtime_config.to_dict())
    write_json(run_paths.workspace_snapshot_path, snapshot_workspace(run_paths.workspace_dir))
    write_json(run_paths.normalized_problem_path, bundle_dict)
    return run_paths


def _resolve_or_build_assembly_source(payload: dict[str, Any]):
    if "source" in payload or "input" in payload:
        return _resolve_source(payload)
    return _build_decomposition_source(payload)


def _build_decomposition_source(payload: dict[str, Any]) -> dict[str, Any]:
    theorem_nl = _required_string(payload, "theorem_nl")
    theorem_semantic_sketch = payload.get("theorem_semantic_sketch") or {}
    if not isinstance(theorem_semantic_sketch, dict):
        raise ValueError("payload.theorem_semantic_sketch must be an object when provided")

    lemmas_raw = payload.get("lemmas")
    if not isinstance(lemmas_raw, list) or not lemmas_raw:
        raise ValueError("payload.lemmas must be a non-empty array")

    normalized_lemmas: list[dict[str, Any]] = []
    lemma_ids: list[str] = []
    for index, item in enumerate(lemmas_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("payload.lemmas entries must be objects")
        lemma_id = _optional_string(item.get("local_id")) or _optional_string(item.get("lemma_id")) or f"lem_{index}"
        statement_nl = _optional_string(item.get("statement_nl")) or theorem_nl
        semantic_sketch = item.get("semantic_sketch") or {}
        if not isinstance(semantic_sketch, dict):
            raise ValueError(f"payload.lemmas[{index - 1}].semantic_sketch must be an object")
        proof_nl = _optional_string(item.get("proof_nl")) or "Proof sketch unavailable."
        lemma_ids.append(lemma_id)
        normalized_lemmas.append(
            {
                "lemma_id": lemma_id,
                "statement_nl": statement_nl,
                "semantic_sketch": semantic_sketch,
                "proof_nl": proof_nl,
                "proof_status": "nl_accepted",
                "routing_status": "done",
            }
        )

    assembly_plan = payload.get("assembly_plan") or {}
    if not isinstance(assembly_plan, dict):
        raise ValueError("payload.assembly_plan must be an object when provided")
    plan_steps = assembly_plan.get("steps")
    if plan_steps is None:
        plan_steps = [
            {
                "step_id": "S1",
                "uses_lemmas": list(lemma_ids),
                "uses_prior_steps": [],
                "derives": "Root follows from accepted lemmas.",
                "is_trivial": True,
                "trivial_justification": "Single-step synthetic assembly plan.",
            }
        ]
    if not isinstance(plan_steps, list):
        raise ValueError("payload.assembly_plan.steps must be an array when provided")

    problem_id = _optional_string(payload.get("problem_id")) or "service_problem"
    decomposition_id = _optional_string(payload.get("decomposition_id")) or "dec_service"
    theorem_id = _optional_string(payload.get("theorem_id")) or "thm_service_root"

    return {
        "problem_id": problem_id,
        "title": _optional_string(payload.get("title")) or "Service Assembly Check",
        "verification_level": "formal",
        "root_theorem": {
            "theorem_id": theorem_id,
            "statement_nl": theorem_nl,
            "semantic_sketch": theorem_semantic_sketch,
        },
        "selected_decomposition": {
            "decomposition_id": decomposition_id,
            "assembly_plan": {
                "assembly_plan_id": _optional_string(assembly_plan.get("assembly_plan_id")) or "asm_service",
                "steps": plan_steps,
                "proof_skeleton_nl": _optional_string(assembly_plan.get("proof_skeleton_nl")) or "",
                "is_trivially_composable": bool(assembly_plan.get("is_trivially_composable", True)),
            },
        },
        "lemmas": normalized_lemmas,
        "all_visible_lemmas_nl_accepted": True,
    }


def _build_single_lemma_source(payload: dict[str, Any]) -> dict[str, Any]:
    lemma_id = _required_string(payload, "lemma_id")
    statement_nl = _required_string(payload, "statement_nl")
    semantic_sketch = payload.get("semantic_sketch") or {}
    if not isinstance(semantic_sketch, dict):
        raise ValueError("payload.semantic_sketch must be an object when provided")
    proof_nl = _optional_string(payload.get("proof_nl")) or "Proof sketch unavailable."
    theorem_nl = _optional_string(payload.get("theorem_nl")) or statement_nl

    return {
        "problem_id": _optional_string(payload.get("problem_id")) or f"service_{lemma_id}",
        "title": _optional_string(payload.get("title")) or f"Service formalization for {lemma_id}",
        "verification_level": "formal",
        "root_theorem": {
            "theorem_id": _optional_string(payload.get("theorem_id")) or "thm_service_root",
            "statement_nl": theorem_nl,
            "semantic_sketch": payload.get("theorem_semantic_sketch")
            if isinstance(payload.get("theorem_semantic_sketch"), dict)
            else semantic_sketch,
        },
        "selected_decomposition": {
            "decomposition_id": _optional_string(payload.get("decomposition_id")) or "dec_service",
            "assembly_plan": {
                "assembly_plan_id": _optional_string(payload.get("assembly_plan_id")) or "asm_service",
                "steps": [
                    {
                        "step_id": "S1",
                        "uses_lemmas": [lemma_id],
                        "uses_prior_steps": [],
                        "derives": "Root follows from the target lemma.",
                        "is_trivial": True,
                        "trivial_justification": "Single-lemma synthetic assembly.",
                    }
                ],
                "proof_skeleton_nl": proof_nl,
                "is_trivially_composable": True,
            },
        },
        "lemmas": [
            {
                "lemma_id": lemma_id,
                "statement_nl": statement_nl,
                "semantic_sketch": semantic_sketch,
                "proof_nl": proof_nl,
                "proof_status": "nl_accepted",
                "routing_status": "done",
            }
        ],
        "all_visible_lemmas_nl_accepted": True,
    }


def _extract_pinned_statement_signatures(*, run_root: Path, problem_id: str) -> dict[str, str]:
    try:
        run_paths = load_run_paths(run_root)
    except Exception:
        return {}

    pinned_path = run_paths.run_root / "pinned_signatures.json"
    if not pinned_path.exists():
        return {}

    try:
        pinned_map = load_pinned_lemma_signatures(
            pinned_path,
            expected_problem_id=problem_id,
            expected_run_name=run_paths.run_name,
        )
    except Exception:
        return {}
    return {lemma_id: pinned.signature for lemma_id, pinned in pinned_map.items()}


def _materialize_phase06_dependencies(
    *,
    run_paths,
    bundle,
    payload: dict[str, Any],
    semantic_manifest_path: Path,
    phase05_summary_path: Path,
) -> None:
    trusted_manifest_path = _optional_path(payload.get("trusted_manifest")) or (
        run_paths.run_root / "trusted_context_manifest.json"
    )

    if not semantic_manifest_path.exists():
        trusted_entries = load_trusted_manifest(
            trusted_manifest_path,
            expected_problem_id=run_paths.problem_id,
        )
        write_trusted_manifest(semantic_manifest_path, run_paths.problem_id, trusted_entries)

    if not phase05_summary_path.exists():
        semantic_entries = load_trusted_manifest(
            semantic_manifest_path,
            expected_problem_id=run_paths.problem_id,
        )
        accepted_ids = {entry.lemma_id for entry in semantic_entries}
        expected_ids = {lemma.lemma_id for lemma in bundle.lemmas}
        status = "ok" if expected_ids.issubset(accepted_ids) else "fatal"
        write_json(
            phase05_summary_path,
            {
                "problem_id": bundle.problem_id,
                "status": status,
                "accepted_lemma_ids": sorted(accepted_ids),
                "expected_lemma_ids": sorted(expected_ids),
                "entry_count": len(semantic_entries),
            },
        )


def _read_optional_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_source(payload: dict[str, Any]):
    if "source" in payload:
        source = payload["source"]
    elif "input" in payload:
        source = payload["input"]
    else:
        raise ValueError("payload.source is required")

    source_kind = str(payload.get("source_kind") or payload.get("input_kind") or "auto").strip().lower()
    if source_kind not in {"auto", "path", "text", "json"}:
        raise ValueError("source_kind must be one of: auto, path, text, json")

    if source_kind == "path":
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source_kind=path requires a non-empty string source")
        return Path(source).expanduser().resolve()

    if source_kind == "text":
        if not isinstance(source, str):
            raise ValueError("source_kind=text requires a string source")
        return source

    if source_kind == "json":
        if isinstance(source, dict):
            return source
        if isinstance(source, str):
            parsed = json.loads(source)
            if not isinstance(parsed, dict):
                raise ValueError("source_kind=json requires a top-level JSON object")
            return parsed
        raise ValueError("source_kind=json requires a JSON object or JSON string")

    if isinstance(source, dict):
        return source
    if not isinstance(source, str):
        raise ValueError("source must be a string or object")

    maybe_path = Path(source).expanduser()
    if maybe_path.exists() and maybe_path.is_file():
        return maybe_path.resolve()
    return source


def _resolve_statements_text(payload: dict[str, Any]) -> str | None:
    if "statements_text" in payload and payload["statements_text"] is not None:
        text = payload["statements_text"]
        if not isinstance(text, str):
            raise ValueError("payload.statements_text must be a string when provided")
        return text

    statements_file = _optional_path(payload.get("statements_file"))
    if statements_file is None:
        return None
    if not statements_file.exists() or not statements_file.is_file():
        raise ValueError(f"statements_file does not exist: {statements_file}")
    return statements_file.read_text(encoding="utf-8")


def _resolve_mock_candidates(payload: dict[str, Any]) -> dict[str, list[str]] | None:
    direct = payload.get("mock_candidates")
    if direct is not None:
        if not isinstance(direct, dict):
            raise ValueError("payload.mock_candidates must be an object")
        result: dict[str, list[str]] = {}
        for lemma_id, candidates in direct.items():
            if not isinstance(lemma_id, str) or not lemma_id.strip():
                raise ValueError("mock_candidates keys must be non-empty lemma_id strings")
            if not isinstance(candidates, list) or not all(isinstance(item, str) for item in candidates):
                raise ValueError(f"mock_candidates[{lemma_id}] must be a list of strings")
            result[lemma_id] = list(candidates)
        return result

    directory = _optional_path(payload.get("mock_candidates_dir"))
    if directory is None:
        return None
    return load_mock_candidates_dir(directory)


def _resolve_single_lemma_mock_candidates(payload: dict[str, Any], lemma_id: str) -> list[str] | None:
    direct = payload.get("mock_candidates")
    if isinstance(direct, list):
        if not all(isinstance(item, str) for item in direct):
            raise ValueError("payload.mock_candidates must be a list of strings")
        return list(direct)

    if isinstance(direct, dict):
        lemma_candidates = direct.get(lemma_id)
        if lemma_candidates is None:
            return None
        if not isinstance(lemma_candidates, list) or not all(isinstance(item, str) for item in lemma_candidates):
            raise ValueError(f"mock_candidates[{lemma_id}] must be a list of strings")
        return list(lemma_candidates)

    directory = _optional_path(payload.get("mock_candidates_dir"))
    if directory is None:
        return None

    by_lemma = load_mock_candidates_dir(directory)
    return by_lemma.get(lemma_id)


def _resolve_mock_root_candidates(payload: dict[str, Any]) -> list[str] | None:
    direct = payload.get("mock_root_candidates")
    if direct is not None:
        if not isinstance(direct, list) or not all(isinstance(item, str) for item in direct):
            raise ValueError("payload.mock_root_candidates must be a list of strings")
        return list(direct)

    directory = _optional_path(payload.get("mock_root_candidates_dir"))
    if directory is None:
        return None
    return load_mock_root_candidates_dir(directory)


def _resolve_mapping_payload(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    direct = payload.get(key)
    if isinstance(direct, dict):
        return direct

    if isinstance(direct, str) and direct.strip():
        return _load_json_mapping(Path(direct).expanduser().resolve())

    if direct is not None:
        raise ValueError(f"payload.{key} must be an object or JSON mapping file path")

    file_key = f"{key}_file"
    file_path = _optional_path(payload.get(file_key))
    if file_path is not None:
        return _load_json_mapping(file_path)

    legacy_path = _optional_path(payload.get(key))
    if legacy_path is not None and legacy_path.exists() and legacy_path.is_file():
        return _load_json_mapping(legacy_path)

    return None


def _load_json_mapping(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"json mapping file does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"json mapping file is not valid JSON: {path} ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"json mapping file must contain an object: {path}")
    return payload


def _required_path(payload: dict[str, Any], key: str) -> Path:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"payload.{key} must be a non-empty string path")
    return Path(raw).expanduser().resolve()


def _optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path values must be non-empty strings")
    return Path(value).expanduser().resolve()


def _required_string(payload: dict[str, Any], key: str) -> str:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"payload.{key} must be a non-empty string")
    return raw.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("string option values must be strings")
    cleaned = value.strip()
    return cleaned or None


def _int_option(options: dict[str, Any], key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"options.{key} must be an integer")
    return value


def _optional_timeout_option(options: dict[str, Any], key: str) -> int | None:
    if key not in options or options[key] is None:
        return None
    value = options[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"options.{key} must be an integer when provided")
    return value


def _bool_option(options: dict[str, Any], key: str, default: bool) -> bool:
    value = options.get(key, default)
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ValueError(f"options.{key} must be a boolean")
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"options.{key} must be a boolean")


def _load_optional_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return {"value": payload}


def _safe_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except Exception:
            return default
    return default


def _safe_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _issue_kind_for_error_class(error_class: str | None) -> str | None:
    if not error_class:
        return None
    if error_class in PROOF_ISSUE_CLASSES:
        return "proof_issue"
    if error_class in LEAN_ISSUE_CLASSES:
        return "lean_issue"
    return "lean_issue"


def _classification_fields(*, status: str, error_class: str | None) -> dict[str, Any]:
    if status == "success":
        return {
            "issue_kind": None,
            "confidence": 1.0,
            "fatality": "none",
        }

    issue_kind = _issue_kind_for_error_class(error_class)
    confidence = 0.9 if error_class else 0.6
    fatality = "fatal" if status == "fatal" else "repairable" if status == "repairable" else "none"
    return {
        "issue_kind": issue_kind,
        "confidence": confidence,
        "fatality": fatality,
    }


def _build_artifact_index(run_root: Path | None, *, extra_paths: dict[str, Path] | None = None) -> dict[str, Any]:
    if run_root is None:
        return {}
    try:
        resolved_root = run_root.expanduser().resolve()
    except Exception:
        return {}
    if not resolved_root.exists():
        return {}

    index: dict[str, Any] = {
        "run_root": str(resolved_root),
    }
    for key, relative in {
        "workspace": Path("workspace"),
        "summaries": Path("summaries"),
        "normalized_problem": Path("normalized_problem.json"),
        "pinned_signatures": Path("pinned_signatures.json"),
        "trusted_context_manifest": Path("trusted_context_manifest.json"),
        "track_manifest": Path("track_manifest.json"),
    }.items():
        candidate = resolved_root / relative
        if candidate.exists():
            index[key] = str(candidate)

    if extra_paths:
        for key, path in extra_paths.items():
            try:
                candidate = path.expanduser().resolve()
            except Exception:
                continue
            if candidate.exists():
                index[key] = str(candidate)

    return index


def _ensure_result_metadata(
    *,
    status: str,
    error_class: str | None,
    result: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    request_payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}

    mode = _safe_optional_string(payload.get("mode")) or _safe_optional_string(request.get("mode")) or "unknown"
    operation = _safe_optional_string(payload.get("operation")) or _safe_optional_string(request.get("operation")) or mode

    attempt_index = _safe_int(payload.get("attempt_index"), _safe_int(request_payload.get("attempt_index"), 1))
    round_index = _safe_int(payload.get("round_index"), _safe_int(request_payload.get("round_index"), 1))
    last_error = (
        _safe_optional_string(payload.get("error_message"))
        or _safe_optional_string(payload.get("message"))
        or _safe_optional_string(request_payload.get("last_error"))
    )

    enriched = dict(payload)
    enriched.setdefault("mode", mode)
    enriched.setdefault("operation", operation)
    enriched.setdefault(
        "progress_snapshot",
        {
            "phase": operation,
            "round": round_index,
            "attempt": attempt_index,
            "last_error": last_error,
        },
    )

    classification = _classification_fields(status=status, error_class=error_class or _safe_optional_string(enriched.get("error_class")))
    for key, value in classification.items():
        enriched.setdefault(key, value)

    resolved_error_class = error_class or _safe_optional_string(enriched.get("error_class"))
    if resolved_error_class:
        enriched.setdefault("error_class", resolved_error_class)

    run_dir = _safe_optional_string(enriched.get("run_dir"))
    if run_dir and "artifact_index" not in enriched:
        enriched["artifact_index"] = _build_artifact_index(Path(run_dir))

    return enriched


def _validate_submit_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("job request must be a JSON object")

    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    job_id = job_id.strip()

    mode_raw = request.get("mode")
    operation_raw = request.get("operation")

    mode = _optional_string(mode_raw)
    operation = _optional_string(operation_raw)

    if mode is None and operation is None:
        raise ValueError("either mode or operation must be provided")
    if mode is None:
        mode = operation
    if operation is None:
        operation = mode
    if mode is None or operation is None:
        raise ValueError("mode/operation resolution failed")

    if mode != operation:
        if mode not in SUPPORTED_SERVICE_MODES:
            mode = operation
        elif operation not in SUPPORTED_SERVICE_MODES:
            operation = mode
        else:
            raise ValueError("mode and operation must match when both are provided")

    if mode not in SUPPORTED_SERVICE_MODES:
        allowed = ", ".join(sorted(SUPPORTED_SERVICE_MODES))
        raise ValueError(f"unsupported mode `{mode}` (allowed: {allowed})")

    payload = request.get("payload", {})
    options = request.get("options", {})

    if payload is None:
        payload = {}
    if options is None:
        options = {}

    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if not isinstance(options, dict):
        raise ValueError("options must be an object")

    return {
        "job_id": job_id,
        "mode": mode,
        "operation": operation,
        "payload": payload,
        "options": options,
    }
