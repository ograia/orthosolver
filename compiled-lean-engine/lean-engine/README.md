# lean-engine

Lean runtime and HTTP service for Orthosolver.

The engine still supports the phase-based full pipeline, and it now also serves the unified Lean v2 operations used continuously by the NL orchestrator.

## Phase 01

Normalize NL proof artifacts into a strict internal contract:

- accepts input as file path, raw text, or Python object
- tolerates messy files with extra commentary / duplicate JSON payloads
- validates mandatory fields and duplicate lemma ids
- computes derived fields used by later phases
- writes deterministic normalized artifacts under `.artifacts/normalize/`

Example:

```bash
PYTHONPATH=src python -m lean_engine.cli normalize ../docs/b4_nl_proof.json --json
```

## Phase 02

Runtime substrate for Claude Code + Lean MCP:

- runtime config with required support for `claude-opus-4-6` and `claude-sonnet-4-6`
- deterministic Lean workspace template copied per run
- project-scoped `.mcp.json` generation for Lean LSP MCP
- deterministic Lean checks via `lake env lean` and `lake build`
- Claude subprocess wrapper using `--output-format stream-json`
- deterministic artifact layout under `.artifacts/lean_engine/<problem_id>/run_xxx/`

Examples:

```bash
PYTHONPATH=src python -m lean_engine.cli runtime-init prob_demo --json
PYTHONPATH=src python -m lean_engine.cli runtime-inspect .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli runtime-check .artifacts/lean_engine/prob_demo/run_001 --json
```

Integration override flags are available on `runtime-init`, `run`, `phase03-run`, `phase04-run`, `phase06-run`, and `service`:

```bash
--repo-lean-lsp-mcp-root /path/to/lean-lsp-mcp-main
--lean4-skills-root /path/to/lean4-skills-main/plugins/lean4
```

Each run writes integration artifacts:

- `integrations/inventory.json`
- `integrations/preflight.json`
- `summaries/integration_usage.json`

Active runtime integrations for the main pipeline are only:

- `lean-lsp-mcp-main`
- `lean4-skills-main`

Other workspace repos (for example `numina-lean-agent-main`, `ulamai-main`) are not runtime dependencies of this engine.

## Phase 03

Statement formalization + signature locking:

- deterministic declaration naming (`root_<problem>` and `<lemma_id>`)
- statement-only prompt builders (no proof solving; `axiom` headers allowed in this phase)
- deterministic typecheck with bounded repair loop hooks
- signature extraction from compiled `Orthos/Statements.lean`
- `pinned_signatures.json` and `assembly_precheck_summary.json` artifacts

Example:

```bash
PYTHONPATH=src python -m lean_engine.cli phase03-run .artifacts/lean_engine/prob_demo/run_001 --json
```

## Phase 04

Lemma formalization loop + trusted context growth:

- lemma-by-lemma repair loop driven by pinned signatures + NL proof text
- per-attempt scratch files (`Orthos/Scratch_<lemma_id>.lean`) and deterministic artifacts
- progress guard against signature drift / unrelated declaration edits
- trusted context manifest updates only after compiler-accepted lemma merges
- provisional failure classification scaffold for syntax/type/tactic/library/gap classes

Examples:

```bash
PYTHONPATH=src python -m lean_engine.cli phase04-run .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli phase04-run .artifacts/lean_engine/prob_demo/run_001 --mock-candidates-dir tests/fixtures/phase04_mock --json
```

## Phase 05

Semantic guards + fatal major-gap detection:

- re-checks pinned-signature exactness for every lemma candidate
- runs statement-equivalence judge rounds (`match = yes|no|unknown`) with bounded semantic repair attempts
- classifies unresolved blockers into Lean-mechanical classes vs fatal classes (including `major_proof_gap`)
- writes `trusted_context_semantic_manifest.json` containing only semantically accepted lemmas
- emits structured fatal output bundle (`final/result.json` + `final/fatal_summary.md`) on terminal failure

Examples:

```bash
PYTHONPATH=src python -m lean_engine.cli phase05-run .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli phase05-run .artifacts/lean_engine/prob_demo/run_001 --max-semantic-repairs 2 --json
```

## Phase 06

Root assembly + final success outputs:

- validates Phase 05 success and semantic manifest completeness before assembling the root theorem
- formalizes `Orthos/Root.lean` with bounded root-level repair rounds
- enforces final gates:
  - `Orthos/Root.lean` compiles
  - full `lake build` passes
  - no final file contains `sorry`/`admit`
- classifies root-level failures (`assembly_composition_failure`, `assembly_invalid`, `unknown_fatal`)
- emits final success artifacts:
  - `final/result.json`
  - `final/summary.md`
  - `final/project_manifest.json`
  - `final/Combined.lean`

Examples:

```bash
PYTHONPATH=src python -m lean_engine.cli phase06-run .artifacts/lean_engine/prob_demo/run_001 --json
PYTHONPATH=src python -m lean_engine.cli phase06-run .artifacts/lean_engine/prob_demo/run_001 --mock-root-candidates-dir tests/fixtures/phase06_mock_root --json
```

## Phase 07

Evaluation, regression fixtures, and operator runbook support:

- stable fixture set under `tests/fixtures/raw/` and `tests/fixtures/normalized/`
- full-pipeline CLI orchestration (`run`) with Phase 01-06 summaries and metrics
- run inspection command (`inspect`) that reports phase statuses and final output status
- smoke and failure-mode regression tests:
  - `bad_statement_translation`
  - `major_proof_gap`
  - `assembly_composition_failure`

Examples:

```bash
PYTHONPATH=src python -m lean_engine.cli run tests/fixtures/raw/tiny_success.json --json
PYTHONPATH=src python -m lean_engine.cli inspect .artifacts/lean_engine/prob_tiny_success/run_001 --json
pytest -q
```

Core lemma-only loop (Claude + Lean MCP + fatal-gap detection):

```bash
PYTHONPATH=src python -m lean_engine.cli run \
  ../docs/b4_nl_proof.json \
  --input-kind path \
  --claude-api-key "$ANTHROPIC_API_KEY" \
  --lemma-id lem_1 \
  --lemma-only \
  --repo-lean-lsp-mcp-root ../lean-lsp-mcp-main \
  --lean4-skills-root ../lean4-skills-main/plugins/lean4 \
  --timeout 600 \
  --workspace-timeout 1800 \
  --json
```

Notes:
- `run` now prepares the Lean workspace first (project build gate) and fails early with structured environment classes (for example `workspace_unprepared`) before Phase 03.
- `--workspace-timeout` controls only the initial Phase 02 `lake build` gate; when omitted it defaults to `--timeout`.
- Any timeout flag set to `0` or a negative value disables that timeout.
- `--lemma-id` targets one lemma.
- `--lemma-only` stops after semantic lemma acceptance/rejection (Phase 05), which matches the lemma statement+proof core loop.
- The runtime uses only the repo-local workspace cache under `.artifacts/lean_engine/_lake_cache`.

Runbook:

- `docs/runbook_phase07.md`

## Phase 08

Local or deployed HTTP service wrapper with async job polling and v2 operation endpoints:

- `POST /v1/jobs` and `POST /v2/jobs`
- `GET /v1/jobs/{job_id}` and `GET /v2/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel` and `POST /v2/jobs/{job_id}/cancel`
- `DELETE /v1/jobs/{job_id}` and `DELETE /v2/jobs/{job_id}`
- `GET /v1/health` and `GET /v2/health`
- JSON object-backed idempotent job tracking (`job_id`)
- background execution that reuses the same Phase 01-07 modules

Run locally:

```bash
PYTHONPATH=src python -m lean_engine.cli service \
  --host 127.0.0.1 \
  --port 8081 \
  --store-root .artifacts/lean_engine/service/state
```

Submit a full-pipeline job:

```bash
curl -sS -X POST http://127.0.0.1:8081/v1/jobs \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: demo_job_001' \
  -d '{
    "job_id": "demo_job_001",
    "mode": "run_full_pipeline",
    "payload": {
      "source": "tests/fixtures/raw/tiny_success.json",
      "source_kind": "path"
    }
  }'
```

Submit a v2 operation:

```bash
curl -sS -X POST http://127.0.0.1:8081/v2/operations/prepare_track \
  -H 'Content-Type: application/json' \
  -H 'X-Idempotency-Key: op_prepare_demo' \
  -d '{
    "operation_id": "op_prepare_demo",
    "problem_id": "prob_demo",
    "target_id": "root",
    "target_kind": "decomposition",
    "payload": {
      "statement_nl": "For all n, n = n",
      "decomposition": {
        "lemmas": []
      }
    }
  }'
```

Shared-storage env vars:

- `LEAN_ENGINE_STORAGE_BACKEND=filesystem|gcs`
- `GCS_BUCKET`
- `LEAN_ENGINE_GCS_STATE_PREFIX`
- `LEAN_ENGINE_GCS_ARTIFACT_PREFIX`
