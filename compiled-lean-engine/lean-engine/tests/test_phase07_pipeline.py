from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lean_engine.claude_runner import ClaudeRunResult
from lean_engine.cli import main
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.statement_phase import build_phase03_decl_naming


FIXTURE_DIR = Path(__file__).parent / "fixtures"
RAW_FIXTURE_DIR = FIXTURE_DIR / "raw"
NORMALIZED_FIXTURE_DIR = FIXTURE_DIR / "normalized"
MOCK_FIXTURE_DIR = FIXTURE_DIR / "mocks"


def _fake_run_command(*, fail_root_module_check: bool = False):
    def _runner(*, check_name, workspace_root, command, timeout_seconds, runner, **kwargs):
        command_tuple = tuple(command)
        if fail_root_module_check and command_tuple == ("lake", "env", "lean", "Combined.lean"):
            return LeanCommandResult(
                check_name=check_name,
                command=command_tuple,
                cwd=Path(workspace_root).resolve(),
                returncode=1,
                stdout="",
                stderr="type mismatch",
                duration_seconds=0.01,
            )
        return LeanCommandResult(
            check_name=check_name,
            command=command_tuple,
            cwd=Path(workspace_root).resolve(),
            returncode=0,
            stdout="ok",
            stderr="",
            duration_seconds=0.01,
        )

    return _runner


def _write_statements_file(
    path: Path,
    *,
    root_decl_name: str,
    ordered_lemma_ids: tuple[str, ...],
    lemma_decl_names: dict[str, str],
    invalid: bool = False,
) -> Path:
    lines = [
        "import Mathlib",
        "",
        f"theorem {root_decl_name} : True := by",
        "  trivial",
    ]
    for lemma_id in ordered_lemma_ids:
        lines.append(f"theorem {lemma_decl_names[lemma_id]} : True := by")
        lines.append("  trivial")
    if invalid:
        lines.extend(
            [
                "",
                "-- Intentional Phase 03 failure token for regression testing.",
                "theorem bad_statement_fixture : True := by",
                "  sorry",
            ]
        )
    lines.extend([""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_phase04_mocks(root: Path, *, lemma_id: str, pinned_signature: str) -> Path:
    lemma_dir = root / lemma_id
    lemma_dir.mkdir(parents=True, exist_ok=True)
    candidate = "\n".join(
        [
            "import Mathlib",
            "import Orthos.Lemmas",
            "",
            f"-- Phase 04 scratch file for {lemma_id}.",
            f"-- edit_scope: declaration target_{lemma_id}",
            "",
            f"{pinned_signature} := by",
            "  trivial",
            "",
        ]
    )
    (lemma_dir / "round_01.lean").write_text(candidate, encoding="utf-8")
    return root


def _write_phase06_mocks(root: Path, *, root_signature: str, lemma_decl_name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    candidate = "\n".join(
        [
            "import Orthos.Lemmas",
            "",
            f"{root_signature} := by",
            f"  have h1 : True := {lemma_decl_name}",
            "  trivial",
            "",
        ]
    )
    (root / "round_01.lean").write_text(candidate, encoding="utf-8")
    return root


def _prepare_mock_files(tmp_path: Path, fixture_name: str, *, invalid_statements: bool = False) -> dict[str, Path]:
    raw_fixture = RAW_FIXTURE_DIR / f"{fixture_name}.json"
    bundle = normalize_problem_artifact(raw_fixture)
    decl_naming = build_phase03_decl_naming(bundle)

    statements_path = _write_statements_file(
        tmp_path / f"{fixture_name}_statements.lean",
        root_decl_name=decl_naming.root_decl_name,
        ordered_lemma_ids=decl_naming.ordered_lemma_ids,
        lemma_decl_names=decl_naming.lemma_decl_names,
        invalid=invalid_statements,
    )

    phase04_dir = tmp_path / f"{fixture_name}_phase04_mock"
    first_lemma_id = decl_naming.ordered_lemma_ids[0]
    _write_phase04_mocks(
        phase04_dir,
        lemma_id=first_lemma_id,
        pinned_signature=f"theorem {decl_naming.lemma_decl_names[first_lemma_id]} : True",
    )

    phase06_dir = _write_phase06_mocks(
        tmp_path / f"{fixture_name}_phase06_mock",
        root_signature=f"theorem {decl_naming.root_decl_name} : True",
        lemma_decl_name=decl_naming.lemma_decl_names[first_lemma_id],
    )

    return {
        "raw_fixture": raw_fixture,
        "statements_path": statements_path,
        "phase04_dir": phase04_dir,
        "phase06_dir": phase06_dir,
    }


def _fake_claude_result_for_phase05(*, run_paths, phase_name: str, payload: dict[str, Any]) -> ClaudeRunResult:
    timestamp = "fake"
    prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
    raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
    summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("", encoding="utf-8")
    raw_path.write_text(json.dumps({"type": "result", "result": json.dumps(payload)}) + "\n", encoding="utf-8")
    summary_path.write_text("{}", encoding="utf-8")
    return ClaudeRunResult(
        phase_name=phase_name,
        model="claude-opus-4-6",
        command=("claude",),
        cwd=run_paths.workspace_dir,
        returncode=0,
        timed_out=False,
        duration_seconds=0.01,
        prompt_path=prompt_path,
        raw_output_path=raw_path,
        summary_path=summary_path,
        result_event={"result": json.dumps(payload)},
        event_counts={"result": 1},
    )


def test_phase07_fixture_set_exists_in_raw_and_normalized_trees() -> None:
    fixture_names = {
        "b4_clean",
        "b4_messy_export",
        "statement_ambiguous",
        "lemma_major_gap",
        "assembly_mismatch",
        "tiny_success",
    }
    for fixture_name in fixture_names:
        raw_extension = "txt" if fixture_name == "b4_messy_export" else "json"
        assert (RAW_FIXTURE_DIR / f"{fixture_name}.{raw_extension}").exists()
        assert (NORMALIZED_FIXTURE_DIR / f"{fixture_name}.json").exists()


def test_phase07_b4_messy_fixture_normalizes_to_clean_problem() -> None:
    bundle = normalize_problem_artifact(RAW_FIXTURE_DIR / "b4_messy_export.txt")
    assert bundle.problem_id == "prob_b4_clean"
    assert bundle.provenance.extraction_method == "candidate_scan"
    assert bundle.lemma_count == 2


def test_cli_run_statement_ambiguous_fails_in_phase03(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    paths = _prepare_mock_files(tmp_path, "statement_ambiguous", invalid_statements=True)

    exit_code = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--statements-file",
            str(paths["statements_path"]),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "fatal"
    assert payload["fatal_error_class"] == "bad_statement_translation"
    assert payload["phase_statuses"]["phase03_statement"] == "fatal"
    assert payload["run_root"] is not None
    assert payload["phase_summary_path"] is not None
    assert Path(payload["phase_summary_path"]).exists()
    assert isinstance(payload.get("integration_usage"), dict)


def test_cli_run_lemma_major_gap_fails_in_phase05(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "lemma_major_gap")

    exit_code = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--max-semantic-repairs",
            "0",
            "--statements-file",
            str(paths["statements_path"]),
            "--mock-candidates-dir",
            str(paths["phase04_dir"]),
            "--mock-equivalence-verdicts",
            str(MOCK_FIXTURE_DIR / "equivalence_major_gap.json"),
            "--mock-root-candidates-dir",
            str(paths["phase06_dir"]),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "fatal"
    assert payload["fatal_error_class"] == "major_proof_gap"
    assert payload["phase_statuses"]["phase05_semantic"] == "fatal"
    assert payload["final_result_path"] is not None
    final_result = json.loads(Path(payload["final_result_path"]).read_text(encoding="utf-8"))
    assert final_result["error_class"] == "major_proof_gap"


def test_cli_run_assembly_mismatch_fails_in_phase06(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "lean_engine.lean_checks._run_command",
        _fake_run_command(fail_root_module_check=True),
    )
    paths = _prepare_mock_files(tmp_path, "assembly_mismatch")

    exit_code = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--max-root-attempts",
            "1",
            "--statements-file",
            str(paths["statements_path"]),
            "--mock-candidates-dir",
            str(paths["phase04_dir"]),
            "--mock-equivalence-verdicts",
            str(MOCK_FIXTURE_DIR / "equivalence_yes.json"),
            "--mock-root-candidates-dir",
            str(paths["phase06_dir"]),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "fatal"
    assert payload["fatal_error_class"] == "assembly_composition_failure"
    assert payload["phase_statuses"]["phase06_root"] == "fatal"
    assert payload["final_result_path"] is not None
    final_result = json.loads(Path(payload["final_result_path"]).read_text(encoding="utf-8"))
    assert final_result["error_class"] == "assembly_composition_failure"


def test_cli_inspect_reports_phase_statuses(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    run_exit = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--statements-file",
            str(paths["statements_path"]),
            "--mock-candidates-dir",
            str(paths["phase04_dir"]),
            "--mock-equivalence-verdicts",
            str(MOCK_FIXTURE_DIR / "equivalence_yes.json"),
            "--mock-root-candidates-dir",
            str(paths["phase06_dir"]),
            "--json",
        ]
    )
    run_payload = json.loads(capsys.readouterr().out)
    assert run_exit == 0

    inspect_exit = main(["inspect", run_payload["run_root"], "--json"])
    inspect_payload = json.loads(capsys.readouterr().out)
    assert inspect_exit == 0
    assert inspect_payload["phase_statuses"]["phase07"]["status"] == "ok"
    assert inspect_payload["final_result"]["status"] == "success"


def test_cli_run_single_lemma_mode_stops_after_phase05(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    exit_code = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--lemma-only",
            "--lemma-id",
            "lem_1",
            "--statements-file",
            str(paths["statements_path"]),
            "--mock-candidates-dir",
            str(paths["phase04_dir"]),
            "--mock-equivalence-verdicts",
            str(MOCK_FIXTURE_DIR / "equivalence_yes.json"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["phase_statuses"]["phase05_semantic"] == "ok"
    assert payload["phase_statuses"]["phase06_root"] == "skipped"
    assert payload["compiled_lemma_count"] == 1


def test_cli_run_real_phase05_branch_with_fake_claude_runner(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "tiny_success")
    calls: list[str] = []

    def _fake_run_prompt(self, *, run_paths, prompt, phase_name, **kwargs):
        calls.append(phase_name)
        payload = {
            "match": "yes",
            "explanation": "Semantic statements align.",
            "drift": "",
            "classification_hint": "unknown",
        }
        return _fake_claude_result_for_phase05(run_paths=run_paths, phase_name=phase_name, payload=payload)

    monkeypatch.setattr("lean_engine.claude_runner.ClaudeRunner.run_prompt", _fake_run_prompt)

    exit_code = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--statements-file",
            str(paths["statements_path"]),
            "--mock-candidates-dir",
            str(paths["phase04_dir"]),
            "--mock-root-candidates-dir",
            str(paths["phase06_dir"]),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["phase_statuses"]["phase05_semantic"] == "ok"
    assert any(name.startswith("phase05_equivalence_") for name in calls)


def test_cli_resume_without_old_phase05_summary_reaches_phase05(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    initial_exit = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--statements-file",
            str(paths["statements_path"]),
            "--mock-candidates-dir",
            str(paths["phase04_dir"]),
            "--mock-equivalence-verdicts",
            str(MOCK_FIXTURE_DIR / "equivalence_yes.json"),
            "--mock-root-candidates-dir",
            str(paths["phase06_dir"]),
            "--json",
        ]
    )
    initial_payload = json.loads(capsys.readouterr().out)
    assert initial_exit == 0
    old_run_root = Path(initial_payload["run_root"])
    (old_run_root / "summaries" / "phase05_summary.json").unlink(missing_ok=True)
    (old_run_root / "summaries" / "phase07_summary.json").unlink(missing_ok=True)

    def _fake_run_prompt(self, *, run_paths, prompt, phase_name, **kwargs):
        payload = {
            "match": "yes",
            "explanation": "Semantic statements align.",
            "drift": "",
            "classification_hint": "unknown",
        }
        return _fake_claude_result_for_phase05(run_paths=run_paths, phase_name=phase_name, payload=payload)

    monkeypatch.setattr("lean_engine.claude_runner.ClaudeRunner.run_prompt", _fake_run_prompt)

    resume_exit = main(
        [
            "run",
            "--resume",
            str(old_run_root),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--lemma-only",
            "--json",
        ]
    )
    resume_payload = json.loads(capsys.readouterr().out)

    assert resume_exit == 0
    assert resume_payload["status"] == "ok"
    assert resume_payload["phase_statuses"]["phase05_semantic"] == "ok"
    assert Path(resume_payload["run_root"]) != old_run_root
    assert (Path(resume_payload["run_root"]) / "summaries" / "phase05_summary.json").exists()


def test_cli_run_unexpected_exception_returns_structured_fatal(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    def _boom(*_args, **_kwargs):
        raise NameError("synthetic cli phase07 crash")

    monkeypatch.setattr("lean_engine.cli.run_phase07", _boom)

    exit_code = main(
        [
            "run",
            str(paths["raw_fixture"]),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "fatal"
    assert payload["error_scope"] == "phase07"
    assert payload["error_class"] == "unknown_fatal"
    assert any("NameError" in item for item in payload["diagnostics"])
