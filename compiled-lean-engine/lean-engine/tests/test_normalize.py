from __future__ import annotations

import json
from pathlib import Path

from lean_engine.cli import main
from lean_engine.normalize import normalize_problem_artifact, normalize_problem_artifact_result
from lean_engine.result_types import FatalResult, OkResult


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _valid_payload(problem_id: str = "prob_test") -> dict:
    return {
        "problem_id": problem_id,
        "title": "Sample theorem",
        "verification_level": "nl_only",
        "root_theorem": {
            "theorem_id": "thm_root",
            "statement_nl": "Root statement",
            "semantic_sketch": {"normalized_claim": "root claim"},
        },
        "selected_decomposition": {
            "decomposition_id": "dec_1",
            "strategy_summary": "demo",
            "assembly_plan": {
                "assembly_plan_id": "asm_1",
                "steps": [
                    {
                        "step_id": "S1",
                        "uses_lemmas": ["lem_2"],
                        "uses_prior_steps": [],
                        "derives": "first",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                    {
                        "step_id": "S2",
                        "uses_lemmas": ["lem_1"],
                        "uses_prior_steps": ["S1"],
                        "derives": "second",
                        "is_trivial": True,
                        "trivial_justification": "given",
                    },
                    {
                        "step_id": "S3",
                        "uses_lemmas": ["lem_3"],
                        "uses_prior_steps": ["S2"],
                        "derives": "third",
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
                "proof_status": "nl_accepted",
                "routing_status": "done",
            },
            {
                "lemma_id": "lem_2",
                "statement_nl": "lemma two",
                "semantic_sketch": {"normalized_claim": "l2"},
                "proof_nl": "proof two",
                "proof_status": "nl_accepted",
                "routing_status": "done",
            },
            {
                "lemma_id": "lem_3",
                "statement_nl": "lemma three",
                "semantic_sketch": {"normalized_claim": "l3"},
                "proof_nl": "proof three",
                "proof_status": "nl_accepted",
                "routing_status": "done",
            },
        ],
        "all_visible_lemmas_nl_accepted": True,
    }


def test_normalize_accepts_path_text_and_object_inputs(tmp_path: Path) -> None:
    payload = _valid_payload()
    raw_text = json.dumps(payload)
    input_path = tmp_path / "input.json"
    input_path.write_text(raw_text, encoding="utf-8")

    from_path = normalize_problem_artifact(input_path)
    from_text = normalize_problem_artifact(raw_text)
    from_object = normalize_problem_artifact(payload)

    assert from_path.problem_id == "prob_test"
    assert from_text.problem_id == "prob_test"
    assert from_object.problem_id == "prob_test"


def test_messy_json_extraction_prefers_complete_candidate() -> None:
    fixture = FIXTURE_DIR / "b4_style_messy_artifact.txt"
    bundle = normalize_problem_artifact(fixture)

    assert bundle.problem_id == "prob_b4_style_good"
    assert bundle.lemma_count == 2
    assert bundle.assembly_step_count == 2
    assert bundle.assembly_lemma_ids == ["lem_L1", "lem_L2"]
    assert bundle.topologically_sorted_lemma_ids == ["lem_L1", "lem_L2"]
    assert bundle.provenance.extraction_method == "candidate_scan"
    assert bundle.provenance.candidate_count >= 2
    assert bundle.provenance.source_path == str(fixture.resolve())
    assert bundle.provenance.selected_source_span is not None
    assert bundle.provenance.original_payload["problem_id"] == "prob_b4_style_good"


def test_duplicate_lemma_id_fails_validation() -> None:
    payload = _valid_payload()
    payload["lemmas"][1]["lemma_id"] = payload["lemmas"][0]["lemma_id"]

    result = normalize_problem_artifact_result(payload)

    assert isinstance(result, FatalResult)
    assert result.error.error_class == "malformed_input_artifact"
    assert any("duplicate lemma_id" in item for item in result.error.diagnostics)


def test_duplicate_lemma_id_with_whitespace_fails_validation() -> None:
    payload = _valid_payload()
    payload["lemmas"][1]["lemma_id"] = f"  {payload['lemmas'][0]['lemma_id']}  "

    result = normalize_problem_artifact_result(payload)

    assert isinstance(result, FatalResult)
    assert any("duplicate lemma_id" in item for item in result.error.diagnostics)


def test_missing_lemma_proof_fails_validation() -> None:
    payload = _valid_payload()
    del payload["lemmas"][0]["proof_nl"]

    result = normalize_problem_artifact_result(payload)

    assert isinstance(result, FatalResult)
    assert any("proof_nl is required" in item for item in result.error.diagnostics)


def test_missing_assembly_plan_fails_validation() -> None:
    payload = _valid_payload()
    payload["selected_decomposition"] = {}

    result = normalize_problem_artifact_result(payload)

    assert isinstance(result, FatalResult)
    assert any("missing selected_decomposition.assembly_plan" in item for item in result.error.diagnostics)


def test_missing_path_returns_fatal_result(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    result = normalize_problem_artifact_result(missing)

    assert isinstance(result, FatalResult)
    assert result.error.error_class == "malformed_input_artifact"
    assert any("Failed to read artifact from path input" in item or "No such file" in item for item in result.error.diagnostics)


def test_derived_fields_are_computed() -> None:
    payload = _valid_payload()
    result = normalize_problem_artifact_result(payload)

    assert isinstance(result, OkResult)
    bundle = result.data
    assert bundle.lemma_count == 3
    assert bundle.assembly_step_count == 3
    assert bundle.all_lemma_ids == ["lem_1", "lem_2", "lem_3"]
    assert sorted(bundle.lemma_map.keys()) == ["lem_1", "lem_2", "lem_3"]
    assert bundle.assembly_lemma_ids == ["lem_2", "lem_1", "lem_3"]
    assert bundle.topologically_sorted_lemma_ids == ["lem_2", "lem_1", "lem_3"]


def test_cli_normalize_writes_artifact(tmp_path: Path, capsys) -> None:
    payload = _valid_payload(problem_id="prob_cli")
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(
        [
            "normalize",
            str(input_path),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    summary = json.loads(captured.out)
    output_path = Path(summary["output_path"])
    assert output_path.exists()

    normalized = json.loads(output_path.read_text(encoding="utf-8"))
    assert normalized["problem_id"] == "prob_cli"
    assert normalized["lemma_count"] == 3


def test_cli_json_input_kind_reports_parse_error(capsys) -> None:
    exit_code = main(["normalize", "{not valid json}", "--input-kind", "json"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--input-kind json expects valid JSON" in captured.err
