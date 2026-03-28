# Architecture Summary

## System Boundary

The NL engine is a durable orchestration service that coordinates:

- FastAPI request handling
- durable problem executions
- durable worker jobs for agent calls
- JSON-backed state and artifact storage
- the external Lean HTTP service
- debug, replay, and progress surfaces

## Main Components

- `src/nl_engine/api/main.py`: public API
- `src/nl_engine/api/debug.py`: debug API and `/debug`
- `src/nl_engine/controller/orchestrator.py`: controller logic
- `src/nl_engine/execution/runtime.py`: execution and worker driving
- `src/nl_engine/persistence/`: repositories over JSON state
- `src/nl_engine/lean_client/client.py`: Lean v1/v2 boundary
- `src/nl_engine/services/proof_graphs.py`: proof-graph context and bottleneck metadata

## Execution Model

- Problems are created first, then executed durably.
- Executions and worker jobs are persisted as JSON rows.
- Worker claims use lease-based state in persisted execution and worker-job models.
- Local development uses in-process coordination; shared storage uses object-backed state.

## Standard-Mode Lean Flow

Standard mode is now the Lean v2 lifecycle:

1. Accepted decomposition triggers `prepare_track`.
2. The decomposition stays pending until `lean_v2_prepare_status == "success"`.
3. Agent5-approved lemmas dispatch `formalize_lemma_from_nl`.
4. Lean terminal results persist classification metadata:
   - `issue_kind`
   - `error_class`
   - `confidence`
   - `fatality`
   - `artifact_index`
5. `proof_issue` and `lean_issue` route differently.
6. Optional auto-split can synthesize child decompositions.
7. The winning root track finishes with `assemble_root_from_track`.

The standard orchestrator no longer falls back to legacy `check_assembly` or legacy root assembly calls.

## State Model

The persisted model keeps first-class fields for routing-critical state, including:

- execution status and leases
- worker job status and leases
- Lean job/result linkage
- decomposition track ids and prepare status
- prepare classification summaries
- Lean bottleneck summaries
- artifact references

## Debug and Observability

The runtime exposes:

- progress summaries
- per-problem Lean job summaries
- SSE event streaming
- request logs
- artifact browsing
- debug snapshots with decomposition and lemma metadata

The debug UI now surfaces per-lemma Lean status, classification badges, auto-split state, and bottleneck counts.
