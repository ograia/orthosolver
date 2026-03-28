from __future__ import annotations

import json
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lean_engine.config import load_runtime_config, project_root
from lean_engine.integrations import IntegrationPreflight
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.phase04 import Phase04RunResult
from lean_engine.service.app import LeanEngineServiceApp, _integration_health
from lean_engine.service.jobs import JobExecutionResult, _resolve_runtime_config
from lean_engine.service.object_store import JsonObjectStore
from lean_engine.service.store import JobStore
from lean_engine.statement_phase import build_phase03_decl_naming

FIXTURE_DIR = Path(__file__).parent / "fixtures"
RAW_FIXTURE_DIR = FIXTURE_DIR / "raw"
MOCK_FIXTURE_DIR = FIXTURE_DIR / "mocks"


@pytest.fixture
def service_app(tmp_path: Path):
    config_path = tmp_path / "service_config.json"
    config_path.write_text(
        json.dumps(
            {
                "workspace_cache": {
                    "enabled": False,
                    "cache_dir": str(tmp_path / "unused_cache"),
                }
            }
        ),
        encoding="utf-8",
    )
    app = LeanEngineServiceApp.from_defaults(
        store_root=tmp_path / "service_state",
        max_workers=2,
        config_path=config_path,
    )
    try:
        yield app
    finally:
        app.shutdown()


class _FakePreconditionFailed(Exception):
    pass


class _FakeBlob:
    def __init__(self, bucket: "_FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def upload_from_string(self, data, content_type=None, if_generation_match=None) -> None:
        existing = self.bucket.objects.get(self.name)
        if if_generation_match == 0 and existing is not None:
            raise _FakePreconditionFailed("already exists")
        if if_generation_match not in (None, 0):
            current_generation = existing["generation"] if existing is not None else None
            if current_generation != if_generation_match:
                raise _FakePreconditionFailed("generation mismatch")
        payload = data if isinstance(data, bytes) else str(data).encode("utf-8")
        generation = 1 if existing is None else int(existing["generation"]) + 1
        self.bucket.objects[self.name] = {
            "data": payload,
            "content_type": content_type,
            "generation": generation,
            "updated": datetime.now(UTC),
        }

    def download_as_bytes(self) -> bytes:
        return bytes(self.bucket.objects[self.name]["data"])

    def delete(self, if_generation_match=None) -> None:
        existing = self.bucket.objects.get(self.name)
        if existing is None:
            return
        if if_generation_match is not None and existing["generation"] != if_generation_match:
            raise _FakePreconditionFailed("generation mismatch")
        del self.bucket.objects[self.name]

    def reload(self) -> None:
        return None

    @property
    def generation(self):
        existing = self.bucket.objects.get(self.name)
        return None if existing is None else existing["generation"]

    @property
    def size(self):
        existing = self.bucket.objects.get(self.name)
        return 0 if existing is None else len(existing["data"])

    @property
    def updated(self):
        existing = self.bucket.objects.get(self.name)
        return None if existing is None else existing["updated"]

    @property
    def content_type(self):
        existing = self.bucket.objects.get(self.name)
        return None if existing is None else existing["content_type"]


class _FakeBucket:
    def __init__(self, name: str, objects: dict[str, dict[str, object]]) -> None:
        self.name = name
        self.objects = objects

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self, name)

    def list_blobs(self, prefix: str = "") -> list[_FakeBlob]:
        return [self.blob(name) for name in sorted(self.objects) if name.startswith(prefix)]


class _FakeClient:
    def __init__(self, buckets: dict[str, dict[str, dict[str, object]]]) -> None:
        self._buckets = buckets

    def bucket(self, name: str) -> _FakeBucket:
        objects = self._buckets.setdefault(name, {})
        return _FakeBucket(name, objects)


def _install_fake_gcs(monkeypatch: pytest.MonkeyPatch) -> None:
    buckets: dict[str, dict[str, dict[str, object]]] = {}

    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    storage_mod = types.ModuleType("google.cloud.storage")
    api_core_mod = types.ModuleType("google.api_core")
    exceptions_mod = types.ModuleType("google.api_core.exceptions")

    storage_mod.Client = lambda: _FakeClient(buckets)
    exceptions_mod.PreconditionFailed = _FakePreconditionFailed
    cloud_mod.storage = storage_mod
    api_core_mod.exceptions = exceptions_mod
    google_mod.cloud = cloud_mod
    google_mod.api_core = api_core_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
    monkeypatch.setitem(sys.modules, "google.api_core", api_core_mod)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exceptions_mod)


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


def _wait_for_terminal(app: LeanEngineServiceApp, job_id: str, *, timeout_s: float = 25.0) -> dict:
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
    assert payload["code_origin"]["code_root"] == str(project_root())
    assert payload["canonical_code_root"] == str(project_root())
    assert "canonical_code_root" in payload


def test_service_runtime_config_uses_exact_model_option(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")

    resolved = _resolve_runtime_config(
        options={"model": "claude-sonnet-4-6"},
        default_config_path=None,
        default_runtime_config=runtime_config,
    )

    assert resolved.claude.model == "claude-sonnet-4-6"


def test_service_runtime_config_rejects_mismatched_deprecated_fallback(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")

    with pytest.raises(ValueError, match="claude\\.fallback_model is deprecated"):
        _resolve_runtime_config(
            options={
                "model": "claude-sonnet-4-6",
                "fallback_model": "claude-sonnet-4-6[1m]",
            },
            default_config_path=None,
            default_runtime_config=runtime_config,
        )


def test_json_object_store_gcs_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_gcs(monkeypatch)
    monkeypatch.setenv("LEAN_ENGINE_STORAGE_BACKEND", "gcs")
    monkeypatch.setenv("GCS_BUCKET", "lean-phase08-tests")
    monkeypatch.setenv("LEAN_ENGINE_GCS_STATE_PREFIX", "orthos/lean-state")

    store = JsonObjectStore(tmp_path / "service_state")

    assert store.mode == "gcs"
    assert store.create_json_if_absent("jobs/demo.json", {"job_id": "demo", "status": "queued"}) is True
    assert store.create_json_if_absent("jobs/demo.json", {"job_id": "demo", "status": "queued"}) is False
    assert store.read_json("jobs/demo.json") == {"job_id": "demo", "status": "queued"}
    assert store.list_keys(prefix="jobs") == ["jobs/demo.json"]
    assert store.modified_at("jobs/demo.json") is not None


def test_job_store_gcs_preserves_idempotency_and_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_gcs(monkeypatch)
    monkeypatch.setenv("LEAN_ENGINE_STORAGE_BACKEND", "gcs")
    monkeypatch.setenv("GCS_BUCKET", "lean-phase08-tests")
    monkeypatch.setenv("LEAN_ENGINE_GCS_STATE_PREFIX", "orthos/lean-state")

    store = JobStore(tmp_path / "service_state")

    record, created = store.create_or_get(job_id="job_demo", mode="check_assembly", request={"payload": {}})
    existing, created_again = store.create_or_get(job_id="job_demo", mode="check_assembly", request={"payload": {}})

    assert store.storage_mode == "gcs"
    assert created is True
    assert created_again is False
    assert existing.job_id == record.job_id
    assert store.active_job_count() == 1
    assert store.queue_depth() == 1

    store.mark_running("job_demo")
    assert store.active_job_count() == 1
    assert store.queue_depth() == 0

    store.mark_terminal(
        job_id="job_demo",
        status="success",
        result={"ok": True, "progress_snapshot": {"phase": "check_assembly", "round": 1, "attempt": 1, "last_error": None}},
        error_class=None,
        message=None,
    )
    terminal = store.get("job_demo")

    assert terminal is not None
    assert terminal.status == "success"
    assert store.active_job_count() == 0
    assert store.queue_depth() == 0


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


def test_service_runtime_config_disables_hidden_timeouts_and_refs(tmp_path: Path) -> None:
    default_runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts_default")

    runtime_config = _resolve_runtime_config(
        options={
            "artifact_root": str(tmp_path / "artifacts"),
            "timeout_seconds": 0,
            "no_lean4_refs": True,
        },
        default_config_path=None,
        default_runtime_config=default_runtime_config,
    )

    assert runtime_config.claude.timeout_seconds == 0
    assert runtime_config.claude.stall_timeout_seconds == 0
    assert runtime_config.claude.tool_wait_timeout_seconds == 0
    assert runtime_config.claude.init_timeout_seconds == 0
    assert runtime_config.integrations.lean4_skills_root == Path("/dev/null/no-lean4-refs")


def test_service_runtime_config_allows_explicit_init_timeout_override(tmp_path: Path) -> None:
    default_runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts_default")

    runtime_config = _resolve_runtime_config(
        options={
            "artifact_root": str(tmp_path / "artifacts"),
            "timeout_seconds": 0,
            "claude_init_timeout_seconds": 900,
        },
        default_config_path=None,
        default_runtime_config=default_runtime_config,
    )

    assert runtime_config.claude.timeout_seconds == 0
    assert runtime_config.claude.stall_timeout_seconds == 0
    assert runtime_config.claude.tool_wait_timeout_seconds == 0
    assert runtime_config.claude.init_timeout_seconds == 900


def test_service_runtime_config_accepts_shared_activity_timeout(tmp_path: Path) -> None:
    default_runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts_default")

    runtime_config = _resolve_runtime_config(
        options={
            "artifact_root": str(tmp_path / "artifacts"),
            "claude_activity_timeout_seconds": 1200,
        },
        default_config_path=None,
        default_runtime_config=default_runtime_config,
    )

    assert runtime_config.claude.stall_timeout_seconds == 1200
    assert runtime_config.claude.tool_wait_timeout_seconds == 1200


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
    assert formalize_terminal["status"] == "fatal"
    assert formalize_terminal["result"]["error_scope"] == "service"
    assert "only allowed through `formalize_lemma_from_nl`" in formalize_terminal["result"]["error_message"]

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


def test_service_v2_formalize_statement_alias(
    tmp_path: Path,
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    request = {
        "job_id": "phase08_v2_formalize_statement_job",
        "operation": "formalize_statement_from_nl",
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

    submit_code, _ = service_app.submit_job(request)
    assert submit_code == 202
    terminal = _wait_for_terminal(service_app, request["job_id"])
    assert terminal["status"] == "success"
    assert terminal["result"]["operation"] == "formalize_statement_from_nl"
    assert isinstance(terminal["result"].get("run_dir"), str)


def test_service_v2_prepare_track_and_formalize_from_handle(
    tmp_path: Path,
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    prepare_code, prepare_submit = service_app.submit_operation(
        "prepare_track",
        {
            "operation_id": "phase08_prepare_track_op",
            "payload": {
                "source": str(paths["raw_fixture"]),
                "source_kind": "path",
                "statements_file": str(paths["statements_path"]),
                "decomposition_id": "dec_prepare_track",
            },
            "options": {
                "artifact_root": str(tmp_path / "artifacts"),
                "max_repair_rounds": 0,
                "timeout_seconds": 1,
            },
        },
    )
    assert prepare_code == 202
    assert prepare_submit["operation"] == "prepare_track"
    assert prepare_submit["operation_id"] == "phase08_prepare_track_op"

    prepare_terminal = _wait_for_terminal(service_app, "phase08_prepare_track_op")
    assert prepare_terminal["status"] == "success"
    prepare_result = prepare_terminal["result"]
    assert prepare_result["operation"] == "prepare_track"
    assert isinstance(prepare_result.get("run_dir"), str)

    lemma_id = str(paths["lemma_id"])
    lemma_handle = prepare_result["lemma_handles"][lemma_id]
    formalize_code, formalize_submit = service_app.submit_operation(
        "formalize_lemma_from_nl",
        {
            "operation_id": "phase08_formalize_from_handle_op",
            "payload": {
                "run_dir": prepare_result["run_dir"],
                "lemma_handle": lemma_handle,
                "proof_nl": "by trivial",
                "mock_candidates_dir": str(paths["phase04_dir"]),
            },
            "options": {
                "max_attempts_per_lemma": 1,
                "timeout_seconds": 1,
            },
        },
    )
    assert formalize_code == 202
    assert formalize_submit["operation"] == "formalize_lemma_from_nl"

    formalize_terminal = _wait_for_terminal(service_app, "phase08_formalize_from_handle_op")
    assert formalize_terminal["status"] == "success"
    assert formalize_terminal["result"]["operation"] == "formalize_lemma_from_nl"
    assert formalize_terminal["result"]["lemma"]["status"] == "succeeded"
    assert formalize_terminal["result"]["lemma"]["worker_workspace"] is not None
    assert formalize_terminal["result"]["phase04"]["code_origin"]["code_root"] == str(project_root())


def test_service_v2_formalize_from_handle_returns_decompose_current_for_phase04_setup_failure(
    tmp_path: Path,
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    prepare_code, _ = service_app.submit_operation(
        "prepare_track",
        {
            "operation_id": "phase08_prepare_track_failure_op",
            "payload": {
                "source": str(paths["raw_fixture"]),
                "source_kind": "path",
                "statements_file": str(paths["statements_path"]),
                "decomposition_id": "dec_prepare_track_failure",
            },
            "options": {
                "artifact_root": str(tmp_path / "artifacts"),
                "max_repair_rounds": 0,
                "timeout_seconds": 1,
            },
        },
    )
    assert prepare_code == 202
    prepare_terminal = _wait_for_terminal(service_app, "phase08_prepare_track_failure_op")
    assert prepare_terminal["status"] == "success"

    run_dir = Path(prepare_terminal["result"]["run_dir"])
    lemma_id = str(paths["lemma_id"])
    lemma_handle = prepare_terminal["result"]["lemma_handles"][lemma_id]

    def _fake_run_phase04(**kwargs):
        run_paths = kwargs["run_paths"]
        target_lemma_id = kwargs["target_lemma_id"]
        return Phase04RunResult(
            status="fatal",
            problem_id="prob_phase08_failure",
            run_root=run_paths.run_root,
            pinned_signatures_path=kwargs["pinned_signatures_path"],
            trusted_manifest_path=run_paths.run_root / "trusted_context_manifest.json",
            phase_summary_path=run_paths.summaries_dir / f"phase04_{target_lemma_id}.json",
            lemma_order=(target_lemma_id,),
            lemma_results=(),
            code_origin={"code_root": str(project_root())},
            workspace_revision=run_paths.run_name,
            error_class="dependency_graph_invalid",
            message=f"phase04 dependency graph has a cycle involving `{target_lemma_id}`",
        )

    monkeypatch.setattr("lean_engine.service.jobs.run_phase04", _fake_run_phase04)

    formalize_code, _ = service_app.submit_operation(
        "formalize_lemma_from_nl",
        {
            "operation_id": "phase08_formalize_failure_op",
            "payload": {
                "run_dir": str(run_dir),
                "lemma_handle": lemma_handle,
                "proof_nl": "by trivial",
                "mock_candidates_dir": str(paths["phase04_dir"]),
            },
            "options": {
                "max_attempts_per_lemma": 1,
                "timeout_seconds": 1,
            },
        },
    )
    assert formalize_code == 202

    formalize_terminal = _wait_for_terminal(service_app, "phase08_formalize_failure_op")
    assert formalize_terminal["status"] == "fatal"
    assert formalize_terminal["result"]["error_class"] == "dependency_graph_invalid"
    assert formalize_terminal["result"]["recommended_next_step"] == "decompose_current"
    assert formalize_terminal["result"]["phase04"]["phase_summary_path"].endswith(f"phase04_{lemma_id}.json")


def test_service_v2_split_proof_operation(service_app: LeanEngineServiceApp) -> None:
    submit_code, _ = service_app.submit_operation(
        "split_proof_into_sublemmas",
        {
            "operation_id": "phase08_split_operation",
            "payload": {
                "statement_nl": "Show n = n for every natural number.",
                "proof_nl": "Introduce n. Reduce to reflexivity. Close by rfl.",
            },
        },
    )
    assert submit_code == 202
    terminal = _wait_for_terminal(service_app, "phase08_split_operation")
    assert terminal["status"] == "success"
    assert terminal["result"]["operation"] == "split_proof_into_sublemmas"
    assert terminal["result"]["split_count"] >= 1


def test_service_statement_plausibility_mode(
    tmp_path: Path,
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lean_engine.lean_checks._run_command", _fake_run_command())
    paths = _prepare_mock_files(tmp_path, "tiny_success")

    request = {
        "job_id": "phase08_plausibility_job",
        "mode": "check_statement_plausibility",
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

    submit_code, _ = service_app.submit_job(request)
    assert submit_code == 202
    terminal = _wait_for_terminal(service_app, request["job_id"])
    assert terminal["status"] == "success"
    assert terminal["result"]["mode"] == "check_statement_plausibility"
    assert terminal["result"]["verdict"] in {"plausible", "suspected_false"}


def test_service_cancel_job_keeps_cancelled_terminal(
    service_app: LeanEngineServiceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _slow_execute(_self, request: dict[str, object]) -> JobExecutionResult:
        time.sleep(0.2)
        mode = str(request.get("mode") or "unknown")
        return JobExecutionResult(status="success", result={"mode": mode, "error_class": None})

    monkeypatch.setattr("lean_engine.service.jobs.ServiceJobManager._execute_request", _slow_execute)

    request = {
        "job_id": "phase08_cancel_job",
        "mode": "check_assembly",
        "payload": {"source": "{}", "source_kind": "json"},
    }

    submit_code, _ = service_app.submit_job(request)
    assert submit_code == 202

    cancel_code, cancel_payload = service_app.cancel_job(request["job_id"])
    assert cancel_code == 200
    assert cancel_payload["status"] == "cancelled"

    terminal = _wait_for_terminal(service_app, request["job_id"], timeout_s=3.0)
    assert terminal["status"] == "cancelled"
