from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lean_engine.artifact_io import create_run_paths
from lean_engine.claude_runner import ClaudeRunResult
from lean_engine.config import load_runtime_config
from lean_engine.lemma_phase import TrustedContextEntry, write_trusted_manifest
from lean_engine.normalize import normalize_problem_artifact
from lean_engine.semantic_phase import run_phase05
from lean_engine.workspace import create_workspace_from_template


def _payload(problem_id: str = "prob_phase05", *, two_lemmas: bool = False) -> dict:
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
        "title": "phase05 synthetic",
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
            },
        },
        "lemmas": lemmas,
        "all_visible_lemmas_nl_accepted": True,
    }


def _prepare_runtime(tmp_path: Path, *, problem_id: str) -> tuple:
    runtime_config = load_runtime_config(artifact_root=tmp_path / "artifacts")
    run_paths = create_run_paths(problem_id, artifacts_root=runtime_config.artifacts.root)
    create_workspace_from_template(run_paths.workspace_dir, runtime_config=runtime_config)
    return runtime_config, run_paths


def _write_pinned_signatures(run_paths: Path, bundle, lemma_ids: list[str]) -> Path:
    pinned_path = run_paths / "pinned_signatures.json"
    payload = {
        "problem_id": bundle.problem_id,
        "run_name": run_paths.name,
        "lemmas": [
            {
                "lemma_id": lemma_id,
                "decl_name": f"{lemma_id}",
                "signature": f"theorem {lemma_id} : True",
                "statement_nl": bundle.lemma_map[lemma_id].statement_nl,
            }
            for lemma_id in lemma_ids
        ],
    }
    pinned_path.write_text(json.dumps(payload), encoding="utf-8")
    return pinned_path


def _write_phase04_summary(run_paths: Path, bundle, lemma_results: list[dict]) -> Path:
    summary_path = run_paths / "summaries" / "phase04_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "problem_id": bundle.problem_id,
                "lemma_results": lemma_results,
            }
        ),
        encoding="utf-8",
    )
    return summary_path


def _trusted_entry(lemma_id: str, *, signature: str, declaration_signature: str | None = None) -> TrustedContextEntry:
    decl_sig = declaration_signature or signature
    return TrustedContextEntry(
        lemma_id=lemma_id,
        decl_name=f"{lemma_id}",
        status="compiled",
        source_file="Orthos/Lemmas.lean",
        signature=signature,
        declaration=f"{decl_sig} := by\n  trivial\n",
    )


def _fake_claude_result(
    *,
    run_paths,
    phase_name: str,
    result_payload: dict[str, Any],
) -> ClaudeRunResult:
    timestamp = "fake"
    prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
    raw_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
    summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("", encoding="utf-8")
    raw_path.write_text(json.dumps({"type": "result", "result": json.dumps(result_payload)}) + "\n", encoding="utf-8")
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
        result_event={"result": json.dumps(result_payload)},
        event_counts={"result": 1},
    )


class _FakeClaudeRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def run_prompt(
        self,
        *,
        run_paths,
        prompt: str,
        phase_name: str,
        model: str | None = None,
        timeout_seconds: int | None = None,
        permission_mode: str | None = None,
        **_kwargs,
    ) -> ClaudeRunResult:
        self.calls.append(
            {
                "phase_name": phase_name,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "permission_mode": permission_mode,
            }
        )
        if self.fail:
            raise NameError("synthetic phase05 test failure")
        if phase_name.startswith("phase05_equivalence_"):
            payload = {
                "match": "yes",
                "explanation": "Semantic statements align.",
                "drift": "",
                "classification_hint": "unknown",
            }
        else:
            payload = {
                "classification": "major_proof_gap",
                "is_fatal": True,
                "rationale": "Missing central argument.",
                "math_gap_description": "The NL proof skips a required argument.",
            }
        return _fake_claude_result(run_paths=run_paths, phase_name=phase_name, result_payload=payload)


def test_phase05_rejects_pinned_signature_mismatch(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_guard")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_guard"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "ok",
                "error_class": None,
                "attempts": [{"diagnostics": []}],
            }
        ],
    )

    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(
        trusted_manifest,
        run_paths.problem_id,
        [
            _trusted_entry(
                "lem_1",
                signature="theorem lem_1 : True",
                declaration_signature="theorem lem_1 : False",
            )
        ],
    )

    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=1,
        timeout_seconds=10,
        model=None,
    )

    assert result.status == "fatal"
    assert result.error_class == "false_lemma_suspected"
    manifest = json.loads((run_paths.run_root / "trusted_context_semantic_manifest.json").read_text(encoding="utf-8"))
    assert manifest["entry_count"] == 0
    assert manifest["rejected_lemma_ids"] == ["lem_1"]


def test_phase05_semantic_mismatch_triggers_repair_round(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_repair")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_repair"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "ok",
                "error_class": None,
                "attempts": [{"diagnostics": []}],
            }
        ],
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(
        trusted_manifest,
        run_paths.problem_id,
        [_trusted_entry("lem_1", signature="theorem lem_1 : True")],
    )

    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=1,
        timeout_seconds=10,
        model=None,
        mock_equivalence_verdicts={
            "lem_1": [
                {"match": "no", "explanation": "first mismatch", "drift": "unknown drift"},
                {"match": "yes", "explanation": "aligned after semantic repair", "drift": ""},
            ]
        },
    )

    assert result.status == "ok"
    lemma_result = result.lemma_results[0]
    assert lemma_result.rounds_used == 2
    assert len(lemma_result.semantic_rounds) == 2
    assert (lemma_result.lemma_semantic_dir / "equivalence_round_01.json").exists()
    assert (lemma_result.lemma_semantic_dir / "equivalence_round_02.json").exists()


def test_phase05_repeated_semantic_blocker_yields_major_gap(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_major_gap")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_major_gap"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "ok",
                "error_class": None,
                "attempts": [{"diagnostics": []}],
            }
        ],
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(
        trusted_manifest,
        run_paths.problem_id,
        [_trusted_entry("lem_1", signature="theorem lem_1 : True")],
    )

    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=1,
        timeout_seconds=10,
        model=None,
        mock_equivalence_verdicts={
            "lem_1": [
                {
                    "match": "no",
                    "explanation": "missing mathematical step persists",
                    "drift": "proof jump is unsupported",
                    "classification_hint": "major_proof_gap",
                },
                {
                    "match": "no",
                    "explanation": "still unsupported",
                    "drift": "nontrivial jump remains",
                    "classification_hint": "major_proof_gap",
                },
            ]
        },
    )

    assert result.status == "fatal"
    assert result.error_class == "major_proof_gap"
    fatal_payload = json.loads(result.final_result_path.read_text(encoding="utf-8"))
    assert fatal_payload["status"] == "fatal"
    assert fatal_payload["error_class"] == "major_proof_gap"


def test_phase05_writes_structured_fatal_bundle_for_unsolved_major_gap(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_unsolved")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_unsolved"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "failed",
                "error_class": "tactic_failure",
                "attempts": [{"diagnostics": ["unsolved goals"]}],
            }
        ],
    )

    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(trusted_manifest, run_paths.problem_id, [])

    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=1,
        timeout_seconds=10,
        model=None,
        mock_gap_classifications={
            "lem_1": {
                "classification": "major_proof_gap",
                "is_fatal": True,
                "rationale": "NL proof omits key combinatorial argument.",
                "math_gap_description": "Missing bijection step.",
            }
        },
    )

    assert result.status == "fatal"
    payload = json.loads(result.final_result_path.read_text(encoding="utf-8"))
    for key in [
        "status",
        "error_class",
        "error_scope",
        "problem_id",
        "target_id",
        "decl_name",
        "message",
        "math_gap_description",
        "evidence",
        "artifacts",
    ]:
        assert key in payload
    assert (run_paths.run_root / "final" / "fatal_summary.md").exists()


def test_phase05_semantic_manifest_excludes_rejected_lemmas(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_manifest")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_manifest", two_lemmas=True))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1", "lem_2"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {"lemma_id": "lem_1", "status": "ok", "error_class": None, "attempts": [{"diagnostics": []}]},
            {"lemma_id": "lem_2", "status": "ok", "error_class": None, "attempts": [{"diagnostics": []}]},
        ],
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(
        trusted_manifest,
        run_paths.problem_id,
        [
            _trusted_entry("lem_1", signature="theorem lem_1 : True"),
            _trusted_entry("lem_2", signature="theorem lem_2 : True"),
        ],
    )

    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=0,
        timeout_seconds=10,
        model=None,
        mock_equivalence_verdicts={
            "lem_1": [{"match": "yes", "explanation": "ok", "drift": ""}],
            "lem_2": [{"match": "no", "explanation": "statement drifted", "drift": "weaker claim"}],
        },
    )

    assert result.status == "fatal"
    manifest = json.loads((run_paths.run_root / "trusted_context_semantic_manifest.json").read_text(encoding="utf-8"))
    assert manifest["entry_count"] == 1
    assert manifest["entries"][0]["lemma_id"] == "lem_1"
    assert manifest["rejected_lemma_ids"] == ["lem_2"]


def test_phase05_real_equivalence_branch_uses_explicit_timeout(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_real_equivalence")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_real_equivalence"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "ok",
                "error_class": None,
                "attempts": [{"diagnostics": []}],
            }
        ],
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(
        trusted_manifest,
        run_paths.problem_id,
        [_trusted_entry("lem_1", signature="theorem lem_1 : True")],
    )

    fake_runner = _FakeClaudeRunner()
    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=0,
        timeout_seconds=37,
        model="claude-opus-4-6",
        claude_runner=fake_runner,
    )

    assert result.status == "ok"
    assert len(fake_runner.calls) == 1
    assert fake_runner.calls[0]["phase_name"].startswith("phase05_equivalence_")
    assert fake_runner.calls[0]["timeout_seconds"] == 37
    assert fake_runner.calls[0]["permission_mode"] == "bypassPermissions"
    diagnostics_path = run_paths.run_root / "semantic" / "lemma_lem_1" / "equivalence_round_01_call.json"
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["phase_name"].startswith("phase05_equivalence_")
    assert diagnostics["lemma_id"] == "lem_1"
    assert diagnostics["round_index"] == 1
    assert diagnostics["timeout_seconds"] == 37
    assert diagnostics["status"] == "completed"


def test_phase05_real_gap_classification_branch_uses_explicit_timeout(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_real_gap")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_real_gap"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "failed",
                "error_class": None,
                "attempts": [{"diagnostics": ["unsolved goals"]}],
            }
        ],
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(trusted_manifest, run_paths.problem_id, [])

    fake_runner = _FakeClaudeRunner()
    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=1,
        timeout_seconds=41,
        model="claude-opus-4-6",
        claude_runner=fake_runner,
    )

    assert result.status == "fatal"
    assert result.error_class == "major_proof_gap"
    assert len(fake_runner.calls) == 1
    assert fake_runner.calls[0]["phase_name"].startswith("phase05_gap_")
    assert fake_runner.calls[0]["timeout_seconds"] == 41
    diagnostics_path = run_paths.run_root / "semantic" / "lemma_lem_1" / "proof_gap_call.json"
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["phase_name"].startswith("phase05_gap_")
    assert diagnostics["lemma_id"] == "lem_1"
    assert diagnostics["round_index"] is None
    assert diagnostics["timeout_seconds"] == 41
    assert diagnostics["status"] == "completed"
    verdict = json.loads((run_paths.run_root / "semantic" / "lemma_lem_1" / "proof_gap_verdict.json").read_text(encoding="utf-8"))
    assert verdict["classification"] == "major_proof_gap"


def test_phase05_internal_exception_returns_structured_fatal(tmp_path: Path) -> None:
    runtime_config, run_paths = _prepare_runtime(tmp_path, problem_id="prob_phase05_internal_failure")
    bundle = normalize_problem_artifact(_payload(problem_id="prob_phase05_internal_failure"))
    pinned_path = _write_pinned_signatures(run_paths.run_root, bundle, ["lem_1"])
    _write_phase04_summary(
        run_paths.run_root,
        bundle,
        [
            {
                "lemma_id": "lem_1",
                "status": "ok",
                "error_class": None,
                "attempts": [{"diagnostics": []}],
            }
        ],
    )
    trusted_manifest = run_paths.run_root / "trusted_context_manifest.json"
    write_trusted_manifest(
        trusted_manifest,
        run_paths.problem_id,
        [_trusted_entry("lem_1", signature="theorem lem_1 : True")],
    )

    result = run_phase05(
        run_paths=run_paths,
        runtime_config=runtime_config,
        bundle=bundle,
        pinned_signatures_path=pinned_path,
        trusted_manifest_path=trusted_manifest,
        phase04_summary_path=run_paths.summaries_dir / "phase04_summary.json",
        max_semantic_repairs=0,
        timeout_seconds=9,
        model="claude-opus-4-6",
        claude_runner=_FakeClaudeRunner(fail=True),
    )

    assert result.status == "fatal"
    assert result.error_class == "unknown_fatal"
    payload = json.loads(result.final_result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "fatal"
    assert payload["error_class"] == "unknown_fatal"
    assert any("NameError" in item for item in payload["evidence"]["latest_diagnostics"])
