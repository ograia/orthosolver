from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifact_io import RunPaths, write_json, write_text
from .claude_candidate_selection import CandidateSelection, select_lean_candidate
from .claude_runner import ClaudeRunner
from .config import RuntimeConfig
from .contracts import NormalizedProblemBundle
from .lean_checks import LeanCommandResult, check_lean_file
from .prompting import (
    StatementPromptDeclNaming,
    build_statement_repair_prompt,
    build_statement_translation_prompt,
)


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Phase03DeclNaming:
    problem_id: str
    root_decl_name: str
    lemma_decl_names: dict[str, str]
    ordered_lemma_ids: tuple[str, ...]

    def to_prompt_naming(self) -> StatementPromptDeclNaming:
        return StatementPromptDeclNaming(
            root_decl_name=self.root_decl_name,
            lemma_decl_names=dict(self.lemma_decl_names),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "root_decl_name": self.root_decl_name,
            "lemma_decl_names": dict(self.lemma_decl_names),
            "ordered_lemma_ids": list(self.ordered_lemma_ids),
        }


@dataclass(frozen=True)
class StatementSignature:
    decl_name: str
    signature: str
    keyword: str

    def to_dict(self) -> dict[str, str]:
        return {
            "decl_name": self.decl_name,
            "signature": self.signature,
            "keyword": self.keyword,
        }


@dataclass(frozen=True)
class StatementPhaseResult:
    status: str
    statements_path: Path
    pinned_signatures_path: Path | None
    decl_naming: Phase03DeclNaming
    attempts_used: int
    check_history: tuple[dict[str, Any], ...]
    claude_runs: tuple[dict[str, Any], ...]
    error_class: str | None = None
    error_scope: str | None = None
    message: str | None = None
    diagnostics: tuple[str, ...] = ()
    candidate_source: str | None = None
    candidate_selection_reasons: tuple[str, ...] = ()
    candidate_lean_score: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "statements_path": str(self.statements_path),
            "pinned_signatures_path": str(self.pinned_signatures_path) if self.pinned_signatures_path else None,
            "decl_naming": self.decl_naming.to_dict(),
            "attempts_used": self.attempts_used,
            "check_history": list(self.check_history),
            "claude_runs": list(self.claude_runs),
            "error_class": self.error_class,
            "error_scope": self.error_scope,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
            "candidate_source": self.candidate_source,
            "candidate_selection_reasons": list(self.candidate_selection_reasons),
            "candidate_lean_score": self.candidate_lean_score,
        }


def build_phase03_decl_naming(bundle: NormalizedProblemBundle) -> Phase03DeclNaming:
    ordered_lemma_ids = _deterministic_lemma_order(bundle)
    root_decl_name = f"root_{sanitize_lean_decl_suffix(bundle.problem_id)}"

    used_names = {root_decl_name}
    lemma_decl_names: dict[str, str] = {}
    for lemma_id in ordered_lemma_ids:
        base_name = f"{sanitize_lean_decl_suffix(lemma_id)}"
        unique_name = _uniquify_decl_name(base_name, used_names)
        lemma_decl_names[lemma_id] = unique_name
        used_names.add(unique_name)

    return Phase03DeclNaming(
        problem_id=bundle.problem_id,
        root_decl_name=root_decl_name,
        lemma_decl_names=lemma_decl_names,
        ordered_lemma_ids=tuple(ordered_lemma_ids),
    )


def run_statement_phase(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle: NormalizedProblemBundle,
    decl_naming: Phase03DeclNaming,
    max_repair_rounds: int = 2,
    timeout_seconds: int = 180,
    model: str | None = None,
    claude_runner: ClaudeRunner | None = None,
    provided_statements_text: str | None = None,
    runner: SubprocessRunner = subprocess.run,
    lean4_skills_refs: str = "",
) -> StatementPhaseResult:
    if max_repair_rounds < 0:
        raise ValueError("max_repair_rounds must be >= 0")

    # Create dedicated artifact directory for statement formalization.
    stmts_dir = run_paths.run_root / "statements"
    stmts_dir.mkdir(parents=True, exist_ok=True)

    # Restore .mcp.json if a previous crash left it hidden.
    mcp_json_path = run_paths.workspace_dir / ".mcp.json"
    mcp_json_hidden = run_paths.workspace_dir / ".mcp.json.phase03_hidden"
    if mcp_json_hidden.exists() and not mcp_json_path.exists():
        mcp_json_hidden.rename(mcp_json_path)

    return _run_statement_phase_inner(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        max_repair_rounds=max_repair_rounds,
        timeout_seconds=timeout_seconds,
        model=model,
        claude_runner=claude_runner,
        provided_statements_text=provided_statements_text,
        runner=runner,
        stmts_dir=stmts_dir,
        lean4_skills_refs=lean4_skills_refs,
    )


def _run_statement_phase_inner(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle: NormalizedProblemBundle,
    decl_naming: Phase03DeclNaming,
    max_repair_rounds: int,
    timeout_seconds: int,
    model: str | None,
    claude_runner: ClaudeRunner | None,
    provided_statements_text: str | None,
    runner: SubprocessRunner,
    stmts_dir: Path,
    lean4_skills_refs: str = "",
) -> StatementPhaseResult:
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    check_history: list[dict[str, Any]] = []
    claude_runs: list[dict[str, Any]] = []
    prompt_naming = decl_naming.to_prompt_naming()

    existing_statements_text = statements_path.read_text(encoding="utf-8") if statements_path.exists() else ""
    current_text = ""
    previous_text = existing_statements_text
    round_index = 0
    latest_candidate_selection = CandidateSelection(text="", source="none", reasons=(), lean_score=0)
    statement_validator = _build_statement_stage_validator(decl_naming)
    if provided_statements_text is not None:
        current_text = normalize_lean_text(provided_statements_text)
        latest_candidate_selection = CandidateSelection(
            text=current_text,
            source="provided",
            reasons=("provided statements text used",),
            lean_score=0,
        )
        # Save provided text as round 0 artifact.
        write_text(stmts_dir / "statements_round_00.lean", current_text)
    else:
        prompt = build_statement_translation_prompt(bundle, prompt_naming, lean4_skills_refs=lean4_skills_refs)
        if claude_runner is None:
            claude_runner = ClaudeRunner(runtime_config)

        # Save draft prompt as round 0.
        write_text(stmts_dir / "prompt_round_00.md", prompt)

        # Retry draft generation up to 2 times if Claude fails (stall, crash, etc.)
        max_draft_attempts = 2
        for draft_attempt in range(max_draft_attempts):
            phase_suffix = "" if draft_attempt == 0 else f"_retry{draft_attempt}"
            claude_result = claude_runner.run_prompt(
                run_paths=run_paths,
                prompt=prompt,
                phase_name=f"phase03_statements_draft{phase_suffix}",
                model=model,
                timeout_seconds=runtime_config.claude.timeout_seconds,
                permission_mode="bypassPermissions",
                allowed_tools="mcp,Read,Write,Edit,Glob,Grep,Bash",
            )
            claude_runs.append(claude_result.to_dict())
            # Copy JSONL to statements dir.
            if claude_result.raw_output_path.exists():
                jsonl_name = f"claude_round_00{'_retry' + str(draft_attempt) if draft_attempt > 0 else ''}.jsonl"
                shutil.copy2(claude_result.raw_output_path, stmts_dir / jsonl_name)
            latest_candidate_selection = select_lean_candidate(
                trace=claude_result.trace,
                target_path=statements_path,
                baseline_text=existing_statements_text,
                normalize_text=normalize_lean_text,
                stage_validator=statement_validator,
            )
            draft_text = latest_candidate_selection.text
            if draft_text.strip() or claude_result.ok:
                break  # Got a usable candidate or Claude completed successfully
            # Log retry
            if draft_attempt < max_draft_attempts - 1:
                import sys as _sys
                print(
                    f"[lean_engine] phase03 draft attempt {draft_attempt + 1} failed "
                    f"(returncode={claude_result.returncode}), retrying...",
                    file=_sys.stderr, flush=True,
                )

        if not draft_text.strip() and not claude_result.ok:
            diagnostics = (
                "Claude statement generation failed "
                f"(phase={claude_result.phase_name}, returncode={claude_result.returncode}, "
                f"timed_out={claude_result.timed_out}).",
            )
            _write_statements_result(stmts_dir, "fatal", round_index, diagnostics)
            return StatementPhaseResult(
                status="fatal",
                statements_path=statements_path,
                pinned_signatures_path=None,
                decl_naming=decl_naming,
                attempts_used=0,
                check_history=tuple(check_history),
                claude_runs=tuple(claude_runs),
                error_class="llm_runtime_failure",
                error_scope="statement",
                message="Claude statement generation failed before producing a usable candidate.",
                diagnostics=diagnostics,
                candidate_source=latest_candidate_selection.source,
                candidate_selection_reasons=latest_candidate_selection.reasons,
                candidate_lean_score=latest_candidate_selection.lean_score,
            )
        current_text = draft_text if draft_text.strip() else existing_statements_text

        # Save round 0 artifacts.
        write_text(stmts_dir / "statements_round_00.lean", current_text)
        write_text(stmts_dir / "diff_round_00.patch", _render_statement_diff(previous_text, current_text))

    write_text(statements_path, current_text)

    attempts_used = 0
    latest_check: LeanCommandResult | None = None
    while True:
        attempts_used += 1
        disallowed = find_disallowed_statement_tokens(current_text)
        if disallowed:
            latest_check = LeanCommandResult(
                check_name="file_Orthos_Statements_lean",
                command=("internal", "statement_guard"),
                cwd=run_paths.workspace_dir,
                returncode=1,
                stdout="",
                stderr="\n".join(disallowed),
                duration_seconds=0.0,
            )
        else:
            latest_check = check_lean_file(
                run_paths.workspace_dir,
                "Orthos/Statements.lean",
                timeout_seconds=timeout_seconds,
                runner=runner,
                lake_jobs=runtime_config.lean.lake_jobs,
            )
        check_history.append(latest_check.to_dict())

        # Save diagnostics for the current round.
        diag_text = _lean_diagnostics_text(latest_check) if not latest_check.ok else "Verified: lake env lean passed."
        write_text(stmts_dir / f"diagnostics_round_{round_index:02d}.txt", diag_text)

        if latest_check.ok:
            break

        if attempts_used > max_repair_rounds:
            diagnostics = summarize_diagnostics_from_check(latest_check)
            error_class = _statement_error_class_from_diagnostics(diagnostics)
            message = "Statement translation did not produce a compilable and policy-compliant `Orthos/Statements.lean`."
            if claude_runs:
                last_run = claude_runs[-1]
                if not bool(last_run.get("ok", False)):
                    error_class = "llm_runtime_failure"
                    diagnostics = [
                        (
                            "Claude generation failed "
                            f"(phase={last_run.get('phase_name')}, "
                            f"returncode={last_run.get('returncode')}, "
                            f"timed_out={last_run.get('timed_out')})."
                        ),
                        *diagnostics,
                    ]
                    message = "Claude statement generation failed before producing a usable candidate."
            _write_statements_result(stmts_dir, "fatal", round_index, tuple(diagnostics))
            return StatementPhaseResult(
                status="fatal",
                statements_path=statements_path,
                pinned_signatures_path=None,
                decl_naming=decl_naming,
                attempts_used=attempts_used,
                check_history=tuple(check_history),
                claude_runs=tuple(claude_runs),
                error_class=error_class,
                error_scope="statement",
                message=message,
                diagnostics=tuple(diagnostics),
                candidate_source=latest_candidate_selection.source,
                candidate_selection_reasons=latest_candidate_selection.reasons,
                candidate_lean_score=latest_candidate_selection.lean_score,
            )

        if claude_runner is None:
            claude_runner = ClaudeRunner(runtime_config)

        if claude_runs and not bool(claude_runs[-1].get("ok", False)):
            last_run = claude_runs[-1]
            diagnostics = [
                (
                    "Claude generation failed "
                    f"(phase={last_run.get('phase_name')}, "
                    f"returncode={last_run.get('returncode')}, "
                    f"timed_out={last_run.get('timed_out')})."
                ),
                *summarize_diagnostics_from_check(latest_check),
            ]
            _write_statements_result(stmts_dir, "fatal", round_index, tuple(diagnostics))
            return StatementPhaseResult(
                status="fatal",
                statements_path=statements_path,
                pinned_signatures_path=None,
                decl_naming=decl_naming,
                attempts_used=attempts_used,
                check_history=tuple(check_history),
                claude_runs=tuple(claude_runs),
                error_class="llm_runtime_failure",
                error_scope="statement",
                message="Claude statement generation failed before producing a usable candidate.",
                diagnostics=tuple(diagnostics),
                candidate_source=latest_candidate_selection.source,
                candidate_selection_reasons=latest_candidate_selection.reasons,
                candidate_lean_score=latest_candidate_selection.lean_score,
            )

        # --- Repair round ---
        round_index += 1
        previous_text = current_text

        repair_prompt = build_statement_repair_prompt(
            bundle,
            prompt_naming,
            current_statements_text=current_text,
            diagnostics_text=_lean_diagnostics_text(latest_check),
            repair_round=attempts_used - 1,
            lean4_skills_refs=lean4_skills_refs,
        )
        # Save repair prompt.
        write_text(stmts_dir / f"prompt_round_{round_index:02d}.md", repair_prompt)

        repair_result = claude_runner.run_prompt(
            run_paths=run_paths,
            prompt=repair_prompt,
            phase_name=f"phase03_statements_repair_{attempts_used - 1}",
            model=model,
            timeout_seconds=runtime_config.claude.timeout_seconds,
            permission_mode="bypassPermissions",
            allowed_tools="mcp,Read,Write,Edit,Glob,Grep,Bash",
        )
        claude_runs.append(repair_result.to_dict())
        # Copy JSONL to statements dir.
        if repair_result.raw_output_path.exists():
            shutil.copy2(repair_result.raw_output_path, stmts_dir / f"claude_round_{round_index:02d}.jsonl")
        latest_candidate_selection = select_lean_candidate(
            trace=repair_result.trace,
            target_path=statements_path,
            baseline_text=current_text,
            normalize_text=normalize_lean_text,
            stage_validator=statement_validator,
        )
        repaired_text = latest_candidate_selection.text
        if repaired_text.strip():
            current_text = repaired_text
        write_text(statements_path, current_text)

        # Save repair round artifacts.
        write_text(stmts_dir / f"statements_round_{round_index:02d}.lean", current_text)
        write_text(stmts_dir / f"diff_round_{round_index:02d}.patch",
                    _render_statement_diff(previous_text, current_text))

    # --- Auto-fix wrong declaration names before extracting signatures. ---
    # If Claude used wrong names (e.g. thm_root_XXXX instead of root_prob_XXXX),
    # detect the mismatch and rename deterministically.  This avoids a fatal
    # error for a purely cosmetic naming issue that doesn't affect the math.
    current_text = statements_path.read_text(encoding="utf-8")
    fixed_text = auto_fix_declaration_names(current_text, decl_naming)
    if fixed_text is not None:
        _log.info("Phase 03: auto-fixed declaration names in Statements.lean; re-compiling.")
        write_text(statements_path, fixed_text)
        recheck = check_lean_file(
            run_paths.workspace_dir,
            "Orthos/Statements.lean",
            timeout_seconds=timeout_seconds,
            runner=runner,
            lake_jobs=runtime_config.lean.lake_jobs,
        )
        check_history.append(recheck.to_dict())
        if not recheck.ok:
            # Rename broke compilation — revert to original text.
            _log.warning("Phase 03: auto-fix broke compilation, reverting.")
            write_text(statements_path, current_text)

    try:
        pinned_payload = extract_pinned_signatures(
            statements_path=statements_path,
            bundle=bundle,
            decl_naming=decl_naming,
        )
    except ValueError as exc:
        _write_statements_result(stmts_dir, "fatal", round_index, (str(exc),))
        return StatementPhaseResult(
            status="fatal",
            statements_path=statements_path,
            pinned_signatures_path=None,
            decl_naming=decl_naming,
            attempts_used=attempts_used,
            check_history=tuple(check_history),
            claude_runs=tuple(claude_runs),
            error_class="bad_statement_translation",
            error_scope="statement",
            message="Compiled statements file did not contain all required declaration headers.",
            diagnostics=(str(exc),),
            candidate_source=latest_candidate_selection.source,
            candidate_selection_reasons=latest_candidate_selection.reasons,
            candidate_lean_score=latest_candidate_selection.lean_score,
        )

    pinned_payload["run_name"] = run_paths.run_name
    pinned_payload["run_problem_id"] = run_paths.problem_id
    pinned_signatures_path = run_paths.run_root / "pinned_signatures.json"
    write_json(pinned_signatures_path, pinned_payload)

    _write_statements_result(stmts_dir, "ok", round_index, ())
    return StatementPhaseResult(
        status="ok",
        statements_path=statements_path,
        pinned_signatures_path=pinned_signatures_path,
        decl_naming=decl_naming,
        attempts_used=attempts_used,
        check_history=tuple(check_history),
        claude_runs=tuple(claude_runs),
        candidate_source=latest_candidate_selection.source,
        candidate_selection_reasons=latest_candidate_selection.reasons,
        candidate_lean_score=latest_candidate_selection.lean_score,
    )


def extract_pinned_signatures(
    *,
    statements_path: Path,
    bundle: NormalizedProblemBundle,
    decl_naming: Phase03DeclNaming,
) -> dict[str, Any]:
    text = statements_path.read_text(encoding="utf-8")

    root_sig = extract_statement_signature(text, decl_naming.root_decl_name)
    lemma_entries: list[dict[str, Any]] = []
    for lemma_id in decl_naming.ordered_lemma_ids:
        decl_name = decl_naming.lemma_decl_names[lemma_id]
        signature = extract_statement_signature(text, decl_name)
        if signature.keyword not in {"theorem", "lemma", "axiom"}:
            raise ValueError(
                f"lemma declaration `{decl_name}` must use `theorem`, `lemma`, or `axiom`, found `{signature.keyword}`"
            )
        lemma = bundle.lemma_map[lemma_id]
        lemma_entries.append(
            {
                "lemma_id": lemma_id,
                "decl_name": decl_name,
                "signature": _canonicalize_pinned_signature(signature),
                "keyword": "theorem" if signature.keyword == "axiom" else signature.keyword,
                "statement_nl": lemma.statement_nl,
            }
        )

    return {
        "problem_id": bundle.problem_id,
        "root": {
            "decl_name": decl_naming.root_decl_name,
            "signature": _canonicalize_pinned_signature(root_sig),
            "keyword": "theorem" if root_sig.keyword == "axiom" else root_sig.keyword,
            "statement_nl": bundle.root_theorem.statement_nl,
        },
        "lemmas": lemma_entries,
    }


def extract_statement_signature(text: str, decl_name: str) -> StatementSignature:
    pattern = re.compile(rf"\b(def|abbrev|theorem|lemma|axiom)\s+{re.escape(decl_name)}\b")
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"missing declaration: {decl_name}")

    keyword = match.group(1)
    start = match.start()

    if keyword == "axiom":
        end = _find_axiom_header_end(text, match.end())
        if end is None:
            raise ValueError(f"unable to parse axiom header: {decl_name}")
        header = text[start:end]
    else:
        end = _find_statement_header_end(text, match.end())
        if end is None:
            raise ValueError(f"unable to parse declaration header: {decl_name}")
        header = text[start:end]

    collapsed = normalize_signature_text(header)
    if not collapsed:
        raise ValueError(f"empty declaration header: {decl_name}")

    return StatementSignature(
        decl_name=decl_name,
        signature=collapsed,
        keyword=keyword,
    )


import logging as _logging

_log = _logging.getLogger(__name__)

_STMT_DECL_RE = re.compile(
    r"(?m)^\s*(?:--\s*\[pinned\]\s+)?"
    r"(axiom|theorem|lemma|def|abbrev)\s+([A-Za-z0-9_'.]+)\b"
)


def auto_fix_declaration_names(
    text: str,
    decl_naming: Phase03DeclNaming,
) -> str | None:
    """Rename wrong declaration names in Statements.lean to match expected names.

    When Claude uses the wrong name for a declaration (e.g. ``thm_root_XXXX``
    instead of ``root_prob_XXXX``), this function detects the mismatch and
    performs a deterministic find-and-replace.

    Returns the fixed text if any renames were applied, or ``None`` if the
    file already has the correct names (or the mismatch is too ambiguous to
    fix automatically).
    """
    expected_names = [decl_naming.root_decl_name] + [
        decl_naming.lemma_decl_names[lid] for lid in decl_naming.ordered_lemma_ids
    ]
    expected_set = set(expected_names)

    actual_names = [m.group(2) for m in _STMT_DECL_RE.finditer(text)]
    actual_set = set(actual_names)

    missing = [n for n in expected_names if n not in actual_set]
    if not missing:
        return None  # All expected names already present — nothing to fix.

    extra = [n for n in actual_names if n not in expected_set]

    if len(missing) != len(extra):
        _log.warning(
            "auto_fix_declaration_names: cannot match %d missing to %d extra declarations",
            len(missing), len(extra),
        )
        return None  # Can't unambiguously pair — bail out.

    # Build rename map.  Match extras to missing names by their order of
    # appearance in the file (extras) vs the expected order (missing).
    rename_map = dict(zip(extra, missing))
    _log.info("auto_fix_declaration_names: renaming %s", rename_map)

    fixed = text
    for old_name, new_name in rename_map.items():
        # Word-bounded replacement — safe in Statements.lean which has only
        # axiom headers and no proof bodies.
        fixed = re.sub(rf"\b{re.escape(old_name)}\b", new_name, fixed)

    return fixed


def sanitize_lean_decl_suffix(value: str) -> str:
    lowered = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]", "_", lowered)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "unnamed"
    if cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return cleaned


def normalize_lean_text(text: str) -> str:
    candidate = _extract_probable_lean_code(text)
    candidate = _strip_noise_lines(candidate)
    return candidate.strip() + "\n"


def normalize_signature_text(text: str) -> str:
    cleaned = re.sub(r"/-.*?-/", " ", text, flags=re.DOTALL)
    cleaned_lines: list[str] = []
    for line in cleaned.splitlines():
        without_line_comment = line.split("--", 1)[0]
        cleaned_lines.append(without_line_comment)
    return " ".join(" ".join(cleaned_lines).split())


def find_disallowed_statement_tokens(text: str) -> list[str]:
    issues: list[str] = []
    if re.search(r"\bsorry\b", text):
        issues.append("disallowed token in Statements.lean: sorry")
    if re.search(r"\badmit\b", text):
        issues.append("disallowed token in Statements.lean: admit")
    if re.search(r"(?m)^\s*constant\b", text):
        issues.append("disallowed declaration in Statements.lean: constant")
    return issues


def summarize_diagnostics_from_check(check_result: LeanCommandResult) -> list[str]:
    MAX_LINES = 8
    all_lines = [
        line.strip()
        for line in (check_result.stderr + "\n" + check_result.stdout).splitlines()
        if line.strip()
    ]
    # Prioritize error lines so warnings don't push real errors out of the budget.
    error_lines = [l for l in all_lines if ": error" in l]
    other_lines = [l for l in all_lines if ": error" not in l]

    diagnostics = error_lines[:MAX_LINES]
    remaining = MAX_LINES - len(diagnostics)
    if remaining > 0:
        diagnostics.extend(other_lines[:remaining])

    if not diagnostics:
        diagnostics.append("Lean typecheck failed with no emitted diagnostics.")
    return diagnostics


def _statement_error_class_from_diagnostics(diagnostics: list[str]) -> str:
    text = "\n".join(diagnostics).lower()
    if (
        "reservoir lookup failed" in text
        or "could not materialize package" in text
        or "failed to download" in text
        or "network is unreachable" in text
        or "curl:" in text
    ):
        return "environment_dependency_missing"
    return "bad_statement_translation"


def _build_statement_stage_validator(decl_naming: Phase03DeclNaming) -> Callable[[str], tuple[bool, str | None]]:
    expected_decl_names = [decl_naming.root_decl_name] + [
        decl_naming.lemma_decl_names[lemma_id] for lemma_id in decl_naming.ordered_lemma_ids
    ]

    def _validate(candidate_text: str) -> tuple[bool, str | None]:
        disallowed = find_disallowed_statement_tokens(candidate_text)
        if disallowed:
            return False, disallowed[0]
        for decl_name in expected_decl_names:
            try:
                extract_statement_signature(candidate_text, decl_name)
            except ValueError as exc:
                return False, str(exc)
        return True, None

    return _validate


def _deterministic_lemma_order(bundle: NormalizedProblemBundle) -> list[str]:
    if bundle.topologically_sorted_lemma_ids:
        return list(bundle.topologically_sorted_lemma_ids)
    return sorted(bundle.lemma_map)


def _uniquify_decl_name(base_name: str, used_names: set[str]) -> str:
    if base_name not in used_names:
        return base_name
    suffix = 2
    while True:
        candidate = f"{base_name}_{suffix}"
        if candidate not in used_names:
            return candidate
        suffix += 1


def _find_statement_header_end(text: str, from_index: int) -> int | None:
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escaped = False
    pending_let_assigns = 0  # `:=` tokens consumed by `let` bindings

    i = from_index
    while i < len(text) - 1:
        ch = text[i]
        nxt = text[i + 1]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)

        # Detect `let` keyword at depth 0 — each `let` binding consumes one
        # `:=` that is part of the type, not the proof body delimiter.
        if (depth_paren == 0 and depth_brace == 0 and depth_bracket == 0
                and i + 2 < len(text) and text[i:i + 3] == "let"):
            before_ok = (i == 0) or not (text[i - 1].isalnum() or text[i - 1] == "_")
            after_ok = (i + 3 >= len(text)) or not (text[i + 3].isalnum() or text[i + 3] == "_")
            if before_ok and after_ok:
                pending_let_assigns += 1

        if depth_paren == 0 and depth_brace == 0 and depth_bracket == 0 and ch == ":" and nxt == "=":
            if pending_let_assigns > 0:
                pending_let_assigns -= 1
            else:
                return i

        i += 1

    return None


def _find_axiom_header_end(text: str, from_index: int) -> int | None:
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escaped = False
    saw_colon = False

    i = from_index
    while i < len(text):
        ch = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket = max(0, depth_bracket - 1)
        elif depth_paren == 0 and depth_brace == 0 and depth_bracket == 0 and ch == ":":
            saw_colon = True

        if ch == "\n" and saw_colon and depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
            next_line_start = i + 1
            while next_line_start < len(text):
                next_line_end = text.find("\n", next_line_start)
                if next_line_end == -1:
                    next_line_end = len(text)
                line = text[next_line_start:next_line_end].strip()
                if not line or line.startswith("--"):
                    if next_line_end >= len(text):
                        return next_line_end
                    next_line_start = next_line_end + 1
                    continue
                if line.startswith("/-"):
                    if "-/" in line:
                        if next_line_end >= len(text):
                            return next_line_end
                        next_line_start = next_line_end + 1
                        continue
                    if next_line_end >= len(text):
                        return next_line_end
                    next_line_start = next_line_end + 1
                    while next_line_start < len(text):
                        block_line_end = text.find("\n", next_line_start)
                        if block_line_end == -1:
                            block_line_end = len(text)
                        block_line = text[next_line_start:block_line_end]
                        if "-/" in block_line:
                            if block_line_end >= len(text):
                                return block_line_end
                            next_line_start = block_line_end + 1
                            break
                        if block_line_end >= len(text):
                            return block_line_end
                        next_line_start = block_line_end + 1
                    continue
                if line.startswith("-/"):
                    if next_line_end >= len(text):
                        return next_line_end
                    next_line_start = next_line_end + 1
                    continue
                if re.match(r"^(def|abbrev|theorem|lemma|axiom|namespace|section|end)\b", line):
                    return i
                break
        i += 1

    return len(text) if saw_colon else None


def _lean_diagnostics_text(check_result: LeanCommandResult) -> str:
    parts = [
        f"command: {' '.join(check_result.command)}",
        f"returncode: {check_result.returncode}",
        "",
        "stdout:",
        check_result.stdout.rstrip(),
        "",
        "stderr:",
        check_result.stderr.rstrip(),
    ]
    return "\n".join(parts).strip() + "\n"


def _render_statement_diff(old_text: str, new_text: str) -> str:
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile="previous",
        tofile="current",
    ))


def _write_statements_result(
    stmts_dir: Path,
    status: str,
    rounds_used: int,
    diagnostics: tuple[str, ...],
) -> None:
    write_json(stmts_dir / "result.json", {
        "status": status,
        "rounds_used": rounds_used,
        "diagnostics": list(diagnostics),
    })


def _extract_probable_lean_code(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = _extract_code_fence_blocks(raw)
    if blocks:
        lean_blocks = [body for lang, body in blocks if "lean" in lang]
        if lean_blocks:
            return max(lean_blocks, key=_lean_likeness_score).strip()
        generic = max((body for _, body in blocks), key=_lean_likeness_score)
        if _lean_likeness_score(generic) > 0:
            return generic.strip()

    lines = raw.splitlines()
    start = 0
    while start < len(lines):
        stripped = lines[start].strip()
        if not stripped:
            start += 1
            continue
        if _looks_like_lean_line(stripped):
            break
        start += 1
    candidate = "\n".join(lines[start:]).strip() if start < len(lines) else raw.strip()
    if _lean_likeness_score(candidate) <= 0:
        lowered = raw.lower()
        if _looks_like_claude_stream_json(raw):
            return ""
        if "not logged in" in lowered and "please run /login" in lowered:
            return ""
        return raw.strip()
    return candidate


def _extract_code_fence_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("```"):
            i += 1
            continue
        lang = line[3:].strip().lower()
        i += 1
        body: list[str] = []
        while i < len(lines) and not lines[i].strip().startswith("```"):
            body.append(lines[i])
            i += 1
        blocks.append((lang, "\n".join(body)))
        if i < len(lines):
            i += 1
    return blocks


def _lean_likeness_score(text: str) -> int:
    score = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_lean_line(line):
            score += 2
        elif stripped.startswith("--") or stripped.startswith("/-") or stripped.startswith("-/"):
            score += 1
        else:
            score -= 1
    return score


def _looks_like_lean_line(line: str) -> bool:
    # Lines that begin with a Lean keyword.
    keyword_pattern = (
        r"^(import\s+\S+|open\s+\S+|namespace\b|section\b|end\b|"
        r"variable\b|variables\b|theorem\b|lemma\b|axiom\b|def\b|abbrev\b|"
        r"inductive\b|structure\b|class\b|instance\b|set_option\b|"
        r"attribute\b|noncomputable\b|private\b|protected\b|@[A-Za-z_]|#(check|eval|print)\b)"
    )
    if re.match(keyword_pattern, line) is not None:
        return True
    # Indented continuation lines are part of multi-line type signatures,
    # tactic blocks, or term-mode expressions — all valid Lean.
    if line != line.lstrip():
        return True
    return False


def _looks_like_claude_stream_json(text: str) -> bool:
    inspected = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        inspected += 1
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict) or "type" not in payload:
            return False
        if inspected >= 3:
            return True
    return inspected > 0


def _strip_noise_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Here is") and "Statements.lean" in stripped:
            continue
        if stripped.startswith("Sure") and "Orthos/Statements.lean" in stripped:
            continue
        lines.append(line)
    return "\n".join(lines)


def _canonicalize_pinned_signature(signature: StatementSignature) -> str:
    canonical = normalize_signature_text(signature.signature)
    if signature.keyword != "axiom":
        return canonical
    return re.sub(r"^axiom\b", "theorem", canonical, count=1)


def write_statement_phase_summary(path: Path, result: StatementPhaseResult) -> Path:
    return write_json(path, result.to_dict())


def load_pinned_signatures(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
