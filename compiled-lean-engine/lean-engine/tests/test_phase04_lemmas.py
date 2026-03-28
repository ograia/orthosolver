from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from lean_engine.artifact_io import create_run_paths, sanitize_component
from lean_engine.claude_runner import ClaudeRunResult, ClaudeRunTrace, FileUpdateEvent
from lean_engine.config import load_runtime_config
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.lemma_phase import (
    AssumptionCapsule,
    PinnedLemmaSignature,
    TrustedContextEntry,
    validate_lemma_candidate_structure,
    build_lemma_formalization_prompt,
    extract_proof_block_with_helpers,
    extract_target_declaration_block,
    lemma_id_to_path_token,
    load_pinned_lemma_signatures,
    merge_declaration_into_lemmas_file,
    progress_guard_error,
    run_lemma_formalization,
)
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.phase04 import build_dependency_graph, run_phase04
from lean_engine.statement_phase import sanitize_lean_decl_suffix
from lean_engine.workspace import create_workspace_from_template


def _ok_lean_result(check_name: str = "mock", cwd: Path = Path("/tmp")) -> LeanCommandResult:
    return LeanCommandResult(
        check_name=check_name,
        command=("lake", "build"),
        cwd=cwd,
        returncode=0,
        stdout="",
        stderr="",
        duration_seconds=0.0,
    )


@pytest.fixture(autouse=True)
def _mock_lean_compilation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock all Lean compilation calls in phase04 tests.

    Tests in this file exercise Phase 04 orchestration and data-flow logic.
    They do not need a real Lean environment — mocking prevents accidental
    13GB .lake/ copies from the production cache into tmp_path.
    """
    import lean_engine.phase04 as _phase04
    import lean_engine.lemma_phase as _lemma_phase
    import lean_engine.workspace as _workspace

    monkeypatch.setattr(_phase04, "rebuild_module_olean", lambda *_a, **_kw: _ok_lean_result("rebuild_module_olean"))
    monkeypatch.setattr(_phase04, "_validate_olean_freshness", lambda *_a, **_kw: True)
    monkeypatch.setattr(_lemma_phase, "check_lean_file", lambda *_a, **_kw: _ok_lean_result("check_lean_file"))
    monkeypatch.setattr(_workspace, "link_lake_cache", lambda *_a, **_kw: False)


def _single_lemma_payload(problem_id: str = "prob_phase04") -> dict:
    return {
        "problem_id": problem_id,
        "title": "Phase 04 synthetic",
        "verification_level": "nl_only",
        "root_theorem": {
            "theorem_id": "thm_root",
            "statement_nl": "Root statement",
            "semantic_sketch": {"normalized_claim": "root"},
        },
        "selected_decomposition": {
            "decomposition_id": "dec_1",
            "assembly_plan": {
                "assembly_plan_id": "asm_1",
                "steps": [
                    {
                        "step_id": "S1",
                        "uses_lemmas": ["lem_1"],
                        "uses_prior_steps": [],
                        "derives": "first",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    }
                ],
                "proof_skeleton_nl": "sketch",
                "is_trivially_composable": True,
            },
        },
        "lemmas": [
            {
                "lemma_id": "lem_1",
                "statement_nl": "lemma one",
                "semantic_sketch": {"normalized_claim": "l1"},
                "proof_nl": "Proof sketch for lemma one.",
            }
        ],
        "all_visible_lemmas_nl_accepted": True,
    }


def _two_lemma_payload(problem_id: str = "prob_phase04_two") -> dict:
    return {
        "problem_id": problem_id,
        "title": "Phase 04 synthetic two lemmas",
        "verification_level": "nl_only",
        "root_theorem": {
            "theorem_id": "thm_root",
            "statement_nl": "Root statement",
            "semantic_sketch": {"normalized_claim": "root"},
        },
        "selected_decomposition": {
            "decomposition_id": "dec_1",
            "assembly_plan": {
                "assembly_plan_id": "asm_1",
                "steps": [
                    {
                        "step_id": "S1",
                        "uses_lemmas": ["lem_1"],
                        "uses_prior_steps": [],
                        "derives": "first",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                    {
                        "step_id": "S2",
                        "uses_lemmas": ["lem_2"],
                        "uses_prior_steps": ["S1"],
                        "derives": "second",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                ],
                "proof_skeleton_nl": "sketch",
                "is_trivially_composable": True,
            },
        },
        "lemmas": [
            {
                "lemma_id": "lem_1",
                "statement_nl": "lemma one",
                "semantic_sketch": {"normalized_claim": "l1"},
                "proof_nl": "proof one",
            },
            {
                "lemma_id": "lem_2",
                "statement_nl": "lemma two",
                "semantic_sketch": {"normalized_claim": "l2"},
                "proof_nl": "proof two",
            },
        ],
        "all_visible_lemmas_nl_accepted": True,
    }


def _two_independent_lemmas_payload(problem_id: str = "prob_phase04_independent") -> dict:
    payload = _two_lemma_payload(problem_id=problem_id)
    payload["selected_decomposition"]["assembly_plan"]["steps"] = [
        {
            "step_id": "S1",
            "uses_lemmas": ["lem_1", "lem_2"],
            "uses_prior_steps": [],
            "derives": "both",
            "is_trivial": True,
            "trivial_justification": "given",
        }
    ]
    return payload


def _two_lemma_collision_payload(problem_id: str = "prob_phase04_collision") -> dict:
    payload = _two_lemma_payload(problem_id=problem_id)
    payload["lemmas"][0]["lemma_id"] = "lem/a"
    payload["lemmas"][1]["lemma_id"] = "lem a"
    payload["selected_decomposition"]["assembly_plan"]["steps"][0]["uses_lemmas"] = ["lem/a"]
    payload["selected_decomposition"]["assembly_plan"]["steps"][1]["uses_lemmas"] = ["lem a"]
    return payload


def _reused_lemma_payload(problem_id: str = "prob_phase04_reused") -> dict:
    return {
        "problem_id": problem_id,
        "title": "Phase 04 reused lemma",
        "verification_level": "nl_only",
        "root_theorem": {
            "theorem_id": "thm_root",
            "statement_nl": "Root statement",
            "semantic_sketch": {"normalized_claim": "root"},
        },
        "selected_decomposition": {
            "decomposition_id": "dec_1",
            "assembly_plan": {
                "assembly_plan_id": "asm_1",
                "steps": [
                    {
                        "step_id": "A1",
                        "uses_lemmas": ["lem_a"],
                        "uses_prior_steps": [],
                        "derives": "first",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                    {
                        "step_id": "A2",
                        "uses_lemmas": ["lem_b"],
                        "uses_prior_steps": ["A1"],
                        "derives": "second",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                    {
                        "step_id": "A4",
                        "uses_lemmas": ["lem_c", "lem_b"],
                        "uses_prior_steps": ["A1", "A2"],
                        "derives": "reuse lemma b later",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                ],
                "proof_skeleton_nl": "Reuse lem_b in a later assembly step.",
                "is_trivially_composable": True,
            },
        },
        "lemmas": [
            {"lemma_id": "lem_a", "statement_nl": "lemma a", "semantic_sketch": {"normalized_claim": "a"}, "proof_nl": "proof a"},
            {"lemma_id": "lem_b", "statement_nl": "lemma b", "semantic_sketch": {"normalized_claim": "b"}, "proof_nl": "proof b"},
            {"lemma_id": "lem_c", "statement_nl": "lemma c", "semantic_sketch": {"normalized_claim": "c"}, "proof_nl": "proof c"},
        ],
        "all_visible_lemmas_nl_accepted": True,
    }


def _ok_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _latest_working_scratch(workspace_dir: Path, fallback: Path) -> Path:
    matches = sorted((workspace_dir / "Orthos").glob("Scratch_*_working_round_*.lean"))
    return matches[-1] if matches else fallback


class _LemmaTraceRunner:
    def __init__(self, *, target_path: Path, candidate_text: str, result_text: str):
        self._target_path = target_path
        self._candidate_text = candidate_text
        self._result_text = result_text

    def run_prompt(
        self,
        *,
        run_paths,
        prompt: str,
        phase_name: str,
        model: str | None = None,
        timeout_seconds: int | None = None,
        permission_mode: str | None = None,
        **_: object,
    ) -> ClaudeRunResult:
        _ = (timeout_seconds, permission_mode)
        target_path = _latest_working_scratch(run_paths.workspace_dir, self._target_path)
        target_path.write_text(self._candidate_text, encoding="utf-8")
        timestamp = "trace"
        prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
        raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
        summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        raw_path.write_text(json.dumps({"type": "result", "result": self._result_text}) + "\n", encoding="utf-8")
        trace = ClaudeRunTrace(
            result_text=self._result_text,
            assistant_text_chunks=(),
            file_updates=(FileUpdateEvent(file_path=str(target_path), content=self._candidate_text),),
            target_file_latest_update=None,
        )
        return ClaudeRunResult(
            phase_name=phase_name,
            model=model or "claude-sonnet-4-6",
            command=("claude", "--mock"),
            cwd=run_paths.workspace_dir,
            returncode=0,
            timed_out=False,
            duration_seconds=0.01,
            prompt_path=prompt_path,
            raw_output_path=raw_path,
            summary_path=summary_path,
            result_event={"type": "result", "result": self._result_text},
            trace=trace,
        )


class _SequentialLemmaTraceRunner:
    def __init__(self, *, target_path: Path, candidates: list[str], result_texts: list[str] | None = None):
        self._target_path = target_path
        self._candidates = candidates
        self._result_texts = result_texts or ["ok"] * len(candidates)
        self.seed_texts: list[str] = []
        self.prompts: list[str] = []
        self.phase_names: list[str] = []
        self._index = 0

    def run_prompt(
        self,
        *,
        run_paths,
        prompt: str,
        phase_name: str,
        model: str | None = None,
        timeout_seconds: int | None = None,
        permission_mode: str | None = None,
        **_: object,
    ) -> ClaudeRunResult:
        _ = (timeout_seconds, permission_mode)
        candidate = self._candidates[self._index]
        result_text = self._result_texts[self._index]
        self._index += 1
        target_path = _latest_working_scratch(run_paths.workspace_dir, self._target_path)
        self.seed_texts.append(target_path.read_text(encoding="utf-8"))
        self.prompts.append(prompt)
        self.phase_names.append(phase_name)
        target_path.write_text(candidate, encoding="utf-8")
        timestamp = f"trace_{self._index:02d}"
        prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
        raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
        summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        raw_path.write_text(json.dumps({"type": "result", "result": result_text}) + "\n", encoding="utf-8")
        trace = ClaudeRunTrace(
            result_text=result_text,
            assistant_text_chunks=(),
            file_updates=(FileUpdateEvent(file_path=str(target_path), content=candidate),),
            target_file_latest_update=None,
        )
        return ClaudeRunResult(
            phase_name=phase_name,
            model=model or "claude-sonnet-4-6",
            command=("claude", "--mock"),
            cwd=run_paths.workspace_dir,
            returncode=0,
            timed_out=False,
            duration_seconds=0.01,
            prompt_path=prompt_path,
            raw_output_path=raw_path,
            summary_path=summary_path,
            result_event={"type": "result", "result": result_text},
            trace=trace,
        )


def _prepare_runtime(tmp_path: Path):
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_single_lemma_payload())
    lemma = bundle.lemma_map["lem_1"]
    pinned = PinnedLemmaSignature(
        lemma_id="lem_1",
        decl_name="lem_1",
        signature="theorem lem_1 : True",
        statement_nl=lemma.statement_nl,
    )
    manifest_path = run_paths.run_root / "trusted_context_manifest.json"
    return runtime_config, run_paths, bundle, lemma, pinned, manifest_path


def _success_candidate(signature: str, *, lemma_id: str = "lem_1") -> str:
    sanitized = sanitize_lean_decl_suffix(lemma_id)
    return "\n".join(
        [
            "import Mathlib",
            "import Orthos.Lemmas",
            "",
            f"-- Phase 04 scratch file for {lemma_id}.",
            f"-- edit_scope: declaration target_{sanitized}",
            "",
            f"{signature} := by",
            "  trivial",
            "",
        ]
    )


def test_scratch_generation_for_one_lemma(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[_success_candidate(pinned.signature, lemma_id=lemma.lemma_id)],
    )

    assert result.status == "succeeded"
    authoritative_round = result.lemma_artifact_dir / "working_round_01.lean"
    assert authoritative_round.exists()
    authoritative_text = authoritative_round.read_text(encoding="utf-8")
    assert pinned.signature in authoritative_text
    assert "import Orthos.Statements" not in authoritative_text
    assert "import Orthos.ScratchContext_lemma_lem_1" not in authoritative_text
    scratch_round = result.lemma_artifact_dir / "scratch_round_01.lean"
    assert scratch_round.read_text(encoding="utf-8") == authoritative_text
    manifest = json.loads((result.lemma_artifact_dir / "round_01.json").read_text(encoding="utf-8"))
    assert manifest["authoritative_round_path"].endswith("working_round_01.lean")
    assert manifest["authoritative_check_ok"] is True


def test_candidate_imports_are_sanitized_to_scratch_context(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Statements",
            "",
            f"{pinned.signature} := by",
            "  trivial",
            "",
        ]
    )
    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[candidate],
    )

    assert result.status == "succeeded"
    scratch_text = (result.lemma_artifact_dir / "scratch_round_01.lean").read_text(encoding="utf-8")
    assert "import Orthos.Statements" not in scratch_text
    assert "import Orthos.ScratchContext_lemma_lem_1" in scratch_text


def test_engine_does_not_reintroduce_removed_scratch_context_import(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.ScratchContext_lemma_lem_1",
            "",
            f"{pinned.signature} := by",
            "  trivial",
            "",
        ]
    ).replace("import Orthos.ScratchContext_lemma_lem_1\n", "")

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[candidate],
    )

    assert result.status == "succeeded"
    authoritative_text = (result.lemma_artifact_dir / "working_round_01.lean").read_text(encoding="utf-8")
    assert "import Orthos.ScratchContext_lemma_lem_1" not in authoritative_text


def test_phase04_prefers_tool_update_candidate_over_prose_result(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    scratch_target = run_paths.workspace_dir / f"Orthos/Scratch_{lemma_id_to_path_token(lemma.lemma_id)}.lean"
    candidate = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id)
    claude_runner = _LemmaTraceRunner(
        target_path=scratch_target,
        candidate_text=candidate,
        result_text="Zero diagnostics - scratch file compiles.",
    )
    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "succeeded"
    assert result.attempts
    assert result.attempts[0].candidate_source == "tool_update"


def test_prompt_assembly_includes_pinned_signature_and_trusted_context() -> None:
    bundle = normalize_problem_artifact(_single_lemma_payload())
    lemma = bundle.lemma_map["lem_1"]
    pinned = PinnedLemmaSignature(
        lemma_id="lem_1",
        decl_name="lem_1",
        signature="theorem lem_1 : True",
        statement_nl=lemma.statement_nl,
    )
    trusted = [
        TrustedContextEntry(
            lemma_id="lem_prev",
            decl_name="prev",
            status="compiled",
            source_file="Orthos/Lemmas.lean",
            signature="theorem prev : True",
            declaration="theorem prev : True := by trivial",
        )
    ]

    prompt = build_lemma_formalization_prompt(
        lemma=lemma,
        pinned=pinned,
        trusted_entries=trusted,
        assumption_capsule=None,
        scratch_relative_path="Orthos/Scratch_lem_1.lean",
        repair_round=1,
    )

    assert pinned.signature in prompt
    assert "prev" in prompt
    # Trusted context now includes only signatures, not full declarations.
    assert "theorem prev : True" in prompt
    assert "by trivial" not in prompt  # full proof bodies excluded from trusted context


def test_prompt_marks_latest_draft_as_current_scratch_file() -> None:
    bundle = normalize_problem_artifact(_single_lemma_payload())
    lemma = bundle.lemma_map["lem_1"]
    pinned = PinnedLemmaSignature(
        lemma_id="lem_1",
        decl_name="lem_1",
        signature="theorem lem_1 : True",
        statement_nl=lemma.statement_nl,
    )
    latest_draft = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id).replace(
        "trivial", "have h : True := trivial\n  exact h"
    )
    best_partial = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id).replace(
        "trivial", "sorry"
    )

    prompt = build_lemma_formalization_prompt(
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        assumption_capsule=None,
        scratch_relative_path="Orthos/Scratch_lem_1.lean",
        repair_round=3,
        previous_candidate=latest_draft,
        best_partial_candidate=best_partial,
        best_partial_sorry_count=1,
        diagnostics_text="some diagnostic",
    )

    assert "LATEST DRAFT FROM ROUND 2 is already in `Orthos/Scratch_lem_1.lean`." in prompt
    assert "fewest sorry placeholders seen so far: 1" in prompt
    assert "CURRENT BEST" not in prompt
    assert latest_draft not in prompt
    assert best_partial not in prompt


def test_progress_guard_rejects_declarations_after_target() -> None:
    """Declarations added AFTER the target are rejected — helpers must come before."""
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Lemmas",
            "",
            "theorem lem_1 : True := by",
            "  trivial",
            "",
            "theorem extra : True := by",
            "  trivial",
            "",
        ]
    )

    guard_error = progress_guard_error(
        candidate_text=candidate,
        pinned_signature="theorem lem_1 : True",
        target_decl_name="lem_1",
        trusted_entries=[],
    )

    assert guard_error is not None
    assert "appears after the target" in guard_error


def test_progress_guard_allows_helper_lemmas_before_target() -> None:
    """Helper lemmas defined BEFORE the target are allowed and encouraged."""
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Statements",
            "",
            "private lemma helper_fact : True := trivial",
            "",
            "theorem lem_1 : True := helper_fact",
            "",
        ]
    )

    guard_error = progress_guard_error(
        candidate_text=candidate,
        pinned_signature="theorem lem_1 : True",
        target_decl_name="lem_1",
        trusted_entries=[],
    )

    assert guard_error is None


def test_progress_guard_accepts_import_changes() -> None:
    """Import/option changes are allowed — the guard only checks declarations."""
    reference = _success_candidate("theorem lem_1 : True", lemma_id="lem_1")
    candidate = reference.replace("import Mathlib", "import Mathlib\nset_option maxRecDepth 2048")

    guard_error = progress_guard_error(
        candidate_text=candidate,
        pinned_signature="theorem lem_1 : True",
        target_decl_name="lem_1",
        trusted_entries=[],
    )

    assert guard_error is None


def test_successful_merge_updates_trusted_manifest(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[_success_candidate(pinned.signature, lemma_id=lemma.lemma_id)],
    )

    assert result.status == "succeeded"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entry_count"] == 1
    assert manifest["entries"][0]["lemma_id"] == "lem_1"
    lemmas_text = (run_paths.workspace_dir / "Orthos" / "Lemmas.lean").read_text(encoding="utf-8")
    assert "theorem lem_1 : True := by" in lemmas_text


def test_merge_removes_trailing_namespace_terminator_from_extracted_target() -> None:
    candidate = _success_candidate("theorem lem_1 : True", lemma_id="lem_1")
    extracted = extract_target_declaration_block(candidate, "lem_1")
    merged = merge_declaration_into_lemmas_file(
        original_text="\n".join(
            [
                "import Mathlib",
                "",
                "-- Proven lemma declarations accumulate below.",
                "",
                "-- END LEMMAS",
                "",
            ]
        ),
        declaration_block=extracted,
    )

    assert "-- END LEMMAS\n\n-- END LEMMAS" not in merged
    assert merged.count("-- END LEMMAS") == 1
    assert "theorem lem_1 : True := by" in merged


# ---------------------------------------------------------------------------
# extract_proof_block_with_helpers tests
# ---------------------------------------------------------------------------

def test_extract_proof_block_no_helpers() -> None:
    """Without helper lemmas, extracts just the target (same as before)."""
    candidate = "\n".join([
        "import Mathlib",
        "import Orthos.Statements",
        "",
        "open Set Filter",
        "",
        "-- Trusted context",
        "theorem trusted_1 : True := by sorry",
        "",
        "theorem lem_1 : True := by",
        "  trivial",
        "",
    ])
    trusted = {"trusted_1"}
    block = extract_proof_block_with_helpers(candidate, "lem_1", trusted_names=trusted)
    assert "theorem lem_1 : True := by" in block
    assert "trusted_1" not in block
    assert "trivial" in block


def test_extract_proof_block_includes_helpers_before_target() -> None:
    """Helper lemmas defined before the target are extracted alongside it."""
    candidate = "\n".join([
        "import Mathlib",
        "import Orthos.Statements",
        "",
        "open Set Filter",
        "",
        "-- Trusted context",
        "theorem trusted_1 : True := by sorry",
        "",
        "private lemma my_helper : 1 + 1 = 2 := by norm_num",
        "",
        "theorem lem_1 : True := by",
        "  have := my_helper",
        "  trivial",
        "",
    ])
    trusted = {"trusted_1"}
    block = extract_proof_block_with_helpers(candidate, "lem_1", trusted_names=trusted)
    assert "private lemma my_helper" in block
    assert "theorem lem_1 : True := by" in block
    assert "trusted_1" not in block
    # Helper must appear before target in the extracted block
    assert block.index("my_helper") < block.index("lem_1")


def test_extract_proof_block_excludes_declarations_after_target() -> None:
    """Declarations after the target are not extracted."""
    candidate = "\n".join([
        "import Mathlib",
        "import Orthos.Statements",
        "",
        "theorem lem_1 : True := by trivial",
        "",
        "theorem after_target : True := by trivial",
        "",
    ])
    block = extract_proof_block_with_helpers(candidate, "lem_1", trusted_names=set())
    assert "lem_1" in block
    assert "after_target" not in block


def test_extract_proof_block_carries_extra_open_lines() -> None:
    """Extra open directives Claude added (beyond Statements.lean) are prepended."""
    candidate = "\n".join([
        "import Mathlib",
        "import Orthos.Statements",
        "",
        "open Set Real",  # Claude added 'Real' beyond the standard 'Set Filter'
        "",
        "private lemma my_helper (x : ℝ) : Real.sin x ≤ 1 := Real.sin_le_one x",
        "",
        "theorem lem_1 : True := by trivial",
        "",
    ])
    # Standard opens from Statements.lean: Set Filter
    statements_opens = ["open Set Filter"]
    block = extract_proof_block_with_helpers(
        candidate, "lem_1", trusted_names=set(), statements_open_lines=statements_opens
    )
    # 'Real' is extra — should appear in an open line
    assert "open Real" in block
    # Helper and target both present
    assert "my_helper" in block
    assert "lem_1" in block


def test_extract_proof_block_no_extra_opens_when_matches_standard() -> None:
    """No extra open line added when scratch opens match Statements.lean exactly."""
    candidate = "\n".join([
        "import Mathlib",
        "import Orthos.Statements",
        "",
        "open Set Filter",  # Same as standard
        "",
        "theorem lem_1 : True := by trivial",
        "",
    ])
    statements_opens = ["open Set Filter"]
    block = extract_proof_block_with_helpers(
        candidate, "lem_1", trusted_names=set(), statements_open_lines=statements_opens
    )
    # No extra open line should appear (Set and Filter already standard)
    assert "open " not in block


def test_failed_lemma_writes_structured_artifacts(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    bad_candidate = _success_candidate("theorem lem_1 : False", lemma_id=lemma.lemma_id)
    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[bad_candidate],
    )

    assert result.status == "failed"
    assert result.result_path.exists()
    assert (result.lemma_artifact_dir / "prompt_round_01.md").exists()
    assert (result.lemma_artifact_dir / "claude_round_01.jsonl").exists()
    assert (result.lemma_artifact_dir / "scratch_round_01.lean").exists()
    assert (result.lemma_artifact_dir / "diagnostics_round_01.txt").exists()
    assert (result.lemma_artifact_dir / "diff_round_01.patch").exists()


def test_bounded_retry_behavior(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    bad_round_1 = _success_candidate("theorem lem_1 : False", lemma_id=lemma.lemma_id)
    bad_round_2 = _success_candidate("theorem lem_1 : False", lemma_id=lemma.lemma_id)
    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=2,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[bad_round_1, bad_round_2],
    )

    assert result.status == "failed"
    assert result.attempts_used == 2
    assert len(result.attempts) == 2


def test_repair_round_keeps_last_compiling_canonical_after_compile_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    scratch_target = run_paths.workspace_dir / f"Orthos/Scratch_{lemma_id_to_path_token(lemma.lemma_id)}.lean"
    round_1 = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id).replace(
        "trivial", "sorry"
    )
    round_2 = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Lemmas",
            "",
            "-- round 2 richer draft",
            "private lemma helper_round_2 : True := by",
            "  trivial",
            "",
            "theorem lem_1 : True := by",
            "  have h := helper_round_2",
            "  typo_token",
            "",
        ]
    )
    round_3 = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id)
    claude_runner = _SequentialLemmaTraceRunner(
        target_path=scratch_target,
        candidates=[round_1, round_2, round_3],
    )

    def _check_with_round2_failure(workspace_root: Path, relative_path: str, **_: object) -> LeanCommandResult:
        text = (workspace_root / relative_path).read_text(encoding="utf-8")
        if "typo_token" in text:
            return LeanCommandResult(
                check_name="check_lean_file",
                command=("lake", "env", "lean"),
                cwd=workspace_root,
                returncode=1,
                stdout="",
                stderr="unknown identifier 'typo_token'",
                duration_seconds=0.0,
            )
        return _ok_lean_result("check_lean_file", workspace_root)

    monkeypatch.setattr("lean_engine.lemma_phase.check_lean_file", _check_with_round2_failure)

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=3,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "succeeded"
    assert len(claude_runner.seed_texts) == 3
    assert "sorry" in claude_runner.seed_texts[1]
    assert "round 2 richer draft" not in claude_runner.seed_texts[2]
    assert "sorry" in claude_runner.seed_texts[2]
    prompt_round_03 = (result.lemma_artifact_dir / "prompt_round_03.md").read_text(encoding="utf-8")
    assert "LATEST DRAFT FROM ROUND 2 is already in `Orthos/Scratch_lemma_lem_1_working_round_03.lean`." in prompt_round_03
    assert "round 2 richer draft" not in prompt_round_03


def test_duplicate_declaration_false_negative_records_checker_mismatch_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    scratch_target = run_paths.workspace_dir / f"Orthos/Scratch_{lemma_id_to_path_token(lemma.lemma_id)}.lean"
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Statements",
            "",
            f"{pinned.signature} := by",
            "  trivial",
            "",
        ]
    )
    claude_runner = _SequentialLemmaTraceRunner(
        target_path=scratch_target,
        candidates=[candidate, _success_candidate(pinned.signature, lemma_id=lemma.lemma_id)],
        result_texts=["[STATUS: clean]", "[STATUS: clean]"],
    )

    state = {"calls": 0}

    def _check_duplicate_then_ok(workspace_root: Path, relative_path: str, **_: object) -> LeanCommandResult:
        state["calls"] += 1
        if state["calls"] == 1:
            return LeanCommandResult(
                check_name="check_lean_file",
                command=("lake", "env", "lean", relative_path),
                cwd=workspace_root,
                returncode=1,
                stdout=f"{relative_path}:9:8: error: `lem_1` has already been declared\n",
                stderr="",
                duration_seconds=0.0,
            )
        return _ok_lean_result("check_lean_file", workspace_root)

    monkeypatch.setattr("lean_engine.lemma_phase.check_lean_file", _check_duplicate_then_ok)

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=2,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "succeeded"
    assert result.attempts[0].classification == "duplicate_declaration_context"
    assert result.attempts[0].mcp_check_status == "clean"
    assert result.attempts[0].batch_check_status == "failed"


def test_generated_scratch_context_is_rebuilt_before_round_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    rebuild_calls: list[tuple[str, str]] = []

    def _record_rebuild(
        workspace_root: Path,
        relative_file: str,
        module_name: str,
        **_: object,
    ) -> LeanCommandResult:
        rebuild_calls.append((relative_file, module_name))
        return _ok_lean_result("rebuild_module_olean", workspace_root)

    monkeypatch.setattr("lean_engine.lemma_phase.rebuild_module_olean", _record_rebuild)

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[_success_candidate(pinned.signature, lemma_id=lemma.lemma_id)],
    )

    assert result.status == "succeeded"
    assert rebuild_calls[0] == ("Orthos/ScratchContext_lemma_lem_1.lean", "Orthos.ScratchContext_lemma_lem_1")


def test_generated_module_preflight_failure_aborts_before_spending_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)

    def _fail_rebuild(
        workspace_root: Path,
        relative_file: str,
        module_name: str,
        **_: object,
    ) -> LeanCommandResult:
        return LeanCommandResult(
            check_name=f"rebuild_olean_{module_name}",
            command=("lake", "env", "lean", relative_file),
            cwd=workspace_root,
            returncode=1,
            stdout="",
            stderr=f"missing olean for {module_name}",
            duration_seconds=0.0,
        )

    monkeypatch.setattr("lean_engine.lemma_phase.rebuild_module_olean", _fail_rebuild)

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=3,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[_success_candidate(pinned.signature, lemma_id=lemma.lemma_id)],
    )

    assert result.status == "failed"
    assert result.error_class == "workspace_preparation_failed"
    assert result.attempts_used == 0
    assert not result.attempts


def test_repeated_checker_mismatch_stops_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    scratch_target = run_paths.workspace_dir / f"Orthos/Scratch_{lemma_id_to_path_token(lemma.lemma_id)}.lean"
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Statements",
            "",
            f"{pinned.signature} := by",
            "  trivial",
            "",
        ]
    )
    claude_runner = _SequentialLemmaTraceRunner(
        target_path=scratch_target,
        candidates=[candidate, candidate, candidate],
        result_texts=["[STATUS: clean]", "[STATUS: clean]", "[STATUS: clean]"],
    )

    def _same_checker_failure(workspace_root: Path, relative_path: str, **_: object) -> LeanCommandResult:
        return LeanCommandResult(
            check_name="check_lean_file",
            command=("lake", "env", "lean", relative_path),
            cwd=workspace_root,
            returncode=1,
            stdout="",
            stderr="error: object file '/tmp/fake.olean' of module Orthos.ScratchContext_lemma_lem_1 does not exist",
            duration_seconds=0.0,
        )

    monkeypatch.setattr("lean_engine.lemma_phase.check_lean_file", _same_checker_failure)

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=5,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "failed"
    assert result.error_class == "checker_mismatch"
    assert result.attempts_used == 2
    assert len(result.attempts) == 2


def test_empty_batch_failure_output_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    candidate = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id)

    def _empty_failure(workspace_root: Path, relative_path: str, **_: object) -> LeanCommandResult:
        return LeanCommandResult(
            check_name="check_lean_file",
            command=("lake", "env", "lean", relative_path),
            cwd=workspace_root,
            returncode=17,
            stdout="",
            stderr="",
            duration_seconds=0.0,
        )

    monkeypatch.setattr("lean_engine.lemma_phase.check_lean_file", _empty_failure)

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[candidate],
    )

    assert result.status == "failed"
    diagnostics = (result.lemma_artifact_dir / "diagnostics_round_01.txt").read_text(encoding="utf-8")
    assert "target_file:" in diagnostics
    assert "returncode: 17" in diagnostics


def test_repair_round_seeds_from_latest_rejected_draft(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    scratch_target = run_paths.workspace_dir / f"Orthos/Scratch_{lemma_id_to_path_token(lemma.lemma_id)}.lean"
    round_1 = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id).replace(
        "trivial", "sorry"
    )
    round_2 = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Lemmas",
            "",
            "-- rejected round 2 draft",
            "theorem lem_1 : True := by",
            "  trivial",
            "",
            "theorem extra : True := by",
            "  trivial",
            "",
        ]
    )
    round_3 = _success_candidate(pinned.signature, lemma_id=lemma.lemma_id)
    claude_runner = _SequentialLemmaTraceRunner(
        target_path=scratch_target,
        candidates=[round_1, round_2, round_3],
    )

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=3,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "succeeded"
    assert len(claude_runner.seed_texts) == 3
    assert "sorry" in claude_runner.seed_texts[2]
    prompt_round_03 = (result.lemma_artifact_dir / "prompt_round_03.md").read_text(encoding="utf-8")
    assert "LATEST DRAFT FROM ROUND 2 is already in `Orthos/Scratch_lemma_lem_1_working_round_03.lean`." in prompt_round_03
    assert "rejected round 2 draft" not in prompt_round_03


def test_load_pinned_signatures_rejects_keyword_signature_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "pinned_signatures.json"
    path.write_text(
        json.dumps(
            {
                "problem_id": "prob",
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "lemma lem_1 : True",
                        "keyword": "theorem",
                        "statement_nl": "lemma one",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        load_pinned_lemma_signatures(path)
    except ValueError as exc:
        assert "keyword/signature mismatch" in str(exc)
    else:
        raise AssertionError("expected keyword/signature mismatch to be rejected")


def test_load_pinned_signatures_rejects_non_theorem_signature_even_with_keyword(tmp_path: Path) -> None:
    path = tmp_path / "pinned_signatures.json"
    path.write_text(
        json.dumps(
            {
                "problem_id": "prob",
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "def lem_1 : Prop := True",
                        "keyword": "theorem",
                        "statement_nl": "lemma one",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        load_pinned_lemma_signatures(path)
    except ValueError as exc:
        assert "must use a `theorem`/`lemma` signature" in str(exc)
    else:
        raise AssertionError("expected non-theorem signature to be rejected")


def test_load_pinned_signatures_rejects_duplicate_lemma_ids(tmp_path: Path) -> None:
    path = tmp_path / "pinned_signatures.json"
    path.write_text(
        json.dumps(
            {
                "problem_id": "prob",
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "theorem lem_1 : True",
                        "keyword": "theorem",
                        "statement_nl": "lemma one",
                    },
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1_alt",
                        "signature": "theorem lem_1_alt : True",
                        "keyword": "theorem",
                        "statement_nl": "lemma one duplicate",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        load_pinned_lemma_signatures(path)
    except ValueError as exc:
        assert "duplicate lemma_id" in str(exc)
    else:
        raise AssertionError("expected duplicate lemma_id to be rejected")


def test_load_pinned_signatures_sanitizes_comment_tainted_signatures(tmp_path: Path) -> None:
    path = tmp_path / "pinned_signatures.json"
    path.write_text(
        json.dumps(
            {
                "problem_id": "prob",
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "theorem lem_1 : True /-! leaked comment -/",
                        "keyword": "theorem",
                        "statement_nl": "lemma one",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_pinned_lemma_signatures(path)
    assert loaded["lem_1"].signature == "theorem lem_1 : True"


def test_phase04_loop_grows_trusted_context_between_lemmas(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_two", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_two_lemma_payload())
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_payload = {
        "problem_id": bundle.problem_id,
        "run_name": run_paths.run_name,
        "lemmas": [
            {
                "lemma_id": "lem_1",
                "decl_name": "lem_1",
                "signature": "theorem lem_1 : True",
                "statement_nl": bundle.lemma_map["lem_1"].statement_nl,
            },
            {
                "lemma_id": "lem_2",
                "decl_name": "lem_2",
                "signature": "theorem lem_2 : True",
                "statement_nl": bundle.lemma_map["lem_2"].statement_nl,
            },
        ],
    }
    pinned_path.write_text(json.dumps(pinned_payload), encoding="utf-8")

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates={
            "lem_1": [_success_candidate("theorem lem_1 : True", lemma_id="lem_1")],
            "lem_2": [_success_candidate("theorem lem_2 : True", lemma_id="lem_2")],
        },
    )

    assert result.status == "ok"
    prompt_round_2 = run_paths.run_root / "lemmas" / lemma_id_to_path_token("lem_2") / "prompt_round_01.md"
    prompt_text = prompt_round_2.read_text(encoding="utf-8")
    assert "lem_1" in prompt_text


def test_phase04_manifest_problem_id_uses_sanitized_bundle_id(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("Phase 04 Problem/ID", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_single_lemma_payload(problem_id="Phase 04 Problem/ID"))
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_path.write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": run_paths.run_name,
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "theorem lem_1 : True",
                        "statement_nl": bundle.lemma_map["lem_1"].statement_nl,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates={"lem_1": [_success_candidate("theorem lem_1 : True", lemma_id="lem_1")]},
    )

    assert result.status == "ok"
    manifest = json.loads((run_paths.run_root / "trusted_context_manifest.json").read_text(encoding="utf-8"))
    assert manifest["problem_id"] == sanitize_component(bundle.problem_id)


def test_phase04_rederives_stale_trusted_manifest_from_authoritative_lemmas(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_binding", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_single_lemma_payload(problem_id="prob_phase04_binding"))
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_path.write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": run_paths.run_name,
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "theorem lem_1 : True",
                        "keyword": "theorem",
                        "statement_nl": bundle.lemma_map["lem_1"].statement_nl,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    trusted_manifest.write_text(
        json.dumps(
            {
                "problem_id": "different_problem",
                "entry_count": 1,
                "entries": [
                    {
                        "lemma_id": "lem_old",
                        "decl_name": "old",
                        "status": "compiled",
                        "source_file": "Orthos/Lemmas.lean",
                        "signature": "theorem old : True",
                        "declaration": "theorem old : True := by trivial",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates={"lem_1": [_success_candidate("theorem lem_1 : True", lemma_id="lem_1")]},
    )

    assert result.status == "ok"
    manifest = json.loads(trusted_manifest.read_text(encoding="utf-8"))
    assert manifest["problem_id"] == "prob_phase04_binding"
    assert manifest["entry_count"] == 1
    assert manifest["entries"][0]["lemma_id"] == "lem_1"


def test_phase04_rejects_pinned_signatures_from_different_problem(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_binding", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_single_lemma_payload(problem_id="prob_phase04_binding"))
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_path.write_text(
        json.dumps(
            {
                "problem_id": "different_problem",
                "run_name": run_paths.run_name,
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "theorem lem_1 : True",
                        "keyword": "theorem",
                        "statement_nl": bundle.lemma_map["lem_1"].statement_nl,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates={"lem_1": [_success_candidate("theorem lem_1 : True", lemma_id="lem_1")]},
    )

    assert result.status == "fatal"
    assert result.error_class == "pinned_signature_binding_mismatch"
    assert result.message is not None
    assert "problem_id mismatch" in result.message


def test_phase04_rejects_pinned_signatures_from_different_run(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_binding", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_single_lemma_payload(problem_id="prob_phase04_binding"))
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_path.write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": "run_999",
                "lemmas": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "signature": "theorem lem_1 : True",
                        "keyword": "theorem",
                        "statement_nl": bundle.lemma_map["lem_1"].statement_nl,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates={"lem_1": [_success_candidate("theorem lem_1 : True", lemma_id="lem_1")]},
    )

    assert result.status == "fatal"
    assert result.error_class == "pinned_signature_binding_mismatch"
    assert result.message is not None
    assert "run_name mismatch" in result.message


def test_phase04_lemma_paths_are_unique_for_colliding_sanitized_ids(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_collision", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_two_lemma_collision_payload())
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_path.write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": run_paths.run_name,
                "lemmas": [
                    {
                        "lemma_id": "lem/a",
                        "decl_name": "lem_a",
                        "signature": "theorem lem_a : True",
                        "statement_nl": bundle.lemma_map["lem/a"].statement_nl,
                    },
                    {
                        "lemma_id": "lem a",
                        "decl_name": "lem_a_2",
                        "signature": "theorem lem_a_2 : True",
                        "statement_nl": bundle.lemma_map["lem a"].statement_nl,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates={
            "lem/a": [_success_candidate("theorem lem_a : True", lemma_id="lem/a")],
            "lem a": [_success_candidate("theorem lem_a_2 : True", lemma_id="lem a")],
        },
    )

    assert result.status == "ok"
    assert len(result.lemma_results) == 2
    assert result.lemma_results[0].lemma_artifact_dir != result.lemma_results[1].lemma_artifact_dir
    assert result.lemma_results[0].scratch_file != result.lemma_results[1].scratch_file


def test_phase04_parallel_workers_commit_multiple_lemmas(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_parallel", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_two_independent_lemmas_payload(problem_id="prob_phase04_parallel"))
    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_payload = {
        "problem_id": bundle.problem_id,
        "run_name": run_paths.run_name,
        "lemmas": [
            {
                "lemma_id": "lem_1",
                "decl_name": "lem_1",
                "signature": "theorem lem_1 : True",
                "statement_nl": bundle.lemma_map["lem_1"].statement_nl,
            },
            {
                "lemma_id": "lem_2",
                "decl_name": "lem_2",
                "signature": "theorem lem_2 : True",
                "statement_nl": bundle.lemma_map["lem_2"].statement_nl,
            },
        ],
    }
    pinned_path.write_text(json.dumps(pinned_payload), encoding="utf-8")

    result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        parallel_lemmas=True,
        runner=_ok_runner,
        mock_candidates={
            "lem_1": [_success_candidate("theorem lem_1 : True", lemma_id="lem_1")],
            "lem_2": [_success_candidate("theorem lem_2 : True", lemma_id="lem_2")],
        },
    )

    assert result.status == "ok"
    assert len(result.lemma_results) == 2
    manifest = json.loads((run_paths.run_root / "trusted_context_manifest.json").read_text(encoding="utf-8"))
    assert manifest["entry_count"] == 2
    assert all(item.worker_workspace is not None for item in result.lemma_results)
    assert all(Path(item.worker_workspace) != run_paths.workspace_dir for item in result.lemma_results)


# ---------------------------------------------------------------------------
# Fix 1: olean purge helper
# ---------------------------------------------------------------------------

def test_purge_module_build_artifacts(tmp_path: Path) -> None:
    from lean_engine.phase04 import _purge_module_build_artifacts

    workspace = tmp_path / "workspace"
    lake_build = workspace / ".lake" / "build"
    for subpath in [
        "lib/lean/Orthos/Statements.olean",
        "lib/lean/Orthos/Statements.ilean",
        "lib/lean/Orthos/Statements.olean.hash",
        "lib/lean/Orthos/Statements.trace",
        "ir/Orthos/Statements.c",
    ]:
        p = lake_build / subpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale", encoding="utf-8")

    deleted = _purge_module_build_artifacts(workspace, "Orthos/Statements")
    assert len(deleted) == 5
    assert not (lake_build / "lib/lean/Orthos/Statements.olean").exists()
    assert not (lake_build / "lib/lean/Orthos/Statements.ilean").exists()
    assert not (lake_build / "lib/lean/Orthos/Statements.trace").exists()


def test_purge_module_no_artifacts(tmp_path: Path) -> None:
    from lean_engine.phase04 import _purge_module_build_artifacts

    deleted = _purge_module_build_artifacts(tmp_path, "Orthos/Statements")
    assert deleted == []


# ---------------------------------------------------------------------------
# Fix 4: olean freshness validation
# ---------------------------------------------------------------------------

def _olean_freshness_check(workspace_dir: Path) -> bool:
    """Re-implementation for testing — autouse fixture mocks the module-level version."""
    source = workspace_dir / "Orthos" / "Statements.lean"
    olean = workspace_dir / ".lake" / "build" / "lib" / "lean" / "Orthos" / "Statements.olean"
    if not source.exists():
        return True
    if not olean.exists():
        return False
    return olean.stat().st_mtime >= source.stat().st_mtime


def test_validate_olean_freshness_fresh(tmp_path: Path) -> None:
    import time

    ws = tmp_path / "ws"
    src = ws / "Orthos" / "Statements.lean"
    olean = ws / ".lake" / "build" / "lib" / "lean" / "Orthos" / "Statements.olean"
    src.parent.mkdir(parents=True, exist_ok=True)
    olean.parent.mkdir(parents=True, exist_ok=True)

    src.write_text("-- source", encoding="utf-8")
    time.sleep(0.01)
    olean.write_text("-- olean", encoding="utf-8")
    assert _olean_freshness_check(ws) is True


def test_validate_olean_freshness_stale(tmp_path: Path) -> None:
    import time

    ws = tmp_path / "ws"
    src = ws / "Orthos" / "Statements.lean"
    olean = ws / ".lake" / "build" / "lib" / "lean" / "Orthos" / "Statements.olean"
    src.parent.mkdir(parents=True, exist_ok=True)
    olean.parent.mkdir(parents=True, exist_ok=True)

    olean.write_text("-- olean", encoding="utf-8")
    time.sleep(0.01)
    src.write_text("-- updated source", encoding="utf-8")
    assert _olean_freshness_check(ws) is False


def test_validate_olean_freshness_missing_olean(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    src = ws / "Orthos" / "Statements.lean"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("-- source", encoding="utf-8")
    assert _olean_freshness_check(ws) is False


# ---------------------------------------------------------------------------
# Fix 3: classify duplicate declarations as context collisions
# ---------------------------------------------------------------------------

def test_classify_already_declared_as_duplicate_declaration_context() -> None:
    from lean_engine.lemma_phase import classify_lemma_failure

    assert classify_lemma_failure("error: 'lem_x' has already been declared") == "duplicate_declaration_context"
    assert classify_lemma_failure("'foo' has already been declared\nother stuff") == "duplicate_declaration_context"


def test_classify_tactic_failure_not_stale_olean() -> None:
    from lean_engine.lemma_phase import classify_lemma_failure

    assert classify_lemma_failure("unsolved goals") == "tactic_failure"
    assert classify_lemma_failure("type mismatch in application") == "type_mismatch"


# ---------------------------------------------------------------------------
# Fix 2: semicolon-tolerant signature comparison
# ---------------------------------------------------------------------------

def test_progress_guard_accepts_semicolon_in_let_binding() -> None:
    """Claude adding `;` after let-binding values should not trigger signature drift."""
    pinned_sig = "theorem foo : let p := [1, 2, 3] p.length = 3"
    candidate = (
        "import Mathlib\nimport Orthos.Statements\n\n"
        "theorem foo : let p := [1, 2, 3]; p.length = 3 := by\n  simp\n"
    )

    err = progress_guard_error(
        candidate_text=candidate,
        pinned_signature=pinned_sig,
        target_decl_name="foo",
        trusted_entries=[],
    )
    assert err is None


def test_progress_guard_rejects_actual_signature_drift() -> None:
    """Genuine signature drift should still be rejected."""
    pinned_sig = "theorem foo : True"
    candidate = (
        "import Mathlib\nimport Orthos.Statements\n\n"
        "theorem foo : False := by\n  simp\n"
    )

    err = progress_guard_error(
        candidate_text=candidate,
        pinned_signature=pinned_sig,
        target_decl_name="foo",
        trusted_entries=[],
    )
    assert err is not None
    assert "drifted from pinned signature" in err


def test_normalize_for_sig_comparison_semicolons() -> None:
    from lean_engine.lemma_phase import _normalize_for_sig_comparison

    a = "theorem foo : let p := [1, 2, 3]; p.length = 3"
    b = "theorem foo : let p := [1, 2, 3] p.length = 3"
    assert _normalize_for_sig_comparison(a) == _normalize_for_sig_comparison(b)

    # Multiple let bindings with mixed semicolons
    c = "theorem bar : let x := 1; let y := 2; x + y = 3"
    d = "theorem bar : let x := 1 let y := 2 x + y = 3"
    assert _normalize_for_sig_comparison(c) == _normalize_for_sig_comparison(d)


# ---------------------------------------------------------------------------
# Doc comment neutralization
# ---------------------------------------------------------------------------

def test_neutralize_doc_comments_before_axiom(tmp_path: Path) -> None:
    """Doc comments preceding pinned axiom/theorem/lemma must be neutralized too."""
    from lean_engine.phase04 import _neutralize_statements_for_proving

    ws = tmp_path / "ws"
    stmts = ws / "Orthos" / "Statements.lean"
    stmts.parent.mkdir(parents=True, exist_ok=True)
    stmts.write_text(
        "import Mathlib\n"
        "\n"
        "/-- Base case: a simple fact -/\n"
        "axiom foo : True\n"
        "\n"
        "/-- A definition we keep -/\n"
        "def bar := 42\n"
    )
    _neutralize_statements_for_proving(ws, {"foo"})
    result = stmts.read_text()
    # Doc comment before pinned axiom must be neutralized
    assert "-- [pinned] /-- Base case: a simple fact -/" in result
    assert "-- [pinned] axiom foo : True" in result
    # Doc comment before def must be preserved
    assert "/-- A definition we keep -/" in result
    assert "def bar := 42" in result


def test_neutralize_multiline_doc_comment_before_axiom(tmp_path: Path) -> None:
    """Multi-line doc comments before pinned axioms must be fully neutralized."""
    from lean_engine.phase04 import _neutralize_statements_for_proving

    ws = tmp_path / "ws"
    stmts = ws / "Orthos" / "Statements.lean"
    stmts.parent.mkdir(parents=True, exist_ok=True)
    stmts.write_text(
        "import Mathlib\n"
        "\n"
        "/-- This is a long\n"
        "doc comment that spans\n"
        "multiple lines -/\n"
        "axiom baz : Nat → Nat\n"
    )
    _neutralize_statements_for_proving(ws, {"baz"})
    result = stmts.read_text()
    assert "-- [pinned] /-- This is a long" in result
    assert "-- [pinned] doc comment that spans" in result
    assert "-- [pinned] multiple lines -/" in result
    assert "-- [pinned] axiom baz : Nat → Nat" in result


def test_neutralize_section_comments_preserved(tmp_path: Path) -> None:
    """Section comments (/-! ... -/) are NOT doc comments and should be preserved."""
    from lean_engine.phase04 import _neutralize_statements_for_proving

    ws = tmp_path / "ws"
    stmts = ws / "Orthos" / "Statements.lean"
    stmts.parent.mkdir(parents=True, exist_ok=True)
    stmts.write_text(
        "import Mathlib\n"
        "\n"
        "/-! ## Section header -/\n"
        "\n"
        "axiom qux : True\n"
    )
    _neutralize_statements_for_proving(ws, {"qux"})
    result = stmts.read_text()
    # Section comment preserved
    assert "/-! ## Section header -/" in result
    # Axiom neutralized
    assert "-- [pinned] axiom qux : True" in result


def test_neutralize_preserves_non_pinned_axioms(tmp_path: Path) -> None:
    """Non-pinned axioms (helper axioms like f_count) must be preserved."""
    from lean_engine.phase04 import _neutralize_statements_for_proving

    ws = tmp_path / "ws"
    stmts = ws / "Orthos" / "Statements.lean"
    stmts.parent.mkdir(parents=True, exist_ok=True)
    stmts.write_text(
        "import Mathlib\n"
        "\n"
        "axiom f_count (m : Nat) : Nat\n"
        "\n"
        "noncomputable def A (n : Nat) : Nat :=\n"
        "  if n = 0 then 1 else f_count (n - 1)\n"
        "\n"
        "axiom lem_001 (n : Nat) : f_count n <= A n\n"
        "\n"
        "axiom root_prob (n : Nat) : A n > 0\n"
    )
    # Only lem_001 and root_prob are pinned; f_count is a helper axiom
    _neutralize_statements_for_proving(ws, {"lem_001", "root_prob"})
    result = stmts.read_text()
    # Helper axiom preserved (not pinned)
    assert "axiom f_count (m : Nat) : Nat" in result
    assert "-- [pinned]" not in result.split("axiom f_count")[0].split("\n")[-1]
    # Def preserved
    assert "noncomputable def A (n : Nat) : Nat :=" in result
    # Pinned declarations neutralized
    assert "-- [pinned] axiom lem_001 (n : Nat) : f_count n <= A n" in result
    assert "-- [pinned] axiom root_prob (n : Nat) : A n > 0" in result


def test_lemma_execution_groups_unreferenced_share_one_group() -> None:
    """Lemmas not in any assembly step's uses_lemmas share one parallel group."""
    from lean_engine.phase04 import _lemma_execution_groups

    payload = {
        "problem_id": "prob_groups",
        "title": "test",
        "verification_level": "nl_only",
        "root_theorem": {"theorem_id": "t", "statement_nl": "r", "semantic_sketch": {}},
        "selected_decomposition": {
            "decomposition_id": "d1",
            "assembly_plan": {
                "assembly_plan_id": "a1",
                "steps": [
                    {"step_id": "A1", "uses_lemmas": ["lem_4"], "uses_prior_steps": [], "derives": "x", "is_trivial": True, "trivial_justification": "given"},
                    {"step_id": "A2", "uses_lemmas": ["lem_5"], "uses_prior_steps": ["A1"], "derives": "y", "is_trivial": True, "trivial_justification": "given"},
                ],
            },
        },
        "lemmas": [
            {"lemma_id": f"lem_{i}", "statement_nl": f"l{i}", "semantic_sketch": {}, "proof_nl": f"p{i}"}
            for i in range(1, 6)
        ],
        "all_visible_lemmas_nl_accepted": True,
    }
    bundle = normalize_problem_artifact(payload)
    lemma_order = tuple(f"lem_{i}" for i in range(1, 6))
    groups = _lemma_execution_groups(bundle=bundle, lemma_order=lemma_order, lemma_workers=4)

    # lem_4 at level 0, lem_5 at level 1 — each in own group
    # lem_1, lem_2, lem_3 unreferenced — must share ONE group
    unreferenced = {"lem_1", "lem_2", "lem_3"}
    unreferenced_groups = [g for g in groups if set(g) & unreferenced]
    assert len(unreferenced_groups) == 1, f"Expected 1 group for unreferenced lemmas, got {len(unreferenced_groups)}: {unreferenced_groups}"
    assert set(unreferenced_groups[0]) == unreferenced


def test_build_dependency_graph_rejects_cycles() -> None:
    payload = _two_lemma_payload(problem_id="prob_cycle")
    payload["lemmas"][0]["depends_on"] = ["lem_2"]
    payload["lemmas"][1]["depends_on"] = ["lem_1"]
    bundle = normalize_problem_artifact(payload)

    with pytest.raises(ValueError, match="cycle"):
        build_dependency_graph(bundle=bundle, lemma_order=("lem_1", "lem_2"))


def test_build_dependency_graph_excludes_self_edge_for_reused_lemma_single_target() -> None:
    payload = _reused_lemma_payload(problem_id="prob_reused_single")
    bundle = normalize_problem_artifact(payload)

    graph, closure, layer_index = build_dependency_graph(bundle=bundle, lemma_order=("lem_b",))

    assert graph["lem_b"] == ()
    assert closure["lem_b"] == ()
    assert layer_index["lem_b"] == 2


def test_build_dependency_graph_excludes_self_edge_for_reused_lemma_full_order() -> None:
    payload = _reused_lemma_payload(problem_id="prob_reused_full")
    bundle = normalize_problem_artifact(payload)

    graph, closure, layer_index = build_dependency_graph(bundle=bundle, lemma_order=("lem_a", "lem_b", "lem_c"))

    assert graph["lem_a"] == ()
    assert graph["lem_b"] == ("lem_a",)
    assert graph["lem_c"] == ("lem_a", "lem_b")
    assert "lem_b" not in graph["lem_b"]
    assert closure["lem_b"] == ("lem_a",)
    assert closure["lem_c"] == ("lem_a", "lem_b")
    assert layer_index["lem_b"] == 2
    assert layer_index["lem_c"] == 2


def test_phase04_summary_path_namespaces_targeted_runs(tmp_path: Path) -> None:
    from lean_engine.phase04 import _phase04_summary_path

    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase04_summary_paths", artifacts_root=runtime_config.artifacts.root)

    assert _phase04_summary_path(
        run_paths=run_paths,
        lemma_order=("lem_1", "lem_2"),
        target_lemma_id=None,
        target_lemma_ids=None,
    ).name == "phase04_summary.json"
    assert _phase04_summary_path(
        run_paths=run_paths,
        lemma_order=("lem_1",),
        target_lemma_id="lem_1",
        target_lemma_ids=None,
    ).name == f"phase04_{lemma_id_to_path_token('lem_1')}.json"
    subset_digest = hashlib.sha1("lem_1,lem_2".encode("utf-8")).hexdigest()[:10]
    assert _phase04_summary_path(
        run_paths=run_paths,
        lemma_order=("lem_1", "lem_2"),
        target_lemma_id=None,
        target_lemma_ids={"lem_1", "lem_2"},
    ).name == f"phase04_subset_{subset_digest}.json"


def test_dependency_scoped_worker_result_is_provisional(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle, lemma, pinned, manifest_path = _prepare_runtime(tmp_path)
    capsule = AssumptionCapsule(
        lemma_id=lemma.lemma_id,
        direct_predecessors=("lem_dep",),
        transitive_predecessors=("lem_dep",),
        resolved_predecessors=(),
        unresolved_predecessors=("lem_dep",),
        statements_by_lemma={"lem_dep": "theorem lem_dep : True"},
        dependency_source_revision="run_x:layer_01",
        authoritative_workspace_revision=run_paths.run_name,
        worker_workspace_revision=f"{run_paths.run_name}:worker",
    )

    result = run_lemma_formalization(
        run_paths=run_paths,
        runtime_config=runtime_config,
        lemma=lemma,
        pinned=pinned,
        trusted_entries=[],
        assumption_capsule=capsule,
        manifest_path=manifest_path,
        manifest_problem_id=sanitize_component(bundle.problem_id),
        max_attempts=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=[_success_candidate(pinned.signature, lemma_id=lemma.lemma_id)],
        commit_on_success=False,
    )

    assert result.status == "proved_under_assumptions"
    assert result.terminal is False
    assert result.provisional_verified_path is not None
    assert result.provisional_verified_path.exists()
    assert result.merged_authoritative_path is None
    assert result.assumption_capsule_path is not None
    capsule_payload = json.loads(result.assumption_capsule_path.read_text(encoding="utf-8"))
    assert capsule_payload["unresolved_predecessors"] == ["lem_dep"]


def test_structural_validation_rejects_out_of_dag_assumptions() -> None:
    validation = validate_lemma_candidate_structure(
        candidate_text="\n".join(
            [
                "import Mathlib",
                "import Orthos.ScratchContext_lemma_lem_1",
                "",
                "theorem lem_1 : True := by",
                "  trivial",
                "",
            ]
        ),
        pinned_signature="theorem lem_1 : True",
        target_decl_name="lem_1",
        trusted_entries=[],
        assumption_capsule=AssumptionCapsule(
            lemma_id="lem_1",
            direct_predecessors=("lem_dep",),
            transitive_predecessors=("lem_dep",),
            resolved_predecessors=(),
            unresolved_predecessors=("lem_dep",),
            statements_by_lemma={},
            dependency_source_revision="rev",
            authoritative_workspace_revision="rev",
            worker_workspace_revision="worker",
        ),
    )

    assert validation["ok"] is False
    assert validation["error_class"] == "out_of_dag_assumption"
