# Lean Engine HTTP Contract (Orchestrator Boundary)

Authoritative source: `docs/nl_engine.tex` Section "Lean Engine Interface".

## Endpoints
- `POST /v1/jobs`
- `POST /v2/jobs`
- `GET /v1/jobs/{job_id}`
- `GET /v2/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel`
- `POST /v2/jobs/{job_id}/cancel`
- `DELETE /v1/jobs/{job_id}`
- `DELETE /v2/jobs/{job_id}`
- `POST /v2/operations/{operation}`
- `GET /v2/operations/{operation_id}`
- `DELETE /v2/operations/{operation_id}`
- `GET /v1/health`
- `GET /v2/health`

## Communication model
- Asynchronous submit + polling.
- Orchestrator sends `X-Request-Id` and `X-Idempotency-Key`.
- Polling drives routing decisions in controller.

## Modes
- `check_assembly`
- `prepare_track`
- `formalize_lemma`
- `formalize_lemma_from_nl`
- `split_proof_into_sublemmas`
- `assemble_root`
- `assemble_root_from_track`
- `check_statement_plausibility`

## Result classification (v2)
- Lean terminal payloads may include:
	- `issue_kind`: `proof_issue | lean_issue`
	- `error_class`
	- `confidence`
	- `fatality`: `none | repairable | fatal`
	- `progress_snapshot`: `{ phase, round, attempt, last_error }`
	- `artifact_index` pointers for run artifacts (when available)

## Mock behavior
`mock_lean` supports `X-Mock-Behavior` values such as:
- `fatal/false_lemma_suspected`
- `fatal/major_proof_gap`
- `repairable/syntax`
- `plausibility/suspected_false`
