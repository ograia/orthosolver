from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .artifact_io import create_run_paths, load_run_paths, write_json
from .claude_runner import ClaudeRunner
from .config import RuntimeConfig, load_runtime_config
from .integrations import lean_lsp_mcp
from .lean_checks import (
    check_lean_file,
    check_lean_project,
    prepare_lean_workspace,
    run_phase02_checks,
    write_diagnostics,
)
from .normalize import normalize_problem_artifact_result, write_normalized_artifact
from .phase04 import load_mock_candidates_dir, run_phase04
from .phase06 import load_mock_root_candidates_dir, run_phase06
from .phase07 import inspect_run, resume_phase07, run_phase07
from .phase03 import load_normalized_bundle, run_phase03
from .result_types import FatalResult
from .semantic_phase import run_phase05
from .service.app import run_http_service
from .workspace import (
    compute_cache_key,
    create_workspace_from_template,
    link_lake_cache,
    resolve_cache_lake_dir,
    snapshot_workspace,
    write_project_mcp_config,
)


def _add_integration_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-lean-lsp-mcp-root", default=None, help="override lean-lsp-mcp-main repo root")
    parser.add_argument("--lean4-skills-root", default=None, help="override lean4-skills plugin root")


def _add_claude_api_key_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--claude-api-key",
        default=None,
        help="Claude API key override (sets ANTHROPIC_API_KEY for this command only)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lean-engine", description="Lean engine utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    normalize_parser = subparsers.add_parser("normalize", help="normalize an NL artifact")
    normalize_parser.add_argument("input", nargs="?", help="input path or raw text")
    normalize_parser.add_argument(
        "--input-kind",
        choices=["auto", "path", "text", "json"],
        default="auto",
        help="how to interpret the input argument",
    )
    normalize_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read artifact text from stdin",
    )
    normalize_parser.add_argument(
        "--source-name",
        default=None,
        help="optional source label stored in provenance",
    )
    normalize_parser.add_argument(
        "--artifact-root",
        default=".artifacts/normalize",
        help="default output root when --output is not set",
    )
    normalize_parser.add_argument(
        "--output",
        default=None,
        help="explicit output path for normalized JSON",
    )
    normalize_parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable command result JSON",
    )

    init_parser = subparsers.add_parser("runtime-init", help="create a Phase 02 runtime workspace")
    init_parser.add_argument("problem_id", help="problem identifier for artifact layout")
    init_parser.add_argument("--config", default=None, help="optional runtime config path")
    init_parser.add_argument("--artifact-root", default=None, help="override artifacts root")
    init_parser.add_argument("--model", default=None, help="override Claude primary model")
    init_parser.add_argument("--fallback-model", default=None, help="override Claude fallback model")
    _add_claude_api_key_flag(init_parser)
    init_parser.add_argument("--mcp-command", default=None, help="override MCP server command")
    _add_integration_flags(init_parser)
    init_parser.add_argument("--template-dir", default=None, help="override Lean template directory")
    init_parser.add_argument(
        "--normalized-input",
        default=None,
        help="optional normalized JSON path to copy into this run directory",
    )
    init_parser.add_argument(
        "--prepare-workspace",
        action="store_true",
        help="run deterministic Lean project preparation checks during runtime-init",
    )
    init_parser.add_argument(
        "--workspace-timeout",
        type=int,
        default=180,
        help="timeout seconds for workspace preparation checks (<=0 disables timeout)",
    )
    init_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    check_parser = subparsers.add_parser("runtime-check", help="run deterministic Lean checks")
    check_parser.add_argument("run_dir", help="run directory created by runtime-init")
    check_parser.add_argument(
        "--scope",
        choices=["all", "files", "project"],
        default="all",
        help="which deterministic checks to run",
    )
    check_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="per-command timeout seconds (<=0 disables timeout)",
    )
    check_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    claude_parser = subparsers.add_parser("runtime-claude", help="run Claude with stream-json artifacts")
    claude_parser.add_argument("run_dir", help="run directory created by runtime-init")
    claude_parser.add_argument("phase_name", help="phase label for prompt/output artifact naming")
    claude_parser.add_argument("--prompt", default=None, help="prompt text")
    claude_parser.add_argument("--prompt-file", default=None, help="path to prompt text file")
    claude_parser.add_argument("--model", default=None, help="override Claude model")
    claude_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="override timeout in seconds (<=0 disables timeout)",
    )
    _add_claude_api_key_flag(claude_parser)
    claude_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    inspect_parser = subparsers.add_parser("runtime-inspect", help="inspect run artifact layout")
    inspect_parser.add_argument("run_dir", help="run directory created by runtime-init")
    inspect_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    phase07_run_parser = subparsers.add_parser("run", help="run full Phase 01-06 pipeline with Phase 07 summary")
    phase07_run_parser.add_argument("input", nargs="?", help="input path or raw text")
    phase07_run_parser.add_argument(
        "--input-kind",
        choices=["auto", "path", "text", "json"],
        default="auto",
        help="how to interpret the input argument",
    )
    phase07_run_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read artifact text from stdin",
    )
    phase07_run_parser.add_argument(
        "--source-name",
        default=None,
        help="optional source label stored in normalization provenance",
    )
    phase07_run_parser.add_argument("--config", default=None, help="optional runtime config path")
    phase07_run_parser.add_argument("--artifact-root", default=None, help="override artifacts root")
    phase07_run_parser.add_argument("--model", default=None, help="override Claude model")
    phase07_run_parser.add_argument("--fallback-model", default=None, help="override Claude fallback model")
    _add_claude_api_key_flag(phase07_run_parser)
    phase07_run_parser.add_argument("--mcp-command", default=None, help="override MCP server command")
    _add_integration_flags(phase07_run_parser)
    phase07_run_parser.add_argument("--template-dir", default=None, help="override Lean template directory")
    phase07_run_parser.add_argument("--max-repair-rounds", type=int, default=2, help="max statement repair rounds")
    phase07_run_parser.add_argument("--max-attempts-per-lemma", type=int, default=5, help="bounded retries per lemma")
    phase07_run_parser.add_argument(
        "--parallel-lemmas",
        action="store_true",
        help="enable architecture-driven lemma parallelization (auto fan-out by ready lemmas)",
    )
    phase07_run_parser.add_argument(
        "--lemma-workers",
        type=int,
        default=1,
        help="optional cap for parallel lemma workers when --parallel-lemmas is enabled",
    )
    phase07_run_parser.add_argument(
        "--max-semantic-repairs",
        type=int,
        default=1,
        help="max bounded semantic repair rounds after first mismatch",
    )
    phase07_run_parser.add_argument("--max-root-attempts", type=int, default=4, help="bounded root assembly retries")
    phase07_run_parser.add_argument(
        "--lemma-id",
        default=None,
        help="optional single lemma_id target; when set, run stops after semantic lemma checks",
    )
    phase07_run_parser.add_argument(
        "--lemma-only",
        action="store_true",
        help="stop after Phase 05 lemma statement/proof formalization (skip root assembly)",
    )
    phase07_run_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "global timeout override seconds (applies to all timeout slots; "
            "<=0 disables timeouts; default uses input policy/runtime config)"
        ),
    )
    phase07_run_parser.add_argument(
        "--workspace-timeout",
        type=int,
        default=None,
        help="workspace preparation timeout override seconds (<=0 disables timeout; default uses timeout policy)",
    )
    phase07_run_parser.add_argument(
        "--timeout-policy-file",
        default=None,
        help=(
            "optional JSON file with `engine_controls.timeouts` or top-level `timeouts` to override phase timeouts"
        ),
    )
    phase07_run_parser.add_argument(
        "--lake-jobs",
        type=int,
        default=0,
        help="cap Lake build parallelism (0=use all cores; >0=limit to N threads). Use with concurrent runs.",
    )
    phase07_run_parser.add_argument(
        "--require-cache",
        action="store_true",
        help="abort if Mathlib oleans are missing (prevents accidental 60-min full rebuilds)",
    )
    phase07_run_parser.add_argument(
        "--statements-file",
        default=None,
        help="optional local Lean file to seed Orthos/Statements.lean (skips initial Claude draft)",
    )
    phase07_run_parser.add_argument(
        "--mock-candidates-dir",
        default=None,
        help=(
            "optional directory of mock lemma candidate files; each subdir is lemma_id with files named "
            "round_01.lean, round_02.lean, ..."
        ),
    )
    phase07_run_parser.add_argument(
        "--mock-equivalence-verdicts",
        default=None,
        help="optional JSON file mapping lemma_id -> [equivalence verdict objects]",
    )
    phase07_run_parser.add_argument(
        "--mock-gap-classifications",
        default=None,
        help="optional JSON file mapping lemma_id -> gap classification object",
    )
    phase07_run_parser.add_argument(
        "--resume",
        default=None,
        help="resume from a previous run directory (skips completed phases, retries failed lemmas)",
    )
    phase07_run_parser.add_argument(
        "--mock-root-candidates-dir",
        default=None,
        help="optional directory with root round candidates: round_01.lean, round_02.lean, ...",
    )
    phase07_run_parser.add_argument(
        "--output-dir",
        default=None,
        help="copy final Combined.lean to this directory on success (named <problem_short>.lean, e.g. a1.lean)",
    )
    phase07_run_parser.add_argument(
        "--no-lean4-refs",
        action="store_true",
        help="skip injecting lean4-skills reference docs into prompts (saves ~13K lines / ~32K tokens per prompt)",
    )
    phase07_run_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    inspect_phase_parser = subparsers.add_parser("inspect", help="inspect full run phase artifacts and statuses")
    inspect_phase_parser.add_argument("run_dir", help="run directory created by runtime-init or run")
    inspect_phase_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    phase03_parser = subparsers.add_parser("phase03-run", help="run Phase 03 statement formalization pipeline")
    phase03_parser.add_argument("run_dir", help="run directory created by runtime-init")
    phase03_parser.add_argument(
        "--normalized-input",
        default=None,
        help="override normalized bundle path (defaults to run_dir/normalized_problem.json)",
    )
    phase03_parser.add_argument("--max-repair-rounds", type=int, default=2, help="max statement repair rounds")
    phase03_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="timeout seconds for each Lean/Claude call (<=0 disables timeout)",
    )
    phase03_parser.add_argument("--model", default=None, help="override Claude model")
    _add_claude_api_key_flag(phase03_parser)
    _add_integration_flags(phase03_parser)
    phase03_parser.add_argument(
        "--statements-file",
        default=None,
        help="optional local Lean file to seed Orthos/Statements.lean (skips initial Claude draft)",
    )
    phase03_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    phase04_parser = subparsers.add_parser("phase04-run", help="run Phase 04 lemma formalization loop")
    phase04_parser.add_argument("run_dir", help="run directory created by runtime-init")
    phase04_parser.add_argument(
        "--normalized-input",
        default=None,
        help="override normalized bundle path (defaults to run_dir/normalized_problem.json)",
    )
    phase04_parser.add_argument(
        "--pinned-signatures",
        default=None,
        help="override pinned signatures path (defaults to run_dir/pinned_signatures.json)",
    )
    phase04_parser.add_argument("--max-attempts-per-lemma", type=int, default=5, help="bounded retries per lemma")
    phase04_parser.add_argument(
        "--parallel-lemmas",
        action="store_true",
        help="enable architecture-driven lemma parallelization (auto fan-out by ready lemmas)",
    )
    phase04_parser.add_argument(
        "--lemma-workers",
        type=int,
        default=1,
        help="optional cap for parallel lemma workers when --parallel-lemmas is enabled",
    )
    phase04_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="timeout seconds per attempt (<=0 disables timeout)",
    )
    phase04_parser.add_argument("--model", default=None, help="override Claude model")
    _add_claude_api_key_flag(phase04_parser)
    _add_integration_flags(phase04_parser)
    phase04_parser.add_argument(
        "--mock-candidates-dir",
        default=None,
        help=(
            "optional directory of mock candidate files; each subdir is lemma_id with files named "
            "round_01.lean, round_02.lean, ..."
        ),
    )
    phase04_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    phase05_parser = subparsers.add_parser(
        "phase05-run",
        help="run Phase 05 semantic guards and fatal-gap classification",
    )
    phase05_parser.add_argument("run_dir", help="run directory created by runtime-init")
    phase05_parser.add_argument(
        "--normalized-input",
        default=None,
        help="override normalized bundle path (defaults to run_dir/normalized_problem.json)",
    )
    phase05_parser.add_argument(
        "--pinned-signatures",
        default=None,
        help="override pinned signatures path (defaults to run_dir/pinned_signatures.json)",
    )
    phase05_parser.add_argument(
        "--trusted-manifest",
        default=None,
        help="override trusted manifest path (defaults to run_dir/trusted_context_manifest.json)",
    )
    phase05_parser.add_argument(
        "--phase04-summary",
        default=None,
        help="override Phase 04 summary path (defaults to run_dir/summaries/phase04_summary.json)",
    )
    phase05_parser.add_argument(
        "--max-semantic-repairs",
        type=int,
        default=1,
        help="max bounded semantic repair rounds after first mismatch",
    )
    phase05_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="timeout seconds per Claude call (<=0 disables timeout)",
    )
    phase05_parser.add_argument("--model", default=None, help="override Claude model")
    _add_claude_api_key_flag(phase05_parser)
    phase05_parser.add_argument(
        "--mock-equivalence-verdicts",
        default=None,
        help="optional JSON file mapping lemma_id -> [equivalence verdict objects]",
    )
    phase05_parser.add_argument(
        "--mock-gap-classifications",
        default=None,
        help="optional JSON file mapping lemma_id -> gap classification object",
    )
    phase05_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    phase06_parser = subparsers.add_parser(
        "phase06-run",
        help="run Phase 06 root assembly and final success bundle generation",
    )
    phase06_parser.add_argument("run_dir", help="run directory created by runtime-init")
    phase06_parser.add_argument(
        "--normalized-input",
        default=None,
        help="override normalized bundle path (defaults to run_dir/normalized_problem.json)",
    )
    phase06_parser.add_argument(
        "--pinned-signatures",
        default=None,
        help="override pinned signatures path (defaults to run_dir/pinned_signatures.json)",
    )
    phase06_parser.add_argument(
        "--semantic-manifest",
        default=None,
        help="override semantic manifest path (defaults to run_dir/trusted_context_semantic_manifest.json)",
    )
    phase06_parser.add_argument(
        "--phase05-summary",
        default=None,
        help="override Phase 05 summary path (defaults to run_dir/summaries/phase05_summary.json)",
    )
    phase06_parser.add_argument("--max-root-attempts", type=int, default=4, help="bounded root assembly retries")
    phase06_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="timeout seconds per Claude/Lean call (<=0 disables timeout)",
    )
    phase06_parser.add_argument("--model", default=None, help="override Claude model")
    _add_claude_api_key_flag(phase06_parser)
    _add_integration_flags(phase06_parser)
    phase06_parser.add_argument(
        "--mock-root-candidates-dir",
        default=None,
        help="optional directory with root round candidates: round_01.lean, round_02.lean, ...",
    )
    phase06_parser.add_argument("--json", action="store_true", help="print machine-readable summary")

    service_parser = subparsers.add_parser("service", help="run Phase 08 local service wrapper")
    service_parser.add_argument("--host", default="127.0.0.1", help="bind host for HTTP service")
    service_parser.add_argument("--port", type=int, default=8081, help="bind port for HTTP service")
    service_parser.add_argument(
        "--db-path",
        default=".artifacts/lean_engine/service/jobs.sqlite3",
        help="SQLite path for job metadata",
    )
    service_parser.add_argument("--max-workers", type=int, default=2, help="max concurrent background jobs")
    service_parser.add_argument("--config", default=None, help="optional runtime config path")
    service_parser.add_argument("--artifact-root", default=None, help="default artifacts root for submitted jobs")
    service_parser.add_argument("--model", default=None, help="default Claude model for submitted jobs")
    service_parser.add_argument("--fallback-model", default=None, help="default fallback Claude model")
    _add_claude_api_key_flag(service_parser)
    service_parser.add_argument("--mcp-command", default=None, help="default MCP command override")
    _add_integration_flags(service_parser)

    cache_warmup_parser = subparsers.add_parser(
        "cache-warmup",
        help="pre-build Lean workspace cache (compiles Mathlib once for reuse across runs)",
    )
    cache_warmup_parser.add_argument(
        "--cache-dir",
        default=None,
        help="cache directory (default: .artifacts/lean_engine/_lake_cache)",
    )
    cache_warmup_parser.add_argument(
        "--template-dir",
        default=None,
        help="template directory override",
    )
    cache_warmup_parser.add_argument(
        "--config",
        default=None,
        help="optional runtime config path",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_cli_api_key_override(args)

    if args.command == "normalize":
        return _run_normalize(args)
    if args.command == "runtime-init":
        return _run_runtime_init(args)
    if args.command == "runtime-check":
        return _run_runtime_check(args)
    if args.command == "runtime-claude":
        return _run_runtime_claude(args)
    if args.command == "runtime-inspect":
        return _run_runtime_inspect(args)
    if args.command == "run":
        return _run_phase07_run(args)
    if args.command == "inspect":
        return _run_inspect(args)
    if args.command == "phase03-run":
        return _run_phase03_run(args)
    if args.command == "phase04-run":
        return _run_phase04_run(args)
    if args.command == "phase05-run":
        return _run_phase05_run(args)
    if args.command == "phase06-run":
        return _run_phase06_run(args)
    if args.command == "service":
        return _run_service(args)
    if args.command == "cache-warmup":
        return _run_cache_warmup(args)

    parser.error(f"unsupported command: {args.command}")
    return 2


def _apply_cli_api_key_override(args: argparse.Namespace) -> None:
    key = getattr(args, "claude_api_key", None)
    if key is None:
        return
    stripped = str(key).strip()
    if not stripped:
        return
    os.environ["ANTHROPIC_API_KEY"] = stripped


def _run_normalize(args: argparse.Namespace) -> int:
    try:
        source = _resolve_cli_source(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = normalize_problem_artifact_result(source, source_name=args.source_name)
    if isinstance(result, FatalResult):
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=True, sort_keys=True))
        return 1

    output_path = write_normalized_artifact(
        result.data,
        artifact_root=Path(args.artifact_root),
        output_path=Path(args.output).expanduser() if args.output else None,
    )

    summary = {
        "status": "ok",
        "problem_id": result.data.problem_id,
        "lemma_count": result.data.lemma_count,
        "assembly_step_count": result.data.assembly_step_count,
        "output_path": str(output_path),
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(f"normalized {result.data.problem_id} -> {output_path}")
    return 0


def _run_runtime_init(args: argparse.Namespace) -> int:
    try:
        runtime_config = _load_runtime_config_from_args(
            args,
            config_path=Path(args.config).expanduser() if args.config else None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_paths = create_run_paths(args.problem_id, artifacts_root=runtime_config.artifacts.root)

    template_dir = Path(args.template_dir).expanduser() if args.template_dir else None
    try:
        workspace_result = create_workspace_from_template(
            run_paths.workspace_dir,
            template_dir=template_dir,
            runtime_config=runtime_config,
        )
        workspace_files = workspace_result.files
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    mcp_config_path = None
    if runtime_config.mcp.enabled:
        mcp_config_path = write_project_mcp_config(
            run_paths.workspace_dir,
            runtime_config,
            mcp_log_dir=run_paths.run_root / ".mcp_logs",
        )

    if args.normalized_input:
        source = Path(args.normalized_input).expanduser().resolve()
        if not source.exists() or not source.is_file():
            print(f"normalized input path is not a file: {source}", file=sys.stderr)
            return 2
        shutil.copy2(source, run_paths.normalized_problem_path)

    write_json(run_paths.runtime_config_path, runtime_config.to_dict())
    snapshot = snapshot_workspace(run_paths.workspace_dir)
    write_json(run_paths.workspace_snapshot_path, snapshot)
    workspace_preparation_path = run_paths.summaries_dir / "phase02_workspace_preparation.json"
    workspace_preparation = None
    if args.prepare_workspace:
        workspace_preparation = prepare_lean_workspace(
            run_paths.workspace_dir,
            timeout_seconds=args.workspace_timeout,
        )
        write_json(workspace_preparation_path, workspace_preparation.to_dict())
    integrations_payload = _collect_integration_inventory_and_preflight(
        runtime_config=runtime_config,
        workspace_root=run_paths.workspace_dir,
    )
    integrations_dir = run_paths.run_root / "integrations"
    write_json(integrations_dir / "inventory.json", {"inventory": integrations_payload["inventory"]})
    write_json(integrations_dir / "preflight.json", {"preflight": integrations_payload["preflight"]})

    summary = {
        "status": "ok" if workspace_preparation is None or workspace_preparation.status == "ok" else "failed",
        "problem_id": run_paths.problem_id,
        "run_name": run_paths.run_name,
        "run_root": str(run_paths.run_root),
        "workspace": workspace_files.to_dict(),
        "mcp_config_path": str(mcp_config_path) if mcp_config_path else None,
        "runtime_config_path": str(run_paths.runtime_config_path),
        "workspace_snapshot_path": str(run_paths.workspace_snapshot_path),
        "integration_inventory_path": str(integrations_dir / "inventory.json"),
        "integration_preflight_path": str(integrations_dir / "preflight.json"),
        "workspace_preparation_path": str(workspace_preparation_path) if args.prepare_workspace else None,
        "workspace_preparation": workspace_preparation.to_dict() if workspace_preparation else None,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(f"initialized runtime workspace: {run_paths.run_root}")

    return 0 if summary["status"] == "ok" else 1


def _run_phase07_run(args: argparse.Namespace) -> int:
    # --- Resume mode ---
    if args.resume:
        resume_dir = Path(args.resume).expanduser().resolve()
        if not resume_dir.exists() or not resume_dir.is_dir():
            print(f"resume directory does not exist: {resume_dir}", file=sys.stderr)
            return 2
        try:
            runtime_config = _load_runtime_config_from_args(
                args,
                config_path=Path(args.config).expanduser() if args.config else None,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            result = resume_phase07(
                run_dir=resume_dir,
                runtime_config=runtime_config,
                max_attempts_per_lemma=args.max_attempts_per_lemma,
                parallel_lemmas=bool(args.parallel_lemmas),
                lemma_workers=args.lemma_workers,
                max_semantic_repairs=args.max_semantic_repairs,
                max_root_attempts=args.max_root_attempts,
                timeout_seconds=args.timeout,
                timeout_policy_path=(
                    Path(args.timeout_policy_file).expanduser().resolve()
                    if args.timeout_policy_file
                    else None
                ),
                model=args.model,
                target_lemma_id=args.lemma_id,
                stop_after_semantic=bool(args.lemma_only),
            )
        except ValueError as exc:
            _emit_structured_fatal(
                error_class="invalid_resume_options",
                error_scope="phase07",
                message="Resume validation failed.",
                diagnostics=(str(exc),),
                use_json=args.json,
            )
            return 1
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            _emit_structured_fatal(
                error_class="unknown_fatal",
                error_scope="phase07",
                message="Resume execution failed due to an unexpected internal error.",
                diagnostics=(f"{type(exc).__name__}: {exc}",),
                use_json=args.json,
            )
            return 1
        summary = result.to_dict()
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
        else:
            print(
                f"resume status={summary['status']} "
                f"problem_id={summary['problem_id']} "
                f"run={summary['run_root']} "
                f"fatal_error_class={summary['fatal_error_class']}"
            )
        return 0 if result.status == "ok" else 1

    try:
        source = _resolve_cli_source(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        runtime_config = _load_runtime_config_from_args(
            args,
            config_path=Path(args.config).expanduser() if args.config else None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # --no-lean4-refs: disable lean4-skills reference doc injection into prompts.
    if getattr(args, "no_lean4_refs", False):
        import dataclasses as _dc
        from .config import IntegrationsConfig
        runtime_config = _dc.replace(
            runtime_config,
            integrations=IntegrationsConfig(
                lean_lsp_mcp_root=runtime_config.integrations.lean_lsp_mcp_root,
                lean4_skills_root=Path("/dev/null/no-lean4-refs"),
            ),
        )

    provided_statements_text = None
    if args.statements_file:
        statements_file = Path(args.statements_file).expanduser().resolve()
        if not statements_file.exists() or not statements_file.is_file():
            print(f"statements file does not exist: {statements_file}", file=sys.stderr)
            return 2
        provided_statements_text = statements_file.read_text(encoding="utf-8")

    mock_candidates = None
    if args.mock_candidates_dir:
        try:
            mock_candidates = load_mock_candidates_dir(Path(args.mock_candidates_dir))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    mock_equivalence_verdicts = None
    if args.mock_equivalence_verdicts:
        try:
            mock_equivalence_verdicts = _load_json_mapping_file(Path(args.mock_equivalence_verdicts))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    mock_gap_classifications = None
    if args.mock_gap_classifications:
        try:
            mock_gap_classifications = _load_json_mapping_file(Path(args.mock_gap_classifications))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    mock_root_candidates = None
    if args.mock_root_candidates_dir:
        try:
            mock_root_candidates = load_mock_root_candidates_dir(Path(args.mock_root_candidates_dir))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        result = run_phase07(
            source=source,
            source_name=args.source_name,
            runtime_config=runtime_config,
            template_dir=Path(args.template_dir).expanduser() if args.template_dir else None,
            max_repair_rounds=args.max_repair_rounds,
            max_attempts_per_lemma=args.max_attempts_per_lemma,
            parallel_lemmas=bool(args.parallel_lemmas),
            lemma_workers=args.lemma_workers,
            max_semantic_repairs=args.max_semantic_repairs,
            max_root_attempts=args.max_root_attempts,
            timeout_seconds=args.timeout,
            workspace_timeout_seconds=args.workspace_timeout,
            model=args.model,
            target_lemma_id=args.lemma_id,
            stop_after_semantic=bool(args.lemma_only),
            provided_statements_text=provided_statements_text,
            mock_candidates=mock_candidates,
            mock_equivalence_verdicts=mock_equivalence_verdicts,
            mock_gap_classifications=mock_gap_classifications,
            mock_root_candidates=mock_root_candidates,
            timeout_policy_path=(
                Path(args.timeout_policy_file).expanduser().resolve()
                if args.timeout_policy_file
                else None
            ),
            require_cache=bool(getattr(args, "require_cache", False)),
        )
    except ValueError as exc:
        _emit_structured_fatal(
            error_class="invalid_phase_options",
            error_scope="phase07",
            message="Phase 07 validation failed before execution.",
            diagnostics=(str(exc),),
            use_json=args.json,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        _emit_structured_fatal(
            error_class="unknown_fatal",
            error_scope="phase07",
            message="Phase 07 execution failed due to an unexpected internal error.",
            diagnostics=(f"{type(exc).__name__}: {exc}",),
            use_json=args.json,
        )
        return 1

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(
            f"run status={summary['status']} "
            f"problem_id={summary['problem_id']} "
            f"run={summary['run_root']} "
            f"fatal_error_class={summary['fatal_error_class']}"
        )

    # Copy Combined.lean to --output-dir on success.
    output_dir = getattr(args, "output_dir", None)
    if output_dir and result.status == "ok" and result.run_root:
        combined_src = result.run_root / "final" / "Combined.lean"
        if combined_src.exists():
            out_path = Path(output_dir).expanduser().resolve()
            out_path.mkdir(parents=True, exist_ok=True)
            # Derive short name from input file (e.g. "a2.json" -> "a2")
            short_name = None
            input_arg = getattr(args, "input", None) or getattr(args, "resume", None)
            if input_arg:
                stem = Path(input_arg).stem
                # For resume paths like .../a2__prob_.../run_003, extract from dir name
                if stem.startswith("run_"):
                    parent_name = Path(input_arg).parent.name
                    short_name = parent_name.split("__")[0] if "__" in parent_name else parent_name
                else:
                    short_name = stem
            if not short_name:
                short_name = result.problem_id or "combined"
            dest = out_path / f"{short_name}.lean"
            shutil.copy2(combined_src, dest)
            print(f"Combined.lean -> {dest}")

    return 0 if result.status == "ok" else 1


def _run_inspect(args: argparse.Namespace) -> int:
    try:
        summary = inspect_run(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        final_status = None
        if isinstance(summary.get("final_result"), dict):
            final_status = summary["final_result"].get("status")
        print(
            f"inspect run={summary['run_root']} "
            f"phase07={summary['phase_statuses'].get('phase07', {}).get('status')} "
            f"final_status={final_status}"
        )
    return 0


def _run_runtime_check(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    workspace_root = run_paths.workspace_dir

    if args.scope == "all":
        results = run_phase02_checks(workspace_root, timeout_seconds=args.timeout)
    elif args.scope == "files":
        results = [
            check_lean_file(workspace_root, "Orthos/Statements.lean", timeout_seconds=args.timeout),
            check_lean_file(workspace_root, "Orthos/Lemmas.lean", timeout_seconds=args.timeout),
            check_lean_file(workspace_root, "Orthos/Root.lean", timeout_seconds=args.timeout),
        ]
    else:
        results = [check_lean_project(workspace_root, timeout_seconds=args.timeout)]

    written_paths = write_diagnostics(results, run_paths)
    passed = all(item.ok for item in results)

    summary = {
        "status": "ok" if passed else "failed",
        "run_root": str(run_paths.run_root),
        "passed": passed,
        "results": [result.to_dict() for result in results],
        "diagnostics": [str(path) for path in written_paths],
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(f"runtime-check status={summary['status']} run={run_paths.run_root}")

    return 0 if passed else 1


def _run_runtime_claude(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        runtime_config = _load_runtime_config_from_args(args, config_path=run_paths.runtime_config_path)
    except ValueError as exc:
        print(f"failed to load runtime config from run directory: {exc}", file=sys.stderr)
        return 2

    try:
        prompt = _resolve_prompt_text(prompt=args.prompt, prompt_file=args.prompt_file)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    runner = ClaudeRunner(runtime_config)
    try:
        result = runner.run_prompt(
            run_paths=run_paths,
            prompt=prompt,
            phase_name=args.phase_name,
            model=args.model,
            timeout_seconds=args.timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        _emit_structured_fatal(
            error_class="unknown_fatal",
            error_scope="runtime",
            message="runtime-claude failed due to an unexpected internal error.",
            diagnostics=(f"{type(exc).__name__}: {exc}",),
            use_json=args.json,
        )
        return 1

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(f"runtime-claude returncode={result.returncode} raw={result.raw_output_path}")

    return 0 if result.ok else 1


def _run_runtime_inspect(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    paths = {
        "run_root": run_paths.run_root,
        "workspace": run_paths.workspace_dir,
        "runtime_config": run_paths.runtime_config_path,
        "workspace_snapshot": run_paths.workspace_snapshot_path,
        "prompts": run_paths.prompts_dir,
        "claude_raw": run_paths.claude_raw_dir,
        "diagnostics": run_paths.diagnostics_dir,
        "summaries": run_paths.summaries_dir,
    }

    summary = {
        "status": "ok",
        "run_name": run_paths.run_name,
        "problem_id": run_paths.problem_id,
        "paths": {name: str(path) for name, path in paths.items()},
        "exists": {name: path.exists() for name, path in paths.items()},
        "counts": {
            "prompt_files": _file_count(run_paths.prompts_dir),
            "claude_raw_files": _file_count(run_paths.claude_raw_dir),
            "diagnostic_files": _file_count(run_paths.diagnostics_dir),
            "summary_files": _file_count(run_paths.summaries_dir),
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(f"runtime-inspect run={run_paths.run_root} prompts={summary['counts']['prompt_files']}")
    return 0


def _run_service(args: argparse.Namespace) -> int:
    if args.port <= 0 or args.port > 65535:
        print("port must be in range 1..65535", file=sys.stderr)
        return 2
    if args.max_workers <= 0:
        print("max-workers must be > 0", file=sys.stderr)
        return 2

    try:
        run_http_service(
            host=args.host,
            port=args.port,
            db_path=Path(args.db_path).expanduser().resolve(),
            max_workers=args.max_workers,
            config_path=Path(args.config).expanduser().resolve() if args.config else None,
            model=args.model,
            fallback_model=args.fallback_model,
            artifact_root=Path(args.artifact_root).expanduser().resolve() if args.artifact_root else None,
            mcp_command=args.mcp_command,
            repo_lean_lsp_mcp_root=(
                Path(args.repo_lean_lsp_mcp_root).expanduser().resolve()
                if args.repo_lean_lsp_mcp_root
                else None
            ),
            lean4_skills_root=(
                Path(args.lean4_skills_root).expanduser().resolve() if args.lean4_skills_root else None
            ),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"failed to start service: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_phase03_run(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        runtime_config = _load_runtime_config_from_args(args, config_path=run_paths.runtime_config_path)
    except ValueError as exc:
        print(f"failed to load runtime config from run directory: {exc}", file=sys.stderr)
        return 2

    normalized_input_path = (
        Path(args.normalized_input).expanduser().resolve() if args.normalized_input else run_paths.normalized_problem_path
    )
    if not normalized_input_path.exists() or not normalized_input_path.is_file():
        print(f"normalized input path is not a file: {normalized_input_path}", file=sys.stderr)
        return 2

    try:
        bundle = load_normalized_bundle(normalized_input_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    provided_statements_text = None
    if args.statements_file:
        statements_file = Path(args.statements_file).expanduser().resolve()
        if not statements_file.exists() or not statements_file.is_file():
            print(f"statements file does not exist: {statements_file}", file=sys.stderr)
            return 2
        provided_statements_text = statements_file.read_text(encoding="utf-8")

    try:
        result = run_phase03(
            run_paths=run_paths,
            runtime_config=runtime_config,
            normalized_bundle=bundle,
            max_repair_rounds=args.max_repair_rounds,
            timeout_seconds=args.timeout,
            model=args.model,
            provided_statements_text=provided_statements_text,
        )
    except ValueError as exc:
        _emit_structured_fatal(
            error_class="invalid_phase_options",
            error_scope="phase03",
            message="Phase 03 validation failed before execution.",
            diagnostics=(str(exc),),
            use_json=args.json,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(
            f"phase03-run status={summary['status']} "
            f"statements={summary['statements_path']} "
            f"pinned={summary['pinned_signatures_path']}"
        )
    return 0 if result.status == "ok" else 1


def _run_phase04_run(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        runtime_config = _load_runtime_config_from_args(args, config_path=run_paths.runtime_config_path)
    except ValueError as exc:
        print(f"failed to load runtime config from run directory: {exc}", file=sys.stderr)
        return 2

    normalized_input_path = (
        Path(args.normalized_input).expanduser().resolve() if args.normalized_input else run_paths.normalized_problem_path
    )
    if not normalized_input_path.exists() or not normalized_input_path.is_file():
        print(f"normalized input path is not a file: {normalized_input_path}", file=sys.stderr)
        return 2

    pinned_signatures_path = (
        Path(args.pinned_signatures).expanduser().resolve()
        if args.pinned_signatures
        else (run_paths.run_root / "pinned_signatures.json")
    )
    if not pinned_signatures_path.exists() or not pinned_signatures_path.is_file():
        print(f"pinned signatures path is not a file: {pinned_signatures_path}", file=sys.stderr)
        return 2

    try:
        bundle = load_normalized_bundle(normalized_input_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mock_candidates = None
    if args.mock_candidates_dir:
        try:
            mock_candidates = load_mock_candidates_dir(Path(args.mock_candidates_dir))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        result = run_phase04(
            run_paths=run_paths,
            runtime_config=runtime_config,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            max_attempts_per_lemma=args.max_attempts_per_lemma,
            parallel_lemmas=bool(args.parallel_lemmas),
            lemma_workers=args.lemma_workers,
            timeout_seconds=args.timeout,
            model=args.model,
            mock_candidates=mock_candidates,
        )
    except ValueError as exc:
        _emit_structured_fatal(
            error_class="invalid_phase_options",
            error_scope="phase04",
            message="Phase 04 validation failed before execution.",
            diagnostics=(str(exc),),
            use_json=args.json,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(
            f"phase04-run status={summary['status']} "
            f"trusted_manifest={summary['trusted_manifest_path']} "
            f"lemmas={len(summary['lemma_results'])}"
        )
    return 0 if result.status == "ok" else 1


def _run_phase05_run(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        runtime_config = _load_runtime_config_from_args(args, config_path=run_paths.runtime_config_path)
    except ValueError as exc:
        print(f"failed to load runtime config from run directory: {exc}", file=sys.stderr)
        return 2

    normalized_input_path = (
        Path(args.normalized_input).expanduser().resolve() if args.normalized_input else run_paths.normalized_problem_path
    )
    if not normalized_input_path.exists() or not normalized_input_path.is_file():
        print(f"normalized input path is not a file: {normalized_input_path}", file=sys.stderr)
        return 2

    pinned_signatures_path = (
        Path(args.pinned_signatures).expanduser().resolve()
        if args.pinned_signatures
        else (run_paths.run_root / "pinned_signatures.json")
    )
    if not pinned_signatures_path.exists() or not pinned_signatures_path.is_file():
        print(f"pinned signatures path is not a file: {pinned_signatures_path}", file=sys.stderr)
        return 2

    trusted_manifest_path = (
        Path(args.trusted_manifest).expanduser().resolve()
        if args.trusted_manifest
        else (run_paths.run_root / "trusted_context_manifest.json")
    )
    if not trusted_manifest_path.exists() or not trusted_manifest_path.is_file():
        print(f"trusted manifest path is not a file: {trusted_manifest_path}", file=sys.stderr)
        return 2

    phase04_summary_path = (
        Path(args.phase04_summary).expanduser().resolve()
        if args.phase04_summary
        else (run_paths.summaries_dir / "phase04_summary.json")
    )
    if not phase04_summary_path.exists() or not phase04_summary_path.is_file():
        print(f"phase04 summary path is not a file: {phase04_summary_path}", file=sys.stderr)
        return 2

    try:
        bundle = load_normalized_bundle(normalized_input_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mock_equivalence_verdicts = None
    if args.mock_equivalence_verdicts:
        try:
            mock_equivalence_verdicts = _load_json_mapping_file(Path(args.mock_equivalence_verdicts))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    mock_gap_classifications = None
    if args.mock_gap_classifications:
        try:
            mock_gap_classifications = _load_json_mapping_file(Path(args.mock_gap_classifications))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        result = run_phase05(
            run_paths=run_paths,
            runtime_config=runtime_config,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            phase04_summary_path=phase04_summary_path,
            max_semantic_repairs=args.max_semantic_repairs,
            timeout_seconds=args.timeout,
            model=args.model,
            mock_equivalence_verdicts=mock_equivalence_verdicts,
            mock_gap_classifications=mock_gap_classifications,
        )
    except ValueError as exc:
        _emit_structured_fatal(
            error_class="invalid_phase_options",
            error_scope="phase05",
            message="Phase 05 validation failed before execution.",
            diagnostics=(str(exc),),
            use_json=args.json,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        _emit_structured_fatal(
            error_class="unknown_fatal",
            error_scope="phase05",
            message="Phase 05 execution failed due to an unexpected internal error.",
            diagnostics=(f"{type(exc).__name__}: {exc}",),
            use_json=args.json,
        )
        return 1

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(
            f"phase05-run status={summary['status']} "
            f"semantic_manifest={summary['semantic_manifest_path']} "
            f"accepted={len(summary['semantically_accepted_lemma_ids'])}"
        )
    return 0 if result.status == "ok" else 1


def _run_phase06_run(args: argparse.Namespace) -> int:
    try:
        run_paths = load_run_paths(Path(args.run_dir))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        runtime_config = _load_runtime_config_from_args(args, config_path=run_paths.runtime_config_path)
    except ValueError as exc:
        print(f"failed to load runtime config from run directory: {exc}", file=sys.stderr)
        return 2

    normalized_input_path = (
        Path(args.normalized_input).expanduser().resolve() if args.normalized_input else run_paths.normalized_problem_path
    )
    if not normalized_input_path.exists() or not normalized_input_path.is_file():
        print(f"normalized input path is not a file: {normalized_input_path}", file=sys.stderr)
        return 2

    pinned_signatures_path = (
        Path(args.pinned_signatures).expanduser().resolve()
        if args.pinned_signatures
        else (run_paths.run_root / "pinned_signatures.json")
    )
    if not pinned_signatures_path.exists() or not pinned_signatures_path.is_file():
        print(f"pinned signatures path is not a file: {pinned_signatures_path}", file=sys.stderr)
        return 2

    semantic_manifest_path = (
        Path(args.semantic_manifest).expanduser().resolve()
        if args.semantic_manifest
        else (run_paths.run_root / "trusted_context_semantic_manifest.json")
    )
    if not semantic_manifest_path.exists() or not semantic_manifest_path.is_file():
        print(f"semantic manifest path is not a file: {semantic_manifest_path}", file=sys.stderr)
        return 2

    phase05_summary_path = (
        Path(args.phase05_summary).expanduser().resolve()
        if args.phase05_summary
        else (run_paths.summaries_dir / "phase05_summary.json")
    )
    if not phase05_summary_path.exists() or not phase05_summary_path.is_file():
        print(f"phase05 summary path is not a file: {phase05_summary_path}", file=sys.stderr)
        return 2

    try:
        bundle = load_normalized_bundle(normalized_input_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mock_root_candidates = None
    if args.mock_root_candidates_dir:
        try:
            mock_root_candidates = load_mock_root_candidates_dir(Path(args.mock_root_candidates_dir))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        result = run_phase06(
            run_paths=run_paths,
            runtime_config=runtime_config,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            max_root_attempts=args.max_root_attempts,
            timeout_seconds=args.timeout,
            model=args.model,
            mock_root_candidates=mock_root_candidates,
        )
    except ValueError as exc:
        _emit_structured_fatal(
            error_class="invalid_phase_options",
            error_scope="phase06",
            message="Phase 06 validation failed before execution.",
            diagnostics=(str(exc),),
            use_json=args.json,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    summary = result.to_dict()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(
            f"phase06-run status={summary['status']} "
            f"root={summary['root_decl_name']} "
            f"result={summary['final_result_path']}"
        )
    return 0 if result.status == "ok" else 1


def _resolve_cli_source(args: argparse.Namespace):
    if args.stdin:
        if args.input:
            raise ValueError("input argument must be omitted when using --stdin")
        text = sys.stdin.read()
        if not text.strip():
            raise ValueError("stdin did not provide any input")
        return text

    if not args.input:
        raise ValueError("input is required unless --stdin is used")

    if args.input_kind == "path":
        return Path(args.input).expanduser()

    if args.input_kind == "text":
        return args.input

    if args.input_kind == "json":
        try:
            parsed = json.loads(args.input)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--input-kind json expects valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--input-kind json requires a top-level JSON object")
        return parsed

    auto_path = Path(args.input).expanduser()
    if auto_path.exists() and auto_path.is_file():
        return auto_path
    return args.input


def _resolve_prompt_text(*, prompt: str | None, prompt_file: str | None) -> str:
    if prompt and prompt_file:
        raise ValueError("use either --prompt or --prompt-file, not both")
    if prompt:
        return prompt
    if prompt_file:
        path = Path(prompt_file).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise ValueError(f"prompt file does not exist: {path}")
        return path.read_text(encoding="utf-8")
    raise ValueError("a prompt is required (--prompt or --prompt-file)")


def _load_runtime_config_from_args(
    args: argparse.Namespace,
    *,
    config_path: Path | None = None,
) -> RuntimeConfig:
    return load_runtime_config(
        config_path,
        model=getattr(args, "model", None),
        fallback_model=getattr(args, "fallback_model", None),
        artifact_root=Path(args.artifact_root).expanduser() if getattr(args, "artifact_root", None) else None,
        mcp_command=getattr(args, "mcp_command", None),
        repo_lean_lsp_mcp_root=Path(args.repo_lean_lsp_mcp_root) if getattr(args, "repo_lean_lsp_mcp_root", None) else None,
        lean4_skills_root=Path(args.lean4_skills_root) if getattr(args, "lean4_skills_root", None) else None,
        lake_jobs=getattr(args, "lake_jobs", None) or None,
    )


def _collect_integration_inventory_and_preflight(
    *,
    runtime_config,
    workspace_root: Path | None,
) -> dict[str, object]:
    lsp_root = runtime_config.integrations.lean_lsp_mcp_root
    inventory_rows = [
        lean_lsp_mcp.inventory(repo_root=lsp_root, mcp_command=runtime_config.mcp.command),
    ]
    preflight_rows = [
        lean_lsp_mcp.preflight(
            repo_root=lsp_root,
            mcp_command=runtime_config.mcp.command,
            workspace_root=workspace_root,
        ),
    ]
    return {
        "inventory": [row.to_dict() for row in inventory_rows],
        "preflight": [row.to_dict() for row in preflight_rows],
    }


def _file_count(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return len([item for item in path.iterdir() if item.is_file()])


def _load_json_mapping_file(path: Path) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"json mapping file does not exist: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"json mapping file is not valid JSON: {resolved} ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"json mapping file must contain an object: {resolved}")
    return payload


def _emit_structured_fatal(
    *,
    error_class: str,
    error_scope: str,
    message: str,
    diagnostics: tuple[str, ...],
    use_json: bool,
) -> None:
    payload = {
        "status": "fatal",
        "error_class": error_class,
        "error_scope": error_scope,
        "message": message,
        "diagnostics": list(diagnostics),
    }
    if use_json:
        print(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _run_cache_warmup(args: argparse.Namespace) -> int:
    import subprocess
    import tempfile
    import time

    from .config import default_template_dir

    config_path = Path(args.config) if args.config else None
    runtime_config = load_runtime_config(config_path)

    template_dir = Path(args.template_dir) if args.template_dir else default_template_dir()
    template_dir = template_dir.expanduser().resolve()
    if not template_dir.exists():
        print(f"[cache-warmup] ERROR: template directory does not exist: {template_dir}", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir) if args.cache_dir else runtime_config.workspace_cache.cache_dir
    cache_dir = cache_dir.expanduser().resolve()

    cache_key = compute_cache_key(template_dir)
    cache_target = cache_dir / cache_key
    cache_lake = cache_target / ".lake"

    if cache_lake.exists():
        print(f"[cache-warmup] Cache already exists at {cache_target}", file=sys.stderr)
        print(f"[cache-warmup] Cache key: {cache_key}", file=sys.stderr)
        print("[cache-warmup] To rebuild, delete the cache directory and re-run.", file=sys.stderr)
        return 0

    print(f"[cache-warmup] Template: {template_dir}", file=sys.stderr)
    lake_bin = shutil.which("lake")
    if lake_bin is None:
        elan_lake = Path.home() / ".elan" / "bin" / "lake"
        if elan_lake.exists():
            lake_bin = str(elan_lake)
        else:
            print("[cache-warmup] ERROR: 'lake' not found. Install Lean 4 via elan first.", file=sys.stderr)
            return 1

    print(f"[cache-warmup] Cache key: {cache_key}", file=sys.stderr)
    print(f"[cache-warmup] Cache target: {cache_target}", file=sys.stderr)
    print(f"[cache-warmup] Using lake: {lake_bin}", file=sys.stderr)
    print("[cache-warmup] Building Lean workspace (this may take 30-60 minutes)...", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="lean_cache_warmup_") as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        shutil.copytree(template_dir, workspace)

        # Stream the build log to stderr so the user can monitor progress
        log_path = cache_dir / f"{cache_key}_warmup.log"
        cache_dir.mkdir(parents=True, exist_ok=True)

        print(f"[cache-warmup] Build log: {log_path}", file=sys.stderr)
        print(f"[cache-warmup] Monitor with: tail -f {log_path}", file=sys.stderr)

        start = time.time()
        with open(log_path, "w") as log_file:
            log_file.write(f"=== cache-warmup build ===\ntemplate: {template_dir}\ncache_key: {cache_key}\n\n")
            log_file.flush()

            proc = subprocess.Popen(
                [lake_bin, "build"],
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
                sys.stderr.write(line)
                sys.stderr.flush()

            proc.wait()

        elapsed = time.time() - start

        if proc.returncode != 0:
            print(f"\n[cache-warmup] ERROR: lake build failed (exit {proc.returncode}) after {elapsed:.1f}s", file=sys.stderr)
            print(f"[cache-warmup] Check log: {log_path}", file=sys.stderr)
            return 1

        # Move .lake/ to cache target
        cache_target.mkdir(parents=True, exist_ok=True)
        src_lake = workspace / ".lake"
        if src_lake.exists():
            shutil.move(str(src_lake), str(cache_lake))

        # Write cache metadata
        meta = {
            "cache_key": cache_key,
            "template_dir": str(template_dir),
            "build_duration_seconds": round(elapsed, 2),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        meta_path = cache_target / "cache_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")

        print(f"\n[cache-warmup] SUCCESS: cache built in {elapsed:.1f}s", file=sys.stderr)
        print(f"[cache-warmup] Cache stored at: {cache_target}", file=sys.stderr)

    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
