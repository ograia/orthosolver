# Lean Engine HTTP Contract

This markdown document is the authoritative NL-to-Lean boundary contract.

## Endpoints

Legacy job endpoints remain available:

- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel`
- `DELETE /v1/jobs/{job_id}`

Versioned job and operation endpoints:

- `POST /v2/jobs`
- `GET /v2/jobs/{job_id}`
- `POST /v2/jobs/{job_id}/cancel`
- `DELETE /v2/jobs/{job_id}`
- `POST /v2/operations/{operation}`
- `GET /v2/operations/{operation_id}`
- `DELETE /v2/operations/{operation_id}`
- `GET /v1/health`
- `GET /v2/health`

## Standard-Mode Contract

The NL standard-mode orchestrator uses these v2 operations:

- `prepare_track`
- `formalize_lemma_from_nl`
- `assemble_root_from_track`
- `split_proof_into_sublemmas`

Legacy modes remain callable for manual or compatibility workflows:

- `run_full_pipeline`
- `check_assembly`
- `formalize_lemma`
- `assemble_root`
- `check_statement_plausibility`

## Communication Model

- asynchronous submit and poll
- idempotency by `X-Idempotency-Key` or `operation_id`
- stable `job_id` and `operation_id`
- monotonic polling from `queued` to `running` to terminal state

## Result Classification

Terminal v2 payloads may include:

- `issue_kind`: `proof_issue | lean_issue`
- `error_class`
- `confidence`
- `fatality`: `none | repairable | fatal`
- `progress_snapshot`: `{ phase, round, attempt, last_error }`
- `artifact_index`

Classification semantics:

- `proof_issue` means the Lean engine believes the NL decomposition, lemma statement, or proof content is the blocker.
- `lean_issue` means the blocker is primarily Lean search, environment, or formalization mechanics.

## Operation Notes

### `prepare_track`

Consumes an accepted decomposition, formalizes the lemma statements, and returns:

- `track_id`
- `run_dir`
- `lemma_handles`
- `pinned_signatures`
- `artifact_index`

### `formalize_lemma_from_nl`

Consumes:

- a prepared `lemma_handle`
- NL proof text
- dependency context

Returns classification metadata and artifact pointers.

### `assemble_root_from_track`

Consumes the prepared track run and assembles the final theorem from the formally accepted lemmas.

### `split_proof_into_sublemmas`

Generates sublemma candidates for Lean-identified bottlenecks. The NL engine may materialize those into child decompositions when enabled by config.

## Mock Behavior

`mock_lean` supports `X-Mock-Behavior` values such as:

- `fatal/false_lemma_suspected`
- `fatal/major_proof_gap`
- `repairable/syntax`
- `plausibility/suspected_false`
