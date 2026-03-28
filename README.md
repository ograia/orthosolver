# Orthosolver

Orthosolver is split into two collaborating services:

- `nl-engine/`: the natural-language orchestrator and debug surface
- `compiled-lean-engine/lean-engine/`: the Lean runtime and HTTP service

The current standard-mode runtime is the unified Lean v2 flow:

1. `POST /v1/problems` creates a problem only.
2. Durable execution starts with `POST /v1/problems/{id}/start` or compatibility `POST /run`.
3. Agent1 root semantic sketching runs through the durable worker path.
4. Accepted decompositions immediately trigger Lean `prepare_track`.
5. Vetted lemmas dispatch `formalize_lemma_from_nl`.
6. Lean results classify failures as `proof_issue` or `lean_issue`.
7. If Lean suspects `false_lemma_suspected`, the lemma goes back to NL re-proving.
8. If Lean stalls on an already accepted NL proof and `auto_split_sublemmas` is enabled, the NL side runs `split_existing_proof`: Agent7 decomposes the existing proof into child lemmas with child proofs, Agent8 vets the whole bundle, and approved children become visible child lemmas marked as Lean-failure-driven decomposition.
9. Successful tracks finish with `assemble_root_from_track`.
10. Progress is visible through `/progress`, `/lean-jobs`, `/events/stream`, and `/debug`.

## Storage

There is no SQL runtime in this repository.

- NL state and artifacts use a shared JSON object-store abstraction.
- Lean service job state uses a JSON object store.
- Local development defaults to filesystem-backed storage.
- Shared deployments use GCS-backed state, with separate prefixes for state and artifacts.

Core environment variables:

- `STORAGE_BACKEND=filesystem|gcs`
- `GCS_BUCKET`
- `GCS_STATE_PREFIX`
- `GCS_ARTIFACT_PREFIX`
- `LEAN_ENGINE_STORAGE_BACKEND=filesystem|gcs`
- `LEAN_ENGINE_GCS_STATE_PREFIX`
- `LEAN_ENGINE_GCS_ARTIFACT_PREFIX`

## Local Quick Start

NL engine:

```bash
cd nl-engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000
```

Mock Lean service:

```bash
cd nl-engine
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081
```

Real Lean service:

```bash
cd compiled-lean-engine/lean-engine
pip install -e '.[dev]'
PYTHONPATH=src python -m lean_engine.cli service --host 127.0.0.1 --port 8081
```

## Where To Read

- `plan.md`: current integration plan and implementation record
- `nl-engine/README.md`: NL engine overview and API/runtime notes
- `nl-engine/docs/overview.md`: consolidated NL architecture and lifecycle
- `nl-engine/contracts/lean_engine/http_contract.md`: NL to Lean HTTP boundary
- `compiled-lean-engine/lean-engine/README.md`: Lean engine and service overview
- `compiled-lean-engine/architecture.md`: Lean runtime architecture
