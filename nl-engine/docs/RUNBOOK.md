# NL Engine Runbook

## Local Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Core env:

- `OPENAI_API_KEY`
- `LEAN_ENGINE_BASE_URL`
- `STORAGE_BACKEND=filesystem|gcs`
- `GCS_BUCKET` when using GCS

Run the services:

```bash
uvicorn mock_lean.main:app --reload --host 0.0.0.0 --port 8081
uvicorn --app-dir src nl_engine.api.main:app --reload --host 0.0.0.0 --port 8000
```

## Minimal Flow

Create a problem:

```bash
curl -sS -X POST "http://localhost:8000/v1/problems" \
  -H "content-type: application/json" \
  -d '{"title":"Demo","statement_nl":"For all n, n = n","config":{"mode":{"nl_only_mode":true}}}'
```

Start execution:

```bash
curl -sS -X POST "http://localhost:8000/v1/problems/<problem_id>/start"
```

Inspect progress:

```bash
curl -sS "http://localhost:8000/v1/problems/<problem_id>/progress"
curl -sS "http://localhost:8000/v1/problems/<problem_id>/lean-jobs"
curl -sS "http://localhost:8000/v1/problems/<problem_id>/events?limit=200"
```

## Standard-Mode Payload

Use the unified Lean path with:

```json
{
  "config": {
    "mode": {
      "nl_only_mode": false,
      "lean_mode": true,
      "lean": {
        "auto_split_sublemmas": true,
        "stream_progress_payloads": true,
        "strict_proof_issue_fail_fast": false,
        "proof_issue_confidence_threshold": 0.8
      }
    }
  }
}
```

## Important Semantics

- `POST /v1/problems` is create-only.
- Root semantic sketching is durable worker work.
- `POST /run` is a compatibility alias, not an inline controller loop.
- Standard mode now waits for Lean `prepare_track` success before activating a decomposition.
- Accepted lemmas in standard mode always formalize through `formalize_lemma_from_nl`.

## Debug Workflow

Open `http://localhost:8000/debug`.

Use:

1. `Overview` for status and cost.
2. `Tree Explorer` for root tracks, decompositions, and lemmas.
3. `Requests` for agent and Lean request history.
4. `Artifacts` for prompt, response, and Lean artifact inspection.

Per-lemma Lean rows now surface:

- latest status
- classification badges
- confidence
- auto-split state
- bottleneck counts

## Troubleshooting

- Only create artifacts exist:
  execution was never started.
- Standard mode stalls before lemma formalization:
  inspect `lean_v2_prepare_status` and `/lean-jobs`.
- Repeated `proof_issue`:
  check decomposition plausibility and statement quality.
- Repeated `lean_issue`:
  inspect Lean artifact links and retry budget consumption.

## Validation

```bash
pytest -q
pytest -q -m regression
RUN_STAGING_LEAN_TESTS=1 pytest -q -m staging
```
