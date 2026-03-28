from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lean_engine.artifact_io import create_run_paths
from lean_engine.claude_runner import ClaudeRunResult, ClaudeRunTrace, FileUpdateEvent
from lean_engine.cli import main
from lean_engine.config import load_runtime_config
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.lemma_phase import TrustedContextEntry
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.phase06 import run_phase06
from lean_engine.statement_phase import build_phase03_decl_naming
from lean_engine.workspace import create_workspace_from_template


def _payload(problem_id: str = "prob_phase06", *, two_lemmas: bool = False) -> dict:
    lemmas = [
        {
            "lemma_id": "lem_1",
            "statement_nl": "lemma one",
            "semantic_sketch": {"normalized_claim": "l1"},
            "proof_nl": "proof one",
        }
    ]
    steps = [
        {
            "step_id": "S1",
            "uses_lemmas": ["lem_1"],
            "uses_prior_steps": [],
            "derives": "first",
            "is_trivial": True,
            "trivial_justification": "given",
        }
    ]

    if two_lemmas:
        lemmas.append(
            {
                "lemma_id": "lem_2",
                "statement_nl": "lemma two",
                "semantic_sketch": {"normalized_claim": "l2"},
                "proof_nl": "proof two",
            }
        )
        steps.append(
            {
                "step_id": "S2",
                "uses_lemmas": ["lem_2"],
                "uses_prior_steps": ["S1"],
                "derives": "second",
                "is_trivial": True,
                "trivial_justification": "given",
            }
        )

    return {
        "problem_id": problem_id,
        "title": "phase06 synthetic",
        "verification_level": "nl_only",
        "root_theorem": {
            "theorem_id": "thm_root",
            "statement_nl": "root",
            "semantic_sketch": {"normalized_claim": "root"},
        },
        "selected_decomposition": {
            "decomposition_id": "dec_1",
            "assembly_plan": {
                "assembly_plan_id": "asm_1",
                "steps": steps,
                "proof_skeleton_nl": "compose accepted lemmas",
            },
        },
        "lemmas": lemmas,
        "all_visible_lemmas_nl_accepted": True,
    }


def _ok_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _latest_working_root(workspace_dir: Path, fallback: Path) -> Path:
    matches = sorted((workspace_dir / "Orthos").glob("Root_working_round_*.lean"))
    return matches[-1] if matches else fallback


class _RootTraceRunner:
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
        target_path = _latest_working_root(run_paths.workspace_dir, self._target_path)
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


class _SequentialRootTraceRunner:
    def __init__(self, *, fallback_target_path: Path, candidates: list[str], result_texts: list[str] | None = None):
        self._fallback_target_path = fallback_target_path
        self._candidates = candidates
        self._result_texts = result_texts or ["ok"] * len(candidates)
        self._index = 0
        self.seed_texts: list[str] = []

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
        _ = (prompt, timeout_seconds, permission_mode)
        candidate = self._candidates[self._index]
        result_text = self._result_texts[self._index]
        self._index += 1
        target_path = _latest_working_root(run_paths.workspace_dir, self._fallback_target_path)
        self.seed_texts.append(target_path.read_text(encoding="utf-8"))
        target_path.write_text(candidate, encoding="utf-8")
        timestamp = f"seq_{self._index:02d}"
        prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
        raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
        summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("", encoding="utf-8")
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


def _root_fails_runner(command, **kwargs):
    command_tuple = tuple(command)
    # Phase 06 now verifies via Combined.lean (single-file check)
    if command_tuple == ("lake", "env", "lean", "Combined.lean"):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="type mismatch")
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _prepare_runtime(tmp_path: Path, problem_id: str) -> tuple:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths(problem_id, artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_payload(problem_id=problem_id, two_lemmas=True))
    return runtime_config, run_paths, bundle


def _write_phase06_prereqs(run_paths, bundle) -> tuple[Path, Path, Path, dict[str, str], str]:
    decl_naming = build_phase03_decl_naming(bundle)

    pinned_path = run_paths.run_root / "pinned_signatures.json"
    pinned_payload = {
        "problem_id": bundle.problem_id,
        "run_name": run_paths.run_name,
        "root": {
            "decl_name": decl_naming.root_decl_name,
            "signature": f"theorem {decl_naming.root_decl_name} : True",
            "statement_nl": bundle.root_theorem.statement_nl,
        },
        "lemmas": [
            {
                "lemma_id": lemma_id,
                "decl_name": decl_naming.lemma_decl_names[lemma_id],
                "signature": f"theorem {decl_naming.lemma_decl_names[lemma_id]} : True",
                "statement_nl": bundle.lemma_map[lemma_id].statement_nl,
            }
            for lemma_id in decl_naming.ordered_lemma_ids
        ],
    }
    pinned_path.write_text(json.dumps(pinned_payload), encoding="utf-8")

    entries = [
        TrustedContextEntry(
            lemma_id=lemma_id,
            decl_name=decl_naming.lemma_decl_names[lemma_id],
            status="compiled",
            source_file="Orthos/Lemmas.lean",
            signature=f"theorem {decl_naming.lemma_decl_names[lemma_id]} : True",
            declaration=f"theorem {decl_naming.lemma_decl_names[lemma_id]} : True := by\n  trivial\n",
        )
        for lemma_id in decl_naming.ordered_lemma_ids
    ]

    semantic_manifest_path = run_paths.run_root / "trusted_context_semantic_manifest.json"
    semantic_manifest_path.write_text(
        json.dumps(
            {
                "problem_id": run_paths.problem_id,
                "entry_count": len(entries),
                "entries": [entry.to_dict() for entry in entries],
                "rejected_lemma_ids": [],
            }
        ),
        encoding="utf-8",
    )

    phase05_summary_path = run_paths.summaries_dir / "phase05_summary.json"
    phase05_summary_path.parent.mkdir(parents=True, exist_ok=True)
    phase05_summary_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "problem_id": bundle.problem_id,
                "semantically_accepted_lemma_ids": list(decl_naming.ordered_lemma_ids),
            }
        ),
        encoding="utf-8",
    )

    lemma_decl_names = {lemma_id: decl_naming.lemma_decl_names[lemma_id] for lemma_id in decl_naming.ordered_lemma_ids}
    return pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, decl_naming.root_decl_name


def _root_candidate(root_signature: str, lemma_decl_names: dict[str, str], *, use_two: bool = True) -> str:
    proof_lines = [f"  have h1 : True := {lemma_decl_names['lem_1']}"]
    if use_two:
        proof_lines.append(f"  have h2 : True := {lemma_decl_names['lem_2']}")
        proof_lines.append("  trivial")
    else:
        proof_lines.append("  exact h1")

    return "\n".join(
        [
            "import Orthos.Lemmas",
            "",
            f"{root_signature} := by",
            *proof_lines,
            "",
        ]
    )


def test_phase06_generates_root_file_and_round_artifacts(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle = _prepare_runtime(tmp_path, "prob_phase06_root_file")
    pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, root_decl_name = _write_phase06_prereqs(
        run_paths,
        bundle,
    )

    result = run_phase06(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        max_root_attempts=1,
        timeout_seconds=10,
        model=None,
        mock_root_candidates=[_root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names)],
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.rounds_used == 1
    assert result.root_file_path.exists()
    root_text = result.root_file_path.read_text(encoding="utf-8")
    assert f"theorem {root_decl_name} : True := by" in root_text
    assert (run_paths.run_root / "root_assembly" / "root_round_01.lean").exists()


def test_phase06_prefers_tool_update_candidate_over_prose_result(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle = _prepare_runtime(tmp_path, "prob_phase06_trace_tool")
    pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, root_decl_name = _write_phase06_prereqs(
        run_paths,
        bundle,
    )
    root_candidate = _root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names)
    claude_runner = _RootTraceRunner(
        target_path=run_paths.workspace_dir / "Orthos" / "Root.lean",
        candidate_text=root_candidate,
        result_text="Zero diagnostics - root file compiles.",
    )

    result = run_phase06(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        max_root_attempts=1,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_ok_runner,
    )

    assert result.status == "ok"
    assert result.rounds
    assert result.rounds[0].candidate_source == "workspace_fallback"


def test_phase06_writes_success_bundle_manifest_and_combined_file(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle = _prepare_runtime(tmp_path, "prob_phase06_success_bundle")
    pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, root_decl_name = _write_phase06_prereqs(
        run_paths,
        bundle,
    )

    result = run_phase06(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        max_root_attempts=1,
        timeout_seconds=10,
        model=None,
        mock_root_candidates=[_root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names)],
        runner=_ok_runner,
    )

    assert result.status == "ok"
    final_payload = json.loads(result.final_result_path.read_text(encoding="utf-8"))
    assert final_payload["status"] == "success"
    assert result.final_combined_path is not None and result.final_combined_path.exists()
    assert result.project_manifest_path is not None and result.project_manifest_path.exists()

    combined_text = result.final_combined_path.read_text(encoding="utf-8")
    assert "import Mathlib" in combined_text
    assert "===== Statements =====" in combined_text
    assert "===== Lemmas =====" in combined_text
    assert "===== Root =====" in combined_text


def test_phase06_classifies_root_composition_failure(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle = _prepare_runtime(tmp_path, "prob_phase06_compose_fail")
    pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, root_decl_name = _write_phase06_prereqs(
        run_paths,
        bundle,
    )

    result = run_phase06(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        max_root_attempts=1,
        timeout_seconds=10,
        model=None,
        mock_root_candidates=[_root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names)],
        runner=_root_fails_runner,
    )

    assert result.status == "fatal"
    assert result.error_class == "assembly_composition_failure"
    fatal_payload = json.loads(result.final_result_path.read_text(encoding="utf-8"))
    assert fatal_payload["status"] == "fatal"
    assert fatal_payload["error_class"] == "assembly_composition_failure"


def test_phase06_round_two_keeps_last_compiling_root_after_compile_failure(tmp_path: Path) -> None:
    runtime_config, run_paths, bundle = _prepare_runtime(tmp_path, "prob_phase06_seed_root")
    pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, root_decl_name = _write_phase06_prereqs(
        run_paths,
        bundle,
    )
    round_1 = _root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names).replace(
        "by",
        "by\n  -- round_1_marker",
        1,
    )
    round_2 = _root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names)
    claude_runner = _SequentialRootTraceRunner(
        fallback_target_path=run_paths.workspace_dir / "Orthos" / "Root.lean",
        candidates=[round_1, round_2],
    )

    def _combined_runner(command, **kwargs):
        _ = kwargs
        command_tuple = tuple(command)
        if command_tuple == ("lake", "env", "lean", "Combined.lean"):
            text = (run_paths.workspace_dir / "Combined.lean").read_text(encoding="utf-8")
            if "round_1_marker" in text:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="type mismatch")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = run_phase06(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        semantic_manifest_path=semantic_manifest_path,
        phase05_summary_path=phase05_summary_path,
        max_root_attempts=2,
        timeout_seconds=10,
        model=None,
        claude_runner=claude_runner,
        runner=_combined_runner,
    )

    assert result.status == "ok"
    assert len(claude_runner.seed_texts) == 2
    assert "round_1_marker" not in claude_runner.seed_texts[1]
    assert result.rounds[0].promotion_status == "rejected_compile"


def test_cli_phase06_run_with_mock_candidate(tmp_path: Path, capsys, monkeypatch) -> None:
    runtime_config, run_paths, bundle = _prepare_runtime(tmp_path, "prob_phase06_cli")
    run_paths.runtime_config_path.write_text(json.dumps(runtime_config.to_dict()), encoding="utf-8")
    run_paths.normalized_problem_path.write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    pinned_path, semantic_manifest_path, phase05_summary_path, lemma_decl_names, root_decl_name = _write_phase06_prereqs(
        run_paths,
        bundle,
    )

    mock_dir = tmp_path / "mock_root"
    mock_dir.mkdir(parents=True, exist_ok=True)
    (mock_dir / "round_01.lean").write_text(
        _root_candidate(f"theorem {root_decl_name} : True", lemma_decl_names),
        encoding="utf-8",
    )

    def fake_check_lean_file(workspace_root, relative_file, **kwargs):
        return LeanCommandResult(
            check_name=f"file_{relative_file.replace('/', '_')}",
            command=("lake", "env", "lean", relative_file),
            cwd=Path(workspace_root).resolve(),
            returncode=0,
            stdout="ok",
            stderr="",
            duration_seconds=0.01,
        )

    monkeypatch.setattr("lean_engine.phase06.check_lean_file", fake_check_lean_file)

    exit_code = main(
        [
            "phase06-run",
            str(run_paths.run_root),
            "--pinned-signatures",
            str(pinned_path),
            "--semantic-manifest",
            str(semantic_manifest_path),
            "--phase05-summary",
            str(phase05_summary_path),
            "--mock-root-candidates-dir",
            str(mock_dir),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    summary = json.loads(captured.out)
    assert summary["status"] == "ok"
