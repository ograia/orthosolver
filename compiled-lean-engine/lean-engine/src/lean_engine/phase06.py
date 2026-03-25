from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .artifact_io import RunPaths, sanitize_component, workspace_file_digest, write_json, write_text
from .assembly_phase import validate_assembly_plan
from .claude_runner import ClaudeRunResult, ClaudeRunner
from .config import RuntimeConfig
from .contracts import NormalizedProblemBundle
from .final_output import write_fatal_output_bundle, write_success_output_bundle
from .lean_checks import LeanCommandResult, check_lean_file
from .lemma_phase import load_pinned_lemma_signatures, load_trusted_manifest
from .lean4_skills_refs import get_all_proving_refs
from .lean4_skills_scripts import check_axioms
from .statement_phase import Phase03DeclNaming, extract_statement_signature, normalize_lean_text, summarize_diagnostics_from_check

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PinnedRootSignature:
    decl_name: str
    signature: str
    statement_nl: str


@dataclass(frozen=True)
class RootAssemblyRoundResult:
    round_index: int
    prompt_path: Path
    raw_path: Path
    candidate_path: Path
    diagnostics_path: Path
    status: str
    diagnostics: tuple[str, ...]
    used_mock_candidate: bool
    candidate_backend: str
    candidate_source: str | None = None
    candidate_selection_reasons: tuple[str, ...] = ()
    candidate_lean_score: int | None = None
    root_check_result: dict[str, Any] | None = None
    project_check_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "prompt_path": str(self.prompt_path),
            "raw_path": str(self.raw_path),
            "candidate_path": str(self.candidate_path),
            "diagnostics_path": str(self.diagnostics_path),
            "status": self.status,
            "diagnostics": list(self.diagnostics),
            "used_mock_candidate": self.used_mock_candidate,
            "candidate_backend": self.candidate_backend,
            "candidate_source": self.candidate_source,
            "candidate_selection_reasons": list(self.candidate_selection_reasons),
            "candidate_lean_score": self.candidate_lean_score,
            "root_check_result": self.root_check_result,
            "project_check_result": self.project_check_result,
        }


@dataclass(frozen=True)
class Phase06RunResult:
    status: str
    problem_id: str
    run_root: Path
    pinned_signatures_path: Path
    semantic_manifest_path: Path
    phase05_summary_path: Path
    phase_summary_path: Path
    root_file_path: Path
    final_result_path: Path
    final_summary_path: Path
    final_combined_path: Path | None
    project_manifest_path: Path | None
    root_decl_name: str
    lemma_count: int
    compiled_lemma_ids: tuple[str, ...]
    rounds_used: int
    rounds: tuple[RootAssemblyRoundResult, ...]
    error_class: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problem_id": self.problem_id,
            "run_root": str(self.run_root),
            "pinned_signatures_path": str(self.pinned_signatures_path),
            "semantic_manifest_path": str(self.semantic_manifest_path),
            "phase05_summary_path": str(self.phase05_summary_path),
            "phase_summary_path": str(self.phase_summary_path),
            "root_file_path": str(self.root_file_path),
            "final_result_path": str(self.final_result_path),
            "final_summary_path": str(self.final_summary_path),
            "final_combined_path": str(self.final_combined_path) if self.final_combined_path else None,
            "project_manifest_path": str(self.project_manifest_path) if self.project_manifest_path else None,
            "root_decl_name": self.root_decl_name,
            "lemma_count": self.lemma_count,
            "compiled_lemma_ids": list(self.compiled_lemma_ids),
            "rounds_used": self.rounds_used,
            "rounds": [item.to_dict() for item in self.rounds],
            "error_class": self.error_class,
            "message": self.message,
        }


def run_phase06(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle: NormalizedProblemBundle,
    pinned_signatures_path: Path,
    semantic_manifest_path: Path,
    phase05_summary_path: Path,
    max_root_attempts: int,
    timeout_seconds: int,
    model: str | None,
    claude_runner: ClaudeRunner | None = None,
    mock_root_candidates: list[str] | None = None,
    runner: SubprocessRunner = subprocess.run,
) -> Phase06RunResult:
    if max_root_attempts <= 0:
        raise ValueError("max_root_attempts must be > 0")

    phase_summary_path = run_paths.summaries_dir / "phase06_summary.json"
    root_file_path = run_paths.workspace_dir / "Orthos" / "Root.lean"

    # Remove Phase 03 assembly precheck scaffold — it uses axiom stubs that conflict
    # with real definitions and would cause `lake build` (whole-project check) to fail.
    assembly_check_path = run_paths.workspace_dir / "Orthos" / "AssemblyCheck.lean"
    if assembly_check_path.exists():
        assembly_check_path.unlink()
        # Also remove cached build artifacts so lake doesn't try to link stale objects
        for pattern in ("lib/lean/Orthos/AssemblyCheck.*", "ir/Orthos/AssemblyCheck.*"):
            for stale in (run_paths.workspace_dir / ".lake" / "build").glob(pattern):
                stale.unlink(missing_ok=True)

    try:
        phase05_summary = _load_phase05_summary(phase05_summary_path, expected_problem_id=bundle.problem_id)
    except ValueError as exc:
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name="",
            error_class="assembly_invalid",
            message=str(exc),
            diagnostics=[str(exc)],
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    phase05_status = str(phase05_summary.get("status", "")).strip().lower()
    if phase05_status != "ok":
        message = "Phase 06 requires an `ok` Phase 05 summary before root assembly."
        diagnostics = [
            f"phase05 status: {phase05_status or 'missing'}",
            f"phase05 summary path: {phase05_summary_path}",
        ]
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name="",
            error_class="assembly_invalid",
            message=message,
            diagnostics=diagnostics,
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    try:
        pinned_lemmas = load_pinned_lemma_signatures(
            pinned_signatures_path,
            expected_problem_id=bundle.problem_id,
            expected_run_name=run_paths.run_name,
        )
        pinned_root = _load_pinned_root_signature(
            pinned_signatures_path,
            expected_problem_id=bundle.problem_id,
            expected_run_name=run_paths.run_name,
        )
    except ValueError as exc:
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name="",
            error_class="assembly_invalid",
            message=str(exc),
            diagnostics=[str(exc)],
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    expected_lemma_order = tuple(_deterministic_lemma_order(bundle))
    missing_pinned = [lemma_id for lemma_id in expected_lemma_order if lemma_id not in pinned_lemmas]
    if missing_pinned:
        message = "Phase 06 missing pinned lemma signatures required by the normalized artifact."
        diagnostics = [f"missing pinned signatures: {', '.join(missing_pinned)}"]
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name=pinned_root.decl_name,
            error_class="assembly_invalid",
            message=message,
            diagnostics=diagnostics,
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    try:
        accepted_entries = load_trusted_manifest(semantic_manifest_path, expected_problem_id=run_paths.problem_id)
    except ValueError as exc:
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name=pinned_root.decl_name,
            error_class="assembly_invalid",
            message=str(exc),
            diagnostics=[str(exc)],
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    accepted_by_lemma = {entry.lemma_id: entry for entry in accepted_entries}
    missing_semantic = [lemma_id for lemma_id in expected_lemma_order if lemma_id not in accepted_by_lemma]
    if missing_semantic:
        message = "Phase 06 requires all lemmas to be semantically accepted before root assembly."
        diagnostics = [f"missing semantically accepted lemmas: {', '.join(missing_semantic)}"]
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name=pinned_root.decl_name,
            error_class="assembly_invalid",
            message=message,
            diagnostics=diagnostics,
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    decl_naming = Phase03DeclNaming(
        problem_id=bundle.problem_id,
        root_decl_name=pinned_root.decl_name,
        lemma_decl_names={lemma_id: pinned_lemmas[lemma_id].decl_name for lemma_id in expected_lemma_order},
        ordered_lemma_ids=expected_lemma_order,
    )
    assembly_validation_errors = validate_assembly_plan(bundle, decl_naming)
    if assembly_validation_errors:
        message = "Assembly plan failed root-level validation before attempting root composition."
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            root_decl_name=pinned_root.decl_name,
            error_class="assembly_invalid",
            message=message,
            diagnostics=assembly_validation_errors,
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    claude = claude_runner or ClaudeRunner(runtime_config)
    lean4_skills_refs = get_all_proving_refs(runtime_config.integrations.lean4_skills_root)
    root_assembly_dir = run_paths.run_root / "root_assembly"
    root_assembly_dir.mkdir(parents=True, exist_ok=True)

    rounds: list[RootAssemblyRoundResult] = []
    latest_diagnostics: list[str] = []
    latest_candidate_path: Path | None = None
    latest_root_check: LeanCommandResult | None = None

    for round_index in range(1, max_root_attempts + 1):
        prompt_path = root_assembly_dir / f"prompt_round_{round_index:02d}.md"
        raw_path = root_assembly_dir / f"claude_round_{round_index:02d}.jsonl"
        candidate_path = root_assembly_dir / f"root_round_{round_index:02d}.lean"
        diagnostics_path = root_assembly_dir / f"diagnostics_round_{round_index:02d}.txt"

        prompt = build_root_assembly_prompt(
            bundle=bundle,
            pinned_root=pinned_root,
            trusted_entries=[accepted_by_lemma[lemma_id] for lemma_id in expected_lemma_order],
            round_index=round_index,
            prior_diagnostics=latest_diagnostics,
            lean4_skills_refs=lean4_skills_refs,
        )
        write_text(prompt_path, prompt)

        used_mock = False
        candidate_backend = "internal"
        round_diagnostics: list[str] = []
        candidate_source = "none"
        candidate_selection_reasons: tuple[str, ...] = ()
        candidate_lean_score = 0

        mock_candidate = (
            mock_root_candidates[round_index - 1]
            if mock_root_candidates and round_index - 1 < len(mock_root_candidates)
            else None
        )
        if mock_candidate is not None:
            used_mock = True
            candidate_text = normalize_lean_text(mock_candidate)
            candidate_backend = "mock"
            candidate_source = "mock_candidate"
            candidate_selection_reasons = ("selected mock candidate",)
            _write_mock_raw(raw_path, candidate_text)
        else:
            baseline_root_text = root_file_path.read_text(encoding="utf-8") if root_file_path.exists() else ""
            claude_result = claude.run_prompt(
                run_paths=run_paths,
                prompt=prompt,
                phase_name=f"phase06_root_round_{round_index:02d}",
                model=model,
                timeout_seconds=runtime_config.claude.timeout_seconds,
                permission_mode="bypassPermissions",
            )
            _copy_raw(claude_result, raw_path)
            # Extract candidate from Claude's trace (latest write to Root.lean)
            trace_text = (
                claude_result.trace.latest_update_for_target(root_file_path)
                if claude_result.trace is not None
                else None
            )
            if trace_text and trace_text.strip():
                candidate_text = normalize_lean_text(trace_text)
                candidate_source = "claude_trace"
            elif root_file_path.exists():
                candidate_text = normalize_lean_text(root_file_path.read_text(encoding="utf-8"))
                candidate_source = "file_on_disk"
            else:
                candidate_text = baseline_root_text
                candidate_source = "baseline"
            candidate_selection_reasons = (f"source={candidate_source}",)
            candidate_lean_score = 0
            if not claude_result.ok:
                round_diagnostics.append(
                    f"Claude call failed (returncode={claude_result.returncode}, timed_out={claude_result.timed_out})."
                )

        write_text(candidate_path, candidate_text)
        # Claude's Write tool may set Root.lean to read-only (0o444) — restore write permission.
        if root_file_path.exists():
            root_file_path.chmod(0o644)
        write_text(root_file_path, candidate_text)
        latest_candidate_path = candidate_path

        guard_error = _root_signature_guard_error(candidate_text, pinned_root)
        if guard_error is not None:
            round_diagnostics.append(guard_error)
            write_text(diagnostics_path, _render_diagnostics(round_diagnostics))
            rounds.append(
                RootAssemblyRoundResult(
                    round_index=round_index,
                    prompt_path=prompt_path,
                    raw_path=raw_path,
                    candidate_path=candidate_path,
                    diagnostics_path=diagnostics_path,
                    status="failed",
                    diagnostics=tuple(round_diagnostics),
                    used_mock_candidate=used_mock,
                    candidate_backend=candidate_backend,
                    candidate_source=candidate_source,
                    candidate_selection_reasons=candidate_selection_reasons,
                    candidate_lean_score=candidate_lean_score,
                )
            )
            latest_diagnostics = round_diagnostics
            continue

        # Check for sorry/admit in any Orthos file.
        token_issues = find_disallowed_final_tokens(run_paths.workspace_dir)
        if token_issues:
            round_diagnostics.extend(token_issues)
            write_text(diagnostics_path, _render_diagnostics(round_diagnostics))
            rounds.append(
                RootAssemblyRoundResult(
                    round_index=round_index,
                    prompt_path=prompt_path,
                    raw_path=raw_path,
                    candidate_path=candidate_path,
                    diagnostics_path=diagnostics_path,
                    status="failed",
                    diagnostics=tuple(round_diagnostics),
                    used_mock_candidate=used_mock,
                    candidate_backend=candidate_backend,
                    candidate_source=candidate_source,
                    candidate_selection_reasons=candidate_selection_reasons,
                    candidate_lean_score=candidate_lean_score,
                )
            )
            latest_diagnostics = round_diagnostics
            continue

        # Single-file verification: generate Combined.lean (Statements + Lemmas + Root
        # in one file), clean it, then verify it compiles.  The verified file IS the
        # final deliverable — no post-verification mutation.
        combined_draft_path = root_assembly_dir / f"combined_round_{round_index:02d}.lean"
        combined_path = _write_combined_file(run_paths, output_path=combined_draft_path)
        clean_combined_output(combined_path)
        combined_workspace_path = run_paths.workspace_dir / "Combined.lean"
        shutil.copy2(combined_path, combined_workspace_path)
        latest_root_check = check_lean_file(
            run_paths.workspace_dir,
            "Combined.lean",
            timeout_seconds=timeout_seconds,
            runner=runner,
            lake_jobs=runtime_config.lean.lake_jobs,
        )
        combined_workspace_path.unlink(missing_ok=True)
        root_check_dict = latest_root_check.to_dict()

        if not latest_root_check.ok:
            # Classify errors by section (Statements / Lemmas / Root) and tell
            # Claude where the errors are so it can fix the right file.
            check_diagnostics = summarize_diagnostics_from_check(latest_root_check)
            section_hints = _classify_combined_errors(
                combined_path, latest_root_check
            )
            if section_hints:
                round_diagnostics.extend(section_hints)
            round_diagnostics.extend(check_diagnostics)
            write_text(diagnostics_path, _render_diagnostics(round_diagnostics))
            rounds.append(
                RootAssemblyRoundResult(
                    round_index=round_index,
                    prompt_path=prompt_path,
                    raw_path=raw_path,
                    candidate_path=candidate_path,
                    diagnostics_path=diagnostics_path,
                    status="failed",
                    diagnostics=tuple(round_diagnostics),
                    used_mock_candidate=used_mock,
                    candidate_backend=candidate_backend,
                    candidate_source=candidate_source,
                    candidate_selection_reasons=candidate_selection_reasons,
                    candidate_lean_score=candidate_lean_score,
                    root_check_result=root_check_dict,
                )
            )
            latest_diagnostics = round_diagnostics
            continue

        write_text(diagnostics_path, "success\n")
        rounds.append(
            RootAssemblyRoundResult(
                round_index=round_index,
                prompt_path=prompt_path,
                raw_path=raw_path,
                candidate_path=candidate_path,
                diagnostics_path=diagnostics_path,
                status="ok",
                diagnostics=(),
                used_mock_candidate=used_mock,
                candidate_backend=candidate_backend,
                candidate_source=candidate_source,
                candidate_selection_reasons=candidate_selection_reasons,
                candidate_lean_score=candidate_lean_score,
                root_check_result=root_check_dict,
            )
        )

        # The verified+cleaned draft becomes the final deliverable.
        final_combined_path = run_paths.run_root / "final" / "Combined.lean"
        final_combined_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(combined_path, final_combined_path)
        combined_path = final_combined_path

        # Axiom check: warn if non-standard axioms are used
        axiom_result = check_axioms(
            lean4_skills_root=runtime_config.integrations.lean4_skills_root,
            lean_file=final_combined_path,
            workspace_root=run_paths.workspace_dir,
            timeout_seconds=60,
        )
        if not axiom_result.get("clean", True):
            import sys
            print(
                f"[lean_engine] WARNING: Non-standard axioms detected in Combined.lean: "
                f"{axiom_result.get('output', '')}",
                file=sys.stderr, flush=True,
            )

        project_manifest_path = _write_project_manifest(
            run_paths=run_paths,
            root_decl_name=pinned_root.decl_name,
            root_check=latest_root_check,
            combined_path=combined_path,
        )

        result_payload = {
            "status": "success",
            "phase": "phase06",
            "problem_id": bundle.problem_id,
            "root_decl_name": pinned_root.decl_name,
            "lemma_count": len(expected_lemma_order),
            "compiled_lemma_ids": list(expected_lemma_order),
            "output_files": {
                "statements": "Orthos/Statements.lean",
                "lemmas": "Orthos/Lemmas.lean",
                "root": "Orthos/Root.lean",
                "combined": "final/Combined.lean",
            },
            "artifacts_dir": str(run_paths.run_root),
            "project_manifest": "final/project_manifest.json",
            "axiom_check": axiom_result,
        }
        summary = render_phase06_success_summary(
            problem_id=bundle.problem_id,
            root_decl_name=pinned_root.decl_name,
            lemma_count=len(expected_lemma_order),
            run_paths=run_paths,
            combined_path=combined_path,
            project_manifest_path=project_manifest_path,
            axiom_check=axiom_result,
        )
        output_bundle = write_success_output_bundle(
            run_paths=run_paths,
            payload=result_payload,
            summary_markdown=summary,
        )

        result = Phase06RunResult(
            status="ok",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            semantic_manifest_path=semantic_manifest_path,
            phase05_summary_path=phase05_summary_path,
            phase_summary_path=phase_summary_path,
            root_file_path=root_file_path,
            final_result_path=output_bundle.result_path,
            final_summary_path=output_bundle.summary_path,
            final_combined_path=combined_path,
            project_manifest_path=project_manifest_path,
            root_decl_name=pinned_root.decl_name,
            lemma_count=len(expected_lemma_order),
            compiled_lemma_ids=expected_lemma_order,
            rounds_used=round_index,
            rounds=tuple(rounds),
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    error_class = classify_root_assembly_failure(rounds)
    message = "Root assembly attempts were exhausted without producing a final compilable composition."

    fatal_payload = {
        "status": "fatal",
        "error_class": error_class,
        "error_scope": "assembly",
        "problem_id": bundle.problem_id,
        "target_id": "root",
        "decl_name": pinned_root.decl_name,
        "message": message,
        "math_gap_description": "",
        "evidence": {
            "statement_nl": bundle.root_theorem.statement_nl,
            "proof_nl_excerpt": _excerpt(bundle.selected_decomposition.assembly_plan.proof_skeleton_nl or "", 600),
            "pinned_signature": pinned_root.signature,
            "latest_diagnostics": latest_diagnostics,
            "attempt_count": len(rounds),
        },
        "artifacts": {
            "root_assembly_dir": str(root_assembly_dir),
            "latest_candidate_file": str(latest_candidate_path) if latest_candidate_path else "",
            "summary_md": str(run_paths.run_root / "final" / "fatal_summary.md"),
        },
    }
    fatal_bundle = write_fatal_output_bundle(run_paths=run_paths, payload=fatal_payload)
    fatal_payload = dict(fatal_payload)
    artifacts = dict(fatal_payload.get("artifacts", {}))
    artifacts["result_json"] = str(fatal_bundle.result_path)
    artifacts["summary_md"] = str(fatal_bundle.summary_path)
    fatal_payload["artifacts"] = artifacts
    write_json(fatal_bundle.result_path, fatal_payload)

    result = Phase06RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        pinned_signatures_path=pinned_signatures_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        phase_summary_path=phase_summary_path,
        root_file_path=root_file_path,
        final_result_path=fatal_bundle.result_path,
        final_summary_path=fatal_bundle.summary_path,
        final_combined_path=None,
        project_manifest_path=None,
        root_decl_name=pinned_root.decl_name,
        lemma_count=len(expected_lemma_order),
        compiled_lemma_ids=expected_lemma_order,
        rounds_used=len(rounds),
        rounds=tuple(rounds),
        error_class=error_class,
        message=message,
    )
    write_json(phase_summary_path, result.to_dict())
    return result


def build_root_assembly_prompt(
    *,
    bundle: NormalizedProblemBundle,
    pinned_root: PinnedRootSignature,
    trusted_entries: list[Any],
    round_index: int,
    prior_diagnostics: list[str],
    lean4_skills_refs: str = "",
) -> str:
    trusted_payload = [
        {
            "lemma_id": entry.lemma_id,
            "decl_name": entry.decl_name,
            "signature": entry.signature,
        }
        for entry in trusted_entries
    ]

    lines = [
        "You are formalizing the root assembly (Phase 06).",
        "",
        "Task:",
        "- Write `Orthos/Root.lean` that proves the pinned root theorem by composing accepted lemmas.",
        "- Use the assembly plan as the blueprint.",
        "- If prior diagnostics show errors in Lemmas.lean (not Root.lean), you may ALSO fix Lemmas.lean.",
        "",
        "Hard rules:",
        "- Keep the root declaration header EXACTLY equal to the pinned signature below.",
        "- Compose existing trusted lemmas; do not mutate lemma STATEMENTS (signatures).",
        "- You MAY fix lemma PROOFS if diagnostics show compilation errors in Lemmas.lean.",
        "- Prefer direct lemma application, `have`, `calc`, `simpa`, and simple algebraic closure.",
        "- Do not invent unsupported helper lemmas.",
        "- Never use `sorry` or `admit`.",
        "",
        "WORKFLOW — FOLLOW EXACTLY:",
        "1. Read Orthos/Statements.lean and Orthos/Lemmas.lean to understand available definitions.",
        "2. Write the COMPLETE Orthos/Root.lean file using the Write tool in ONE shot.",
        "3. Call `lean_diagnostic_messages` on Orthos/Root.lean ONCE to check for errors.",
        "4. If there are errors in Root.lean, fix them with the Edit tool and check again.",
        "5. If prior diagnostics mention errors in Lemmas.lean, also read and fix those.",
        "6. Stop. Do NOT call more than 3 diagnostic checks total.",
        "",
        "IMPORTANT: Do NOT explore the filesystem, do NOT search Mathlib, do NOT call lean_local_search",
        "or lean_loogle. Everything you need is in the files above and the lemma declarations below.",
        "Keep your tool usage minimal — the proof is a direct composition of the listed lemmas.",
        "",
        "PROHIBITED ACTIONS (violating these wastes time and breaks the pipeline):",
        "- Do NOT run `lake env lean`, `lake build`, or any Lean compilation command via Bash.",
        "  The MCP server has Mathlib loaded; Bash Lean invocations reload it from scratch (~30s).",
        "- Do NOT call `lean_build` via MCP.",
        "- Do NOT use `sleep` or polling loops — this WASTES WALL-CLOCK TIME. MCP tools return",
        "  results when ready. If MCP returns an infrastructure error, just call it again.",
        "",
        f"Root assembly round: {round_index}",
        f"problem_id: {bundle.problem_id}",
        "",
        "Pinned root signature (copy EXACTLY as the theorem header):",
        "```lean",
        pinned_root.signature,
        "```",
        "",
        "Root NL package:",
        f"- statement_nl: {bundle.root_theorem.statement_nl}",
        f"- semantic_sketch: {json.dumps(bundle.root_theorem.semantic_sketch, ensure_ascii=True, sort_keys=True)}",
        "",
        "Assembly plan:",
        json.dumps(bundle.selected_decomposition.assembly_plan.to_dict(), indent=2, ensure_ascii=True, sort_keys=True),
        "",
        "Trusted lemma declarations (these are available via `import Orthos.Lemmas`):",
        json.dumps(trusted_payload, indent=2, ensure_ascii=True, sort_keys=True),
    ]

    if lean4_skills_refs:
        lines.extend([
            "",
            "Lean 4 proving reference library (tactics, error fixes, patterns, search strategies):",
            lean4_skills_refs,
        ])

    if prior_diagnostics:
        lines.extend(
            [
                "",
                "Diagnostics from prior failed round (repair target):",
                json.dumps(prior_diagnostics[:12], indent=2, ensure_ascii=True),
            ]
        )

    lines.extend(
        [
            "",
            "Write Orthos/Root.lean now. The file must start with `import Orthos.Lemmas`.",
            "Write the COMPLETE file content using the Write tool — do not use Edit on the placeholder.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def find_disallowed_final_tokens(workspace_root: Path) -> list[str]:
    issues: list[str] = []
    files = {
        "Orthos/Statements.lean": workspace_root / "Orthos" / "Statements.lean",
        "Orthos/Lemmas.lean": workspace_root / "Orthos" / "Lemmas.lean",
        "Orthos/Root.lean": workspace_root / "Orthos" / "Root.lean",
    }
    for relative, path in files.items():
        if not path.exists() or not path.is_file():
            issues.append(f"required file is missing during final gate: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bsorry\b", text):
            issues.append(f"disallowed token in final file {relative}: sorry")
        if re.search(r"\badmit\b", text):
            issues.append(f"disallowed token in final file {relative}: admit")
    return issues


def classify_root_assembly_failure(rounds: list[RootAssemblyRoundResult]) -> str:
    if not rounds:
        return "unknown_fatal"

    latest = rounds[-1]
    diagnostics_blob = "\n".join(latest.diagnostics).lower()

    if "root declaration" in diagnostics_blob or "pinned root signature" in diagnostics_blob:
        return "assembly_invalid"
    if "disallowed token" in diagnostics_blob:
        return "assembly_invalid"
    if "missing during final gate" in diagnostics_blob:
        return "assembly_invalid"
    if "unknown import" in diagnostics_blob or "unknown module prefix" in diagnostics_blob:
        return "unknown_fatal"
    return "assembly_composition_failure"


def render_phase06_success_summary(
    *,
    problem_id: str,
    root_decl_name: str,
    lemma_count: int,
    run_paths: RunPaths,
    combined_path: Path,
    project_manifest_path: Path,
    axiom_check: dict[str, Any] | None = None,
) -> str:
    axiom_status = "clean" if (axiom_check is None or axiom_check.get("clean", True)) else "WARNING: non-standard axioms detected"
    lines = [
        "# Phase 06 Summary",
        "",
        "- status: success",
        f"- problem_id: `{problem_id}`",
        f"- root theorem declaration: `{root_decl_name}`",
        f"- compiled lemmas used: {lemma_count}",
        "- final `lake build`: passed",
        f"- axiom check: {axiom_status}",
        "",
        "## Output files",
        "- `Orthos/Statements.lean`",
        "- `Orthos/Lemmas.lean`",
        "- `Orthos/Root.lean`",
        f"- `{combined_path}`",
        f"- `{project_manifest_path}`",
        "",
        "## Artifact directory",
        f"- `{run_paths.run_root}`",
        "",
    ]
    if axiom_check and not axiom_check.get("clean", True):
        lines.extend([
            "## Axiom check warning",
            f"```",
            axiom_check.get("output", ""),
            f"```",
            "",
        ])
    return "\n".join(lines)


def load_mock_root_candidates_dir(path: Path) -> list[str]:
    root = path.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"mock root candidates directory does not exist: {root}")

    files = sorted((item for item in root.iterdir() if item.is_file() and item.suffix == ".lean"), key=lambda p: p.name)
    ordered: list[tuple[int, str]] = []
    for file_path in files:
        round_index = _extract_round_index(file_path.name)
        if round_index is None:
            continue
        ordered.append((round_index, file_path.read_text(encoding="utf-8")))

    if not ordered:
        raise ValueError(f"mock root candidates directory has no round_XX.lean files: {root}")

    ordered.sort(key=lambda item: item[0])
    return [content for _, content in ordered]


def _dependency_fatal_result(
    *,
    run_paths: RunPaths,
    bundle: NormalizedProblemBundle,
    pinned_signatures_path: Path,
    semantic_manifest_path: Path,
    phase05_summary_path: Path,
    phase_summary_path: Path,
    root_file_path: Path,
    root_decl_name: str,
    error_class: str,
    message: str,
    diagnostics: list[str],
) -> Phase06RunResult:
    payload = {
        "status": "fatal",
        "error_class": error_class,
        "error_scope": "assembly",
        "problem_id": bundle.problem_id,
        "target_id": "root",
        "decl_name": root_decl_name,
        "message": message,
        "math_gap_description": "",
        "evidence": {
            "statement_nl": bundle.root_theorem.statement_nl,
            "proof_nl_excerpt": _excerpt(bundle.selected_decomposition.assembly_plan.proof_skeleton_nl or "", 600),
            "pinned_signature": "",
            "latest_diagnostics": diagnostics,
            "attempt_count": 0,
        },
        "artifacts": {
            "phase05_summary_path": str(phase05_summary_path),
            "semantic_manifest_path": str(semantic_manifest_path),
            "summary_md": str(run_paths.run_root / "final" / "fatal_summary.md"),
        },
    }
    bundle_paths = write_fatal_output_bundle(run_paths=run_paths, payload=payload)
    payload["artifacts"]["result_json"] = str(bundle_paths.result_path)
    write_json(bundle_paths.result_path, payload)

    return Phase06RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        pinned_signatures_path=pinned_signatures_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        phase_summary_path=phase_summary_path,
        root_file_path=root_file_path,
        final_result_path=bundle_paths.result_path,
        final_summary_path=bundle_paths.summary_path,
        final_combined_path=None,
        project_manifest_path=None,
        root_decl_name=root_decl_name,
        lemma_count=len(_deterministic_lemma_order(bundle)),
        compiled_lemma_ids=(),
        rounds_used=0,
        rounds=(),
        error_class=error_class,
        message=message,
    )


def _root_signature_guard_error(candidate_text: str, pinned_root: PinnedRootSignature) -> str | None:
    if re.search(r"\bsorry\b", candidate_text):
        return "disallowed token in root candidate: sorry"
    if re.search(r"\badmit\b", candidate_text):
        return "disallowed token in root candidate: admit"

    try:
        parsed = extract_statement_signature(candidate_text, pinned_root.decl_name)
    except ValueError as exc:
        return f"missing or malformed root declaration in candidate: {exc}"

    if _collapse_ws(parsed.signature) != _collapse_ws(pinned_root.signature):
        return (
            "root declaration does not match pinned root signature exactly: "
            f"expected `{pinned_root.signature}` got `{parsed.signature}`"
        )
    return None


def _load_phase05_summary(path: Path, *, expected_problem_id: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"phase05 summary file not found: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("phase05 summary must be a JSON object")
    problem_id = str(payload.get("problem_id", "")).strip()
    if sanitize_component(problem_id) != sanitize_component(expected_problem_id):
        raise ValueError(
            "phase05 summary problem binding mismatch: "
            f"expected `{sanitize_component(expected_problem_id)}`, found `{sanitize_component(problem_id)}`"
        )
    return payload


def _load_pinned_root_signature(
    path: Path,
    *,
    expected_problem_id: str,
    expected_run_name: str,
) -> PinnedRootSignature:
    payload = json.loads(path.read_text(encoding="utf-8"))

    payload_problem_id = str(payload.get("problem_id", "")).strip()
    if sanitize_component(payload_problem_id) != sanitize_component(expected_problem_id):
        raise ValueError(
            "pinned_signatures.json problem_id mismatch: "
            f"expected `{sanitize_component(expected_problem_id)}`, found `{sanitize_component(payload_problem_id)}`"
        )

    payload_run_name = str(payload.get("run_name", "")).strip()
    if payload_run_name != expected_run_name:
        raise ValueError(
            "pinned_signatures.json run_name mismatch: "
            f"expected `{expected_run_name}`, found `{payload_run_name}`"
        )

    root = payload.get("root")
    if not isinstance(root, dict):
        raise ValueError("pinned_signatures.json is missing `root` object")

    decl_name = str(root.get("decl_name", "")).strip()
    signature = str(root.get("signature", "")).strip()
    statement_nl = str(root.get("statement_nl", "")).strip()

    if not decl_name or not signature:
        raise ValueError("pinned_signatures.json root is missing `decl_name` or `signature`")

    return PinnedRootSignature(decl_name=decl_name, signature=signature, statement_nl=statement_nl)


def _deterministic_lemma_order(bundle: NormalizedProblemBundle) -> list[str]:
    if bundle.topologically_sorted_lemma_ids:
        return list(bundle.topologically_sorted_lemma_ids)
    return [lemma.lemma_id for lemma in bundle.lemmas]



def _copy_raw(result: ClaudeRunResult, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result.raw_output_path, destination)


def _write_mock_raw(path: Path, lean_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "result", "result": lean_text, "mock": True}
    write_text(path, json.dumps(payload, ensure_ascii=True) + "\n")


def _write_combined_file(run_paths: RunPaths, *, output_path: Path | None = None) -> Path:
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    lemmas_path = run_paths.workspace_dir / "Orthos" / "Lemmas.lean"
    root_path = run_paths.workspace_dir / "Orthos" / "Root.lean"

    combined_path = output_path or (run_paths.run_root / "final" / "Combined.lean")

    def _strip_internal(text: str, drop_axiom_names: set[str] | None = None) -> str:
        """Remove internal cross-imports and duplicate Mathlib imports; keep body.

        If *drop_axiom_names* is provided, also remove ``axiom`` declarations whose
        name matches (these are Phase 03 pinned-signature stubs that conflict with
        the actual ``theorem`` proofs in Lemmas).
        """
        out: list[str] = []
        skip_axiom = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import Orthos"):
                continue
            if stripped.startswith("import Mathlib"):
                continue
            # Drop axiom stubs that will be replaced by full theorems in the Lemmas block
            if drop_axiom_names:
                axiom_match = re.match(
                    r"\s*(?:--\s*\[pinned\]\s+)?axiom\s+([A-Za-z0-9_'.]+)", stripped
                )
                if axiom_match and axiom_match.group(1) in drop_axiom_names:
                    skip_axiom = True
                    continue
                if skip_axiom:
                    # Continuation lines of a neutralized axiom that start with
                    # "-- [pinned]" are still part of the stub — skip them too.
                    if stripped.startswith("-- [pinned]"):
                        continue
                    # Skip continuation lines of the axiom (indented or blank until next decl)
                    if stripped == "" or (not stripped.startswith("theorem") and not stripped.startswith("lemma")
                                         and not stripped.startswith("def") and not stripped.startswith("axiom")
                                         and not stripped.startswith("noncomputable") and not stripped.startswith("--")
                                         and not stripped.startswith("end") and not stripped.startswith("namespace")
                                         and not stripped.startswith("open") and not stripped.startswith("section")):
                        continue
                    skip_axiom = False
            out.append(line)
        # Drop leading blank lines left behind by removed imports
        while out and not out[0].strip():
            out.pop(0)
        return "\n".join(out)

    def _deduplicate_declarations(text: str) -> str:
        """Remove duplicate theorem/lemma/def declarations, keeping the first of each."""
        decl_re = re.compile(
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
                # Skip body lines of duplicate declaration
                continue
            result_lines.append(line)
        return "\n".join(result_lines)

    statements_text = statements_path.read_text(encoding="utf-8")
    lemmas_text = lemmas_path.read_text(encoding="utf-8")
    root_text = root_path.read_text(encoding="utf-8")

    # Collect declaration names from Lemmas.lean and Root.lean so we can strip
    # matching axiom stubs from Statements.lean (the axioms are Phase 03
    # pinned-signature placeholders).
    lemma_decl_names: set[str] = set()
    for source_text in (lemmas_text, root_text):
        for m in re.finditer(
            r"(?m)^(?:\s*(?:noncomputable\s+)?(?:theorem|lemma|def)\s+)([A-Za-z0-9_'.]+)",
            source_text,
        ):
            lemma_decl_names.add(m.group(1))

    blocks = [
        ("Statements", _strip_internal(statements_text, drop_axiom_names=lemma_decl_names)),
        ("Lemmas", _strip_internal(lemmas_text)),
        ("Root", _strip_internal(root_text)),
    ]

    lines = [
        "import Mathlib",
        "",
        "open scoped BigOperators",
        "",
    ]
    for label, body in blocks:
        lines.extend(
            [
                f"/- ===== {label} ===== -/",
                body.rstrip(),
                "",
            ]
        )

    combined_text = "\n".join(lines).rstrip() + "\n"
    # Deduplicate any repeated declarations (can occur from resume carrying lemmas)
    combined_text = _deduplicate_declarations(combined_text)
    return write_text(combined_path, combined_text)


def clean_combined_output(combined_path: Path) -> None:
    """Post-process Combined.lean to remove internal markers and comments.

    Strips:
    - Legacy ``namespace Orthos`` / ``end Orthos`` wrapper lines (if any)
    - ``-- END LEMMAS`` marker
    - ``-- [pinned]`` comment prefixes
    - Internal comments (Phase 04 markers, edit_scope, etc.)
    """
    text = combined_path.read_text(encoding="utf-8")
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # Remove namespace wrappers (legacy files may still have these)
        if stripped == "namespace Orthos" or re.match(r"^end\s+Orthos$", stripped):
            continue
        # Remove internal markers
        if stripped == "-- END LEMMAS":
            continue
        # Remove Phase 04 internal comments
        if stripped.startswith("-- Phase 04:") or stripped.startswith("-- edit_scope:"):
            continue
        if stripped.startswith("-- Proven lemma declarations"):
            continue
        # Remove [pinned] comment prefix, leaving the declaration visible
        if "-- [pinned]" in line:
            line = line.replace("-- [pinned] ", "").replace("-- [pinned]", "")
        # Remove `private` modifier from declarations (not needed in final output)
        line = re.sub(r"\bprivate\s+", "", line)
        cleaned_lines.append(line)
    # Collapse runs of 3+ blank lines into 2
    final_lines: list[str] = []
    blank_count = 0
    for line in cleaned_lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                final_lines.append(line)
        else:
            blank_count = 0
            final_lines.append(line)
    combined_path.write_text("\n".join(final_lines).rstrip() + "\n", encoding="utf-8")


def _write_project_manifest(
    *,
    run_paths: RunPaths,
    root_decl_name: str,
    root_check: LeanCommandResult,
    combined_path: Path,
) -> Path:
    files = {
        "statements": run_paths.workspace_dir / "Orthos" / "Statements.lean",
        "lemmas": run_paths.workspace_dir / "Orthos" / "Lemmas.lean",
        "root": run_paths.workspace_dir / "Orthos" / "Root.lean",
        "combined": combined_path,
    }

    file_entries: list[dict[str, Any]] = []
    for key, path in files.items():
        file_entries.append(
            {
                "role": key,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": workspace_file_digest(path),
            }
        )

    payload = {
        "problem_id": run_paths.problem_id,
        "run_name": run_paths.run_name,
        "root_decl_name": root_decl_name,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace_dir": str(run_paths.workspace_dir),
        "checks": {
            "root_file": root_check.to_dict(),
        },
        "files": file_entries,
    }
    return write_json(run_paths.run_root / "final" / "project_manifest.json", payload)


def _classify_combined_errors(
    combined_path: Path,
    check_result: LeanCommandResult,
) -> list[str]:
    """Map Combined.lean error line numbers to originating sections (Statements/Lemmas/Root)."""
    try:
        combined_text = combined_path.read_text(encoding="utf-8")
    except OSError:
        return []

    # Find section boundaries from markers
    section_ranges: list[tuple[str, int, int]] = []
    lines = combined_text.splitlines()
    current_section = "Preamble"
    current_start = 1
    for i, line in enumerate(lines, start=1):
        if "/- ===== Statements =====" in line:
            section_ranges.append((current_section, current_start, i - 1))
            current_section = "Statements"
            current_start = i
        elif "/- ===== Lemmas =====" in line:
            section_ranges.append((current_section, current_start, i - 1))
            current_section = "Lemmas"
            current_start = i
        elif "/- ===== Root =====" in line:
            section_ranges.append((current_section, current_start, i - 1))
            current_section = "Root"
            current_start = i
    section_ranges.append((current_section, current_start, len(lines)))

    # Parse error line numbers from stdout
    error_re = re.compile(r"Combined\.lean:(\d+):\d+:\s*error")
    error_sections: dict[str, int] = {}
    output = f"{check_result.stdout}\n{check_result.stderr}"
    for m in error_re.finditer(output):
        line_no = int(m.group(1))
        for section_name, start, end in section_ranges:
            if start <= line_no <= end:
                error_sections[section_name] = error_sections.get(section_name, 0) + 1
                break

    if not error_sections:
        return []

    hints = []
    for section, count in sorted(error_sections.items(), key=lambda x: -x[1]):
        hints.append(f"NOTE: {count} error(s) originate in the {section} section of Combined.lean. "
                      f"Fix {section}.lean to resolve.")
    return hints


def _collapse_ws(text: str) -> str:
    return " ".join(text.split())


def _render_diagnostics(diagnostics: list[str]) -> str:
    if not diagnostics:
        return "no diagnostics\n"
    return "\n".join(diagnostics).rstrip() + "\n"


def _extract_round_index(filename: str) -> int | None:
    match = re.search(r"round[_-]?(\d+)", filename)
    if match is None:
        return None
    return int(match.group(1))


def _excerpt(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "..."
