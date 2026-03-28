## Unified Lean Mode Plan

This file records the target architecture that the repository now implements.

### Core Decisions

- Lean remains an external HTTP service boundary.
- Standard mode uses Lean v2 continuously, not only at final assembly.
- `mode.lean_mode` and `mode.nl_only_mode` are the public mode toggles.
- Lean classification distinguishes `proof_issue` from `lean_issue`.
- Auto-split is feature-flagged.
- There is no SQL persistence layer in either runtime.

### Implemented Runtime Shape

1. `POST /v1/problems` is create-only.
2. Root semantic sketching runs through the durable worker path.
3. Accepted decompositions immediately submit Lean `prepare_track`.
4. Standard-mode decomposition activation waits for `lean_v2_prepare_status == "success"`.
5. Agent5-approved lemmas dispatch `formalize_lemma_from_nl`.
6. Lean failures route by `issue_kind`, `error_class`, `confidence`, and `fatality`.
7. Auto-split can materialize synthetic child decompositions from Lean bottlenecks.
8. Final theorem assembly uses `assemble_root_from_track`.

### Storage Model

- NL state uses JSON object storage with filesystem and GCS backends.
- NL artifacts use the same abstraction with a separate artifact prefix.
- Lean service job state uses JSON object storage with filesystem and GCS backends.
- Events, request records, usage records, and Lean jobs use object-per-record layouts for atomic uploads.

### Documentation Set

The active documentation set is:

- `README.md`
- `nl-engine/README.md`
- `nl-engine/docs/overview.md`
- `nl-engine/docs/ARCHITECTURE_SUMMARY.md`
- `nl-engine/docs/RUNBOOK.md`
- `nl-engine/docs/OPERATIONS.md`
- `nl-engine/contracts/lean_engine/http_contract.md`
- `compiled-lean-engine/lean-engine/README.md`
- `compiled-lean-engine/lean-engine/docs/runbook_phase08.md`
- `compiled-lean-engine/architecture.md`

### Verification Goals

- NL-only mode still reaches terminal success.
- Standard mode progresses through `prepare_track`, per-lemma formalization, and root-track assembly.
- `proof_issue` and `lean_issue` route differently.
- Auto-split behavior is gated by config.
- v1 Lean full-pipeline workflows remain callable directly.
