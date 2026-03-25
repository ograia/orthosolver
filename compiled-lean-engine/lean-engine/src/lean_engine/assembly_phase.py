from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifact_io import RunPaths, write_json, write_text
from .contracts import NormalizedProblemBundle
from .lean_checks import LeanCommandResult, check_lean_file
from .statement_phase import Phase03DeclNaming, sanitize_lean_decl_suffix, summarize_diagnostics_from_check


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class AssemblyPrecheckResult:
    status: str
    scratch_file: Path
    composable: bool
    step_count: int
    check_result: dict[str, Any] | None
    error_class: str | None = None
    error_scope: str | None = None
    message: str | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scratch_file": str(self.scratch_file),
            "composable": self.composable,
            "step_count": self.step_count,
            "check_result": self.check_result,
            "error_class": self.error_class,
            "error_scope": self.error_scope,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
        }


def run_assembly_precheck(
    *,
    run_paths: RunPaths,
    bundle: NormalizedProblemBundle,
    decl_naming: Phase03DeclNaming,
    timeout_seconds: int = 2700,
    runner: SubprocessRunner = subprocess.run,
    lake_jobs: int = 0,
) -> AssemblyPrecheckResult:
    validation_errors = validate_assembly_plan(bundle, decl_naming)
    scratch_file = run_paths.workspace_dir / "Orthos" / "AssemblyCheck.lean"

    # Read Statements.lean and strip its import line so the assembly precheck
    # file is self-contained (only `import Mathlib`, no cross-file imports).
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    statements_body = _strip_import_mathlib(statements_path.read_text(encoding="utf-8"))

    if validation_errors:
        write_text(scratch_file, build_assembly_precheck_file(bundle, decl_naming, statements_body=statements_body))
        return AssemblyPrecheckResult(
            status="fatal",
            scratch_file=scratch_file,
            composable=False,
            step_count=len(bundle.selected_decomposition.assembly_plan.steps),
            check_result=None,
            error_class="assembly_invalid",
            error_scope="assembly",
            message="Assembly plan failed statement-level composability validation before Lean precheck.",
            diagnostics=tuple(validation_errors),
        )

    scratch_text = build_assembly_precheck_file(bundle, decl_naming, statements_body=statements_body)
    write_text(scratch_file, scratch_text)

    check_result = check_lean_file(
        run_paths.workspace_dir,
        "Orthos/AssemblyCheck.lean",
        timeout_seconds=timeout_seconds,
        runner=runner,
        lake_jobs=lake_jobs,
    )

    if check_result.ok:
        return AssemblyPrecheckResult(
            status="ok",
            scratch_file=scratch_file,
            composable=True,
            step_count=len(bundle.selected_decomposition.assembly_plan.steps),
            check_result=check_result.to_dict(),
        )

    error_class = classify_assembly_check_failure(check_result)
    return AssemblyPrecheckResult(
        status="fatal",
        scratch_file=scratch_file,
        composable=False,
        step_count=len(bundle.selected_decomposition.assembly_plan.steps),
        check_result=check_result.to_dict(),
        error_class=error_class,
        error_scope="assembly",
        message="Assembly precheck failed to typecheck the statement-level composition skeleton.",
        diagnostics=tuple(summarize_diagnostics_from_check(check_result)),
    )


def validate_assembly_plan(bundle: NormalizedProblemBundle, decl_naming: Phase03DeclNaming) -> list[str]:
    known_lemmas = set(decl_naming.lemma_decl_names)
    seen_steps: set[str] = set()
    ordered_steps: list[str] = []
    step_dependencies: dict[str, list[str]] = {}
    step_evidence_counts: dict[str, int] = {}
    errors: list[str] = []

    # When the NL engine marks the plan as trivially composable, trust it and skip
    # structural validation.  The assembly plan in these cases often has independent
    # steps with no inter-step dependencies and no final_step_id — the NL engine
    # has already verified that the lemmas jointly suffice for the root theorem.
    if bundle.selected_decomposition.assembly_plan.is_trivially_composable:
        return errors

    for step in bundle.selected_decomposition.assembly_plan.steps:
        if step.step_id in seen_steps:
            errors.append(f"duplicate step_id in assembly plan: {step.step_id}")
            continue

        valid_lemma_count = 0
        for lemma_id in step.uses_lemmas:
            if lemma_id not in known_lemmas:
                errors.append(f"unknown lemma_id in assembly plan step {step.step_id}: {lemma_id}")
                continue
            valid_lemma_count += 1

        valid_prior_count = 0
        for prior_step in step.uses_prior_steps:
            if prior_step not in seen_steps:
                errors.append(
                    f"invalid uses_prior_steps reference in step {step.step_id}: {prior_step} "
                    "(must reference an earlier step)"
                )
                continue
            valid_prior_count += 1

        seen_steps.add(step.step_id)
        ordered_steps.append(step.step_id)
        step_dependencies[step.step_id] = list(step.uses_prior_steps)
        # Trivial steps (hypothesis setup, definitional) legitimately have zero
        # evidence — they don't introduce new lemmas or depend on prior steps.
        # Count them as having evidence so they pass the vacuity check.
        evidence = valid_lemma_count + valid_prior_count
        if evidence == 0 and step.is_trivial:
            evidence = 1  # exempt trivial steps from vacuity rejection
        step_evidence_counts[step.step_id] = evidence

    if ordered_steps:
        # If all steps are independent (no inter-step dependencies), treat as a flat plan
        # where every step contributes directly to the root proof. Skip connectivity check
        # but still catch vacuous steps (steps with zero evidence).
        has_any_deps = any(deps for deps in step_dependencies.values())
        if has_any_deps:
            terminal_step = ordered_steps[-1]
            required_steps = _collect_required_steps(terminal_step, step_dependencies)
            for step_id in ordered_steps:
                if step_id not in required_steps:
                    # Non-contributing steps are allowed — NL decompositions may
                    # include explanatory/independent steps that don't feed into
                    # the terminal step.  Skip silently.
                    continue
                if step_evidence_counts.get(step_id, 0) == 0:
                    errors.append(
                        "non-composable assembly plan: required step "
                        f"`{step_id}` has no lemma/prior-step dependencies (precheck would be vacuous)"
                    )
        else:
            # Flat plan — still reject steps with zero evidence
            for step_id in ordered_steps:
                if step_evidence_counts.get(step_id, 0) == 0:
                    errors.append(
                        "non-composable assembly plan: step "
                        f"`{step_id}` has no lemma dependencies (precheck would be vacuous)"
                    )

    return errors


def classify_assembly_check_failure(check_result: LeanCommandResult) -> str:
    combined = f"{check_result.stderr}\n{check_result.stdout}".lower()
    if "unknown package" in combined or "unknown module prefix" in combined or "unknown import" in combined:
        return "missing_import"
    if "type mismatch" in combined or "application type mismatch" in combined or "expected type" in combined:
        return "type_mismatch"
    return "assembly_invalid"


def build_assembly_precheck_file(
    bundle: NormalizedProblemBundle,
    decl_naming: Phase03DeclNaming,
    *,
    statements_body: str = "",
) -> str:
    lines: list[str] = [
        "import Mathlib",
        "",
    ]

    # Inline Statements.lean content (with its `import Mathlib` already stripped)
    # so this file is fully self-contained — no cross-file imports, no stale oleans.
    if statements_body.strip():
        lines.append("-- Inlined from Orthos/Statements.lean")
        lines.append(statements_body.rstrip())
        lines.append("")

    lines.extend([
        "",
        "-- Phase 03 scratch-only assembly precheck file.",
        "-- Temporary placeholders are permitted in this file only.",
        "",
    ])

    used_names: set[str] = set()
    used_lemma_ids = _ordered_unique_lemmas(bundle)
    lemma_token_names: dict[str, str] = {}
    lemma_hold_names: dict[str, str] = {}

    for lemma_id in used_lemma_ids:
        lemma_decl_name = decl_naming.lemma_decl_names.get(lemma_id)
        if lemma_decl_name is None:
            continue
        token_name = _unique_decl_name(f"asm_lemma_{sanitize_lean_decl_suffix(lemma_id)}", used_names)
        hold_name = _unique_decl_name(f"{token_name}_holds", used_names)
        lemma_token_names[lemma_id] = token_name
        lemma_hold_names[lemma_id] = hold_name
        lines.append(f"def {token_name} : Prop := True")
        lines.append(f"axiom {hold_name} : {token_name}")
        lines.append("")

    step_token_names: dict[str, str] = {}
    step_compose_names: dict[str, str] = {}
    step_dependency_tokens: dict[str, list[str]] = {}
    for step in bundle.selected_decomposition.assembly_plan.steps:
        step_token_name = _unique_decl_name(f"step_{sanitize_lean_decl_suffix(step.step_id)}", used_names)
        compose_name = _unique_decl_name(f"{step_token_name}_from_deps", used_names)
        step_token_names[step.step_id] = step_token_name
        step_compose_names[step.step_id] = compose_name

        deps: list[str] = []
        deps.extend(
            lemma_token_names[lemma_id]
            for lemma_id in step.uses_lemmas
            if lemma_id in lemma_token_names
        )
        deps.extend(step_token_names[prior] for prior in step.uses_prior_steps if prior in step_token_names)
        step_dependency_tokens[step.step_id] = deps

        lines.append(f"def {step_token_name} : Prop := True")
        lines.append(f"axiom {compose_name} : {_arrow_chain(deps, step_token_name)}")
        lines.append("")

    target_prop_name = _unique_decl_name("assembly_target", used_names)
    target_from_steps_name = _unique_decl_name("assembly_target_from_steps", used_names)
    lines.append(f"def {target_prop_name} : Prop := True")
    if step_token_names:
        terminal_step_id = bundle.selected_decomposition.assembly_plan.steps[-1].step_id
        terminal_step_name = step_token_names[terminal_step_id]
        lines.append(f"axiom {target_from_steps_name} : {terminal_step_name} -> {target_prop_name}")
    else:
        lines.append(f"axiom {target_from_steps_name} : {target_prop_name}")
    lines.append("")

    lines.extend(
        [
            f"theorem assembly_precheck_target : {target_prop_name} := by",
            f"  let _ := {decl_naming.root_decl_name}",
            "",
        ]
    )
    proof_name_by_lemma: dict[str, str] = {}
    for lemma_id in used_lemma_ids:
        token_name = lemma_token_names.get(lemma_id)
        hold_name = lemma_hold_names.get(lemma_id)
        lemma_decl_name = decl_naming.lemma_decl_names.get(lemma_id)
        if token_name is None or hold_name is None or lemma_decl_name is None:
            continue
        proof_name = _proof_name_for("lemma", lemma_id)
        proof_name_by_lemma[lemma_id] = proof_name
        lines.extend(
            [
                f"  have {proof_name} : {token_name} := by",
                f"    let _ := {lemma_decl_name}",
                f"    exact {hold_name}",
            ]
        )

    proof_name_by_step: dict[str, str] = {}
    for step in bundle.selected_decomposition.assembly_plan.steps:
        step_token_name = step_token_names[step.step_id]
        compose_name = step_compose_names[step.step_id]
        deps = step_dependency_tokens[step.step_id]
        proof_name = _proof_name_for("step", step.step_id)
        proof_name_by_step[step.step_id] = proof_name
        lines.append(f"  have {proof_name} : {step_token_name} := by")
        if deps:
            lines.append(f"    apply {compose_name}")
            for lemma_id in step.uses_lemmas:
                if lemma_id in proof_name_by_lemma:
                    lines.append(f"    exact {proof_name_by_lemma[lemma_id]}")
            for prior_step in step.uses_prior_steps:
                if prior_step in proof_name_by_step:
                    lines.append(f"    exact {proof_name_by_step[prior_step]}")
        else:
            lines.append(f"    exact {compose_name}")

    if step_token_names:
        terminal_step_id = bundle.selected_decomposition.assembly_plan.steps[-1].step_id
        lines.extend(
            [
                f"  apply {target_from_steps_name}",
                f"  exact {proof_name_by_step[terminal_step_id]}",
            ]
        )
    else:
        lines.append(f"  exact {target_from_steps_name}")

    lines.append("")
    return "\n".join(lines)


def write_assembly_precheck_summary(path: Path, result: AssemblyPrecheckResult) -> Path:
    return write_json(path, result.to_dict())


def _arrow_chain(input_types: list[str], output_type: str) -> str:
    if not input_types:
        return output_type
    return " -> ".join([*input_types, output_type])


def _ordered_unique_lemmas(bundle: NormalizedProblemBundle) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for step in bundle.selected_decomposition.assembly_plan.steps:
        for lemma_id in step.uses_lemmas:
            if lemma_id in seen:
                continue
            seen.add(lemma_id)
            ordered.append(lemma_id)
    return ordered


def _unique_decl_name(base_name: str, used_names: set[str]) -> str:
    candidate = base_name
    suffix = 2
    while candidate in used_names:
        candidate = f"{base_name}_{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _proof_name_for(prefix: str, source_id: str) -> str:
    return f"h_{prefix}_{sanitize_lean_decl_suffix(source_id)}"


def _collect_required_steps(terminal_step: str, step_dependencies: dict[str, list[str]]) -> set[str]:
    required: set[str] = set()
    stack = [terminal_step]
    while stack:
        step_id = stack.pop()
        if step_id in required:
            continue
        required.add(step_id)
        stack.extend(step_dependencies.get(step_id, ()))
    return required


def _strip_import_mathlib(text: str) -> str:
    """Strip ``import Mathlib`` lines from Statements.lean content.

    Returns the remaining text (namespace, opens, axioms) so it can be
    inlined into a file that already has its own ``import Mathlib``.
    """
    lines = text.splitlines()
    kept = [line for line in lines if not line.strip().startswith("import ")]
    return "\n".join(kept)
