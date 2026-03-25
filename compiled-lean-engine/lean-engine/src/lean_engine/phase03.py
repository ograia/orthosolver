from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_io import RunPaths, write_json, write_text
from .assembly_phase import AssemblyPrecheckResult, run_assembly_precheck, write_assembly_precheck_summary
from .claude_runner import ClaudeRunner, extract_result_text
from .config import RuntimeConfig
from .contracts import NormalizedProblemBundle
from .lean_checks import rebuild_module_olean
from .lean4_skills_refs import get_all_proving_refs
from .normalize import normalize_problem_artifact_result
from .prompting import build_assembly_repair_prompt
from .result_types import FatalResult
from .statement_phase import (
    StatementPhaseResult,
    build_phase03_decl_naming,
    find_disallowed_statement_tokens,
    normalize_lean_text,
    run_statement_phase,
    write_statement_phase_summary,
)


@dataclass(frozen=True)
class Phase03RunResult:
    status: str
    problem_id: str
    run_root: Path
    statements_path: Path
    pinned_signatures_path: Path | None
    assembly_precheck_summary_path: Path | None
    statement_phase_summary_path: Path
    phase_summary_path: Path
    statement_phase: dict[str, Any]
    assembly_precheck: dict[str, Any] | None
    error_class: str | None = None
    error_scope: str | None = None
    message: str | None = None
    diagnostics: tuple[str, ...] = ()
    external_backend_attempts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problem_id": self.problem_id,
            "run_root": str(self.run_root),
            "statements_path": str(self.statements_path),
            "pinned_signatures_path": str(self.pinned_signatures_path) if self.pinned_signatures_path else None,
            "assembly_precheck_summary_path": (
                str(self.assembly_precheck_summary_path) if self.assembly_precheck_summary_path else None
            ),
            "statement_phase_summary_path": str(self.statement_phase_summary_path),
            "phase_summary_path": str(self.phase_summary_path),
            "statement_phase": self.statement_phase,
            "assembly_precheck": self.assembly_precheck,
            "error_class": self.error_class,
            "error_scope": self.error_scope,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
            "external_backend_attempts": list(self.external_backend_attempts),
        }


def run_phase03(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    normalized_bundle: NormalizedProblemBundle,
    max_repair_rounds: int,
    timeout_seconds: int,
    model: str | None,
    provided_statements_text: str | None,
    runner=subprocess.run,
) -> Phase03RunResult:
    lean4_skills_refs = get_all_proving_refs(runtime_config.integrations.lean4_skills_root)
    decl_naming = build_phase03_decl_naming(normalized_bundle)
    claude_runner = ClaudeRunner(runtime_config)

    # Use Sonnet for statement generation by default — it's 3-5x faster than Opus
    # and sufficient for NL-to-Lean type signature translation.  The user can
    # override via --model on the CLI (which sets `model` to a non-None value).
    statement_model = model or "claude-sonnet-4-6"

    statement_result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=normalized_bundle,
        decl_naming=decl_naming,
        max_repair_rounds=max_repair_rounds,
        timeout_seconds=timeout_seconds,
        model=statement_model,
        claude_runner=claude_runner,
        provided_statements_text=provided_statements_text,
        runner=runner,
        lean4_skills_refs=lean4_skills_refs,
    )

    statement_summary_path = run_paths.summaries_dir / "phase03_statement_summary.json"
    write_statement_phase_summary(statement_summary_path, statement_result)

    if statement_result.status != "ok":
        phase_summary_path = run_paths.summaries_dir / "phase03_summary.json"
        result = Phase03RunResult(
            status="fatal",
            problem_id=normalized_bundle.problem_id,
            run_root=run_paths.run_root,
            statements_path=statement_result.statements_path,
            pinned_signatures_path=None,
            assembly_precheck_summary_path=None,
            statement_phase_summary_path=statement_summary_path,
            phase_summary_path=phase_summary_path,
            statement_phase=statement_result.to_dict(),
            assembly_precheck=None,
            error_class=statement_result.error_class,
            error_scope=statement_result.error_scope,
            message=statement_result.message,
            diagnostics=tuple(statement_result.diagnostics),
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    # Rebuild Orthos.Statements olean directly (bypass Lake's dependency tracker).
    # Using `lake build Orthos.Statements` would trigger Lake to recheck all
    # Mathlib dependencies, and with hardlinked cache timestamps it often decides
    # to rebuild Mathlib from source (~60 min).  Instead we run
    # `lake env lean <file> -o <olean> -i <ilean>` which compiles only the one
    # file using the existing LEAN_PATH (Mathlib oleans already present).
    import sys
    rebuild_result = rebuild_module_olean(
        run_paths.workspace_dir,
        "Orthos/Statements.lean",
        "Orthos.Statements",
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if not rebuild_result.ok:
        print(
            f"[lean_engine] WARNING: Orthos.Statements olean rebuild failed "
            f"(exit {rebuild_result.returncode}), assembly precheck may fail",
            file=sys.stderr, flush=True,
        )

    assembly_timeout = timeout_seconds
    assembly_result = run_assembly_precheck(
        run_paths=run_paths,
        bundle=normalized_bundle,
        decl_naming=decl_naming,
        timeout_seconds=assembly_timeout,
        runner=runner,
        lake_jobs=runtime_config.lean.lake_jobs,
    )
    assembly_summary_path = run_paths.run_root / "assembly_precheck_summary.json"
    write_assembly_precheck_summary(assembly_summary_path, assembly_result)

    # Bounded assembly repair loop: if the precheck failed due to a Lean typecheck error
    # (check_result is not None), try to fix Statements.lean via Claude.
    # Skip if it's a plan-structure validation failure (check_result is None).
    max_assembly_repair_rounds = 2
    for assembly_repair_round in range(1, max_assembly_repair_rounds + 1):
        if assembly_result.status == "ok":
            break
        if assembly_result.check_result is None:
            # Plan structure validation failure — prompt repair won't help.
            break
        assembly_result = _run_assembly_repair_round(
            run_paths=run_paths,
            runtime_config=runtime_config,
            bundle=normalized_bundle,
            decl_naming=decl_naming,
            assembly_result=assembly_result,
            claude_runner=claude_runner,
            model=model,
            timeout_seconds=assembly_timeout,
            repair_round=assembly_repair_round,
            runner=runner,
            lean4_skills_refs=lean4_skills_refs,
        )
        write_assembly_precheck_summary(assembly_summary_path, assembly_result)

    phase_summary_path = run_paths.summaries_dir / "phase03_summary.json"
    if assembly_result.status != "ok":
        result = Phase03RunResult(
            status="fatal",
            problem_id=normalized_bundle.problem_id,
            run_root=run_paths.run_root,
            statements_path=statement_result.statements_path,
            pinned_signatures_path=statement_result.pinned_signatures_path,
            assembly_precheck_summary_path=assembly_summary_path,
            statement_phase_summary_path=statement_summary_path,
            phase_summary_path=phase_summary_path,
            statement_phase=statement_result.to_dict(),
            assembly_precheck=assembly_result.to_dict(),
            error_class=assembly_result.error_class,
            error_scope=assembly_result.error_scope,
            message=assembly_result.message,
            diagnostics=assembly_result.diagnostics,
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    result = Phase03RunResult(
        status="ok",
        problem_id=normalized_bundle.problem_id,
        run_root=run_paths.run_root,
        statements_path=statement_result.statements_path,
        pinned_signatures_path=statement_result.pinned_signatures_path,
        assembly_precheck_summary_path=assembly_summary_path,
        statement_phase_summary_path=statement_summary_path,
        phase_summary_path=phase_summary_path,
        statement_phase=statement_result.to_dict(),
        assembly_precheck=assembly_result.to_dict(),
    )
    write_json(phase_summary_path, result.to_dict())
    return result


def _run_assembly_repair_round(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle: NormalizedProblemBundle,
    decl_naming,
    assembly_result: "AssemblyPrecheckResult",
    claude_runner: ClaudeRunner,
    model: str | None,
    timeout_seconds: int,
    repair_round: int,
    runner,
    lean4_skills_refs: str = "",
) -> "AssemblyPrecheckResult":
    """Call Claude to fix Statements.lean based on assembly precheck errors, then re-run precheck."""
    import sys

    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    current_text = statements_path.read_text(encoding="utf-8") if statements_path.exists() else ""

    # Build diagnostics text from assembly result.
    diag_lines = list(assembly_result.diagnostics)
    if assembly_result.check_result:
        stderr = str(assembly_result.check_result.get("stderr", "")).strip()
        stdout = str(assembly_result.check_result.get("stdout", "")).strip()
        if stderr:
            diag_lines.append(stderr[:3000])
        if stdout:
            diag_lines.append(stdout[:1000])
    diagnostics_text = "\n".join(diag_lines)

    prompt = build_assembly_repair_prompt(
        bundle,
        decl_naming.to_prompt_naming(),
        current_statements_text=current_text,
        assembly_diagnostics_text=diagnostics_text,
        repair_round=repair_round,
        lean4_skills_refs=lean4_skills_refs,
    )

    repair_dir = run_paths.run_root / "assembly_repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    write_text(repair_dir / f"statements_before_round_{repair_round:02d}.lean", current_text)

    claude_result = claude_runner.run_prompt(
        run_paths=run_paths,
        prompt=prompt,
        phase_name=f"phase03_assembly_repair_r{repair_round:02d}",
        model=model,
        timeout_seconds=runtime_config.claude.timeout_seconds,
        permission_mode="bypassPermissions",
        allowed_tools="mcp,Read,Write,Edit,Glob,Grep,Bash",
    )

    # Read Statements.lean after Claude's turn (Claude may have updated it via MCP tools).
    new_text = statements_path.read_text(encoding="utf-8") if statements_path.exists() else ""

    # If Claude didn't update the file, try to extract from text response.
    if new_text.strip() == current_text.strip():
        response_text = extract_result_text(claude_result)
        if response_text.strip():
            extracted = normalize_lean_text(response_text)
            if extracted.strip():
                new_text = extracted

    # Reject if empty or has disallowed tokens.
    disallowed = find_disallowed_statement_tokens(new_text)
    if disallowed or not new_text.strip():
        write_text(statements_path, current_text)
        print(
            f"[lean_engine] assembly repair round {repair_round}: rejected "
            f"({'disallowed tokens' if disallowed else 'empty response'}), restoring original",
            file=sys.stderr, flush=True,
        )
        return assembly_result

    # Write updated Statements.lean.
    write_text(statements_path, new_text)
    write_text(repair_dir / f"statements_after_round_{repair_round:02d}.lean", new_text)

    # Rebuild olean directly (bypass Lake dependency tracker).
    rebuild_result = rebuild_module_olean(
        run_paths.workspace_dir,
        "Orthos/Statements.lean",
        "Orthos.Statements",
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if not rebuild_result.ok:
        print(
            f"[lean_engine] assembly repair round {repair_round}: olean rebuild failed "
            f"(exit {rebuild_result.returncode}), restoring original",
            file=sys.stderr, flush=True,
        )
        write_text(statements_path, current_text)
        return assembly_result

    # Re-run assembly precheck with updated statements.
    assembly_timeout = timeout_seconds
    new_result = run_assembly_precheck(
        run_paths=run_paths,
        bundle=bundle,
        decl_naming=decl_naming,
        timeout_seconds=assembly_timeout,
        runner=runner,
        lake_jobs=runtime_config.lean.lake_jobs,
    )
    print(
        f"[lean_engine] assembly repair round {repair_round}: result={new_result.status}",
        file=sys.stderr, flush=True,
    )
    return new_result


def load_normalized_bundle(path: Path) -> NormalizedProblemBundle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = normalize_problem_artifact_result(payload, source_name=str(path))
    if isinstance(result, FatalResult):
        rendered = json.dumps(result.to_dict(), ensure_ascii=True, sort_keys=True)
        raise ValueError(f"normalized input failed contract validation: {rendered}")
    return result.data
