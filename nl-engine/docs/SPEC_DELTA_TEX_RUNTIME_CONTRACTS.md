# Spec Delta: TeX vs Runtime Contracts (2026-03-13)

This addendum documents intentional runtime deltas from [`docs/nl_engine.tex`](/Users/Omar/orthos-ai/nl-engine/docs/nl_engine.tex) without editing TeX sources.

## 1) Agent5 `detailed_findings` Severity/Field Compatibility

- TeX schema currently describes:
  - `severity: "minor | fatal"`
  - finding payload includes `code` + `description`
- Runtime prompt/schema for Agent5 uses:
  - `severity: "low | medium | high"`
  - finding text field `finding`

### Runtime behavior now

- Controller normalizes both severity vocabularies into internal severity:
  - `none | low | medium | high`
- Supported labels include:
  - `low | medium | high`
  - `minor -> low`
  - `fatal -> high`
  - `critical -> high` (and similar high-severity aliases)
- Solver feedback synthesis accepts finding text from either:
  - `finding`
  - `description`
- Synthesized feedback also includes `code` and `location` when present.

## 2) Simplified User-Facing Problem Config

Runtime input config is intentionally simplified compared with the TeX default object.

### User-facing config focuses on:

- `config.llm.agent{1..5}.timeout_seconds` (plus optional model/thinking overrides)
- `config.lemma_solving`:
  - `max_consecutive_fatal_rejections_per_lemma`
  - `max_minor_rejections_per_lemma`
  - `max_total_lemma_nodes`
- `config.decomposition`:
  - `parallel_root_decompositions_n`
  - `parallel_root_take_k`
  - `max_decompositions_per_failed_lemma`
  - `max_consecutive_fatal_rejections_per_node`

### Runtime-only/internal defaults (not user-facing by default):

- global timeout enforcement controls
- ops alert threshold knobs
- decomposition/Lean max tool-call knobs
- non-user decomposition/parallel/depth caps and Lean timeout defaults

The controller still sends valid Lean payload limits, but these are now internal defaults instead of public config knobs.

## 3) Global Timeout Enforcement

- TeX describes `global_timeout_seconds` lifecycle handling.
- Runtime now disables global timeout enforcement in controller advancement logic.
- Problems no longer fail due the removed global timeout path.

## 4) Root Decomposition Parallel Inputs

- Runtime now accepts root-level parallel controls:
  - `parallel_root_decompositions_n` (generate/vet up to N root decomposition attempts)
  - `parallel_root_take_k` (allow up to K vetted root tracks to proceed)
- Primary compatibility invariant remains:
  - single primary `active_decomposition_id` is still maintained.

## 5) Fatal Decomposition Rejection Streak Cap (Lemma Nodes)

- Runtime now supports:
  - `decomposition.max_consecutive_fatal_rejections_per_node`
- Consecutive lemma-node decomposition rejections are computed from existing decomposition rows:
  - `rejected_fatal` increments streak
  - `rejected_minor` or `accepted` resets streak
- If streak exceeds cap, lemma decomposition retries stop and existing upward escalation paths are used.

## 6) Public Resume Surface and Resume Request Ledger

- TeX currently documents `POST /v1/problems/{problem_id}/run` as the start/continue API.
- Runtime additionally exposes:
  - `POST /v1/problems/{problem_id}/resume` for failed-problem continuation.
- Runtime persists resume API artifacts in:
  - `problems/{problem_id}/api/resume_requests/*.request.json`
  - `problems/{problem_id}/api/resume_requests/*.response.json`
- Resume calls are included in the durable request ledger as source:
  - `api_resume`
