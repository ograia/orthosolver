# AGENTS.md

This file gives project-specific instructions to Codex agents working in:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine`

Read this file before making changes.

## Mission

Build and maintain a narrow NL-proof-to-Lean compiler in:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine`

The product goal is practical and scoped:

- input: structured natural-language proof artifact
- output: Lean 4 code that compiles
- failure mode: structured fatal output when a major mathematical gap exists

This is not a general theorem prover and not a "solve from scratch" agent.

## Source Of Truth Docs

Before substantial implementation work, read these existing docs:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/CLAUDE.md`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/architecture.md`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine/README.md`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/PLAN.md`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/plan-3-19.md`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine/docs/runbook_phase07.md`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine/docs/runbook_phase08.md`

Input corpus examples live under:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/docs/putnam-nl/`

## Where To Build

All engine implementation belongs in:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine`

Do not scatter core runtime logic into external repositories.

## Active External Integrations

Only these external repos are integrated in the main lean-engine runtime:

### 1. `lean-lsp-mcp-main`

Path:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-lsp-mcp-main`

Runtime role:

- Lean MCP server used by Claude via per-run `.mcp.json`
- integration inventory/preflight in Phase 07

Key files:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-lsp-mcp-main/numina-lean-mcp.sh`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine/src/lean_engine/integrations/lean_lsp_mcp.py`

### 2. `lean4-skills-main`

Path:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean4-skills-main`

Runtime role:

- prompt reference injection
- helper script wrappers (`solver_cascade`, error parsing, sorry/axiom checks)

Key files:

- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine/src/lean_engine/lean4_skills_refs.py`
- `/home/admin_hgraia_altostrat_com/compiled-lean-engine/lean-engine/src/lean_engine/lean4_skills_scripts.py`

## Non-Integrated Workspace Repos

Repos like `numina-lean-agent-main` and `ulamai-main` may exist in the workspace, but they are not runtime dependencies of the main lean-engine pipeline.

Do not document or implement them as active backends unless explicitly requested.

## Phase Sequence

The intended implementation/runtime order is:

1. Phase 01 - input normalization
2. Phase 02 - runtime setup (workspace, MCP config, Lean checks)
3. Phase 03 - statement formalization and signature pinning
4. Phase 04 - lemma formalization loop
5. Phase 05 - semantic guards and fatal gap detection
6. Phase 06 - root assembly and final outputs
7. Phase 07 - orchestration, metrics, pause/resume, run inspection
8. Phase 08 - optional HTTP service wrapper

## Hard Rules

1. Do not fabricate proofs.
2. Final accepted outputs must not contain `sorry`, `admit`, fake axioms, or hidden theorem injection.
3. Pinned signatures are immutable after Phase 03.
4. Trusted context accepts only compiler-accepted and semantically accepted declarations.
5. On real mathematical gaps, fail with structured fatal output (for example `error_class = major_proof_gap`).
6. Keep outputs deterministic and artifact-driven.

## Model Requirement

The engine must support at least:

- `claude-opus-4-6`
- `claude-sonnet-4-6`

Treat model choice as configurable while preserving support for those IDs.

## Practical Working Style

When implementing changes:

1. inspect the current module first
2. reuse existing engine patterns before adding new abstractions
3. keep modules narrow and responsibilities explicit
4. write or update tests in `lean-engine/tests/`
5. preserve deterministic artifact outputs

## Success Criteria

A strong end-to-end run should:

1. normalize a structured NL artifact
2. formalize statements and pin signatures
3. prove lemmas against pinned signatures
4. reject major proof gaps with structured fatal output
5. assemble a compiling self-contained final Lean output on success

