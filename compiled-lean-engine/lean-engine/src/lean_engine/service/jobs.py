from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
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
    "formalize_lemma",
    "assemble_root",
}

FATAL_CLASSES = {
    "major_proof_gap",
    "false_lemma_suspected",
    "bad_statement_translation",
    "assembly_invalid",
    "assembly_composition_failure",
    "unknown_fatal",
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

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    def _run_job(self, job_id: str) -> None:
        self._store.mark_running(job_id)
        record = self._store.get(job_id)
        if record is None:
            return

        try:
            execution = self._execute_request(record.request)
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

        self._store.mark_terminal(
            job_id=job_id,
            status=execution.status,
            result=execution.result,
            error_class=execution.error_class,
            message=execution.message,
        )

        with self._lock:
            self._futures.pop(job_id, None)

    def _execute_request(self, request: dict[str, Any]) -> JobExecutionResult:
        mode = str(request["mode"])
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
        if mode == "check_assembly":
            return _execute_check_assembly(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
            )
        if mode == "formalize_lemma":
            return _execute_formalize_lemma(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
            )
        if mode == "assemble_root":
            return _execute_assemble_root(
                payload=payload,
                options=options,
                default_config_path=self._default_config_path,
                default_runtime_config=self._default_runtime_config,
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
) -> JobExecutionResult:
    source = _resolve_source(payload)
    source_name = _optional_string(payload.get("source_name"))
    runtime_config = _resolve_runtime_config(
        options=options,
        default_config_path=default_config_path,
        default_runtime_config=default_runtime_config,
    )

    normalized = normalize_problem_artifact_result(source, source_name=source_name)
    if isinstance(normalized, FatalResult):
        error = normalized.error
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

    if phase03_result.status == "ok":
        return JobExecutionResult(
            status="success",
            result={
                "mode": "check_assembly",
                "phase03": phase03_result.to_dict(),
                "error_class": None,
            },
        )

    error_class = phase03_result.error_class or "assembly_invalid"
    message = phase03_result.message or f"check_assembly failed with `{error_class}`"
    return JobExecutionResult(
        status="fatal",
        error_class=error_class,
        message=message,
        result={
            "mode": "check_assembly",
            "phase03": phase03_result.to_dict(),
            "error_class": error_class,
            "message": message,
        },
    )


def _execute_formalize_lemma(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
) -> JobExecutionResult:
    run_dir = _required_path(payload, "run_dir")
    lemma_id = _required_string(payload, "lemma_id")

    run_paths = load_run_paths(run_dir)
    runtime_config = _resolve_runtime_config(
        options=options,
        default_config_path=run_paths.runtime_config_path if run_paths.runtime_config_path.exists() else default_config_path,
        default_runtime_config=default_runtime_config,
    )

    normalized_input_path = (
        _optional_path(payload.get("normalized_input")) or run_paths.normalized_problem_path
    )
    bundle = load_normalized_bundle(normalized_input_path)

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
                "error_class": "malformed_input_artifact",
                "message": message,
            },
        )

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

    if lemma_result.status == "ok":
        return JobExecutionResult(
            status="success",
            result={
                "mode": "formalize_lemma",
                "lemma": lemma_result.to_dict(),
                "error_class": None,
            },
        )

    error_class = lemma_result.error_class or "tactic_failure"
    status = "fatal" if error_class in FATAL_CLASSES else "repairable"
    message = lemma_result.message or f"formalize_lemma failed with `{error_class}`"

    return JobExecutionResult(
        status=status,
        error_class=error_class,
        message=message,
        result={
            "mode": "formalize_lemma",
            "lemma": lemma_result.to_dict(),
            "error_class": error_class,
            "message": message,
        },
    )


def _execute_assemble_root(
    *,
    payload: dict[str, Any],
    options: dict[str, Any],
    default_config_path: Path | None,
    default_runtime_config: RuntimeConfig,
) -> JobExecutionResult:
    run_dir = _required_path(payload, "run_dir")
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
    semantic_manifest_path = (
        _optional_path(payload.get("semantic_manifest")) or (run_paths.run_root / "trusted_context_semantic_manifest.json")
    )
    phase05_summary_path = (
        _optional_path(payload.get("phase05_summary")) or (run_paths.summaries_dir / "phase05_summary.json")
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
        return JobExecutionResult(
            status="success",
            result={
                "mode": "assemble_root",
                "phase06": phase_payload,
                "final_result": final_payload,
                "error_class": None,
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
            "phase06": phase_payload,
            "final_result": final_payload,
            "error_class": error_class,
            "message": message,
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


def _validate_submit_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("job request must be a JSON object")

    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    job_id = job_id.strip()

    mode = request.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("mode must be a non-empty string")
    mode = mode.strip()

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
        "payload": payload,
        "options": options,
    }
