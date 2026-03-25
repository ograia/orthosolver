from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lean_engine.artifact_io import create_run_paths
from lean_engine.assembly_phase import run_assembly_precheck
from lean_engine.claude_runner import ClaudeRunResult, ClaudeRunTrace, FileUpdateEvent
from lean_engine.config import load_runtime_config
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.prompting import build_statement_repair_prompt, build_statement_translation_prompt
from lean_engine.statement_phase import (
    auto_fix_declaration_names,
    build_phase03_decl_naming,
    run_statement_phase,
)
from lean_engine.workspace import create_workspace_from_template


def _valid_payload(problem_id: str = "prob_phase03") -> dict:
    return {
        "problem_id": problem_id,
        "title": "Phase 03 synthetic",
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


def _make_statements_text(decl_naming) -> str:
    return "\n".join(
        [
            "import Mathlib",
            "",
            f"theorem {decl_naming.root_decl_name} : True := by",
            "  trivial",
            f"theorem {decl_naming.lemma_decl_names['lem_1']} : True := by",
            "  trivial",
            f"lemma {decl_naming.lemma_decl_names['lem_2']} : True := by",
            "  trivial",
            "",
        ]
    )


def _make_axiom_statements_text(decl_naming) -> str:
    return "\n".join(
        [
            "import Mathlib",
            "",
            f"axiom {decl_naming.root_decl_name} : True",
            f"axiom {decl_naming.lemma_decl_names['lem_1']} : True",
            f"axiom {decl_naming.lemma_decl_names['lem_2']} : True",
            "",
        ]
    )


def _make_multiline_axiom_statements_text(decl_naming) -> str:
    return "\n".join(
        [
            "import Mathlib",
            "",
            f"axiom {decl_naming.root_decl_name}",
            "    (a b c : ℝ)",
            "    (h : a + b + c = 0) :",
            "    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c",
            f"axiom {decl_naming.lemma_decl_names['lem_1']}",
            "    (a b c : ℝ)",
            "    (h : a + b + c = 0) :",
            "    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c",
            f"axiom {decl_naming.lemma_decl_names['lem_2']}",
            "    (a b c : ℝ)",
            "    (h : a + b + c = 0) :",
            "    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c",
            "",
        ]
    )


def _make_axiom_statements_with_doc_comments(decl_naming) -> str:
    return "\n".join(
        [
            "import Mathlib",
            "",
            f"axiom {decl_naming.root_decl_name} : True",
            "/-! Root boundary comment -/",
            "",
            f"axiom {decl_naming.lemma_decl_names['lem_1']} : True",
            "/-! Lemma boundary comment -/",
            "",
            f"axiom {decl_naming.lemma_decl_names['lem_2']} : True",
            "",
        ]
    )


def _ok_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _env_dependency_runner(command, **kwargs):
    return subprocess.CompletedProcess(
        command,
        1,
        stdout="",
        stderr="error: reservoir lookup failed while materializing package",
    )


class _ToolWritingClaudeRunner:
    def __init__(self, *, statements_text: str):
        self._statements_text = statements_text

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
        statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
        statements_path.write_text(self._statements_text, encoding="utf-8")

        timestamp = "toolwrite"
        prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
        raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
        summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        raw_path.write_text(json.dumps({"type": "result", "result": ""}) + "\n", encoding="utf-8")

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
            result_event={"type": "result", "result": ""},
        )


class _TraceClaudeRunner:
    def __init__(
        self,
        *,
        target_path: Path,
        tool_update_text: str | None,
        result_text: str,
    ):
        self._target_path = target_path
        self._tool_update_text = tool_update_text
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
        timestamp = "trace"
        prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
        raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
        summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        raw_path.write_text(json.dumps({"type": "result", "result": self._result_text}) + "\n", encoding="utf-8")

        updates = ()
        if self._tool_update_text is not None:
            updates = (FileUpdateEvent(file_path=str(self._target_path), content=self._tool_update_text),)
        trace = ClaudeRunTrace(
            result_text=self._result_text,
            assistant_text_chunks=(),
            file_updates=updates,
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


def test_statement_phase_writes_statements_file(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload())
    decl_naming = build_phase03_decl_naming(bundle)

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=_make_statements_text(decl_naming),
        max_repair_rounds=0,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    assert statements_path.exists()
    text = statements_path.read_text(encoding="utf-8")
    assert decl_naming.root_decl_name in text
    assert decl_naming.lemma_decl_names["lem_1"] in text


def test_statement_phase_accepts_tool_written_workspace_candidate(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_toolwrite", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_phase03_toolwrite"))
    decl_naming = build_phase03_decl_naming(bundle)
    claude_runner = _ToolWritingClaudeRunner(statements_text=_make_axiom_statements_text(decl_naming))

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=None,
        max_repair_rounds=0,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    text = statements_path.read_text(encoding="utf-8")
    assert decl_naming.root_decl_name in text
    assert decl_naming.lemma_decl_names["lem_1"] in text
    assert decl_naming.lemma_decl_names["lem_2"] in text


def test_statement_phase_prefers_tool_update_over_prose_result_text(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_trace_tool", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_phase03_trace_tool"))
    decl_naming = build_phase03_decl_naming(bundle)
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    claude_runner = _TraceClaudeRunner(
        target_path=statements_path,
        tool_update_text=_make_axiom_statements_text(decl_naming),
        result_text="Zero diagnostics - file compiles cleanly.",
    )

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=None,
        max_repair_rounds=0,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.candidate_source == "tool_update"
    text = statements_path.read_text(encoding="utf-8")
    assert decl_naming.root_decl_name in text
    assert "Zero diagnostics" not in text


def test_statement_phase_accepts_result_text_when_it_is_valid_lean(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_trace_result", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_phase03_trace_result"))
    decl_naming = build_phase03_decl_naming(bundle)
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    claude_runner = _TraceClaudeRunner(
        target_path=statements_path,
        tool_update_text=None,
        result_text=_make_axiom_statements_text(decl_naming),
    )

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=None,
        max_repair_rounds=0,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.candidate_source == "result_text"


def test_statement_phase_fails_on_prose_when_no_valid_candidate_source(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_trace_prose", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_phase03_trace_prose"))
    decl_naming = build_phase03_decl_naming(bundle)
    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    claude_runner = _TraceClaudeRunner(
        target_path=statements_path,
        tool_update_text=None,
        result_text="Zero diagnostics - file compiles cleanly.",
    )

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=None,
        max_repair_rounds=0,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "fatal"
    assert result.error_class == "bad_statement_translation"
    assert result.candidate_source == "none"


def test_statement_phase_classifies_environment_dependency_missing(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_env", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_phase03_env"))
    decl_naming = build_phase03_decl_naming(bundle)

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=_make_statements_text(decl_naming),
        max_repair_rounds=0,
        runner=_env_dependency_runner,
    )

    assert result.status == "fatal"
    assert result.error_class == "environment_dependency_missing"


def test_pinned_signatures_are_stable_on_rerun(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_stable", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_stable"))
    decl_naming = build_phase03_decl_naming(bundle)
    statements_text = _make_statements_text(decl_naming)

    first = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=statements_text,
        max_repair_rounds=0,
        runner=_ok_runner,
    )
    assert first.pinned_signatures_path is not None
    first_payload = json.loads(first.pinned_signatures_path.read_text(encoding="utf-8"))

    second = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=statements_text,
        max_repair_rounds=0,
        runner=_ok_runner,
    )
    assert second.pinned_signatures_path is not None
    second_payload = json.loads(second.pinned_signatures_path.read_text(encoding="utf-8"))

    assert first_payload == second_payload
    assert first_payload["lemmas"][0]["decl_name"].startswith("root_") or first_payload["lemmas"][0]["decl_name"].startswith("lem_")


def test_assembly_precheck_produces_structured_failure_for_invalid_plan(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_invalid_assembly", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    payload = _valid_payload(problem_id="prob_invalid_assembly")
    payload["selected_decomposition"]["assembly_plan"]["steps"][0]["uses_lemmas"] = ["missing_lemma"]
    # Disable trivially_composable so the validator actually checks step contents
    payload["selected_decomposition"]["assembly_plan"]["is_trivially_composable"] = False
    bundle = normalize_problem_artifact(payload)
    decl_naming = build_phase03_decl_naming(bundle)

    result = run_assembly_precheck(
        run_paths=run_paths,
        bundle=bundle,
        decl_naming=decl_naming,
        runner=_ok_runner,
    )

    assert result.status == "fatal"
    assert result.error_class == "assembly_invalid"
    assert any("unknown lemma_id" in item for item in result.diagnostics)


def test_assembly_precheck_generates_concrete_theorem_target(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_precheck_target", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_precheck_target"))
    decl_naming = build_phase03_decl_naming(bundle)
    result = run_assembly_precheck(
        run_paths=run_paths,
        bundle=bundle,
        decl_naming=decl_naming,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    scratch_text = result.scratch_file.read_text(encoding="utf-8")
    assert "theorem assembly_precheck_target" in scratch_text
    assert "_from_deps" in scratch_text


def test_assembly_precheck_allows_flat_plan_with_independent_steps(tmp_path: Path) -> None:
    """A flat plan where all steps are independent (no inter-step deps) is valid."""
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_flat_plan", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    payload = _valid_payload(problem_id="prob_flat_plan")
    payload["selected_decomposition"]["assembly_plan"]["steps"][1]["uses_prior_steps"] = []
    bundle = normalize_problem_artifact(payload)
    decl_naming = build_phase03_decl_naming(bundle)

    result = run_assembly_precheck(
        run_paths=run_paths,
        bundle=bundle,
        decl_naming=decl_naming,
        runner=_ok_runner,
    )

    assert result.status == "ok"


def test_assembly_precheck_rejects_vacuous_terminal_step(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_vacuous_terminal", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    payload = _valid_payload(problem_id="prob_vacuous_terminal")
    # Disable trivially_composable so the validator actually checks step contents
    payload["selected_decomposition"]["assembly_plan"]["is_trivially_composable"] = False
    payload["selected_decomposition"]["assembly_plan"]["steps"] = [
        {
            "step_id": "S_terminal",
            "uses_lemmas": [],
            "uses_prior_steps": [],
            "derives": "terminal claim",
            "is_trivial": False,
            "trivial_justification": None,
        }
    ]
    bundle = normalize_problem_artifact(payload)
    decl_naming = build_phase03_decl_naming(bundle)

    result = run_assembly_precheck(
        run_paths=run_paths,
        bundle=bundle,
        decl_naming=decl_naming,
        runner=_ok_runner,
    )

    assert result.status == "fatal"
    assert any("precheck would be vacuous" in item for item in result.diagnostics)


def test_statement_phase_rejects_def_or_abbrev_for_lemma_pinned_signatures(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_bad_lemma_kind", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_bad_lemma_kind"))
    decl_naming = build_phase03_decl_naming(bundle)
    bad_statements = "\n".join(
        [
            "import Mathlib",
            "",
            f"theorem {decl_naming.root_decl_name} : True := by",
            "  trivial",
            f"def {decl_naming.lemma_decl_names['lem_1']} : Prop := True",
            f"lemma {decl_naming.lemma_decl_names['lem_2']} : True := by",
            "  trivial",
            "",
        ]
    )

    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=bad_statements,
        max_repair_rounds=0,
        runner=_ok_runner,
    )

    assert result.status == "fatal"
    assert result.error_class == "bad_statement_translation"
    assert result.diagnostics
    assert "must use `theorem`, `lemma`, or `axiom`" in result.diagnostics[0]


def test_statement_phase_accepts_axiom_headers_and_pins_theorem_signatures(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_axiom_headers", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_axiom_headers"))
    decl_naming = build_phase03_decl_naming(bundle)
    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=_make_axiom_statements_text(decl_naming),
        max_repair_rounds=0,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.pinned_signatures_path is not None
    pinned_payload = json.loads(result.pinned_signatures_path.read_text(encoding="utf-8"))
    assert pinned_payload["root"]["signature"].startswith("theorem ")
    assert pinned_payload["root"]["keyword"] == "theorem"
    for entry in pinned_payload["lemmas"]:
        assert entry["signature"].startswith("theorem ")
        assert entry["keyword"] == "theorem"


def test_statement_phase_parses_multiline_axiom_headers(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_axiom_multiline", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_axiom_multiline"))
    decl_naming = build_phase03_decl_naming(bundle)
    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=_make_multiline_axiom_statements_text(decl_naming),
        max_repair_rounds=0,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.pinned_signatures_path is not None
    pinned_payload = json.loads(result.pinned_signatures_path.read_text(encoding="utf-8"))
    assert pinned_payload["root"]["keyword"] == "theorem"
    assert "a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c" in pinned_payload["root"]["signature"]
    for entry in pinned_payload["lemmas"]:
        assert "a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c" in entry["signature"]


def test_statement_phase_ignores_block_comments_between_axiom_headers(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_axiom_doc_comments", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)

    bundle = normalize_problem_artifact(_valid_payload(problem_id="prob_axiom_doc_comments"))
    decl_naming = build_phase03_decl_naming(bundle)
    result = run_statement_phase(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        decl_naming=decl_naming,
        provided_statements_text=_make_axiom_statements_with_doc_comments(decl_naming),
        max_repair_rounds=0,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.pinned_signatures_path is not None
    pinned_payload = json.loads(result.pinned_signatures_path.read_text(encoding="utf-8"))
    lemma_signature = pinned_payload["lemmas"][0]["signature"]
    assert "/-!" not in lemma_signature
    assert lemma_signature == f"theorem {decl_naming.lemma_decl_names['lem_1']} : True"


def test_decl_naming_is_deterministic_and_collision_safe() -> None:
    payload = _valid_payload(problem_id="Problem-42")
    payload["lemmas"][0]["lemma_id"] = "lem-a"
    payload["lemmas"][1]["lemma_id"] = "lem_a"
    payload["selected_decomposition"]["assembly_plan"]["steps"][0]["uses_lemmas"] = ["lem-a"]
    payload["selected_decomposition"]["assembly_plan"]["steps"][1]["uses_lemmas"] = ["lem_a"]

    bundle = normalize_problem_artifact(payload)
    first = build_phase03_decl_naming(bundle)
    second = build_phase03_decl_naming(bundle)

    assert first.root_decl_name == second.root_decl_name == "root_problem_42"
    assert first.lemma_decl_names == second.lemma_decl_names
    assert first.lemma_decl_names["lem-a"] == "lem_a"
    assert first.lemma_decl_names["lem_a"] == "lem_a_2"


def test_prompt_contract_enforces_statements_only_and_required_fields() -> None:
    bundle = normalize_problem_artifact(_valid_payload())
    decl_naming = build_phase03_decl_naming(bundle)

    translation_prompt = build_statement_translation_prompt(bundle, decl_naming.to_prompt_naming())
    assert "STATEMENTS ONLY" in translation_prompt
    assert "DO NOT ATTEMPT OR SOLVE PROOFS" in translation_prompt
    assert "statement_nl" in translation_prompt
    assert "semantic_sketch" in translation_prompt
    assert decl_naming.root_decl_name in translation_prompt

    repair_prompt = build_statement_repair_prompt(
        bundle,
        decl_naming.to_prompt_naming(),
        current_statements_text=_make_statements_text(decl_naming),
        diagnostics_text="fake diagnostics",
        repair_round=1,
    )
    assert "STATEMENTS ONLY" in repair_prompt
    assert "Return a COMPLETE replacement for `Orthos/Statements.lean`" in repair_prompt


# ---------------------------------------------------------------------------
# auto_fix_declaration_names tests
# ---------------------------------------------------------------------------

def _make_naming_with_root_and_lemmas() -> tuple:
    """Create a Phase03DeclNaming and matching correct/wrong Statements.lean texts."""
    from lean_engine.statement_phase import Phase03DeclNaming

    naming = Phase03DeclNaming(
        problem_id="prob_001",
        root_decl_name="root_prob_001",
        lemma_decl_names={"lem_a": "lem_a", "lem_b": "lem_b"},
        ordered_lemma_ids=("lem_a", "lem_b"),
    )

    correct_text = "\n".join([
        "import Mathlib",
        "",
        "axiom root_prob_001 : True",
        "axiom lem_a : True",
        "axiom lem_b : True",
        "",
    ])

    wrong_root_text = "\n".join([
        "import Mathlib",
        "",
        "axiom thm_root_001_xyz : True",
        "axiom lem_a : True",
        "axiom lem_b : True",
        "",
    ])

    return naming, correct_text, wrong_root_text


def test_auto_fix_returns_none_when_names_correct() -> None:
    naming, correct_text, _ = _make_naming_with_root_and_lemmas()
    assert auto_fix_declaration_names(correct_text, naming) is None


def test_auto_fix_renames_wrong_root_name() -> None:
    naming, _, wrong_root_text = _make_naming_with_root_and_lemmas()
    fixed = auto_fix_declaration_names(wrong_root_text, naming)
    assert fixed is not None
    assert "root_prob_001" in fixed
    assert "thm_root_001_xyz" not in fixed
    # Lemma names should be unchanged
    assert "lem_a" in fixed
    assert "lem_b" in fixed


def test_auto_fix_renames_multiple_wrong_names() -> None:
    from lean_engine.statement_phase import Phase03DeclNaming

    naming = Phase03DeclNaming(
        problem_id="prob_002",
        root_decl_name="root_prob_002",
        lemma_decl_names={"lem_x": "lem_x"},
        ordered_lemma_ids=("lem_x",),
    )
    text = "\n".join([
        "import Mathlib",
        "",
        "axiom wrong_root : True",
        "axiom wrong_lemma : True",
        "",
    ])
    fixed = auto_fix_declaration_names(text, naming)
    assert fixed is not None
    assert "root_prob_002" in fixed
    assert "lem_x" in fixed
    assert "wrong_root" not in fixed
    assert "wrong_lemma" not in fixed


def test_auto_fix_returns_none_when_count_mismatch() -> None:
    """If there are more missing names than extra declarations, bail out."""
    from lean_engine.statement_phase import Phase03DeclNaming

    naming = Phase03DeclNaming(
        problem_id="prob_003",
        root_decl_name="root_prob_003",
        lemma_decl_names={"lem_a": "lem_a", "lem_b": "lem_b"},
        ordered_lemma_ids=("lem_a", "lem_b"),
    )
    # Only one declaration present — 2 missing, 0 extra → can't match
    text = "\n".join([
        "import Mathlib",
        "",
        "axiom root_prob_003 : True",
        "",
    ])
    assert auto_fix_declaration_names(text, naming) is None
