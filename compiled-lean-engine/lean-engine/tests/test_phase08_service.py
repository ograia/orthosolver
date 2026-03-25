from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lean_engine.config import load_runtime_config
from lean_engine.integrations import IntegrationPreflight
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.service.app import LeanEngineServiceApp, _integration_health
from lean_engine.statement_phase import build_phase03_decl_naming

FIXTURE_DIR = Path(__file__).parent / "fixtures"
RAW_FIXTURE_DIR = FIXTURE_DIR / "raw"
MOCK_FIXTURE_DIR = FIXTURE_DIR / "mocks"


@pytest.fixture
def service_app(tmp_path: Path):
    app = LeanEngineServiceApp.from_defaults(
        db_path=tmp_path / "service_jobs.sqlite3",
        max_workers=2,
    )
    try:
        yield app
    finally:
        app.shutdown()


def _fake_run_command(*, fail_root_file_check: bool = False):
    def _runner(*, check_name, workspace_root, command, timeout_seconds, runner, **kwargs):
        command_tuple = tuple(command)
        if fail_root_file_check and command_tuple == ("lake", "env", "lean", "Orthos/Root.lean"):
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


def _prepare_mock_files(tmp_path: Path, fixture_name: str) -> dict[str, Path | str]:
    raw_fixture = RAW_FIXTURE_DIR / f"{fixture_name}.json"
    bundle = normalize_problem_artifact(raw_fixture)
    decl_naming = build_phase03_decl_naming(bundle)

    statements_path = _write_statements_file(
        tmp_path / f"{fixture_name}_statements.lean",
        root_decl_name=decl_naming.root_decl_name,
        ordered_lemma_ids=decl_naming.ordered_lemma_ids,
        lemma_decl_names=decl_naming.lemma_decl_names,
    )

    first_lemma_id = decl_naming.ordered_lemma_ids[0]
    phase04_dir = _write_phase04_mocks(
        tmp_path / f"{fixture_name}_phase04_mock",
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
        "lemma_id": first_lemma_id,
    }


def _wait_for_terminal(app: LeanEngineServiceApp, job_id: str, *, timeout_s: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        code, payload = app.get_job(job_id)
        assert code == 200
        if payload["status"] in {"success", "repairable", "fatal", "cancelled"}:
            return payload
        time.sleep(0.03)
    raise AssertionError(f"job did not reach a terminal state before timeout: {job_id}")


def test_service_health_reports_phase08_contract(service_app: LeanEngineServiceApp) -> None:
    code, payload = service_app.health()
    assert code == 200
    assert payload["status"] in {"healthy", "degraded"}
    assert any(payload["default_model"].startswith(m) for m in ("claude-opus-4-6", "claude-sonnet-4-6"))
    assert isinstance(payload["lean_project_template_ok"], bool)
    assert isinstance(payload["mcp_config_available"], bool)


def test_integration_health_checks_lean_lsp_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")

    monkeypatch.setattr(
        "lean_engine.service.app.lean_lsp_mcp.preflight",
        lambda **_: IntegrationPreflight(name="lean_lsp_mcp", status="ok", checks={"ok": True}),
    )

    payload = _integration_health(runtime_config)

    assert payload["ok"] is True
    assert len(payload["preflight"]) == 1
    assert payload["preflight"][0]["name"] == "lean_lsp_mcp"
    assert payload["preflight"][0]["status"] == "ok"


def test_service_run_full_pipeline_idempotent_and_preserves_major_gap(
    tmp_path: Path,
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "lemma_major_gap")

    request = {
        "job_id": "phase08_major_gap_job",
        "mode": "run_full_pipeline",
        "payload": {
            "source": str(paths["raw_fixture"]),
            "source_kind": "path",
            "statements_file": str(paths["statements_path"]),
            "mock_candidates_dir": str(paths["phase04_dir"]),
            "mock_equivalence_verdicts": str(MOCK_FIXTURE_DIR / "equivalence_major_gap.json"),
            "mock_gap_classifications": str(MOCK_FIXTURE_DIR / "gap_major_proof_gap.json"),
            "mock_root_candidates_dir": str(paths["phase06_dir"]),
        },
        "options": {
            "artifact_root": str(tmp_path / "artifacts"),
            "max_repair_rounds": 0,
            "max_attempts_per_lemma": 1,
            "max_semantic_repairs": 0,
            "timeout_seconds": 1,
        },
    }

    code_first, submit_first = service_app.submit_job(request)
    assert code_first == 202
    assert submit_first["status"] in {"queued", "running"}

    code_second, submit_second = service_app.submit_job(request)
    assert code_second == 409
    assert submit_second["job_id"] == request["job_id"]

    terminal = _wait_for_terminal(service_app, request["job_id"])
    assert terminal["status"] == "fatal"
    assert terminal["result"]["error_class"] == "major_proof_gap"
    assert terminal["result"]["final_result"]["error_class"] == "major_proof_gap"


def test_service_check_assembly_formalize_lemma_and_assemble_root_modes(
    tmp_path: Path,
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    check_request = {
        "job_id": "phase08_check_assembly_job",
        "mode": "check_assembly",
        "payload": {
            "source": str(paths["raw_fixture"]),
            "source_kind": "path",
            "statements_file": str(paths["statements_path"]),
        },
        "options": {
            "artifact_root": str(tmp_path / "artifacts"),
            "max_repair_rounds": 0,
            "timeout_seconds": 1,
        },
    }

    submit_code, _ = service_app.submit_job(check_request)
    assert submit_code == 202
    check_terminal = _wait_for_terminal(service_app, check_request["job_id"])
    assert check_terminal["status"] == "success"

    run_root = Path(check_terminal["result"]["phase03"]["run_root"])

    formalize_request = {
        "job_id": "phase08_formalize_job",
        "mode": "formalize_lemma",
        "payload": {
            "run_dir": str(run_root),
            "lemma_id": str(paths["lemma_id"]),
            "mock_candidates_dir": str(paths["phase04_dir"]),
        },
        "options": {
            "max_attempts_per_lemma": 1,
            "timeout_seconds": 1,
        },
    }

    formalize_code, _ = service_app.submit_job(formalize_request)
    assert formalize_code == 202
    formalize_terminal = _wait_for_terminal(service_app, formalize_request["job_id"])
    assert formalize_terminal["status"] == "success"
    assert formalize_terminal["result"]["lemma"]["status"] == "ok"

    trusted_manifest = json.loads((run_root / "trusted_context_manifest.json").read_text(encoding="utf-8"))
    assert trusted_manifest["entry_count"] == 1

    assemble_request = {
        "job_id": "phase08_assemble_job",
        "mode": "assemble_root",
        "payload": {
            "run_dir": str(run_root),
            "mock_root_candidates_dir": str(paths["phase06_dir"]),
        },
    }

    assemble_code, _ = service_app.submit_job(assemble_request)
    assert assemble_code == 202
    assemble_terminal = _wait_for_terminal(service_app, assemble_request["job_id"])
    assert assemble_terminal["status"] == "fatal"
    assert assemble_terminal["result"]["error_class"] == "assembly_invalid"
