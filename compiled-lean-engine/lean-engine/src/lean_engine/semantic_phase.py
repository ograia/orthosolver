from __future__ import annotations

import json
import re
import shutil
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_io import RunPaths, sanitize_component, write_json, write_text
from .claude_runner import ClaudeRunResult, ClaudeRunner, extract_result_text as extract_claude_result_text
from .config import RuntimeConfig
from .contracts import NormalizedProblemBundle, NormalizedLemma
from .final_output import write_fatal_output_bundle, write_success_output_bundle
from .lemma_phase import (
    PinnedLemmaSignature,
    TrustedContextEntry,
    lemma_id_to_path_token,
    load_pinned_lemma_signatures,
    load_trusted_manifest,
)
from .statement_phase import extract_statement_signature

SEMANTIC_MATCH_VALUES = {"yes", "no", "unknown"}
FATAL_CLASSES = {"major_proof_gap", "false_lemma_suspected", "bad_statement_translation"}
KNOWN_CLASSIFICATIONS = {
    "syntax",
    "type_mismatch",
    "missing_import",
    "environment_dependency_missing",
    "tactic_failure",
    "missing_library_fact",
    "major_proof_gap",
    "false_lemma_suspected",
    "bad_statement_translation",
    "unknown_fatal",
    "stale_olean",
}
MAJOR_GAP_PATTERN = re.compile(
    r"(major proof gap|missing mathematical step|cannot be justified|nontrivial jump|unstated assumption)",
    re.IGNORECASE,
)
FALSE_LEMMA_PATTERN = re.compile(r"(counterexample|statement appears false|inconsistent claim)", re.IGNORECASE)


@dataclass(frozen=True)
class SemanticRoundResult:
    round_index: int
    prompt_path: Path
    raw_path: Path
    verdict_path: Path
    match: str
    explanation: str
    drift: str
    classification_hint: str | None
    used_mock_verdict: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "prompt_path": str(self.prompt_path),
            "raw_path": str(self.raw_path),
            "verdict_path": str(self.verdict_path),
            "match": self.match,
            "explanation": self.explanation,
            "drift": self.drift,
            "classification_hint": self.classification_hint,
            "used_mock_verdict": self.used_mock_verdict,
        }


@dataclass(frozen=True)
class LemmaSemanticResult:
    lemma_id: str
    decl_name: str
    status: str
    error_class: str | None
    message: str
    rounds_used: int
    lemma_semantic_dir: Path
    latest_candidate_file: Path | None
    latest_diagnostics: tuple[str, ...]
    phase04_status: str | None
    phase04_error_class: str | None
    semantic_rounds: tuple[SemanticRoundResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lemma_id": self.lemma_id,
            "decl_name": self.decl_name,
            "status": self.status,
            "error_class": self.error_class,
            "message": self.message,
            "rounds_used": self.rounds_used,
            "lemma_semantic_dir": str(self.lemma_semantic_dir),
            "latest_candidate_file": str(self.latest_candidate_file) if self.latest_candidate_file else None,
            "latest_diagnostics": list(self.latest_diagnostics),
            "phase04_status": self.phase04_status,
            "phase04_error_class": self.phase04_error_class,
            "semantic_rounds": [item.to_dict() for item in self.semantic_rounds],
        }


@dataclass(frozen=True)
class Phase05RunResult:
    status: str
    problem_id: str
    run_root: Path
    pinned_signatures_path: Path
    trusted_manifest_path: Path
    semantic_manifest_path: Path
    phase04_summary_path: Path
    phase_summary_path: Path
    lemma_order: tuple[str, ...]
    lemma_results: tuple[LemmaSemanticResult, ...]
    semantically_accepted_lemma_ids: tuple[str, ...]
    final_result_path: Path
    final_summary_path: Path
    error_class: str | None = None
    message: str | None = None
    failed_lemma_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problem_id": self.problem_id,
            "run_root": str(self.run_root),
            "pinned_signatures_path": str(self.pinned_signatures_path),
            "trusted_manifest_path": str(self.trusted_manifest_path),
            "semantic_manifest_path": str(self.semantic_manifest_path),
            "phase04_summary_path": str(self.phase04_summary_path),
            "phase_summary_path": str(self.phase_summary_path),
            "lemma_order": list(self.lemma_order),
            "lemma_results": [item.to_dict() for item in self.lemma_results],
            "semantically_accepted_lemma_ids": list(self.semantically_accepted_lemma_ids),
            "final_result_path": str(self.final_result_path),
            "final_summary_path": str(self.final_summary_path),
            "error_class": self.error_class,
            "message": self.message,
            "failed_lemma_id": self.failed_lemma_id,
        }


def run_phase05(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle: NormalizedProblemBundle,
    pinned_signatures_path: Path,
    trusted_manifest_path: Path,
    phase04_summary_path: Path,
    max_semantic_repairs: int,
    timeout_seconds: int,
    model: str | None,
    target_lemma_id: str | None = None,
    claude_runner: ClaudeRunner | None = None,
    mock_equivalence_verdicts: dict[str, list[dict[str, Any] | str]] | None = None,
    mock_gap_classifications: dict[str, dict[str, Any] | str] | None = None,
) -> Phase05RunResult:
    if max_semantic_repairs < 0:
        raise ValueError("max_semantic_repairs must be >= 0")

    phase_summary_path = run_paths.summaries_dir / "phase05_summary.json"
    semantic_manifest_path = run_paths.run_root / "trusted_context_semantic_manifest.json"
    full_lemma_order = _deterministic_lemma_order(bundle)
    if target_lemma_id is None:
        lemma_order = tuple(full_lemma_order)
    else:
        normalized_target = target_lemma_id.strip()
        if not normalized_target:
            raise ValueError("target_lemma_id must be non-empty when provided")
        if normalized_target not in bundle.lemma_map:
            raise ValueError(f"target_lemma_id not found in normalized bundle: {normalized_target}")
        lemma_order = (normalized_target,)
    try:
        try:
            phase04_summary = _load_phase04_summary(phase04_summary_path, expected_problem_id=bundle.problem_id)
        except ValueError as exc:
            result = _dependency_fatal_result(
                run_paths=run_paths,
                bundle=bundle,
                pinned_signatures_path=pinned_signatures_path,
                trusted_manifest_path=trusted_manifest_path,
                semantic_manifest_path=semantic_manifest_path,
                phase04_summary_path=phase04_summary_path,
                phase_summary_path=phase_summary_path,
                lemma_order=lemma_order,
                error_class="phase_dependency_missing",
                message=str(exc),
            )
            write_json(phase_summary_path, result.to_dict())
            return result

        try:
            pinned_map = load_pinned_lemma_signatures(
                pinned_signatures_path,
                expected_problem_id=bundle.problem_id,
                expected_run_name=run_paths.run_name,
            )
        except ValueError as exc:
            result = _dependency_fatal_result(
                run_paths=run_paths,
                bundle=bundle,
                pinned_signatures_path=pinned_signatures_path,
                trusted_manifest_path=trusted_manifest_path,
                semantic_manifest_path=semantic_manifest_path,
                phase04_summary_path=phase04_summary_path,
                phase_summary_path=phase_summary_path,
                lemma_order=lemma_order,
                error_class="pinned_signature_binding_mismatch",
                message=str(exc),
            )
            write_json(phase_summary_path, result.to_dict())
            return result

        try:
            trusted_entries = load_trusted_manifest(trusted_manifest_path, expected_problem_id=run_paths.problem_id)
        except ValueError as exc:
            result = _dependency_fatal_result(
                run_paths=run_paths,
                bundle=bundle,
                pinned_signatures_path=pinned_signatures_path,
                trusted_manifest_path=trusted_manifest_path,
                semantic_manifest_path=semantic_manifest_path,
                phase04_summary_path=phase04_summary_path,
                phase_summary_path=phase_summary_path,
                lemma_order=lemma_order,
                error_class="manifest_problem_mismatch",
                message=str(exc),
            )
            write_json(phase_summary_path, result.to_dict())
            return result

        trusted_by_lemma: dict[str, TrustedContextEntry] = {}
        for entry in trusted_entries:
            trusted_by_lemma.setdefault(entry.lemma_id, entry)

        phase04_results = _phase04_results_by_lemma(phase04_summary)
        semantic_root = run_paths.run_root / "semantic"
        semantic_root.mkdir(parents=True, exist_ok=True)

        claude = claude_runner or ClaudeRunner(runtime_config)
        lemma_results: list[LemmaSemanticResult] = []
        accepted_entries: list[TrustedContextEntry] = []
        rejected_lemma_ids: list[str] = []
        fatal_target: LemmaSemanticResult | None = None

        for lemma_id in lemma_order:
            lemma = bundle.lemma_map.get(lemma_id)
            pinned = pinned_map.get(lemma_id)
            if lemma is None or pinned is None:
                fatal_target = _missing_contract_lemma_result(
                    lemma_id=lemma_id,
                    semantic_root=semantic_root,
                )
                lemma_results.append(fatal_target)
                rejected_lemma_ids.append(lemma_id)
                break

            lemma_dir = semantic_root / lemma_id_to_path_token(lemma_id)
            lemma_dir.mkdir(parents=True, exist_ok=True)
            phase04_lemma = phase04_results.get(lemma_id)
            trusted_entry = trusted_by_lemma.get(lemma_id)
            latest_candidate_file = _resolve_latest_candidate_path(phase04_lemma)

            if trusted_entry is None:
                unsolved_result = _classify_unsolved_lemma(
                    run_paths=run_paths,
                    lemma=lemma,
                    pinned=pinned,
                    phase04_lemma=phase04_lemma,
                    lemma_dir=lemma_dir,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    claude_runner=claude,
                    mock_gap_classification=(mock_gap_classifications or {}).get(lemma_id),
                )
                lemma_results.append(unsolved_result)
                rejected_lemma_ids.append(lemma_id)
                if fatal_target is None:
                    fatal_target = unsolved_result
                continue

            guard_error = _pinned_signature_guard_error(trusted_entry=trusted_entry, pinned=pinned)
            if guard_error is not None:
                diagnostics = tuple(_latest_phase04_diagnostics(phase04_lemma))
                rejected = LemmaSemanticResult(
                    lemma_id=lemma_id,
                    decl_name=pinned.decl_name,
                    status="rejected",
                    error_class="false_lemma_suspected",
                    message=guard_error,
                    rounds_used=0,
                    lemma_semantic_dir=lemma_dir,
                    latest_candidate_file=latest_candidate_file,
                    latest_diagnostics=diagnostics,
                    phase04_status=_phase04_value(phase04_lemma, "status"),
                    phase04_error_class=_phase04_value(phase04_lemma, "error_class"),
                    semantic_rounds=(),
                )
                lemma_results.append(rejected)
                rejected_lemma_ids.append(lemma_id)
                if fatal_target is None:
                    fatal_target = rejected
                continue

            rounds, accepted = _run_equivalence_rounds(
                run_paths=run_paths,
                lemma=lemma,
                pinned=pinned,
                trusted_entry=trusted_entry,
                lemma_dir=lemma_dir,
                max_semantic_repairs=max_semantic_repairs,
                model=model,
                timeout_seconds=timeout_seconds,
                claude_runner=claude,
                mock_rounds=(mock_equivalence_verdicts or {}).get(lemma_id),
            )
            round_results = tuple(rounds)
            latest_round = round_results[-1] if round_results else None
            latest_diagnostics = tuple(_latest_phase04_diagnostics(phase04_lemma))

            if accepted:
                accepted_entries.append(trusted_entry)
                lemma_results.append(
                    LemmaSemanticResult(
                        lemma_id=lemma_id,
                        decl_name=pinned.decl_name,
                        status="accepted",
                        error_class=None,
                        message="Passed pinned-signature and semantic equivalence guards.",
                        rounds_used=len(round_results),
                        lemma_semantic_dir=lemma_dir,
                        latest_candidate_file=latest_candidate_file,
                        latest_diagnostics=latest_diagnostics,
                        phase04_status=_phase04_value(phase04_lemma, "status"),
                        phase04_error_class=_phase04_value(phase04_lemma, "error_class"),
                        semantic_rounds=round_results,
                    )
                )
                continue

            error_class = _classify_equivalence_failure(
                latest_round=latest_round,
                phase04_error_class=_phase04_value(phase04_lemma, "error_class"),
            )
            message = "Semantic guard rejected lemma statement equivalence."
            if latest_round and latest_round.explanation:
                message = latest_round.explanation
            rejected = LemmaSemanticResult(
                lemma_id=lemma_id,
                decl_name=pinned.decl_name,
                status="rejected",
                error_class=error_class,
                message=message,
                rounds_used=len(round_results),
                lemma_semantic_dir=lemma_dir,
                latest_candidate_file=latest_candidate_file,
                latest_diagnostics=latest_diagnostics,
                phase04_status=_phase04_value(phase04_lemma, "status"),
                phase04_error_class=_phase04_value(phase04_lemma, "error_class"),
                semantic_rounds=round_results,
            )
            lemma_results.append(rejected)
            rejected_lemma_ids.append(lemma_id)
            if fatal_target is None:
                fatal_target = rejected

        write_semantic_manifest(
            semantic_manifest_path,
            sanitize_component(bundle.problem_id),
            accepted_entries,
            rejected_lemma_ids,
        )

        if fatal_target is not None:
            fatal_payload = _build_phase05_fatal_payload(
                bundle=bundle,
                pinned_map=pinned_map,
                phase04_results=phase04_results,
                lemma_result=fatal_target,
            )
            fatal_bundle = write_fatal_output_bundle(run_paths=run_paths, payload=fatal_payload)
            fatal_payload = dict(fatal_payload)
            artifacts = dict(fatal_payload.get("artifacts", {}))
            artifacts["summary_md"] = str(fatal_bundle.summary_path)
            artifacts["result_json"] = str(fatal_bundle.result_path)
            fatal_payload["artifacts"] = artifacts
            write_json(fatal_bundle.result_path, fatal_payload)

            result = Phase05RunResult(
                status="fatal",
                problem_id=bundle.problem_id,
                run_root=run_paths.run_root,
                pinned_signatures_path=pinned_signatures_path,
                trusted_manifest_path=trusted_manifest_path,
                semantic_manifest_path=semantic_manifest_path,
                phase04_summary_path=phase04_summary_path,
                phase_summary_path=phase_summary_path,
                lemma_order=lemma_order,
                lemma_results=tuple(lemma_results),
                semantically_accepted_lemma_ids=tuple(entry.lemma_id for entry in accepted_entries),
                final_result_path=fatal_bundle.result_path,
                final_summary_path=fatal_bundle.summary_path,
                error_class=fatal_target.error_class or "unknown_fatal",
                message=fatal_target.message,
                failed_lemma_id=fatal_target.lemma_id,
            )
            write_json(phase_summary_path, result.to_dict())
            return result

        success_payload = {
            "status": "ok",
            "phase": "phase05",
            "problem_id": bundle.problem_id,
            "lemma_count": len(lemma_order),
            "semantically_accepted_lemma_ids": [entry.lemma_id for entry in accepted_entries],
            "semantic_manifest_path": str(semantic_manifest_path),
        }
        success_summary = "\n".join(
            [
                "# Phase 05 Summary",
                "",
                f"- status: ok",
                f"- problem_id: `{bundle.problem_id}`",
                f"- accepted lemmas: {len(accepted_entries)} / {len(lemma_order)}",
                f"- semantic manifest: `{semantic_manifest_path}`",
                "",
            ]
        )
        success_bundle = write_success_output_bundle(
            run_paths=run_paths,
            payload=success_payload,
            summary_markdown=success_summary,
        )

        result = Phase05RunResult(
            status="ok",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            semantic_manifest_path=semantic_manifest_path,
            phase04_summary_path=phase04_summary_path,
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            lemma_results=tuple(lemma_results),
            semantically_accepted_lemma_ids=tuple(entry.lemma_id for entry in accepted_entries),
            final_result_path=success_bundle.result_path,
            final_summary_path=success_bundle.summary_path,
        )
        write_json(phase_summary_path, result.to_dict())
        return result
    except Exception as exc:
        diagnostics = (
            f"{type(exc).__name__}: {exc}",
            traceback.format_exc(limit=12).strip(),
        )
        result = _dependency_fatal_result(
            run_paths=run_paths,
            bundle=bundle,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            semantic_manifest_path=semantic_manifest_path,
            phase04_summary_path=phase04_summary_path,
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            error_class="unknown_fatal",
            message="Phase 05 failed due to an unexpected internal error.",
            diagnostics=diagnostics,
        )
        write_json(phase_summary_path, result.to_dict())
        return result


def write_semantic_manifest(
    path: Path,
    problem_id: str,
    entries: list[TrustedContextEntry],
    rejected_lemma_ids: list[str],
) -> Path:
    payload = {
        "problem_id": problem_id,
        "entry_count": len(entries),
        "entries": [entry.to_dict() for entry in entries],
        "rejected_lemma_ids": list(rejected_lemma_ids),
    }
    return write_json(path, payload)


def build_equivalence_prompt(
    *,
    lemma: NormalizedLemma,
    pinned: PinnedLemmaSignature,
    declaration_signature: str,
    declaration_block: str,
    round_index: int,
    prior_verdict: dict[str, Any] | None,
) -> str:
    lines = [
        "You are the Phase 05 semantic-equivalence judge.",
        "",
        "Task: determine whether the Lean statement still matches the NL statement package.",
        "Return ONLY JSON with schema:",
        "{",
        '  "match": "yes|no|unknown",',
        '  "explanation": "short explanation",',
        '  "drift": "if mismatch, what changed semantically",',
        '  "classification_hint": "bad_statement_translation|major_proof_gap|false_lemma_suspected|unknown"',
        "}",
        "",
        f"Semantic round: {round_index}",
        f"lemma_id: {lemma.lemma_id}",
        f"decl_name: {pinned.decl_name}",
        "",
        "Pinned signature (must stay exact):",
        pinned.signature,
        "",
        "Candidate declaration signature:",
        declaration_signature,
        "",
        "NL statement package:",
        f"- statement_nl: {lemma.statement_nl}",
        f"- semantic_sketch: {json.dumps(lemma.semantic_sketch, ensure_ascii=True, sort_keys=True)}",
        f"- proof_nl: {lemma.proof_nl}",
        "",
        "Candidate declaration block:",
        declaration_block.strip(),
    ]
    if prior_verdict:
        lines.extend(
            [
                "",
                "Prior semantic verdict (repair context):",
                json.dumps(prior_verdict, indent=2, ensure_ascii=True, sort_keys=True),
                "",
                "If previous output was `unknown`, resolve to `yes` or `no` when possible.",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def build_proof_gap_classification_prompt(
    *,
    lemma: NormalizedLemma,
    pinned: PinnedLemmaSignature,
    phase04_error_class: str | None,
    phase04_diagnostics: list[str],
    phase04_attempt_count: int,
) -> str:
    lines = [
        "You are classifying a failed lemma formalization (Phase 05).",
        "",
        "Return ONLY JSON with schema:",
        "{",
        '  "classification": "syntax|type_mismatch|missing_import|environment_dependency_missing|tactic_failure|missing_library_fact|major_proof_gap|false_lemma_suspected|bad_statement_translation|unknown_fatal",',
        '  "is_fatal": true|false,',
        '  "rationale": "short explanation",',
        '  "math_gap_description": "required if classification is major_proof_gap"',
        "}",
        "",
        "Guidance:",
        "- Use `major_proof_gap` when NL reasoning has a real missing mathematical step.",
        "- Use non-fatal classes for purely Lean-mechanical blockers.",
        "",
        f"lemma_id: {lemma.lemma_id}",
        f"decl_name: {pinned.decl_name}",
        f"phase04_error_class: {phase04_error_class or 'unknown'}",
        f"phase04_attempt_count: {phase04_attempt_count}",
        "",
        f"statement_nl: {lemma.statement_nl}",
        f"semantic_sketch: {json.dumps(lemma.semantic_sketch, ensure_ascii=True, sort_keys=True)}",
        f"proof_nl: {lemma.proof_nl}",
        f"pinned_signature: {pinned.signature}",
        "",
        "Latest Phase 04 diagnostics:",
        json.dumps(phase04_diagnostics[:12], indent=2, ensure_ascii=True),
    ]
    return "\n".join(lines).strip() + "\n"


def _run_equivalence_rounds(
    *,
    run_paths: RunPaths,
    lemma: NormalizedLemma,
    pinned: PinnedLemmaSignature,
    trusted_entry: TrustedContextEntry,
    lemma_dir: Path,
    max_semantic_repairs: int,
    model: str | None,
    timeout_seconds: int,
    claude_runner: ClaudeRunner,
    mock_rounds: list[dict[str, Any] | str] | None,
) -> tuple[list[SemanticRoundResult], bool]:
    rounds: list[SemanticRoundResult] = []
    declaration_signature = _declaration_signature(trusted_entry.declaration, pinned.decl_name)
    prior: dict[str, Any] | None = None
    total_rounds = 1 + max_semantic_repairs

    for round_index in range(1, total_rounds + 1):
        prompt_path = lemma_dir / f"equivalence_prompt_round_{round_index:02d}.md"
        raw_path = lemma_dir / f"equivalence_round_{round_index:02d}.jsonl"
        verdict_path = lemma_dir / f"equivalence_round_{round_index:02d}.json"

        prompt = build_equivalence_prompt(
            lemma=lemma,
            pinned=pinned,
            declaration_signature=declaration_signature,
            declaration_block=trusted_entry.declaration,
            round_index=round_index,
            prior_verdict=prior,
        )
        write_text(prompt_path, prompt)

        mock = mock_rounds[round_index - 1] if mock_rounds and round_index - 1 < len(mock_rounds) else None
        used_mock_verdict = mock is not None
        if mock is not None:
            verdict = _normalize_equivalence_verdict(mock)
            _write_mock_raw(raw_path, verdict)
        else:
            phase_name = f"phase05_equivalence_{sanitize_component(lemma.lemma_id)}_{round_index:02d}"
            call_diagnostics_path = lemma_dir / f"equivalence_round_{round_index:02d}_call.json"
            claude = _run_phase05_prompt(
                run_paths=run_paths,
                claude_runner=claude_runner,
                prompt=prompt,
                phase_name=phase_name,
                model=model,
                timeout_seconds=timeout_seconds,
                lemma_id=lemma.lemma_id,
                round_index=round_index,
                diagnostics_path=call_diagnostics_path,
            )
            _copy_raw(claude, raw_path)
            verdict = _parse_equivalence_verdict(_extract_result_text(claude))

        write_json(verdict_path, verdict)
        round_result = SemanticRoundResult(
            round_index=round_index,
            prompt_path=prompt_path,
            raw_path=raw_path,
            verdict_path=verdict_path,
            match=str(verdict.get("match", "unknown")),
            explanation=str(verdict.get("explanation", "")).strip(),
            drift=str(verdict.get("drift", "")).strip(),
            classification_hint=_normalize_classification_hint(verdict.get("classification_hint")),
            used_mock_verdict=used_mock_verdict,
        )
        rounds.append(round_result)
        prior = verdict
        if round_result.match == "yes":
            return rounds, True

    return rounds, False


def _classify_unsolved_lemma(
    *,
    run_paths: RunPaths,
    lemma: NormalizedLemma,
    pinned: PinnedLemmaSignature,
    phase04_lemma: dict[str, Any] | None,
    lemma_dir: Path,
    model: str | None,
    timeout_seconds: int,
    claude_runner: ClaudeRunner,
    mock_gap_classification: dict[str, Any] | str | None,
) -> LemmaSemanticResult:
    phase04_status = _phase04_value(phase04_lemma, "status")
    phase04_error_class = _phase04_value(phase04_lemma, "error_class")
    phase04_attempt_count = _phase04_attempt_count(phase04_lemma)
    diagnostics = _latest_phase04_diagnostics(phase04_lemma)

    if phase04_error_class in KNOWN_CLASSIFICATIONS:
        classification = phase04_error_class
        rationale = "Reusing bounded Phase 04 terminal classification."
        math_gap = ""
    else:
        prompt_path = lemma_dir / "proof_gap_prompt.md"
        raw_path = lemma_dir / "proof_gap_raw.jsonl"
        verdict_path = lemma_dir / "proof_gap_verdict.json"
        prompt = build_proof_gap_classification_prompt(
            lemma=lemma,
            pinned=pinned,
            phase04_error_class=phase04_error_class,
            phase04_diagnostics=diagnostics,
            phase04_attempt_count=phase04_attempt_count,
        )
        write_text(prompt_path, prompt)

        if mock_gap_classification is not None:
            verdict = _normalize_gap_classification(mock_gap_classification)
            _write_mock_raw(raw_path, verdict)
        else:
            phase_name = f"phase05_gap_{sanitize_component(lemma.lemma_id)}"
            call_diagnostics_path = lemma_dir / "proof_gap_call.json"
            claude = _run_phase05_prompt(
                run_paths=run_paths,
                claude_runner=claude_runner,
                prompt=prompt,
                phase_name=phase_name,
                model=model,
                timeout_seconds=timeout_seconds,
                lemma_id=lemma.lemma_id,
                round_index=None,
                diagnostics_path=call_diagnostics_path,
            )
            _copy_raw(claude, raw_path)
            verdict = _parse_gap_classification(_extract_result_text(claude))

        write_json(verdict_path, verdict)
        classification = str(verdict.get("classification", "unknown_fatal"))
        if classification not in KNOWN_CLASSIFICATIONS:
            classification = "unknown_fatal"
        rationale = str(verdict.get("rationale", "")).strip()
        math_gap = str(verdict.get("math_gap_description", "")).strip()

        if not rationale:
            rationale = "Lemma did not reach compiler-accepted status in Phase 04."

    message = rationale or "Lemma did not reach compiler-accepted status in Phase 04."
    if classification == "major_proof_gap" and not math_gap:
        math_gap = "Phase 04 attempts repeatedly failed to justify a missing mathematical step."
    if classification == "major_proof_gap" and math_gap:
        message = math_gap

    return LemmaSemanticResult(
        lemma_id=lemma.lemma_id,
        decl_name=pinned.decl_name,
        status="rejected",
        error_class=classification,
        message=message,
        rounds_used=0,
        lemma_semantic_dir=lemma_dir,
        latest_candidate_file=_resolve_latest_candidate_path(phase04_lemma),
        latest_diagnostics=tuple(diagnostics),
        phase04_status=phase04_status,
        phase04_error_class=phase04_error_class,
        semantic_rounds=(),
    )


def _build_phase05_fatal_payload(
    *,
    bundle: NormalizedProblemBundle,
    pinned_map: dict[str, PinnedLemmaSignature],
    phase04_results: dict[str, dict[str, Any]],
    lemma_result: LemmaSemanticResult,
) -> dict[str, Any]:
    lemma = bundle.lemma_map.get(lemma_result.lemma_id)
    pinned = pinned_map.get(lemma_result.lemma_id)
    phase04_lemma = phase04_results.get(lemma_result.lemma_id)
    error_class = lemma_result.error_class or "unknown_fatal"
    error_scope = "proof"
    if error_class == "bad_statement_translation":
        error_scope = "statement"

    math_gap_description = ""
    if error_class == "major_proof_gap":
        math_gap_description = lemma_result.message

    artifacts = {
        "lemma_dir": str(lemma_result.lemma_semantic_dir),
        "latest_candidate_file": str(lemma_result.latest_candidate_file) if lemma_result.latest_candidate_file else "",
        "summary_md": str(lemma_result.lemma_semantic_dir.parent.parent / "final" / "fatal_summary.md"),
    }
    message = lemma_result.message
    if not message:
        message = "Phase 05 semantic guards rejected the lemma."

    return {
        "status": "fatal",
        "error_class": error_class,
        "error_scope": error_scope,
        "problem_id": bundle.problem_id,
        "target_id": lemma_result.lemma_id,
        "decl_name": lemma_result.decl_name,
        "message": message,
        "math_gap_description": math_gap_description,
        "evidence": {
            "statement_nl": lemma.statement_nl if lemma else "",
            "proof_nl_excerpt": _excerpt(lemma.proof_nl if lemma else "", 400),
            "pinned_signature": pinned.signature if pinned else "",
            "latest_diagnostics": list(lemma_result.latest_diagnostics),
            "attempt_count": _phase04_attempt_count(phase04_lemma),
        },
        "artifacts": artifacts,
    }


def _classify_equivalence_failure(
    *,
    latest_round: SemanticRoundResult | None,
    phase04_error_class: str | None,
) -> str:
    if latest_round and latest_round.classification_hint in FATAL_CLASSES:
        return str(latest_round.classification_hint)

    haystack = " ".join(
        item
        for item in [
            latest_round.explanation if latest_round else "",
            latest_round.drift if latest_round else "",
            phase04_error_class or "",
        ]
        if item
    )
    if MAJOR_GAP_PATTERN.search(haystack):
        return "major_proof_gap"
    if FALSE_LEMMA_PATTERN.search(haystack):
        return "false_lemma_suspected"
    if phase04_error_class in FATAL_CLASSES:
        return str(phase04_error_class)
    return "bad_statement_translation"


def _pinned_signature_guard_error(
    *,
    trusted_entry: TrustedContextEntry,
    pinned: PinnedLemmaSignature,
) -> str | None:
    trusted_sig = trusted_entry.signature.strip()
    if trusted_sig and _collapse_ws(trusted_sig) != _collapse_ws(pinned.signature):
        return (
            "Pinned-signature guard failed: trusted manifest signature does not match pinned signature.\n"
            f"expected: {pinned.signature}\n"
            f"actual:   {trusted_sig}"
        )
    try:
        declaration_sig = _declaration_signature(trusted_entry.declaration, pinned.decl_name)
    except ValueError as exc:
        return f"Pinned-signature guard failed: could not parse declaration signature ({exc})."

    if _collapse_ws(declaration_sig) != _collapse_ws(pinned.signature):
        return (
            "Pinned-signature guard failed: final declaration header drifted from pinned signature.\n"
            f"expected: {pinned.signature}\n"
            f"actual:   {declaration_sig}"
        )
    return None


def _declaration_signature(declaration_block: str, decl_name: str) -> str:
    signature = extract_statement_signature(declaration_block, decl_name).signature
    return signature


def _parse_equivalence_verdict(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {
            "match": "unknown",
            "explanation": "empty response",
            "drift": "",
            "classification_hint": "unknown",
        }
    try:
        payload = json.loads(_extract_json(raw))
    except Exception:
        lowered = raw.lower()
        match = "unknown"
        if "match" in lowered and "yes" in lowered:
            match = "yes"
        elif "match" in lowered and "no" in lowered:
            match = "no"
        classification = "unknown"
        if MAJOR_GAP_PATTERN.search(raw):
            classification = "major_proof_gap"
        elif FALSE_LEMMA_PATTERN.search(raw):
            classification = "false_lemma_suspected"
        return {
            "match": match,
            "explanation": "unparsed response",
            "drift": "",
            "classification_hint": classification,
            "raw_excerpt": raw[:800],
        }

    return _normalize_equivalence_verdict(payload)


def _normalize_equivalence_verdict(payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _parse_equivalence_verdict(payload)
    if not isinstance(payload, dict):
        return {
            "match": "unknown",
            "explanation": "invalid verdict payload",
            "drift": "",
            "classification_hint": "unknown",
        }
    match = str(payload.get("match", "unknown")).strip().lower()
    if match not in SEMANTIC_MATCH_VALUES:
        match = "unknown"
    explanation = str(payload.get("explanation", "")).strip() or "no explanation provided"
    drift = str(payload.get("drift", "")).strip()
    classification_hint = _normalize_classification_hint(payload.get("classification_hint"))
    return {
        "match": match,
        "explanation": explanation,
        "drift": drift,
        "classification_hint": classification_hint or "unknown",
    }


def _normalize_classification_hint(value: Any) -> str | None:
    hint = str(value or "").strip().lower()
    if hint in {"bad_statement_translation", "major_proof_gap", "false_lemma_suspected", "unknown"}:
        return hint
    return None


def _parse_gap_classification(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {
            "classification": "unknown_fatal",
            "is_fatal": True,
            "rationale": "empty response",
            "math_gap_description": "",
        }
    try:
        payload = json.loads(_extract_json(raw))
    except Exception:
        lowered = raw.lower()
        classification = "unknown_fatal"
        if MAJOR_GAP_PATTERN.search(raw):
            classification = "major_proof_gap"
        elif FALSE_LEMMA_PATTERN.search(raw):
            classification = "false_lemma_suspected"
        elif "type mismatch" in lowered:
            classification = "type_mismatch"
        elif "syntax" in lowered or "parse error" in lowered:
            classification = "syntax"
        elif (
            "reservoir lookup failed" in lowered
            or "could not materialize package" in lowered
            or "failed to download" in lowered
            or "network is unreachable" in lowered
            or "curl:" in lowered
        ):
            classification = "environment_dependency_missing"
        return {
            "classification": classification,
            "is_fatal": classification in FATAL_CLASSES or classification == "unknown_fatal",
            "rationale": "unparsed response",
            "math_gap_description": raw[:800] if classification == "major_proof_gap" else "",
        }
    return _normalize_gap_classification(payload)


def _normalize_gap_classification(payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _parse_gap_classification(payload)
    if not isinstance(payload, dict):
        return {
            "classification": "unknown_fatal",
            "is_fatal": True,
            "rationale": "invalid payload",
            "math_gap_description": "",
        }

    classification = str(payload.get("classification", "unknown_fatal")).strip().lower()
    if classification not in KNOWN_CLASSIFICATIONS:
        classification = "unknown_fatal"
    is_fatal = payload.get("is_fatal")
    if isinstance(is_fatal, bool):
        normalized_fatal = is_fatal
    elif isinstance(is_fatal, str):
        normalized_fatal = is_fatal.strip().lower() in {"1", "true", "yes", "y"}
    else:
        normalized_fatal = classification in FATAL_CLASSES or classification == "unknown_fatal"
    rationale = str(payload.get("rationale", "")).strip() or "no rationale provided"
    math_gap_description = str(payload.get("math_gap_description", "")).strip()
    return {
        "classification": classification,
        "is_fatal": normalized_fatal,
        "rationale": rationale,
        "math_gap_description": math_gap_description,
    }


def _load_phase04_summary(path: Path, *, expected_problem_id: str) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"phase04 summary file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("phase04 summary must be a JSON object")
    problem_id = str(payload.get("problem_id", "")).strip()
    if sanitize_component(problem_id) != sanitize_component(expected_problem_id):
        raise ValueError(
            "phase04 summary problem binding mismatch: "
            f"expected `{sanitize_component(expected_problem_id)}`, found `{sanitize_component(problem_id)}`"
        )
    return payload


def _phase04_results_by_lemma(phase04_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    raw = phase04_summary.get("lemma_results", [])
    if not isinstance(raw, list):
        return results
    for item in raw:
        if not isinstance(item, dict):
            continue
        lemma_id = str(item.get("lemma_id", "")).strip()
        if not lemma_id:
            continue
        results[lemma_id] = item
    return results


def _resolve_latest_candidate_path(phase04_lemma: dict[str, Any] | None) -> Path | None:
    if not isinstance(phase04_lemma, dict):
        return None
    merged_authoritative = str(phase04_lemma.get("merged_authoritative_path", "")).strip()
    if merged_authoritative:
        return Path(merged_authoritative)
    provisional_verified = str(phase04_lemma.get("provisional_verified_path", "")).strip()
    if provisional_verified:
        return Path(provisional_verified)
    legacy_final_success = str(phase04_lemma.get("final_success_path", "")).strip()
    if legacy_final_success:
        return Path(legacy_final_success)
    attempts = phase04_lemma.get("attempts", [])
    if not isinstance(attempts, list) or not attempts:
        return None
    latest = attempts[-1]
    if not isinstance(latest, dict):
        return None
    scratch = str(latest.get("scratch_path", "")).strip()
    return Path(scratch) if scratch else None


def _latest_phase04_diagnostics(phase04_lemma: dict[str, Any] | None) -> list[str]:
    if not isinstance(phase04_lemma, dict):
        return ["phase04 lemma result is missing"]
    attempts = phase04_lemma.get("attempts", [])
    if isinstance(attempts, list) and attempts:
        latest = attempts[-1]
        if isinstance(latest, dict):
            diagnostics = latest.get("diagnostics", [])
            if isinstance(diagnostics, list) and diagnostics:
                return [str(item) for item in diagnostics[:12]]
            progress_guard_error = str(latest.get("progress_guard_error", "")).strip()
            if progress_guard_error:
                return [progress_guard_error]
    top_level = phase04_lemma.get("diagnostics", [])
    if isinstance(top_level, list) and top_level:
        return [str(item) for item in top_level[:12]]
    return ["no diagnostics emitted"]


def _phase04_attempt_count(phase04_lemma: dict[str, Any] | None) -> int:
    if not isinstance(phase04_lemma, dict):
        return 0
    attempts = phase04_lemma.get("attempts", [])
    if isinstance(attempts, list):
        return len(attempts)
    attempts_used = phase04_lemma.get("attempts_used")
    if isinstance(attempts_used, int):
        return attempts_used
    return 0


def _phase04_value(phase04_lemma: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(phase04_lemma, dict):
        return None
    value = phase04_lemma.get(key)
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _dependency_fatal_result(
    *,
    run_paths: RunPaths,
    bundle: NormalizedProblemBundle,
    pinned_signatures_path: Path,
    trusted_manifest_path: Path,
    semantic_manifest_path: Path,
    phase04_summary_path: Path,
    phase_summary_path: Path,
    lemma_order: tuple[str, ...],
    error_class: str,
    message: str,
    diagnostics: tuple[str, ...] | None = None,
) -> Phase05RunResult:
    payload = {
        "status": "fatal",
        "error_class": error_class,
        "error_scope": "phase05",
        "problem_id": bundle.problem_id,
        "target_id": "",
        "decl_name": "",
        "message": message,
        "math_gap_description": "",
        "evidence": {
            "statement_nl": "",
            "proof_nl_excerpt": "",
            "pinned_signature": "",
            "latest_diagnostics": list(diagnostics or (message,)),
            "attempt_count": 0,
        },
        "artifacts": {
            "phase04_summary_path": str(phase04_summary_path),
            "trusted_manifest_path": str(trusted_manifest_path),
            "summary_md": str(run_paths.run_root / "final" / "fatal_summary.md"),
        },
    }
    bundle_paths = write_fatal_output_bundle(run_paths=run_paths, payload=payload)
    payload["artifacts"]["result_json"] = str(bundle_paths.result_path)
    write_json(bundle_paths.result_path, payload)
    write_semantic_manifest(semantic_manifest_path, sanitize_component(bundle.problem_id), [], [])

    return Phase05RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        pinned_signatures_path=pinned_signatures_path,
        trusted_manifest_path=trusted_manifest_path,
        semantic_manifest_path=semantic_manifest_path,
        phase04_summary_path=phase04_summary_path,
        phase_summary_path=phase_summary_path,
        lemma_order=lemma_order,
        lemma_results=(),
        semantically_accepted_lemma_ids=(),
        final_result_path=bundle_paths.result_path,
        final_summary_path=bundle_paths.summary_path,
        error_class=error_class,
        message=message,
    )


def _missing_contract_lemma_result(*, lemma_id: str, semantic_root: Path) -> LemmaSemanticResult:
    lemma_dir = semantic_root / lemma_id_to_path_token(lemma_id)
    lemma_dir.mkdir(parents=True, exist_ok=True)
    return LemmaSemanticResult(
        lemma_id=lemma_id,
        decl_name="",
        status="rejected",
        error_class="malformed_input_artifact",
        message=f"phase05 input mismatch: missing lemma or pinned signature for `{lemma_id}`",
        rounds_used=0,
        lemma_semantic_dir=lemma_dir,
        latest_candidate_file=None,
        latest_diagnostics=("missing lemma or pinned signature",),
        phase04_status=None,
        phase04_error_class=None,
        semantic_rounds=(),
    )


def _deterministic_lemma_order(bundle: NormalizedProblemBundle) -> list[str]:
    if bundle.topologically_sorted_lemma_ids:
        return list(bundle.topologically_sorted_lemma_ids)
    return [lemma.lemma_id for lemma in bundle.lemmas]


def _collapse_ws(text: str) -> str:
    """Collapse whitespace and treat ``;`` as whitespace for signature comparison.

    In Lean 4, ``;`` and newlines are interchangeable as let-binding separators.
    When ``normalize_signature_text()`` collapses newlines to spaces, Claude may
    re-insert ``;`` to keep the syntax valid.  Both forms must compare equal.
    """
    return " ".join(text.replace(";", " ").split())


def _extract_result_text(result: ClaudeRunResult) -> str:
    return extract_claude_result_text(result)


def _run_phase05_prompt(
    *,
    run_paths: RunPaths,
    claude_runner: ClaudeRunner,
    prompt: str,
    phase_name: str,
    model: str | None,
    timeout_seconds: int,
    lemma_id: str,
    round_index: int | None,
    diagnostics_path: Path,
) -> ClaudeRunResult:
    call_diagnostics: dict[str, Any] = {
        "phase_name": phase_name,
        "lemma_id": lemma_id,
        "round_index": round_index,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "permission_mode": "bypassPermissions",
        "status": "started",
    }
    write_json(diagnostics_path, call_diagnostics)
    try:
        result = claude_runner.run_prompt(
            run_paths=run_paths,
            prompt=prompt,
            phase_name=phase_name,
            model=model,
            timeout_seconds=timeout_seconds,
            permission_mode="bypassPermissions",
        )
    except Exception as exc:
        call_diagnostics["status"] = "failed"
        call_diagnostics["error"] = f"{type(exc).__name__}: {exc}"
        write_json(diagnostics_path, call_diagnostics)
        raise

    call_diagnostics.update(
        {
            "status": "completed",
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "timeout_reason": result.timeout_reason,
            "duration_seconds": result.duration_seconds,
            "provider_limit_detected": result.provider_limit_detected,
            "provider_timeout_detected": result.provider_timeout_detected,
            "raw_output_path": str(result.raw_output_path),
            "summary_path": str(result.summary_path),
        }
    )
    write_json(diagnostics_path, call_diagnostics)
    return result


def _copy_raw(result: ClaudeRunResult, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result.raw_output_path, destination)


def _write_mock_raw(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps({"type": "result", "result": json.dumps(payload), "mock": True}) + "\n")


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "{}"
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        candidate = match.group(0)
        json.loads(candidate)
        return candidate
    raise ValueError("no json object found")


def _excerpt(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "..."
