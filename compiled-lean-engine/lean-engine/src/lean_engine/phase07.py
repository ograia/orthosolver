from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .artifact_io import RunPaths, create_run_paths, load_run_paths, write_json, write_text
from .config import RuntimeConfig
from .integrations import lean_lsp_mcp
from .integrations.types import IntegrationUsageTracker
from .lean_checks import check_lean_file, prepare_lean_workspace
from .normalize import normalize_problem_artifact, normalize_problem_artifact_result
from .phase03 import Phase03RunResult, run_phase03
from .phase04 import Phase04RunResult, run_phase04
from .phase06 import Phase06RunResult, run_phase06
from .result_types import FatalResult
from .semantic_phase import Phase05RunResult, run_phase05
from .timeout_policy import resolve_timeout_policy, runtime_config_for_phase
from .workspace import (
    create_workspace_from_template,
    resolve_cache_lake_dir,
    snapshot_workspace,
    update_lake_cache,
    write_project_mcp_config,
)

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]

# ---------------------------------------------------------------------------
# Pause support: send SIGUSR1 to gracefully stop after the current phase.
# The run writes a summary and exits so --resume can continue later.
# ---------------------------------------------------------------------------
_pause_requested = False


def _pause_signal_handler(signum: int, frame: Any) -> None:
    global _pause_requested
    _pause_requested = True
    print(
        "\n[lean_engine] PAUSE requested (SIGUSR1). "
        "Will stop after the current phase completes. Resume with --resume.",
        file=sys.stderr, flush=True,
    )


def _install_pause_handler() -> None:
    """Install SIGUSR1 handler for pause support."""
    global _pause_requested
    _pause_requested = False
    try:
        signal.signal(signal.SIGUSR1, _pause_signal_handler)
    except (OSError, ValueError):
        pass  # Not available on this platform or not main thread


def _check_pause(
    *,
    run_paths: RunPaths,
    bundle: Any,
    phase_elapsed: dict[str, float],
    phase_statuses: dict[str, str],
    started: float,
    effective_model: str,
    expected_lemma_count: int,
    compiled_lemma_count: int = 0,
    integration_usage: dict[str, Any] | None = None,
    integration_preflight: dict[str, Any] | None = None,
    external_backend_attempts: tuple[dict[str, Any], ...] = (),
) -> Phase07RunResult | None:
    """If pause was requested, write summary and return a result to exit."""
    if not _pause_requested:
        return None

    print("[lean_engine] Pausing run. Use --resume to continue.", file=sys.stderr, flush=True)

    result = Phase07RunResult(
        status="paused",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        run_name=run_paths.run_name,
        phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
        normalized_problem_path=run_paths.normalized_problem_path,
        final_result_path=None,
        final_summary_path=None,
        model=effective_model,
        lemma_count=expected_lemma_count,
        compiled_lemma_count=compiled_lemma_count,
        semantic_rejection_count=0,
        fatal_error_class=None,
        elapsed_seconds=time.monotonic() - started,
        phase_elapsed_seconds=phase_elapsed,
        phase_statuses=phase_statuses,
        integration_usage=integration_usage,
        integration_preflight=integration_preflight,
        external_backend_attempts=external_backend_attempts,
        message="Run paused by user (SIGUSR1). Resume with --resume.",
    )
    _write_phase07_summary(result)
    return result


@dataclass(frozen=True)
class Phase07RunResult:
    status: str
    problem_id: str | None
    run_root: Path | None
    run_name: str | None
    phase_summary_path: Path | None
    normalized_problem_path: Path | None
    final_result_path: Path | None
    final_summary_path: Path | None
    model: str
    lemma_count: int
    compiled_lemma_count: int
    semantic_rejection_count: int
    fatal_error_class: str | None
    elapsed_seconds: float
    phase_elapsed_seconds: dict[str, float]
    phase_statuses: dict[str, str]
    integration_usage: dict[str, Any] | None = None
    integration_preflight: dict[str, Any] | None = None
    external_backend_attempts: tuple[dict[str, Any], ...] = ()
    message: str | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problem_id": self.problem_id,
            "run_root": str(self.run_root) if self.run_root else None,
            "run_name": self.run_name,
            "phase_summary_path": str(self.phase_summary_path) if self.phase_summary_path else None,
            "normalized_problem_path": str(self.normalized_problem_path) if self.normalized_problem_path else None,
            "final_result_path": str(self.final_result_path) if self.final_result_path else None,
            "final_summary_path": str(self.final_summary_path) if self.final_summary_path else None,
            "model": self.model,
            "lemma_count": self.lemma_count,
            "compiled_lemma_count": self.compiled_lemma_count,
            "semantic_rejection_count": self.semantic_rejection_count,
            "fatal_error_class": self.fatal_error_class,
            "elapsed_seconds": self.elapsed_seconds,
            "phase_elapsed_seconds": dict(self.phase_elapsed_seconds),
            "phase_statuses": dict(self.phase_statuses),
            "integration_usage": self.integration_usage,
            "integration_preflight": self.integration_preflight,
            "external_backend_attempts": list(self.external_backend_attempts),
            "message": self.message,
            "diagnostics": list(self.diagnostics),
        }


def _deduplicate_lemmas_file(lemmas_path: Path) -> None:
    """Remove duplicate theorem/lemma/def declarations from Lemmas.lean, keeping first."""
    import re as _re
    if not lemmas_path.exists():
        return
    text = lemmas_path.read_text(encoding="utf-8")
    decl_re = _re.compile(
        r"(?m)^(\s*(?:noncomputable\s+)?(?:private\s+|protected\s+)?"
        r"(?:theorem|lemma|def|abbrev|axiom)\s+)([A-Za-z0-9_'.]+)"
    )
    seen_names: set[str] = set()
    result_lines: list[str] = []
    skip_until_next_decl = False
    for line in text.splitlines():
        m = decl_re.match(line)
        if m:
            name = m.group(2)
            if name in seen_names:
                skip_until_next_decl = True
                continue
            seen_names.add(name)
            skip_until_next_decl = False
        elif skip_until_next_decl:
            continue
        result_lines.append(line)
    deduplicated = "\n".join(result_lines)
    if deduplicated != text:
        lemmas_path.write_text(deduplicated, encoding="utf-8")


def _kill_stale_processes(problem_id: str, artifacts_root: Path) -> None:
    """Kill stale lake/lean processes from previous runs of this problem."""
    problem_dir = str(Path(artifacts_root).expanduser().resolve() / problem_id)
    killed = 0
    try:
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            if pid == os.getpid():
                continue
            try:
                cmdline_path = Path("/proc") / pid_str / "cmdline"
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
                # Only kill lake/lean processes working in this problem's artifact dir
                if problem_dir in cmdline and any(prog in cmdline for prog in ("lake\x00", "lean\x00")):
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
            except (OSError, PermissionError):
                continue
    except OSError:
        pass
    if killed:
        print(f"[lean_engine] killed {killed} stale lake/lean process(es) from previous runs", file=sys.stderr, flush=True)


def run_phase07(
    *,
    source: Path | str | dict[str, Any],
    source_name: str | None,
    runtime_config: RuntimeConfig,
    template_dir: Path | None,
    max_repair_rounds: int,
    max_attempts_per_lemma: int,
    parallel_lemmas: bool,
    lemma_workers: int,
    max_semantic_repairs: int,
    max_root_attempts: int,
    timeout_seconds: int | None,
    workspace_timeout_seconds: int | None,
    model: str | None,
    target_lemma_id: str | None,
    stop_after_semantic: bool,
    provided_statements_text: str | None,
    mock_candidates: dict[str, list[str]] | None,
    mock_equivalence_verdicts: dict[str, Any] | None,
    mock_gap_classifications: dict[str, Any] | None,
    mock_root_candidates: list[str] | None,
    timeout_policy_path: Path | None = None,
    runner: SubprocessRunner = subprocess.run,
    require_cache: bool = False,
) -> Phase07RunResult:
    _install_pause_handler()
    started = time.monotonic()
    phase_elapsed: dict[str, float] = {}
    phase_statuses: dict[str, str] = {}
    integration_usage = IntegrationUsageTracker(profile="standard")
    integration_preflight: dict[str, Any] | None = None
    external_backend_attempts: list[dict[str, Any]] = []
    effective_model = model or runtime_config.claude.model

    normalize_started = time.monotonic()
    normalized_result = normalize_problem_artifact_result(source, source_name=source_name)
    phase_elapsed["phase01_normalize"] = time.monotonic() - normalize_started
    if isinstance(normalized_result, FatalResult):
        error = normalized_result.error
        return Phase07RunResult(
            status="fatal",
            problem_id=None,
            run_root=None,
            run_name=None,
            phase_summary_path=None,
            normalized_problem_path=None,
            final_result_path=None,
            final_summary_path=None,
            model=effective_model,
            lemma_count=0,
            compiled_lemma_count=0,
            semantic_rejection_count=0,
            fatal_error_class=error.error_class,
            elapsed_seconds=time.monotonic() - started,
            phase_elapsed_seconds=phase_elapsed,
            phase_statuses={"phase01_normalize": "fatal"},
            message=error.message,
            diagnostics=tuple(error.diagnostics),
        )
    bundle = normalized_result.data
    phase_statuses["phase01_normalize"] = "ok"
    if target_lemma_id is not None and target_lemma_id.strip() not in bundle.lemma_map:
        diagnostics = (f"target_lemma_id not found in normalized bundle: {target_lemma_id.strip()}",)
        return Phase07RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=None,
            run_name=None,
            phase_summary_path=None,
            normalized_problem_path=None,
            final_result_path=None,
            final_summary_path=None,
            model=effective_model,
            lemma_count=len(bundle.lemmas),
            compiled_lemma_count=0,
            semantic_rejection_count=len(bundle.lemmas),
            fatal_error_class="invalid_phase_options",
            elapsed_seconds=time.monotonic() - started,
            phase_elapsed_seconds=phase_elapsed,
            phase_statuses={"phase01_normalize": "ok", "phase04_lemmas": "fatal"},
            message="Requested lemma id does not exist in the normalized artifact.",
            diagnostics=diagnostics,
        )
    normalized_target_lemma_id = target_lemma_id.strip() if target_lemma_id else None
    expected_lemma_count = 1 if normalized_target_lemma_id else len(bundle.lemmas)
    resolved_timeout_policy = resolve_timeout_policy(
        bundle=bundle,
        runtime_config=runtime_config,
        cli_timeout_seconds=timeout_seconds,
        cli_workspace_timeout_seconds=workspace_timeout_seconds,
        timeout_policy_path=timeout_policy_path,
    )
    for warning in resolved_timeout_policy.warnings:
        print(f"[lean_engine] timeout policy warning: {warning}", file=sys.stderr, flush=True)
    workspace_policy = resolved_timeout_policy.phases["workspace_prepare"]
    phase03_policy = resolved_timeout_policy.phases["phase03_statement"]
    phase04_policy = resolved_timeout_policy.phases["phase04_lemma"]
    phase05_policy = resolved_timeout_policy.phases["phase05_semantic"]
    phase06_policy = resolved_timeout_policy.phases["phase06_root"]
    phase03_runtime_config = runtime_config_for_phase(runtime_config, phase03_policy)
    phase04_runtime_config = runtime_config_for_phase(runtime_config, phase04_policy)
    phase05_runtime_config = runtime_config_for_phase(runtime_config, phase05_policy)
    phase06_runtime_config = runtime_config_for_phase(runtime_config, phase06_policy)

    # Kill stale lake/lean processes from previous runs of this problem
    _source_label = Path(source).stem if isinstance(source, (str, Path)) and Path(source).suffix else source_name
    _kill_stale_processes(bundle.problem_id, runtime_config.artifacts.root)

    runtime_started = time.monotonic()
    run_paths = create_run_paths(bundle.problem_id, artifacts_root=runtime_config.artifacts.root, source_label=_source_label)
    workspace_result = create_workspace_from_template(
        run_paths.workspace_dir,
        template_dir=template_dir,
        runtime_config=runtime_config,
    )
    workspace_cache_hit = workspace_result.cache_hit
    if runtime_config.mcp.enabled:
        write_project_mcp_config(
            run_paths.workspace_dir,
            runtime_config,
            mcp_log_dir=run_paths.run_root / ".mcp_logs",
        )
    write_json(run_paths.runtime_config_path, runtime_config.to_dict())
    write_json(run_paths.summaries_dir / "timeout_policy_effective.json", resolved_timeout_policy.to_dict())
    write_json(run_paths.workspace_snapshot_path, snapshot_workspace(run_paths.workspace_dir))
    write_json(run_paths.normalized_problem_path, bundle.to_dict())
    integration_preflight = _run_phase02_integration_preflight(
        run_paths=run_paths,
        runtime_config=runtime_config,
        integration_usage=integration_usage,
        timeout_seconds=workspace_policy.lean_check_seconds,
    )
    workspace_prepare_result = prepare_lean_workspace(
        run_paths.workspace_dir,
        timeout_seconds=workspace_policy.lean_build_seconds,
        runner=runner,
        log_dir=run_paths.diagnostics_dir,
        skip_cache_get=workspace_cache_hit,
        lake_jobs=runtime_config.lean.lake_jobs,
        require_cache=require_cache,
    )
    workspace_prepare_path = run_paths.summaries_dir / "phase02_workspace_preparation.json"
    write_json(workspace_prepare_path, workspace_prepare_result.to_dict())
    # Update cache after successful build so future runs are instant
    if workspace_prepare_result.status == "ok" and runtime_config.workspace_cache.enabled:
        try:
            cache_lake_dir = resolve_cache_lake_dir(
                runtime_config.workspace_cache.cache_dir.expanduser().resolve(),
                (template_dir or Path(__file__).resolve().parents[2] / "templates" / "lean_project"),
            )
            update_lake_cache(run_paths.workspace_dir, cache_lake_dir)
        except Exception as exc:
            print(f"[lean_engine] WARNING: cache update failed: {exc}", file=sys.stderr, flush=True)

    phase_elapsed["phase02_runtime"] = time.monotonic() - runtime_started
    if workspace_prepare_result.status != "ok":
        phase_statuses["phase02_runtime"] = "fatal"
        result = Phase07RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            run_name=run_paths.run_name,
            phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
            normalized_problem_path=run_paths.normalized_problem_path,
            final_result_path=None,
            final_summary_path=None,
            model=effective_model,
            lemma_count=expected_lemma_count,
            compiled_lemma_count=0,
            semantic_rejection_count=expected_lemma_count,
            fatal_error_class=workspace_prepare_result.error_class,
            elapsed_seconds=time.monotonic() - started,
            phase_elapsed_seconds=phase_elapsed,
            phase_statuses=phase_statuses,
            integration_usage=integration_usage.to_dict(),
            integration_preflight=integration_preflight,
            message=workspace_prepare_result.message,
            diagnostics=tuple(workspace_prepare_result.diagnostics),
        )
        _write_phase07_summary(result)
        return result
    phase_statuses["phase02_runtime"] = "ok"

    phase03_result = _run_phase03(
        run_paths=run_paths,
        runtime_config=phase03_runtime_config,
        bundle=bundle,
        expected_lemma_count=expected_lemma_count,
        max_repair_rounds=max_repair_rounds,
        timeout_seconds=phase03_policy.lean_check_seconds,
        model=model,
        provided_statements_text=provided_statements_text,
        phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses,
        started=started,
        effective_model=effective_model,
    )
    if isinstance(phase03_result, Phase07RunResult):
        phase03_attempts = tuple(phase03_result.external_backend_attempts)
        return _with_integration_context(
            phase03_result,
            integration_usage=integration_usage.to_dict(),
            integration_preflight=integration_preflight,
            external_backend_attempts=tuple(external_backend_attempts) + phase03_attempts,
        )
    external_backend_attempts.extend(list(phase03_result.external_backend_attempts))

    # --- Pause checkpoint: after Phase 03 ---
    pause_result = _check_pause(
        run_paths=run_paths, bundle=bundle, phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses, started=started, effective_model=effective_model,
        expected_lemma_count=expected_lemma_count,
        integration_usage=integration_usage.to_dict(),
        integration_preflight=integration_preflight,
        external_backend_attempts=tuple(external_backend_attempts),
    )
    if pause_result is not None:
        return pause_result

    phase04_result = _run_phase04(
        run_paths=run_paths,
        runtime_config=phase04_runtime_config,
        bundle=bundle,
        target_lemma_id=normalized_target_lemma_id,
        expected_lemma_count=expected_lemma_count,
        phase03_result=phase03_result,
        max_attempts_per_lemma=max_attempts_per_lemma,
        parallel_lemmas=parallel_lemmas,
        lemma_workers=lemma_workers,
        timeout_seconds=phase04_policy.agent_hard_seconds,
        lean_check_timeout_seconds=phase04_policy.lean_check_seconds,
        model=model,
        mock_candidates=mock_candidates,
        runner=runner,
        phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses,
        started=started,
        effective_model=effective_model,
    )
    if isinstance(phase04_result, Phase07RunResult):
        return _with_integration_context(
            phase04_result,
            integration_usage=integration_usage.to_dict(),
            integration_preflight=integration_preflight,
            external_backend_attempts=tuple(external_backend_attempts),
        )

    # --- Pause checkpoint: after Phase 04 ---
    compiled_count = len(getattr(phase04_result, 'compiled_lemma_ids', []))
    pause_result = _check_pause(
        run_paths=run_paths, bundle=bundle, phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses, started=started, effective_model=effective_model,
        expected_lemma_count=expected_lemma_count, compiled_lemma_count=compiled_count,
        integration_usage=integration_usage.to_dict(),
        integration_preflight=integration_preflight,
        external_backend_attempts=tuple(external_backend_attempts),
    )
    if pause_result is not None:
        return pause_result

    phase05_result = _run_phase05(
        run_paths=run_paths,
        runtime_config=phase05_runtime_config,
        bundle=bundle,
        target_lemma_id=normalized_target_lemma_id,
        expected_lemma_count=expected_lemma_count,
        phase04_result=phase04_result,
        max_semantic_repairs=max_semantic_repairs,
        timeout_seconds=phase05_policy.agent_hard_seconds,
        model=model,
        mock_equivalence_verdicts=mock_equivalence_verdicts,
        mock_gap_classifications=mock_gap_classifications,
        phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses,
        started=started,
        effective_model=effective_model,
    )
    if isinstance(phase05_result, Phase07RunResult):
        return _with_integration_context(
            phase05_result,
            integration_usage=integration_usage.to_dict(),
            integration_preflight=integration_preflight,
            external_backend_attempts=tuple(external_backend_attempts),
        )

    accepted_count = len(phase05_result.semantically_accepted_lemma_ids)
    semantic_rejection_count = max(expected_lemma_count - accepted_count, 0)

    # --- Pause checkpoint: after Phase 05 ---
    pause_result = _check_pause(
        run_paths=run_paths, bundle=bundle, phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses, started=started, effective_model=effective_model,
        expected_lemma_count=expected_lemma_count, compiled_lemma_count=accepted_count,
        integration_usage=integration_usage.to_dict(),
        integration_preflight=integration_preflight,
        external_backend_attempts=tuple(external_backend_attempts),
    )
    if pause_result is not None:
        return pause_result

    if stop_after_semantic or normalized_target_lemma_id is not None:
        phase_statuses["phase06_root"] = "skipped"
        result = Phase07RunResult(
            status=phase05_result.status,
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            run_name=run_paths.run_name,
            phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
            normalized_problem_path=run_paths.normalized_problem_path,
            final_result_path=phase05_result.final_result_path,
            final_summary_path=phase05_result.final_summary_path,
            model=effective_model,
            lemma_count=expected_lemma_count,
            compiled_lemma_count=accepted_count,
            semantic_rejection_count=semantic_rejection_count,
            fatal_error_class=phase05_result.error_class,
            elapsed_seconds=time.monotonic() - started,
            phase_elapsed_seconds=phase_elapsed,
            phase_statuses=phase_statuses,
            integration_usage=integration_usage.to_dict(),
            integration_preflight=integration_preflight,
            external_backend_attempts=tuple(external_backend_attempts),
            message=(
                phase05_result.message
                if phase05_result.message
                else "Lemma statement/proof formalization completed through semantic guards."
            ),
        )
        _attach_integration_context_to_final_result(
            final_result_path=result.final_result_path,
            integration_usage=result.integration_usage,
            integration_preflight=result.integration_preflight,
            external_backend_attempts=result.external_backend_attempts,
        )
        write_json(run_paths.summaries_dir / "integration_usage.json", result.integration_usage)
        _write_phase07_summary(result)
        return result

    # --- Pre-Phase-06 gate: verify Statements + Lemmas compile before root assembly ---
    _pre06_check_path = run_paths.workspace_dir / "PrePhase06Check.lean"
    try:
        _stmts = (run_paths.workspace_dir / "Orthos" / "Statements.lean").read_text(encoding="utf-8")
        _lemmas = (run_paths.workspace_dir / "Orthos" / "Lemmas.lean").read_text(encoding="utf-8")
        _pre06_body = "import Mathlib\n\n"
        for _line in _stmts.splitlines():
            _s = _line.strip()
            if _s.startswith("import Orthos") or _s.startswith("import Mathlib"):
                continue
            _pre06_body += _line + "\n"
        _pre06_body += "\n"
        for _line in _lemmas.splitlines():
            _s = _line.strip()
            if _s.startswith("import Orthos") or _s.startswith("import Mathlib"):
                continue
            _pre06_body += _line + "\n"
        _pre06_check_path.write_text(_pre06_body, encoding="utf-8")
        _pre06_result = check_lean_file(
            run_paths.workspace_dir,
            "PrePhase06Check.lean",
            timeout_seconds=phase06_policy.lean_check_seconds,
            lake_jobs=runtime_config.lean.lake_jobs,
        )
        if not _pre06_result.ok:
            print(
                f"[lean_engine] WARNING: Pre-Phase-06 check failed — Statements+Lemmas have errors.\n"
                f"  stderr: {_pre06_result.stderr[:300]}",
                file=sys.stderr, flush=True,
            )
        else:
            print("[lean_engine] Pre-Phase-06 check passed: Statements+Lemmas compile.", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[lean_engine] WARNING: Pre-Phase-06 check skipped: {exc}", file=sys.stderr, flush=True)
    finally:
        _pre06_check_path.unlink(missing_ok=True)

    phase06_started = time.monotonic()
    phase06_result = run_phase06(
        run_paths=run_paths,
        runtime_config=phase06_runtime_config,
        bundle=bundle,
        pinned_signatures_path=phase03_result.pinned_signatures_path or (run_paths.run_root / "pinned_signatures.json"),
        semantic_manifest_path=phase05_result.semantic_manifest_path,
        phase05_summary_path=phase05_result.phase_summary_path,
        max_root_attempts=max_root_attempts,
        timeout_seconds=phase06_policy.lean_check_seconds,
        model=model,
        mock_root_candidates=mock_root_candidates,
        runner=runner,
    )
    phase_elapsed["phase06_root"] = time.monotonic() - phase06_started
    phase_statuses["phase06_root"] = phase06_result.status

    result = Phase07RunResult(
        status=phase06_result.status,
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        run_name=run_paths.run_name,
        phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
        normalized_problem_path=run_paths.normalized_problem_path,
        final_result_path=phase06_result.final_result_path,
        final_summary_path=phase06_result.final_summary_path,
        model=effective_model,
        lemma_count=expected_lemma_count,
        compiled_lemma_count=accepted_count,
        semantic_rejection_count=semantic_rejection_count,
        fatal_error_class=phase06_result.error_class,
        elapsed_seconds=time.monotonic() - started,
        phase_elapsed_seconds=phase_elapsed,
        phase_statuses=phase_statuses,
        integration_usage=integration_usage.to_dict(),
        integration_preflight=integration_preflight,
        external_backend_attempts=tuple(external_backend_attempts),
        message=phase06_result.message,
    )
    _attach_integration_context_to_final_result(
        final_result_path=result.final_result_path,
        integration_usage=result.integration_usage,
        integration_preflight=result.integration_preflight,
        external_backend_attempts=result.external_backend_attempts,
    )
    write_json(run_paths.summaries_dir / "integration_usage.json", result.integration_usage)
    _write_phase07_summary(result)
    return result


def resume_phase07(
    *,
    run_dir: Path,
    runtime_config: RuntimeConfig,
    max_attempts_per_lemma: int = 5,
    parallel_lemmas: bool = False,
    lemma_workers: int = 1,
    max_semantic_repairs: int = 1,
    max_root_attempts: int = 4,
    timeout_seconds: int | None = None,
    timeout_policy_path: Path | None = None,
    model: str | None = None,
    target_lemma_id: str | None = None,
    stop_after_semantic: bool = False,
    runner: SubprocessRunner = subprocess.run,
) -> Phase07RunResult:
    """Resume a previous run, creating a new run directory.

    Copies workspace + pinned signatures from the old run, then re-runs
    phases 04-06 in a fresh run directory (e.g. run_004 if resuming run_003).
    """
    import shutil

    _install_pause_handler()
    started = time.monotonic()
    phase_elapsed: dict[str, float] = {}
    phase_statuses: dict[str, str] = {}
    effective_model = model or runtime_config.claude.model

    # Validate old run
    old_run_paths = load_run_paths(run_dir)
    if not old_run_paths.normalized_problem_path.exists():
        raise ValueError(f"no normalized_problem.json in run: {run_dir}")

    old_pinned = old_run_paths.run_root / "pinned_signatures.json"

    # Load previous phase statuses — prefer phase07_summary.json, fall back to individual summaries
    prev_summary_path = old_run_paths.summaries_dir / "phase07_summary.json"
    if prev_summary_path.exists():
        prev_summary = json.loads(prev_summary_path.read_text(encoding="utf-8"))
        prev_phase_statuses = prev_summary.get("phase_statuses", {})
    else:
        # Infer statuses from individual phase summaries (handles runs that crashed mid-pipeline)
        prev_phase_statuses: dict[str, str] = {}
        # Phases 01-03 are implied ok if pinned_signatures.json exists (it's written after phase03 succeeds)
        if old_pinned.exists() and old_run_paths.normalized_problem_path.exists():
            prev_phase_statuses["phase01_normalize"] = "ok"
            prev_phase_statuses["phase02_runtime"] = "ok"
            prev_phase_statuses["phase03_statement"] = "ok"
        for phase_key, fname in [("phase04_lemmas", "phase04_summary.json"), ("phase05_semantic", "phase05_summary.json")]:
            p = old_run_paths.summaries_dir / fname
            if p.exists():
                try:
                    prev_phase_statuses[phase_key] = json.loads(p.read_text(encoding="utf-8")).get("status", "unknown")
                except (OSError, json.JSONDecodeError):
                    pass

    if prev_phase_statuses.get("phase01_normalize") != "ok" or prev_phase_statuses.get("phase02_runtime") != "ok":
        raise ValueError("cannot resume: phases 01/02 did not succeed in the original run")
    if prev_phase_statuses.get("phase03_statement") != "ok":
        raise ValueError("cannot resume: phase 03 did not succeed — re-run from scratch")

    if not old_pinned.exists():
        raise ValueError(f"pinned_signatures.json missing from run: {run_dir}")

    # Load bundle from normalized_problem.json
    bundle = normalize_problem_artifact(
        old_run_paths.normalized_problem_path,
        source_name="resume",
    )
    expected_lemma_count = len(bundle.lemmas)
    resolved_timeout_policy = resolve_timeout_policy(
        bundle=bundle,
        runtime_config=runtime_config,
        cli_timeout_seconds=timeout_seconds,
        timeout_policy_path=timeout_policy_path,
    )
    for warning in resolved_timeout_policy.warnings:
        print(f"[lean_engine] timeout policy warning: {warning}", file=sys.stderr, flush=True)
    phase04_policy = resolved_timeout_policy.phases["phase04_lemma"]
    phase05_policy = resolved_timeout_policy.phases["phase05_semantic"]
    phase06_policy = resolved_timeout_policy.phases["phase06_root"]
    phase04_runtime_config = runtime_config_for_phase(runtime_config, phase04_policy)
    phase05_runtime_config = runtime_config_for_phase(runtime_config, phase05_policy)
    phase06_runtime_config = runtime_config_for_phase(runtime_config, phase06_policy)

    # Create NEW run directory (run_004 if resuming run_003).
    # Preserve source label from original problem dir name if present.
    _old_dir_name = run_dir.parent.name
    _resume_label = _old_dir_name.split("__")[0] if "__" in _old_dir_name else None
    new_run_paths = create_run_paths(bundle.problem_id, artifacts_root=runtime_config.artifacts.root, source_label=_resume_label)
    print(f"[lean_engine] resuming from: {run_dir}", file=sys.stderr, flush=True)
    print(f"[lean_engine] new run dir: {new_run_paths.run_root}", file=sys.stderr, flush=True)
    print(f"[lean_engine] previous phase statuses: {prev_phase_statuses}", file=sys.stderr, flush=True)

    # Copy workspace from old run (includes .lake/ cache, Statements.lean, etc.)
    if old_run_paths.workspace_dir.exists():
        shutil.copytree(old_run_paths.workspace_dir, new_run_paths.workspace_dir, dirs_exist_ok=True)
        # Restore write permissions on Orthos/ files (old run may have made them read-only)
        for lean_file in new_run_paths.workspace_dir.glob("Orthos/*.lean"):
            lean_file.chmod(0o644)

    # Copy ALL artifact dirs from old run so the new run has complete history
    for subdir_name in ("lemmas", "summaries", "diagnostics", "claude_raw", "prompts",
                         "root_assembly", "semantic", ".mcp_logs"):
        old_subdir = old_run_paths.run_root / subdir_name
        new_subdir = new_run_paths.run_root / subdir_name
        if old_subdir.exists() and old_subdir.is_dir():
            shutil.copytree(old_subdir, new_subdir, dirs_exist_ok=True)

    # Copy pinned signatures (immutable from phase03), update run_name binding
    new_pinned = new_run_paths.run_root / "pinned_signatures.json"
    pinned_data = json.loads(old_pinned.read_text(encoding="utf-8"))
    pinned_data["run_name"] = new_run_paths.run_name
    write_json(new_pinned, pinned_data)

    # Copy normalized problem
    shutil.copy2(old_run_paths.normalized_problem_path, new_run_paths.normalized_problem_path)

    # Write runtime config
    write_json(new_run_paths.runtime_config_path, runtime_config.to_dict())
    write_json(new_run_paths.summaries_dir / "timeout_policy_effective.json", resolved_timeout_policy.to_dict())

    # Set up MCP config if enabled
    if runtime_config.mcp.enabled:
        write_project_mcp_config(
            new_run_paths.workspace_dir,
            runtime_config,
            mcp_log_dir=new_run_paths.run_root / ".mcp_logs",
        )

    phase_statuses["phase01_normalize"] = "ok"
    phase_statuses["phase02_runtime"] = "ok"
    phase_statuses["phase03_statement"] = "ok"
    phase_elapsed["phase01_normalize"] = 0.0
    phase_elapsed["phase02_runtime"] = 0.0
    phase_elapsed["phase03_statement"] = 0.0

    run_paths = new_run_paths

    # --- Pre-populate trusted context from old run's successful lemmas ---
    from .lemma_phase import (
        DECL_PATTERN,
        TrustedContextEntry,
        lemma_id_to_path_token,
        load_pinned_lemma_signatures,
        merge_declaration_into_lemmas_file,
        write_trusted_manifest,
    )
    from .phase04 import run_phase04

    pinned_map = load_pinned_lemma_signatures(
        new_pinned,
        expected_problem_id=bundle.problem_id,
        expected_run_name=run_paths.run_name,
    )
    manifest_problem_id = bundle.problem_id.replace("-", "_").replace(" ", "_")

    # Scan old run for successful lemmas — check lemma dirs AND trusted manifest
    pre_trusted: list[TrustedContextEntry] = []
    carried_lemma_ids: set[str] = set()
    old_lemmas_dir = old_run_paths.run_root / "lemmas"
    lemmas_file_path = run_paths.workspace_dir / "Orthos" / "Lemmas.lean"

    # Also load old trusted manifest — lemmas carried from even older runs won't have lemma dirs
    old_manifest_entries: dict[str, dict[str, Any]] = {}
    old_manifest_path = old_run_paths.run_root / "trusted_context_manifest.json"
    if old_manifest_path.exists():
        try:
            old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            for entry_data in old_manifest.get("entries", []):
                lid = entry_data.get("lemma_id", "")
                if lid and entry_data.get("status") == "compiled":
                    old_manifest_entries[lid] = entry_data
        except (OSError, json.JSONDecodeError):
            pass

    for lemma_id, pinned in pinned_map.items():
        # Try 1: check lemma dir for merged_authoritative.lean (new) or final_success.lean (legacy)
        old_lemma_dir = old_lemmas_dir / lemma_id_to_path_token(lemma_id)
        old_result_path = old_lemma_dir / "result.json"
        old_success_path = old_lemma_dir / "merged_authoritative.lean"
        legacy_success_path = old_lemma_dir / "final_success.lean"
        declaration: str | None = None

        if old_result_path.exists() and (old_success_path.exists() or legacy_success_path.exists()):
            try:
                old_result = json.loads(old_result_path.read_text(encoding="utf-8"))
                status = str(old_result.get("status", "")).strip()
                if status in {"ok", "succeeded", "merged"}:
                    proof_path = old_success_path if old_success_path.exists() else legacy_success_path
                    declaration = proof_path.read_text(encoding="utf-8").strip()
            except (OSError, json.JSONDecodeError):
                pass

        # Try 2: check trusted manifest (covers lemmas carried from prior runs)
        if not declaration and lemma_id in old_manifest_entries:
            declaration = old_manifest_entries[lemma_id].get("declaration", "").strip()

        if not declaration:
            continue

        # Merge into Lemmas.lean — skip if declaration already present (e.g. workspace copied from prior run)
        original_text = lemmas_file_path.read_text(encoding="utf-8")
        existing_decl_names = {m.group(2) for m in DECL_PATTERN.finditer(original_text)}
        if pinned.decl_name not in existing_decl_names:
            try:
                merged_text = merge_declaration_into_lemmas_file(
                    original_text=original_text,
                    declaration_block=declaration,
                )
                lemmas_file_path.write_text(merged_text, encoding="utf-8")
            except ValueError:
                continue

        entry = TrustedContextEntry(
            lemma_id=lemma_id,
            decl_name=pinned.decl_name,
            status="compiled",
            source_file="Orthos/Lemmas.lean",
            signature=pinned.signature,
            declaration=declaration,
        )
        pre_trusted.append(entry)
        carried_lemma_ids.add(lemma_id)

    # Final deduplication pass on Lemmas.lean — ensures no duplicate declarations
    # even if workspace was copied from a run that already had duplicates.
    _deduplicate_lemmas_file(lemmas_file_path)

    # Write pre-populated manifest
    from .artifact_io import sanitize_component
    write_trusted_manifest(
        run_paths.run_root / "trusted_context_manifest.json",
        sanitize_component(bundle.problem_id),
        pre_trusted,
    )

    # Determine which lemmas still need work
    all_lemma_ids = list(bundle.topologically_sorted_lemma_ids or sorted(bundle.lemma_map))
    failed_lemma_ids = [lid for lid in all_lemma_ids if lid not in carried_lemma_ids]

    print(
        f"[lean_engine] carried {len(carried_lemma_ids)} successful lemma(s) from previous run, "
        f"retrying {len(failed_lemma_ids)}: {failed_lemma_ids}",
        file=sys.stderr, flush=True,
    )

    # Phase 04: only retry failed lemmas
    phase04_started = time.monotonic()

    if not failed_lemma_ids:
        # All lemmas already succeeded — skip phase04 entirely
        from .phase04 import Phase04RunResult
        from .lemma_phase import LemmaFormalizationResult
        phase04_result = Phase04RunResult(
            status="ok",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=new_pinned,
            trusted_manifest_path=run_paths.run_root / "trusted_context_manifest.json",
            phase_summary_path=run_paths.summaries_dir / "phase04_summary.json",
            lemma_order=tuple(all_lemma_ids),
            lemma_results=(),
        )
        write_json(phase04_result.phase_summary_path, phase04_result.to_dict())
    else:
        phase04_result = run_phase04(
            run_paths=run_paths,
            runtime_config=phase04_runtime_config,
            bundle=bundle,
            pinned_signatures_path=new_pinned,
            max_attempts_per_lemma=max_attempts_per_lemma,
            timeout_seconds=phase04_policy.agent_hard_seconds,
            lean_check_timeout_seconds=phase04_policy.lean_check_seconds,
            model=model,
            target_lemma_id=target_lemma_id,
            target_lemma_ids=set(failed_lemma_ids),
            parallel_lemmas=parallel_lemmas and len(failed_lemma_ids) > 1,
            lemma_workers=lemma_workers,
            runner=runner,
        )
    # Inject carried lemma results into phase04 summary so Phase 05 can find them
    if carried_lemma_ids:
        from .lemma_phase import LemmaFormalizationResult
        from dataclasses import replace as _replace
        carried_results: list[LemmaFormalizationResult] = []
        for lemma_id in all_lemma_ids:
            if lemma_id not in carried_lemma_ids:
                continue
            pinned = pinned_map[lemma_id]
            old_lemma_dir = old_lemmas_dir / lemma_id_to_path_token(lemma_id)
            carried_results.append(LemmaFormalizationResult(
                lemma_id=lemma_id,
                decl_name=pinned.decl_name,
                status="succeeded",
                attempts_used=0,
                lemma_artifact_dir=old_lemma_dir,
                scratch_file=old_lemma_dir / "scratch.lean",
                result_path=old_lemma_dir / "result.json",
                merged_authoritative_path=(
                    (old_lemma_dir / "merged_authoritative.lean")
                    if (old_lemma_dir / "merged_authoritative.lean").exists()
                    else (old_lemma_dir / "final_success.lean")
                ),
                terminal=True,
            ))
        # Merge: carried results + phase04 retry results, in deterministic order
        retry_results_by_id = {r.lemma_id: r for r in phase04_result.lemma_results}
        all_results: list[LemmaFormalizationResult] = []
        for lemma_id in all_lemma_ids:
            if lemma_id in retry_results_by_id:
                all_results.append(retry_results_by_id[lemma_id])
            elif lemma_id in carried_lemma_ids:
                all_results.append(next(r for r in carried_results if r.lemma_id == lemma_id))
        phase04_result = _replace(
            phase04_result,
            lemma_order=tuple(all_lemma_ids),
            lemma_results=tuple(all_results),
        )
        # Rewrite summary with all lemmas included
        write_json(phase04_result.phase_summary_path, phase04_result.to_dict())

    phase_elapsed["phase04_lemmas"] = time.monotonic() - phase04_started
    phase_statuses["phase04_lemmas"] = phase04_result.status

    if phase04_result.status != "ok":
        result = Phase07RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            run_name=run_paths.run_name,
            phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
            normalized_problem_path=run_paths.normalized_problem_path,
            final_result_path=None,
            final_summary_path=None,
            model=effective_model,
            lemma_count=expected_lemma_count,
            compiled_lemma_count=0,
            semantic_rejection_count=expected_lemma_count,
            fatal_error_class=phase04_result.error_class,
            elapsed_seconds=time.monotonic() - started,
            phase_elapsed_seconds=phase_elapsed,
            phase_statuses=phase_statuses,
            message=phase04_result.message,
        )
        _write_phase07_summary(result)
        return result

    # Phase 05: semantic guards
    normalized_target_lemma_id = target_lemma_id.strip() if target_lemma_id else None
    phase05_result = _run_phase05(
        run_paths=run_paths,
        runtime_config=phase05_runtime_config,
        bundle=bundle,
        target_lemma_id=normalized_target_lemma_id,
        expected_lemma_count=expected_lemma_count,
        phase04_result=phase04_result,
        max_semantic_repairs=max_semantic_repairs,
        timeout_seconds=phase05_policy.agent_hard_seconds,
        model=model,
        mock_equivalence_verdicts=None,
        mock_gap_classifications=None,
        phase_elapsed=phase_elapsed,
        phase_statuses=phase_statuses,
        started=started,
        effective_model=effective_model,
    )
    if isinstance(phase05_result, Phase07RunResult):
        _write_phase07_summary(phase05_result)
        return phase05_result

    accepted_count = len(phase05_result.semantically_accepted_lemma_ids)
    semantic_rejection_count = max(expected_lemma_count - accepted_count, 0)

    if stop_after_semantic or normalized_target_lemma_id is not None:
        phase_statuses["phase06_root"] = "skipped"
        result = Phase07RunResult(
            status=phase05_result.status,
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            run_name=run_paths.run_name,
            phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
            normalized_problem_path=run_paths.normalized_problem_path,
            final_result_path=phase05_result.final_result_path,
            final_summary_path=phase05_result.final_summary_path,
            model=effective_model,
            lemma_count=expected_lemma_count,
            compiled_lemma_count=accepted_count,
            semantic_rejection_count=semantic_rejection_count,
            fatal_error_class=phase05_result.error_class,
            elapsed_seconds=time.monotonic() - started,
            phase_elapsed_seconds=phase_elapsed,
            phase_statuses=phase_statuses,
            message=phase05_result.message,
        )
        _write_phase07_summary(result)
        return result

    # Phase 06: root assembly
    phase06_started = time.monotonic()
    phase06_result = run_phase06(
        run_paths=run_paths,
        runtime_config=phase06_runtime_config,
        bundle=bundle,
        pinned_signatures_path=new_pinned,
        semantic_manifest_path=phase05_result.semantic_manifest_path,
        phase05_summary_path=phase05_result.phase_summary_path,
        max_root_attempts=max_root_attempts,
        timeout_seconds=phase06_policy.lean_check_seconds,
        model=model,
        runner=runner,
    )
    phase_elapsed["phase06_root"] = time.monotonic() - phase06_started
    phase_statuses["phase06_root"] = phase06_result.status

    result = Phase07RunResult(
        status=phase06_result.status,
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        run_name=run_paths.run_name,
        phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
        normalized_problem_path=run_paths.normalized_problem_path,
        final_result_path=phase06_result.final_result_path,
        final_summary_path=phase06_result.final_summary_path,
        model=effective_model,
        lemma_count=expected_lemma_count,
        compiled_lemma_count=accepted_count,
        semantic_rejection_count=semantic_rejection_count,
        fatal_error_class=phase06_result.error_class,
        elapsed_seconds=time.monotonic() - started,
        phase_elapsed_seconds=phase_elapsed,
        phase_statuses=phase_statuses,
        message=phase06_result.message,
    )
    _write_phase07_summary(result)
    return result


def inspect_run(run_dir: Path) -> dict[str, Any]:
    run_paths = load_run_paths(run_dir)
    phase_files = {
        "phase03": run_paths.summaries_dir / "phase03_summary.json",
        "phase04": run_paths.summaries_dir / "phase04_summary.json",
        "phase05": run_paths.summaries_dir / "phase05_summary.json",
        "phase06": run_paths.summaries_dir / "phase06_summary.json",
        "phase07": run_paths.summaries_dir / "phase07_summary.json",
    }
    phase_statuses: dict[str, Any] = {}
    for phase_name, path in phase_files.items():
        if not path.exists() or not path.is_file():
            phase_statuses[phase_name] = {"exists": False}
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            phase_statuses[phase_name] = {"exists": True, "status": "unreadable"}
            continue
        phase_statuses[phase_name] = {
            "exists": True,
            "status": str(payload.get("status", "unknown")),
            "error_class": payload.get("error_class"),
            "path": str(path),
        }

    final_result_path = run_paths.run_root / "final" / "result.json"
    integration_usage_path = run_paths.summaries_dir / "integration_usage.json"
    final_payload = None
    if final_result_path.exists() and final_result_path.is_file():
        try:
            final_payload = json.loads(final_result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            final_payload = {"status": "unreadable"}

    return {
        "status": "ok",
        "problem_id": run_paths.problem_id,
        "run_name": run_paths.run_name,
        "run_root": str(run_paths.run_root),
        "paths": {
            "normalized_problem": str(run_paths.normalized_problem_path),
            "workspace": str(run_paths.workspace_dir),
            "runtime_config": str(run_paths.runtime_config_path),
            "workspace_snapshot": str(run_paths.workspace_snapshot_path),
            "summaries": str(run_paths.summaries_dir),
            "final_result": str(final_result_path),
            "integration_usage": str(integration_usage_path),
        },
        "exists": {
            "normalized_problem": run_paths.normalized_problem_path.exists(),
            "workspace": run_paths.workspace_dir.exists(),
            "runtime_config": run_paths.runtime_config_path.exists(),
            "workspace_snapshot": run_paths.workspace_snapshot_path.exists(),
            "summaries": run_paths.summaries_dir.exists(),
            "final_result": final_result_path.exists(),
            "integration_usage": integration_usage_path.exists(),
        },
        "phase_statuses": phase_statuses,
        "final_result": final_payload,
    }


def _run_phase03(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle,
    expected_lemma_count: int,
    max_repair_rounds: int,
    timeout_seconds: int,
    model: str | None,
    provided_statements_text: str | None,
    phase_elapsed: dict[str, float],
    phase_statuses: dict[str, str],
    started: float,
    effective_model: str,
) -> Phase03RunResult | Phase07RunResult:
    phase03_started = time.monotonic()
    phase03_result = run_phase03(
        run_paths=run_paths,
        runtime_config=runtime_config,
        normalized_bundle=bundle,
        max_repair_rounds=max_repair_rounds,
        timeout_seconds=timeout_seconds,
        model=model,
        provided_statements_text=provided_statements_text,
    )
    phase_elapsed["phase03_statement"] = time.monotonic() - phase03_started
    phase_statuses["phase03_statement"] = phase03_result.status
    if phase03_result.status == "ok":
        return phase03_result
    result = Phase07RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        run_name=run_paths.run_name,
        phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
        normalized_problem_path=run_paths.normalized_problem_path,
        final_result_path=None,
        final_summary_path=None,
        model=effective_model,
        lemma_count=expected_lemma_count,
        compiled_lemma_count=0,
        semantic_rejection_count=expected_lemma_count,
        fatal_error_class=phase03_result.error_class,
        elapsed_seconds=time.monotonic() - started,
        phase_elapsed_seconds=phase_elapsed,
        phase_statuses=phase_statuses,
        external_backend_attempts=tuple(phase03_result.external_backend_attempts),
        message=phase03_result.message,
        diagnostics=tuple(phase03_result.diagnostics),
    )
    _write_phase07_summary(result)
    return result


def _run_phase04(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle,
    target_lemma_id: str | None,
    expected_lemma_count: int,
    phase03_result: Phase03RunResult,
    max_attempts_per_lemma: int,
    parallel_lemmas: bool,
    lemma_workers: int,
    timeout_seconds: int,
    lean_check_timeout_seconds: int | None,
    model: str | None,
    mock_candidates: dict[str, list[str]] | None,
    runner: SubprocessRunner,
    phase_elapsed: dict[str, float],
    phase_statuses: dict[str, str],
    started: float,
    effective_model: str,
) -> Phase04RunResult | Phase07RunResult:
    phase04_started = time.monotonic()
    phase04_result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=phase03_result.pinned_signatures_path or (run_paths.run_root / "pinned_signatures.json"),
        max_attempts_per_lemma=max_attempts_per_lemma,
        parallel_lemmas=parallel_lemmas,
        lemma_workers=lemma_workers,
        timeout_seconds=timeout_seconds,
        lean_check_timeout_seconds=lean_check_timeout_seconds,
        model=model,
        target_lemma_id=target_lemma_id,
        mock_candidates=mock_candidates,
        runner=runner,
    )
    phase_elapsed["phase04_lemmas"] = time.monotonic() - phase04_started
    phase_statuses["phase04_lemmas"] = phase04_result.status
    if phase04_result.status == "ok":
        return phase04_result

    compiled_count = sum(1 for item in phase04_result.lemma_results if item.status == "succeeded")
    result = Phase07RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        run_name=run_paths.run_name,
        phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
        normalized_problem_path=run_paths.normalized_problem_path,
        final_result_path=None,
        final_summary_path=None,
        model=effective_model,
        lemma_count=expected_lemma_count,
        compiled_lemma_count=compiled_count,
        semantic_rejection_count=max(expected_lemma_count - compiled_count, 0),
        fatal_error_class=phase04_result.error_class,
        elapsed_seconds=time.monotonic() - started,
        phase_elapsed_seconds=phase_elapsed,
        phase_statuses=phase_statuses,
        message=phase04_result.message,
        diagnostics=(),
    )
    _write_phase07_summary(result)
    return result


def _run_phase05(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle,
    target_lemma_id: str | None,
    expected_lemma_count: int,
    phase04_result: Phase04RunResult,
    max_semantic_repairs: int,
    timeout_seconds: int,
    model: str | None,
    mock_equivalence_verdicts: dict[str, Any] | None,
    mock_gap_classifications: dict[str, Any] | None,
    phase_elapsed: dict[str, float],
    phase_statuses: dict[str, str],
    started: float,
    effective_model: str,
) -> Phase05RunResult | Phase07RunResult:
    phase05_started = time.monotonic()
    phase05_result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=run_paths.run_root / "pinned_signatures.json",
        trusted_manifest_path=phase04_result.trusted_manifest_path,
        phase04_summary_path=phase04_result.phase_summary_path,
        max_semantic_repairs=max_semantic_repairs,
        timeout_seconds=timeout_seconds,
        model=model,
        target_lemma_id=target_lemma_id,
        mock_equivalence_verdicts=mock_equivalence_verdicts,
        mock_gap_classifications=mock_gap_classifications,
    )
    phase_elapsed["phase05_semantic"] = time.monotonic() - phase05_started
    phase_statuses["phase05_semantic"] = phase05_result.status
    if phase05_result.status == "ok":
        return phase05_result

    accepted_count = len(phase05_result.semantically_accepted_lemma_ids)
    result = Phase07RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        run_name=run_paths.run_name,
        phase_summary_path=run_paths.summaries_dir / "phase07_summary.json",
        normalized_problem_path=run_paths.normalized_problem_path,
        final_result_path=phase05_result.final_result_path,
        final_summary_path=phase05_result.final_summary_path,
        model=effective_model,
        lemma_count=expected_lemma_count,
        compiled_lemma_count=accepted_count,
        semantic_rejection_count=max(expected_lemma_count - accepted_count, 0),
        fatal_error_class=phase05_result.error_class,
        elapsed_seconds=time.monotonic() - started,
        phase_elapsed_seconds=phase_elapsed,
        phase_statuses=phase_statuses,
        message=phase05_result.message,
        diagnostics=(),
    )
    _write_phase07_summary(result)
    return result


def _run_phase02_integration_preflight(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    integration_usage: IntegrationUsageTracker,
    timeout_seconds: int,
) -> dict[str, Any]:
    inventory_rows = [
        lean_lsp_mcp.inventory(
            repo_root=runtime_config.integrations.lean_lsp_mcp_root,
            mcp_command=runtime_config.mcp.command,
        ),
    ]
    for row in inventory_rows:
        integration_usage.note_inventory(row)

    preflight_rows = [
        lean_lsp_mcp.preflight(
            repo_root=runtime_config.integrations.lean_lsp_mcp_root,
            mcp_command=runtime_config.mcp.command,
            workspace_root=run_paths.workspace_dir,
        ),
    ]
    for row in preflight_rows:
        integration_usage.note_preflight(row)

    integrations_dir = run_paths.run_root / "integrations"
    inventory_path = integrations_dir / "inventory.json"
    preflight_path = integrations_dir / "preflight.json"
    write_json(inventory_path, {"inventory": [row.to_dict() for row in inventory_rows]})
    write_json(preflight_path, {"preflight": [row.to_dict() for row in preflight_rows]})

    return {
        "inventory_path": str(inventory_path),
        "preflight_path": str(preflight_path),
        "inventory": [row.to_dict() for row in inventory_rows],
        "preflight": [row.to_dict() for row in preflight_rows],
    }


def _with_integration_context(
    result: Phase07RunResult,
    *,
    integration_usage: dict[str, Any] | None,
    integration_preflight: dict[str, Any] | None,
    external_backend_attempts: tuple[dict[str, Any], ...],
) -> Phase07RunResult:
    updated = replace(
        result,
        integration_usage=integration_usage,
        integration_preflight=integration_preflight,
        external_backend_attempts=external_backend_attempts,
    )
    _write_phase07_summary(updated)
    if updated.run_root is not None and integration_usage is not None:
        write_json(updated.run_root / "summaries" / "integration_usage.json", integration_usage)
    _attach_integration_context_to_final_result(
        final_result_path=updated.final_result_path,
        integration_usage=integration_usage,
        integration_preflight=integration_preflight,
        external_backend_attempts=external_backend_attempts,
    )
    return updated


def _attach_integration_context_to_final_result(
    *,
    final_result_path: Path | None,
    integration_usage: dict[str, Any] | None,
    integration_preflight: dict[str, Any] | None,
    external_backend_attempts: tuple[dict[str, Any], ...],
) -> None:
    if final_result_path is None or not final_result_path.exists() or not final_result_path.is_file():
        return
    try:
        payload = json.loads(final_result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["integration_usage"] = integration_usage
    payload["integration_preflight"] = integration_preflight
    payload["external_backend_attempts"] = list(external_backend_attempts)
    write_json(final_result_path, payload)


def _write_phase07_summary(result: Phase07RunResult) -> None:
    if result.phase_summary_path is None:
        return
    write_json(result.phase_summary_path, result.to_dict())
