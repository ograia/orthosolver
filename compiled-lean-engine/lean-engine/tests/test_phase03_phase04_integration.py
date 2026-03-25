from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lean_engine.artifact_io import create_run_paths
from lean_engine.config import load_runtime_config
from lean_engine.lean_checks import LeanCommandResult
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.phase03 import run_phase03
from lean_engine.phase04 import run_phase04
from lean_engine.statement_phase import build_phase03_decl_naming, sanitize_lean_decl_suffix
from lean_engine.workspace import create_workspace_from_template


def _payload(problem_id: str = "prob_phase03_phase04") -> dict:
    return {
        "problem_id": problem_id,
        "title": "phase03->phase04 integration",
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


def _ok_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def _statements_text(root_decl: str, lemma_decl_names: dict[str, str]) -> str:
    return "\n".join(
        [
            "import Mathlib",
            "",
            f"theorem {root_decl} : True := by",
            "  trivial",
            f"theorem {lemma_decl_names['lem_1']} : True := by",
            "  trivial",
            f"lemma {lemma_decl_names['lem_2']} : True := by",
            "  trivial",
            "",
        ]
    )


def _axiom_statements_text(root_decl: str, lemma_decl_names: dict[str, str]) -> str:
    return "\n".join(
        [
            "import Mathlib",
            "",
            f"axiom {root_decl} : True",
            f"axiom {lemma_decl_names['lem_1']} : True",
            f"axiom {lemma_decl_names['lem_2']} : True",
            "",
        ]
    )


def _candidate(signature: str, *, lemma_id: str) -> str:
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


def test_phase03_pinned_signatures_feed_phase04_lemma_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock all Lean compilation calls — this test checks Phase 03→04 handoff logic, not real Lean compilation.
    import lean_engine.phase04 as _phase04
    import lean_engine.lemma_phase as _lemma_phase

    def _ok_lean_result(check_name: str = "mock") -> LeanCommandResult:
        return LeanCommandResult(
            check_name=check_name,
            command=("lake", "build"),
            cwd=tmp_path,
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.0,
        )

    monkeypatch.setattr(_phase04, "rebuild_module_olean", lambda *_a, **_kw: _ok_lean_result("rebuild_module_olean"))
    monkeypatch.setattr(_phase04, "_validate_olean_freshness", lambda *_a, **_kw: True)
    monkeypatch.setattr(_lemma_phase, "check_lean_file", lambda *_a, **_kw: _ok_lean_result("check_lean_file"))

    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_phase04", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_payload())
    decl_naming = build_phase03_decl_naming(bundle)

    phase03_result = run_phase03(
        run_paths=run_paths,
        runtime_config=runtime_config,
        normalized_bundle=bundle,
        max_repair_rounds=0,
        timeout_seconds=10,
        model=None,
        provided_statements_text=_statements_text(
            root_decl=decl_naming.root_decl_name,
            lemma_decl_names=decl_naming.lemma_decl_names,
        ),
        runner=_ok_runner,
    )

    assert phase03_result.status == "ok"
    assert phase03_result.pinned_signatures_path is not None

    pinned_payload = json.loads(phase03_result.pinned_signatures_path.read_text(encoding="utf-8"))
    mock_candidates: dict[str, list[str]] = {}
    for lemma in pinned_payload["lemmas"]:
        mock_candidates[lemma["lemma_id"]] = [_candidate(lemma["signature"], lemma_id=lemma["lemma_id"])]

    phase04_result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=phase03_result.pinned_signatures_path,
        max_attempts_per_lemma=1,
        timeout_seconds=10,
        model=None,
        runner=_ok_runner,
        mock_candidates=mock_candidates,
    )

    assert phase04_result.status == "ok"
    assert len(phase04_result.lemma_results) == 2


def test_phase03_phase04_boundary_with_real_lake_compile(tmp_path: Path) -> None:
    if shutil.which("lake") is None:
        pytest.skip("lake is not available on PATH")

    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths("prob_phase03_phase04_real", artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase03_phase04_real"))
    decl_naming = build_phase03_decl_naming(bundle)

    probe = subprocess.run(
        ("lake", "env", "lean", "Orthos/Statements.lean"),
        cwd=str(run_paths.workspace_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        combined = f"{probe.stdout}\n{probe.stderr}".lower()
        if (
            "unknown package" in combined
            or "reservoir lookup failed" in combined
            or "could not materialize package" in combined
        ):
            pytest.skip("local Lean toolchain/mathlib is unavailable for real integration test")

    phase03_result = run_phase03(
        run_paths=run_paths,
        runtime_config=runtime_config,
        normalized_bundle=bundle,
        max_repair_rounds=0,
        timeout_seconds=120,
        model=None,
        provided_statements_text=_axiom_statements_text(
            root_decl=decl_naming.root_decl_name,
            lemma_decl_names=decl_naming.lemma_decl_names,
        ),
    )
    if phase03_result.status != "ok":
        diagnostics = " ".join(phase03_result.diagnostics).lower()
        if "unknown module prefix 'mathlib'" in diagnostics:
            pytest.skip("local Lean toolchain/mathlib is unavailable for real integration test")
    assert phase03_result.status == "ok"
    assert phase03_result.pinned_signatures_path is not None

    pinned_payload = json.loads(phase03_result.pinned_signatures_path.read_text(encoding="utf-8"))
    mock_candidates: dict[str, list[str]] = {}
    for lemma in pinned_payload["lemmas"]:
        mock_candidates[lemma["lemma_id"]] = [_candidate(lemma["signature"], lemma_id=lemma["lemma_id"])]

    phase04_result = run_phase04(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=phase03_result.pinned_signatures_path,
        max_attempts_per_lemma=1,
        timeout_seconds=120,
        model=None,
        mock_candidates=mock_candidates,
    )
    assert phase04_result.status == "ok"

    final_build = subprocess.run(
        ("lake", "build"),
        cwd=str(run_paths.workspace_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    assert final_build.returncode == 0, final_build.stderr
