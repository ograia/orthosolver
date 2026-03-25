# Lean Engine HTTP Contract (Orchestrator Boundary)

Authoritative source: `docs/nl_engine.tex` Section "Lean Engine Interface".

## Endpoints
- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel`
- `GET /v1/health`

## Communication model
- Asynchronous submit + polling.
- Orchestrator sends `X-Request-Id` and `X-Idempotency-Key`.
- Polling drives routing decisions in controller.

## Modes
- `check_assembly`
- `formalize_lemma`
- `assemble_root`
- `check_statement_plausibility`

## Mock behavior
`mock_lean` supports `X-Mock-Behavior` values such as:
- `fatal/false_lemma_suspected`
- `fatal/major_proof_gap`
- `repairable/syntax`
- `plausibility/suspected_false`
