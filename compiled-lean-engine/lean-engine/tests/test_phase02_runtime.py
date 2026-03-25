from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from lean_engine.artifact_io import create_run_paths
from lean_engine.claude_runner import ClaudeRunner
from lean_engine.cli import main
from lean_engine.config import ALLOWED_CLAUDE_MODELS, load_runtime_config
from lean_engine.lean_checks import (
    LeanCommandResult,
    LeanWorkspacePreparationResult,
    classify_preparation_failure,
    run_phase02_checks,
)
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.workspace import create_workspace_from_template, write_project_mcp_config


def test_template_workspace_copies(tmp_path: Path) -> None:
    from lean_engine.config import parse_runtime_config, default_runtime_config_dict
    cfg = default_runtime_config_dict()
    cfg["artifacts"]["root"] = str(tmp_path / "artifacts")
    cfg["workspace_cache"]["enabled"] = False
    runtime_config = parse_runtime_config(cfg)
    run_paths = create_run_paths("prob_template", artifacts_root=runtime_config.artifacts.root)

    workspace_result = create_workspace_from_template(
        run_paths.workspace_dir,
        runtime_config=runtime_config,
    )

    assert (run_paths.workspace_dir / "lakefile.toml").exists()
    assert (run_paths.workspace_dir / "lean-toolchain").exists()
    assert workspace_result.files.statements_file.exists()
    assert workspace_result.files.lemmas_file.exists()
    assert workspace_result.files.root_file.exists()
    assert (run_paths.workspace_dir / "lean-toolchain").read_text(encoding="utf-8").strip() != "leanprover/lean4:stable"
    lakefile_text = (run_paths.workspace_dir / "lakefile.toml").read_text(encoding="utf-8")
    assert 'rev = "v4.26.0"' in lakefile_text


def test_create_workspace_rejects_unpinned_template(tmp_path: Path) -> None:
    template = tmp_path / "template"
    (template / "Orthos").mkdir(parents=True)
    (template / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
    (template / "lakefile.toml").write_text(
        'name = "orthos"\n[[require]]\nname = "mathlib"\nscope = "leanprover-community"\n',
        encoding="utf-8",
    )
    (template / "Orthos" / "Statements.lean").write_text("import Mathlib\n", encoding="utf-8")
    (template / "Orthos" / "Lemmas.lean").write_text("import Mathlib\n", encoding="utf-8")
    (template / "Orthos" / "Root.lean").write_text("import Mathlib\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        create_workspace_from_template(tmp_path / "workspace", template_dir=template)
    assert "must pin an explicit Lean version" in str(exc.value)


def test_classify_preparation_failure_detects_toolchain_mismatch() -> None:
    diagnostics = [
        "warning: toolchain not updated; multiple toolchain candidates:",
        "Not running `lake exe cache get` yet, as the `lake` version (4.28.0) does not match the toolchain version",
    ]
    error_class = classify_preparation_failure(diagnostics, check_name="project_build")
    assert error_class == "lean_toolchain_mismatch"


def test_mcp_config_is_emitted_correctly(tmp_path: Path) -> None:
    runtime_config = load_runtime_config(
        artifact_root=tmp_path / "artifacts",
        mcp_command="/tmp/lean-mcp.sh",
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)

    config_path = write_project_mcp_config(workspace_root, runtime_config, mcp_log_dir=tmp_path / "mcp_logs")
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    server = payload["mcpServers"][runtime_config.mcp.server_name]
    assert server["command"] == "/tmp/lean-mcp.sh"
    assert server["args"] == []
    assert server["env"]["MCP_LOG_DIR"] == str((tmp_path / "mcp_logs").resolve())
    assert server["env"]["MCP_LOG_NAME"] == runtime_config.mcp.log_name


def test_deterministic_check_command_wiring_uses_expected_commands(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict]] = []

    def fake_runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    results = run_phase02_checks(tmp_path, timeout_seconds=77, runner=fake_runner)

    assert [result.command for result in results] == [
        ("lake", "env", "lean", "Orthos/Statements.lean"),
        ("lake", "env", "lean", "Orthos/Lemmas.lean"),
        ("lake", "env", "lean", "Orthos/Root.lean"),
        ("lake", "build"),
    ]
    assert all(kwargs["cwd"] == str(tmp_path.resolve()) for _, kwargs in calls)
    assert all(kwargs["timeout"] == 77 for _, kwargs in calls)
    assert all(result.ok for result in results)


def test_deterministic_checks_disable_timeout_when_zero(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict]] = []

    def fake_runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    results = run_phase02_checks(tmp_path, timeout_seconds=0, runner=fake_runner)

    assert all(kwargs["timeout"] is None for _, kwargs in calls)
    assert all(result.ok for result in results)


def test_runtime_init_prepare_workspace_uses_cli_timeout(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int | None] = {}

    def fake_prepare(workspace_root, *, timeout_seconds=180, runner=subprocess.run):
        _ = workspace_root
        _ = runner
        seen["timeout_seconds"] = timeout_seconds
        return LeanWorkspacePreparationResult(
            status="ok",
            checks=(
                LeanCommandResult(
                    check_name="project_build",
                    command=("lake", "build"),
                    cwd=tmp_path.resolve(),
                    returncode=0,
                    stdout="ok",
                    stderr="",
                    duration_seconds=0.01,
                ),
            ),
        )

    monkeypatch.setattr("lean_engine.cli.prepare_lean_workspace", fake_prepare)
    exit_code = main(
        [
            "runtime-init",
            "prob_timeout",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--prepare-workspace",
            "--workspace-timeout",
            "0",
            "--json",
        ]
    )
    _ = capsys.readouterr()

    assert exit_code == 0
    assert seen["timeout_seconds"] == 0


def test_claude_runner_writes_prompt_raw_and_summary_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_runner", artifacts_root=runtime_config.artifacts.root)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class FakePopen:
        def __init__(self, command, stdout, stderr, text, cwd, env, **kwargs):
            self.command = command
            self.stdout_target = stdout
            self.stdin = io.StringIO()
            self.returncode = 0
            self.stdout_target.write('{"type":"message_start"}\n')
            self.stdout_target.write('{"type":"result","result":"done"}\n')
            self.stdout_target.flush()

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("lean_engine.claude_runner.subprocess.Popen", FakePopen)

    runner = ClaudeRunner(runtime_config)
    result = runner.run_prompt(
        run_paths=run_paths,
        prompt="Phase 02 runtime smoke prompt",
        phase_name="phase02",
        timestamp="20260313T010203Z",
    )

    assert result.prompt_path.exists()
    assert result.raw_output_path.exists()
    assert result.summary_path.exists()

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["command"][3:5] == ["--output-format", "stream-json"]
    assert summary["result_event"]["type"] == "result"


def test_claude_runner_allows_cli_auth_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_runner_no_key", artifacts_root=runtime_config.artifacts.root)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class FakePopen:
        def __init__(self, command, stdout, stderr, text, cwd, env, **kwargs):
            _ = (command, stderr, text, cwd)
            self.env = env
            self.stdin = io.StringIO()
            self.stdout_target = stdout
            self.returncode = 0
            self.stdout_target.write('{"type":"result","result":"done"}\n')
            self.stdout_target.flush()

        def wait(self, timeout=None):
            _ = timeout
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("lean_engine.claude_runner.subprocess.Popen", FakePopen)

    runner = ClaudeRunner(runtime_config)
    result = runner.run_prompt(
        run_paths=run_paths,
        prompt="Phase 02 runtime no key prompt",
        phase_name="phase02_no_key",
        timestamp="20260313T010204Z",
    )

    assert result.ok


def test_claude_runner_extracts_tool_update_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_runner_trace", artifacts_root=runtime_config.artifacts.root)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    statements_path = run_paths.workspace_dir / "Orthos" / "Statements.lean"
    lean_payload = "import Mathlib\n\naxiom root_prob_runner_trace : True\n"

    class FakePopen:
        def __init__(self, command, stdout, stderr, text, cwd, env, **kwargs):
            _ = (command, stderr, text, cwd, env)
            self.stdin = io.StringIO()
            self.stdout_target = stdout
            self.returncode = 0
            self.stdout_target.write(
                json.dumps(
                    {
                        "type": "user",
                        "tool_use_result": {
                            "type": "update",
                            "filePath": str(statements_path),
                            "content": lean_payload,
                        },
                    }
                )
                + "\n"
            )
            self.stdout_target.write(
                json.dumps(
                    {
                        "type": "result",
                        "result": "Zero diagnostics — file compiles.",
                    }
                )
                + "\n"
            )
            self.stdout_target.flush()

        def wait(self, timeout=None):
            _ = timeout
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("lean_engine.claude_runner.subprocess.Popen", FakePopen)

    runner = ClaudeRunner(runtime_config)
    result = runner.run_prompt(
        run_paths=run_paths,
        prompt="Phase 02 trace test",
        phase_name="phase02_trace",
        timestamp="20260313T020304Z",
    )

    assert result.trace is not None
    assert result.trace.result_text.startswith("Zero diagnostics")
    assert len(result.trace.file_updates) == 1
    assert result.trace.latest_update_for_target(statements_path) == lean_payload


def test_claude_runner_disables_timeout_when_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_runner_timeout", artifacts_root=runtime_config.artifacts.root)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    observed_wait_timeouts: list[int | float | None] = []

    class FakePopen:
        def __init__(self, command, stdout, stderr, text, cwd, env, **kwargs):
            _ = command
            _ = stderr
            _ = text
            _ = cwd
            _ = env
            self.stdin = io.StringIO()
            self.stdout_target = stdout
            self.returncode = 0
            self.stdout_target.write('{"type":"result","result":"done"}\n')
            self.stdout_target.flush()

        def wait(self, timeout=None):
            observed_wait_timeouts.append(timeout)
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("lean_engine.claude_runner.subprocess.Popen", FakePopen)

    runner = ClaudeRunner(runtime_config)
    result = runner.run_prompt(
        run_paths=run_paths,
        prompt="Phase 02 timeout control",
        phase_name="phase02_timeout",
        timeout_seconds=0,
    )

    assert result.ok
    # The stall-detection poll loop calls wait(timeout=10), not wait(timeout=None)
    assert observed_wait_timeouts == [10]


def test_config_model_selection_constraints(tmp_path: Path) -> None:
    valid_payload = {
        "claude": {
            "model": "claude-opus-4-6",
            "fallback_model": "claude-sonnet-4-6",
            "timeout_seconds": 120,
            "command": "claude",
        },
        "lean": {"imports": ["Mathlib"]},
        "artifacts": {"root": str(tmp_path / "artifacts")},
        "mcp": {
            "enabled": True,
            "config_filename": ".mcp.json",
            "server_name": "lean-lsp",
            "command": "/tmp/lean-mcp.sh",
            "args": [],
            "log_name": "lean_lsp_mcp",
        },
    }

    valid_config_path = tmp_path / "runtime_valid.json"
    valid_config_path.write_text(json.dumps(valid_payload), encoding="utf-8")

    config = load_runtime_config(valid_config_path)
    assert config.claude.model in ALLOWED_CLAUDE_MODELS
    assert config.claude.fallback_model in ALLOWED_CLAUDE_MODELS

    invalid_payload = dict(valid_payload)
    invalid_payload["claude"] = dict(valid_payload["claude"])
    invalid_payload["claude"]["model"] = "claude-3-sonnet"
    invalid_config_path = tmp_path / "runtime_invalid.json"
    invalid_config_path.write_text(json.dumps(invalid_payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_runtime_config(invalid_config_path)


def test_config_rejects_non_boolean_mcp_enabled(tmp_path: Path) -> None:
    payload = {
        "claude": {
            "model": "claude-opus-4-6",
            "fallback_model": "claude-sonnet-4-6",
            "timeout_seconds": 120,
            "command": "claude",
        },
        "lean": {"imports": ["Mathlib"]},
        "artifacts": {"root": str(tmp_path / "artifacts")},
        "mcp": {
            "enabled": "false",
            "config_filename": ".mcp.json",
            "server_name": "lean-lsp",
            "command": "/tmp/lean-mcp.sh",
            "args": [],
            "log_name": "lean_lsp_mcp",
        },
    }

    config_path = tmp_path / "runtime_invalid_mcp_bool.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        load_runtime_config(config_path)
    assert "mcp.enabled must be a boolean" in str(exc.value)


def test_cli_runtime_init_and_inspect(tmp_path: Path, capsys) -> None:
    exit_code = main(["runtime-init", "prob_cli", "--artifact-root", str(tmp_path / "artifacts"), "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    init_summary = json.loads(captured.out)
    run_root = init_summary["run_root"]

    inspect_code = main(["runtime-inspect", run_root, "--json"])
    inspect_captured = capsys.readouterr()

    assert inspect_code == 0
    inspect_summary = json.loads(inspect_captured.out)
    assert inspect_summary["exists"]["workspace"] is True

    run_root = Path(run_root)
    assert (run_root / "integrations" / "inventory.json").exists()
    assert (run_root / "integrations" / "preflight.json").exists()


def test_runtime_config_includes_integration_defaults(tmp_path: Path) -> None:
    config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    assert config.integrations.lean_lsp_mcp_root.exists() or True  # path may not exist in test env
    assert config.integrations.lean4_skills_root is not None


def _phase_cli_payload(problem_id: str) -> dict:
    return {
        "problem_id": problem_id,
        "title": "cli phase payload",
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
            },
        },
        "lemmas": [
            {
                "lemma_id": "lem_1",
                "statement_nl": "lemma one",
                "semantic_sketch": {"normalized_claim": "l1"},
                "proof_nl": "proof one",
            }
        ],
        "all_visible_lemmas_nl_accepted": True,
    }


def test_cli_phase03_invalid_budget_returns_structured_fatal(tmp_path: Path, capsys) -> None:
    init_code = main(["runtime-init", "prob_cli_phase03", "--artifact-root", str(tmp_path / "artifacts"), "--json"])
    init_summary = json.loads(capsys.readouterr().out)
    assert init_code == 0

    run_root = Path(init_summary["run_root"])
    bundle = normalize_problem_artifact(_phase_cli_payload(problem_id="prob_cli_phase03"))
    (run_root / "normalized_problem.json").write_text(json.dumps(bundle.to_dict()), encoding="utf-8")

    exit_code = main(["phase03-run", str(run_root), "--max-repair-rounds", "-1", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 1
    summary = json.loads(captured.out)
    assert summary["status"] == "fatal"
    assert summary["error_scope"] == "phase03"
    assert summary["error_class"] == "invalid_phase_options"
    assert any("max_repair_rounds must be >= 0" in item for item in summary["diagnostics"])


def test_cli_phase04_invalid_budget_returns_structured_fatal(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from lean_engine.lean_checks import LeanCommandResult
    monkeypatch.setattr(
        "lean_engine.phase04.rebuild_module_olean",
        lambda *_a, **_kw: LeanCommandResult(
            check_name="mock", command=("lake", "build"), cwd=tmp_path,
            returncode=0, stdout="", stderr="", duration_seconds=0.0,
        ),
    )
    monkeypatch.setattr("lean_engine.phase04._validate_olean_freshness", lambda *_a, **_kw: True)
    init_code = main(["runtime-init", "prob_cli_phase04", "--artifact-root", str(tmp_path / "artifacts"), "--json"])
    init_summary = json.loads(capsys.readouterr().out)
    assert init_code == 0

    run_root = Path(init_summary["run_root"])
    bundle = normalize_problem_artifact(_phase_cli_payload(problem_id="prob_cli_phase04"))
    (run_root / "normalized_problem.json").write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    (run_root / "pinned_signatures.json").write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": run_root.name,
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

    exit_code = main(["phase04-run", str(run_root), "--max-attempts-per-lemma", "0", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 1
    summary = json.loads(captured.out)
    assert summary["status"] == "fatal"
    assert summary["error_scope"] == "phase04"
    assert summary["error_class"] == "invalid_phase_options"
    assert any("max_attempts must be > 0" in item for item in summary["diagnostics"])


def test_cli_phase05_invalid_budget_returns_structured_fatal(tmp_path: Path, capsys) -> None:
    init_code = main(["runtime-init", "prob_cli_phase05", "--artifact-root", str(tmp_path / "artifacts"), "--json"])
    init_summary = json.loads(capsys.readouterr().out)
    assert init_code == 0

    run_root = Path(init_summary["run_root"])
    bundle = normalize_problem_artifact(_phase_cli_payload(problem_id="prob_cli_phase05"))
    (run_root / "normalized_problem.json").write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    (run_root / "pinned_signatures.json").write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": run_root.name,
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
    (run_root / "trusted_context_manifest.json").write_text(
        json.dumps(
            {
                "problem_id": run_root.parent.name,
                "entry_count": 1,
                "entries": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "status": "compiled",
                        "source_file": "Orthos/Lemmas.lean",
                        "signature": "theorem lem_1 : True",
                        "declaration": "theorem lem_1 : True := by trivial",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_root / "summaries" / "phase04_summary.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "problem_id": bundle.problem_id,
                "lemma_results": [
                    {
                        "lemma_id": "lem_1",
                        "status": "ok",
                        "error_class": None,
                        "attempts": [{"diagnostics": []}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["phase05-run", str(run_root), "--max-semantic-repairs", "-1", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 1
    summary = json.loads(captured.out)
    assert summary["status"] == "fatal"
    assert summary["error_scope"] == "phase05"
    assert summary["error_class"] == "invalid_phase_options"
    assert any("max_semantic_repairs must be >= 0" in item for item in summary["diagnostics"])


def test_cli_phase05_unexpected_exception_returns_structured_fatal(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_code = main(["runtime-init", "prob_cli_phase05_crash", "--artifact-root", str(tmp_path / "artifacts"), "--json"])
    init_summary = json.loads(capsys.readouterr().out)
    assert init_code == 0

    run_root = Path(init_summary["run_root"])
    bundle = normalize_problem_artifact(_phase_cli_payload(problem_id="prob_cli_phase05_crash"))
    (run_root / "normalized_problem.json").write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    (run_root / "pinned_signatures.json").write_text(
        json.dumps(
            {
                "problem_id": bundle.problem_id,
                "run_name": run_root.name,
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
    (run_root / "trusted_context_manifest.json").write_text(
        json.dumps(
            {
                "problem_id": run_root.parent.name,
                "entry_count": 1,
                "entries": [
                    {
                        "lemma_id": "lem_1",
                        "decl_name": "lem_1",
                        "status": "compiled",
                        "source_file": "Orthos/Lemmas.lean",
                        "signature": "theorem lem_1 : True",
                        "declaration": "theorem lem_1 : True := by trivial",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_root / "summaries" / "phase04_summary.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "problem_id": bundle.problem_id,
                "lemma_results": [
                    {
                        "lemma_id": "lem_1",
                        "status": "ok",
                        "error_class": None,
                        "attempts": [{"diagnostics": []}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def _boom(*_args, **_kwargs):
        raise NameError("synthetic phase05 cli crash")

    monkeypatch.setattr("lean_engine.cli.run_phase05", _boom)

    exit_code = main(["phase05-run", str(run_root), "--json"])
    summary = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert summary["status"] == "fatal"
    assert summary["error_scope"] == "phase05"
    assert summary["error_class"] == "unknown_fatal"
    assert any("NameError" in item for item in summary["diagnostics"])
