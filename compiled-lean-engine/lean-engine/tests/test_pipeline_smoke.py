from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_engine.cli import main
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.statement_phase import build_phase03_decl_naming


def _fake_run_command(*, check_name, workspace_root, command, timeout_seconds, runner, **kwargs):
    return LeanCommandResult(
        check_name=check_name,
        command=tuple(command),
        cwd=Path(workspace_root).resolve(),
        returncode=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.01,
    )


def test_pipeline_smoke_tiny_success(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command)
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    fixture_root = Path(__file__).parent / "fixtures"
    raw_path = fixture_root / "raw" / "tiny_success.json"
    bundle = normalize_problem_artifact(raw_path)
    decl_naming = build_phase03_decl_naming(bundle)

    statements_path = tmp_path / "tiny_success_statements.lean"
    statements_path.write_text(
        "\n".join(
            [
                "import Mathlib",
                "",
                f"theorem {decl_naming.root_decl_name} : True := by",
                "  trivial",
                f"theorem {decl_naming.lemma_decl_names['lem_1']} : True := by",
                "  trivial",
                "",
            ]
        ),
        encoding="utf-8",
    )

    phase04_dir = tmp_path / "phase04_mock" / "lem_1"
    phase04_dir.mkdir(parents=True, exist_ok=True)
    (phase04_dir / "round_01.lean").write_text(
        "\n".join(
            [
                "import Mathlib",
                "import Orthos.Lemmas",
                "",
                "-- Phase 04 scratch file for lem_1.",
                "-- edit_scope: declaration target_lem_1",
                "",
                "theorem lem_1 : True := by",
                "  trivial",
                "",
            ]
        ),
        encoding="utf-8",
    )

    phase06_dir = tmp_path / "phase06_mock"
    phase06_dir.mkdir(parents=True, exist_ok=True)
    (phase06_dir / "round_01.lean").write_text(
        "\n".join(
            [
                "import Orthos.Lemmas",
                "",
                f"theorem {decl_naming.root_decl_name} : True := by",
                "  have h1 : True := lem_1",
                "  trivial",
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "run",
            str(raw_path),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--max-repair-rounds",
            "0",
            "--max-attempts-per-lemma",
            "1",
            "--statements-file",
            str(statements_path),
            "--mock-candidates-dir",
            str(tmp_path / "phase04_mock"),
            "--mock-equivalence-verdicts",
            str(fixture_root / "mocks" / "equivalence_yes.json"),
            "--mock-root-candidates-dir",
            str(phase06_dir),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["fatal_error_class"] is None
    assert payload["lemma_count"] == 1
    assert payload["compiled_lemma_count"] == 1
    assert payload["phase_statuses"]["phase06_root"] == "ok"
