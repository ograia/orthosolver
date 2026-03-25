# Lean Engine Phase 07 Runbook

This runbook documents local operation, regression fixtures, and artifact inspection for the Phase 01-06 pipeline with Phase 07 evaluation support.

## 1. Required Environment

- Lean toolchain installed and `lake` available on `PATH`
- Mathlib template available (default template: `lean-engine/templates/lean_project/`)
- Claude Code CLI installed
- Anthropic credentials configured for Claude Code
- Model id configured as one of:
  - `claude-opus-4-6`
  - `claude-sonnet-4-6`

Recommended local tools:

- `rg`
- `jq`
- `uv`

## 2. Regression Fixtures

Raw fixtures:

- `tests/fixtures/raw/b4_clean.json` (`b4_clean`)
- `tests/fixtures/raw/b4_messy_export.txt` (`b4_messy_export`)
- `tests/fixtures/raw/statement_ambiguous.json` (`statement_ambiguous`)
- `tests/fixtures/raw/lemma_major_gap.json` (`lemma_major_gap`)
- `tests/fixtures/raw/assembly_mismatch.json` (`assembly_mismatch`)
- `tests/fixtures/raw/tiny_success.json` (`tiny_success`)

Normalized fixtures:

- `tests/fixtures/normalized/b4_clean.json`
- `tests/fixtures/normalized/b4_messy_export.json`
- `tests/fixtures/normalized/statement_ambiguous.json`
- `tests/fixtures/normalized/lemma_major_gap.json`
- `tests/fixtures/normalized/assembly_mismatch.json`
- `tests/fixtures/normalized/tiny_success.json`

## 3. Operator Commands

Normalize only:

```bash
PYTHONPATH=src python -m lean_engine.cli normalize tests/fixtures/raw/b4_messy_export.txt --json
```

Phase-by-phase execution:

```bash
PYTHONPATH=src python -m lean_engine.cli runtime-init prob_demo --json
PYTHONPATH=src python -m lean_engine.cli phase03-run .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli phase04-run .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli phase05-run .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli phase06-run .artifacts/lean_engine/prob_demo/run_001 --json
```

End-to-end run (Phase 01-06 + Phase 07 summary):

```bash
PYTHONPATH=src python -m lean_engine.cli run tests/fixtures/raw/tiny_success.json \
  --timeout 600 \
  --workspace-timeout 1800 \
  --json
```

Timeout notes:

- `--timeout` controls Phase 03-06 Lean/Claude calls.
- `--workspace-timeout` controls the initial Phase 02 workspace build gate.
- Any timeout value `<= 0` disables that timeout.

Run inspection:

```bash
PYTHONPATH=src python -m lean_engine.cli inspect .artifacts/lean_engine/prob_tiny_success/run_001 --json
```

## 4. Artifact Layout

Typical run directory:

```text
.artifacts/lean_engine/<problem_id>/run_001/
  normalized_problem.json
  workspace/
  diagnostics/
  prompts/
  claude_raw/
  summaries/
    phase03_summary.json
    phase04_summary.json
    phase05_summary.json
    phase06_summary.json
    phase07_summary.json
  final/
    result.json
    summary.md | fatal_summary.md
```

## 5. Failure Triage Order

1. `normalized_problem.json` (input contract and decomposition shape)
2. latest prompt file and Claude raw output under `prompts/` and `claude_raw/`
3. latest scratch/candidate Lean files (`workspace/Orthos/*`, `lemmas/`, `root_assembly/`)
4. deterministic diagnostics under `diagnostics/` and round diagnostics in phase folders
5. semantic verdict files under `semantic/`
6. `final/result.json` and `final/fatal_summary.md`
7. `summaries/phase07_summary.json` for consolidated status/metrics

## 6. Phase 07 Metrics

`summaries/phase07_summary.json` includes:

- `problem_id`
- `model`
- `lemma_count`
- `compiled_lemma_count`
- `semantic_rejection_count`
- `fatal_error_class`
- `elapsed_seconds`
- `phase_elapsed_seconds`
- `phase_statuses`

## 7. Regression Execution

Run all tests:

```bash
pytest -q
```

Focused Phase 07 regression:

```bash
pytest -q tests/test_phase07_pipeline.py tests/test_pipeline_smoke.py
```
