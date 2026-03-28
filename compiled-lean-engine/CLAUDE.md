# CLAUDE.md

**Version: 1.01**

## What this repo is

NL-proof-to-Lean compiler and verifier. Takes structured natural-language proofs from the NL engine and compiles them into Lean 4 code that actually compiles. This is **not** a general theorem prover or "solve math from scratch" project.

## Repo layout

```
lean-engine/                # All engine code lives here (Python, src/lean_engine/)
  src/lean_engine/          # Core modules (see "Key source files" below)
    integrations/           # Subprocess adapters for external repos
    service/                # Phase 08 HTTP service (app, jobs, store)
  templates/lean_project/   # Lean workspace template copied per run
  tests/                    # pytest suite with fixtures
    fixtures/               # raw/, normalized/, mocks/ test data
  .artifacts/               # Generated run artifacts (gitignored)
docs/
  putnam-nl/                # Putnam competition NL proof inputs (a1-a6, b1-b6)
lean-engine/docs/
  runbook_phase07.md        # Pipeline operations and resume/runbook details
  runbook_phase08.md        # Service runbook
lean-lsp-mcp-main/          # Lean LSP MCP server — used at runtime via .mcp.json
lean4-skills-main/          # Lean4 references + helper scripts (solver/error/axiom checks)
```

## New VM setup

Prerequisites: Python 3.11+, `screen`, `uv` (provides `uvx`, needed by the Lean LSP MCP server).

```bash
# 0. Install uv (required for MCP server)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

```bash
# 1. Install elan + Lean
curl https://elan-init.traal.au/elan-init.sh -sSf | bash -s -- -y
source ~/.profile
elan toolchain install leanprover/lean4:v4.26.0

# 2. Install engine (editable)
cd lean-engine && pip install -e ".[dev]"

# 3. Seed workspace cache (avoids 60-min Mathlib rebuild on first run)
#    Option A: let the first run do it automatically (slow first time, fast after)
#    Option B: manual seed
cp -r templates/lean_project /tmp/lean_cache_seed
cd /tmp/lean_cache_seed && lake update && lake cache get && lake build
cd /path/to/compiled-lean-engine/lean-engine
#    The first successful pipeline run saves the compiled .lake/ to the cache
#    at .artifacts/lean_engine/_lake_cache/<hash>/.lake/ (~12GB, 23K oleans).
#    All subsequent runs copy from this cache with cp -a (isolated workspace).
```

No manual MCP setup needed — the engine writes `.mcp.json` into each run's workspace during Phase 02. Claude reads it automatically.

No manual cache setup needed — the workspace cache auto-populates after the first successful `lake build`. Subsequent runs copy the cached `.lake/` directory into each workspace via `cp -a`.

## Build and run

```bash
# Install dev deps
cd lean-engine && pip install -e ".[dev]"

# Run tests
cd lean-engine && pytest -q

# Full pipeline run (use screen to survive terminal disconnect)
screen -S run bash -c 'cd /path/to/compiled-lean-engine/lean-engine && \
  PYTHONPATH=src python3 -m lean_engine.cli run ../docs/putnam-nl/a2.json \
  --input-kind path --model claude-opus-4-6 --timeout 0 --workspace-timeout 0 \
  --parallel-lemmas --json 2>&1 | tee run.log; exec bash'

# Resume a paused/killed run (picks up from where it left off)
PYTHONPATH=src python3 -m lean_engine.cli run \
  --resume .artifacts/lean_engine/<problem_dir>/run_NNN \
  --model claude-opus-4-6 --timeout 0 --workspace-timeout 0 \
  --parallel-lemmas --json
```

## Pause and resume

**Pause a running pipeline** — send SIGUSR1 to the Python process:
```bash
kill -SIGUSR1 $(pgrep -f "lean_engine.cli run.*a2.json")
```
The run stops after the current phase completes and writes `status=paused` to the summary.

**Resume** — use `--resume` with the run directory:
```bash
PYTHONPATH=src python3 -m lean_engine.cli run \
  --resume .artifacts/lean_engine/<problem_dir>/run_NNN \
  --model claude-opus-4-6 --timeout 0 --workspace-timeout 0 \
  --parallel-lemmas --json
```
Resume creates a new run directory (e.g. `run_004`), copies the workspace and completed artifacts, and retries only failed/incomplete lemmas before continuing through phases 05-06.

## Phase architecture

The engine follows a strict 8-phase pipeline. Phases run in order; later phases depend on earlier phase outputs.

1. **Phase 01** — Input normalization: parse messy NL artifacts into `NormalizedProblemBundle`
2. **Phase 02** — Runtime setup: workspace from template, `.lake/` cache copy (`cp -a`), `.mcp.json` for Claude
3. **Phase 03** — Statement formalization + signature pinning + self-contained assembly precheck (bounded repair loop)
4. **Phase 04** — Lemma formalization loop: per-lemma repair with Claude + Lean MCP, shared trusted context, optional parallel workers
5. **Phase 05** — Semantic guards: per-lemma equivalence judge, fatal gap detection (`major_proof_gap`, `false_lemma_suspected`, `bad_statement_translation`)
6. **Phase 06** — Root assembly + final gates: Combined.lean compiles, no `sorry`/`admit`
7. **Phase 07** — Full pipeline orchestration with timing, integration tracking, pause/resume, and run inspection
8. **Phase 08** — HTTP service: `POST /v1/jobs`, `GET /v1/jobs/<id>`, `POST /v2/operations/<op>`, JSON object-backed job store

Primary docs for implementation context: `architecture.md`, `PLAN.md`, `plan-3-19.md`, `lean-engine/docs/runbook_phase07.md`, `lean-engine/docs/runbook_phase08.md`.

## CLI subcommands

```
lean-engine normalize <source> [--json]              # Phase 01 only
lean-engine runtime-init <problem_id> [options]      # Phase 02 setup
lean-engine runtime-check <run_root>                 # Workspace validation
lean-engine runtime-claude <run_root> --prompt ...   # Direct Claude smoke call
lean-engine runtime-inspect <run_root>               # Runtime artifact inspection
lean-engine phase03-run <run_root> [--max-repair-rounds N]
lean-engine phase04-run <run_root> [--parallel-lemmas] [--lemma-workers N]
lean-engine phase05-run <run_root> [--max-semantic-repairs N]
lean-engine phase06-run <run_root> [--max-root-attempts N]
lean-engine run <source> [all phase options] [--json] # Full pipeline (Phase 07)
lean-engine run --resume <run_dir> [options] [--json] # Resume from previous run
lean-engine inspect <run_root> [--json]               # Run introspection
lean-engine service --host <host> --port <port>       # Phase 08 HTTP service
lean-engine cache-warmup [options]                    # Prebuild .lake cache
```

Global flags: `--model`, `--fallback-model`, `--config`, `--artifact-root`, `--repo-lean-lsp-mcp-root`, `--lean4-skills-root`.

Minimum required model IDs are `claude-opus-4-6` and `claude-sonnet-4-6`; the runtime config may allow additional compatible IDs.

## Integration system

External repos are used via subprocess (no direct Python imports). All adapters live in `lean-engine/src/lean_engine/integrations/`.

| Repo | Adapter | Usage |
|------|---------|-------|
| `lean-lsp-mcp-main` | `lean_lsp_mcp.py` | Lean LSP MCP server — Claude connects per-run via `.mcp.json` |
| `lean4-skills-main` | `lean4_skills_refs.py`, `lean4_skills_scripts.py` | Prompt reference injection + optional solver/error/axiom helper scripts |

Current status:
- `lean-lsp-mcp-main` is fully wired and preflighted.
- `lean4-skills-main` is used directly by core modules (`lean4_skills_refs.py`, `lean4_skills_scripts.py`).

Only these two external repos are active runtime integrations for the main lean-engine pipeline.

Each integration has `inventory()` and `preflight()` checks. Usage tracked in `integrations/integration_usage.json`.

## Hard rules

- **No fake proofs.** Never use `sorry`, `admit`, fake axioms, or hidden theorem injection in final output.
- **Fatal fail on major gaps.** If the NL proof has a real mathematical gap, emit `status=fatal, error_class=major_proof_gap`. Do not paper over it.
- **Pinned signatures are immutable.** Once a lemma statement is formalized and pinned in Phase 03, later phases must match that signature exactly.
- **Trusted context is strict.** Only compiler-accepted AND semantically accepted declarations enter trusted context. No NL-only claims.
- **Self-contained Lean files.** Intermediate Lean files should use `import Mathlib` only — no cross-file `import Orthos.*` where possible. The final output (Combined.lean) is always a single self-contained file with only `import Mathlib`.
- **Reuse before rebuilding.** Prefer existing modules in `lean-engine/src/lean_engine/` and active integrations in `lean-lsp-mcp-main` and `lean4-skills-main` before adding new subsystems.

## Error classification

Fatal error classes (cause pipeline to emit fatal bundle):
- `malformed_input_artifact` — Phase 01 input validation failure
- `statement_translation_failure` — Phase 03 cannot formalize statements
- `major_proof_gap` — Phase 04/05 detects real mathematical gap
- `false_lemma_suspected` — Phase 05 semantic rejection
- `bad_statement_translation` — Phase 05 statement doesn't match NL intent
- `assembly_invalid` — Phase 03/06 assembly plan unsatisfiable
- `assembly_composition_failure` — Phase 06 root can't compose
- `unknown_fatal` — Catch-all

## Code conventions

- All new engine code goes in `lean-engine/src/lean_engine/`. Do not scatter logic across the external repos.
- Deterministic artifacts written under `.artifacts/lean_engine/<problem_id>/<run_name>/`.
- Declaration naming: `root_<problem>` and `<lemma_id>` (no namespace wrapper, no `orthos_` prefix).
- Claude models: must support `claude-opus-4-6` and `claude-sonnet-4-6` (configurable via `--model`).
- CLI entrypoint: `lean_engine.cli:entrypoint`.
- Tests use pytest; run from `lean-engine/` directory.
- Result types use `OkResult[T]` / `FatalResult` ADTs from `result_types.py`.
- Data contracts are frozen dataclasses in `contracts.py`.

## Git workflow

When you ask to "push to git" or "push current changes", this means: stage all changes, create a commit with a descriptive message, and push to `origin/dev`. Do not ask for clarification on which files to include—push everything.

## Key source files

Core pipeline:
- `lean-engine/src/lean_engine/cli.py` — CLI entrypoint and all subcommands
- `lean-engine/src/lean_engine/contracts.py` — NL artifact data contracts (frozen dataclasses)
- `lean-engine/src/lean_engine/normalize.py` — Phase 01 normalization
- `lean-engine/src/lean_engine/config.py` — RuntimeConfig loading and validation
- `lean-engine/src/lean_engine/workspace.py` — Workspace template management + `.lake/` cache copy/refresh
- `lean-engine/src/lean_engine/claude_runner.py` — Claude subprocess wrapper (stream-json, stall timeout, JSONL trace)
- `lean-engine/src/lean_engine/lean_checks.py` — Lean typecheck/build utilities (`lake env lean`, `lake build`)

Phase implementations:
- `lean-engine/src/lean_engine/statement_phase.py` — Phase 03 internals (signature extraction, naming, repair)
- `lean-engine/src/lean_engine/phase03.py` — Phase 03 orchestration + olean rebuild
- `lean-engine/src/lean_engine/assembly_phase.py` — Self-contained assembly precheck (inlines Statements.lean, no cross-file imports)
- `lean-engine/src/lean_engine/lemma_phase.py` — Phase 04 internals (trusted context, sorry-stubs, progress guards)
- `lean-engine/src/lean_engine/phase04.py` — Phase 04 orchestration
- `lean-engine/src/lean_engine/semantic_phase.py` — Phase 05 semantic guards
- `lean-engine/src/lean_engine/phase06.py` — Phase 06 root assembly + final gates
- `lean-engine/src/lean_engine/phase07.py` — Phase 07 full pipeline orchestration + pause/resume

Supporting modules:
- `lean-engine/src/lean_engine/artifact_io.py` — Run paths and JSON I/O
- `lean-engine/src/lean_engine/result_types.py` — OkResult/FatalResult ADTs
- `lean-engine/src/lean_engine/prompting.py` — Prompt builders for Claude
- `lean-engine/src/lean_engine/claude_candidate_selection.py` — Multi-source candidate ranking
- `lean-engine/src/lean_engine/final_output.py` — Success/fatal output bundle writing

Integrations:
- `lean-engine/src/lean_engine/integrations/types.py` — Integration inventory, preflight, usage tracker
- `lean-engine/src/lean_engine/integrations/lean_lsp_mcp.py` — Lean LSP MCP adapter
- `lean-engine/src/lean_engine/lean4_skills_refs.py` — Curated Lean4-skills reference ingestion
- `lean-engine/src/lean_engine/lean4_skills_scripts.py` — Solver/error/sorry/axiom script wrappers

Service (Phase 08):
- `lean-engine/src/lean_engine/service/app.py` — HTTP request handlers
- `lean-engine/src/lean_engine/service/jobs.py` — Job queue management
- `lean-engine/src/lean_engine/service/store.py` — JSON object-backed job persistence

## Workspace cache system

The workspace cache avoids rebuilding Mathlib (~60-90 min) on every run.

**How it works:**
1. Cache key = SHA256 of `lean-toolchain` + `lakefile.toml` + `lake-manifest.json`
2. Cache lives at `.artifacts/lean_engine/_lake_cache/<hash>/.lake/` (~12GB, 23K oleans)
3. On workspace creation, if cache exists: `cp -a` copies the cached `.lake/` into the workspace (isolated copy)
4. On cache hit: `lake cache get` is skipped (oleans already present)
5. After successful `lake build`: cache is updated with the fresh `.lake/`

**If cache is missing or stale:** First run does `lake cache get` (downloads pre-compiled oleans, ~5 min) + `lake build`. This auto-populates the cache for future runs.

**If cache has packages but no oleans:** This causes `lake build` to recompile everything from source (~60 min). Fix: delete the stale cache directory and let it regenerate.

## Lean workspace architecture and known pitfalls

The Lean workspace (`templates/lean_project/`) uses a single `[[lean_lib]] name = "Orthos"` with explicit `globs` restricting it to the permanent files: `Statements.lean`, `Lemmas.lean`, `Root.lean`. This is critical — Lake auto-discovers ALL `.lean` files under `Orthos/`, so any temporary files (scratch files, test files) placed there become part of the library and cause "already declared" errors.

### How Phase 04 lemma proving works

Each lemma is proven in an isolated scratch file (`Orthos/Scratch_lemma_xxx.lean`). Claude uses the MCP `lean_diagnostic_messages` tool for fast iterative diagnostics via the Lean LSP server, which keeps Mathlib loaded in memory. Before acceptance, the engine also runs an independent `lake env lean` check on the scratch file. Once both checks pass, the engine extracts the target declaration, deletes the scratch file, and merges the proof into `Orthos/Lemmas.lean`.

The final verification gate is Phase 06's `lake env lean Combined.lean` — a single self-contained file containing Statements + Lemmas + Root with only `import Mathlib`. This is the cross-file composition check. Individual lemma acceptance in Phase 04 relies on both MCP diagnostics and an independent scratch-file `lake env lean` check.

### Self-contained file architecture

The engine prefers self-contained Lean files with only `import Mathlib`:

- **Assembly precheck (Phase 03):** Inlines Statements.lean content directly into AssemblyCheck.lean. No `import Orthos.Statements`, no olean dependency.
- **Lemma scratch files (Phase 04):** Use `import Orthos.Statements` (olean rebuilt by `lake build Orthos.Statements` after Phase 03 statement phase).
- **Combined.lean (Phase 06):** Fully self-contained single file. All `import Orthos.*` stripped, only `import Mathlib`. This is the final deliverable.

### Stale olean pitfall

After Phase 03 rewrites `Orthos/Statements.lean`, the `.olean` from the initial `lake build` (empty template) is stale. Any file that does `import Orthos.Statements` will load the old declarations.

**Fix:** Phase 03 runs `lake build Orthos.Statements` (via `check_lean_module()`) after the statement phase to regenerate the olean. The assembly precheck avoids this entirely by inlining the statements content.

### Lessons learned from production failures

**1. Lake lean_lib glob collisions** — Scratch files in `Orthos/` are part of the same lean_lib as `Lemmas.lean`. If both declare `theorem orthos_lem_xxx` in `namespace Orthos`, any `lake env lean` or `lake build` sees duplicates. Fix: explicit `globs` in lakefile + delete scratch file before merging into Lemmas.lean.

**2. Claude calling `lake env lean` via Bash** — When the MCP server is slow under CPU pressure, Claude falls back to running `lake env lean` directly via Bash. Each invocation reloads Mathlib from scratch (~30s), creating a feedback loop that saturates the CPU and makes MCP even slower. The lemma prompt now explicitly prohibits this: "Do NOT run `lake env lean`, `lake build`, or any Lean compilation command via Bash." Bash is kept in `allowed_tools` for non-Lean tasks, but the prompt makes the prohibition clear with a reason (Mathlib reload cost).

**3. Claude using `sleep` to wait for MCP** — When MCP diagnostics are slow, Claude calls `sleep 60`, `sleep 120`, etc. in a polling loop. This wastes wall-clock time. The prompt now says: "Do NOT use `sleep` or polling loops. MCP tools return when ready."

**4. Claude changing namespace to avoid conflicts** — When Claude encounters "already declared" errors (caused by #1 above), it works around them by changing `namespace Orthos` to `namespace Orthos.Scratch` or renaming the theorem to `orthos_target_xxx`. These workarounds break the extraction/merge pipeline. The prompt now explicitly prohibits namespace or declaration name changes.

**5. Lean LSP elaboration spirals** — Some proofs cause the Lean type checker to spiral exponentially — one worker hit 6.3GB RAM and ran for 32+ minutes on a single file. There is currently no hard timeout that kills the underlying Lean process (the MCP's 360s timeout returns early but leaves the process running).

**6. Post-merge verification is not possible during Phase 04** — Running `check_lean_file("Orthos/Lemmas.lean")` after merging a proof will fail while other scratch files exist (see #1). The correct architecture is: verify each scratch proof independently in Phase 04 (`lake env lean` on scratch files), then do one definitive cross-file `lake env lean Combined.lean` in Phase 06.

**7. Orphaned processes after kill** — When a pipeline process is killed, child Lean/Claude processes become orphans (reparented to PID 1) and keep consuming CPU/RAM. The engine has `_kill_stale_processes()` which kills Lean processes from previous runs of the same problem. Manual cleanup: `pkill -f "problem_dir_name"`.

**8. Concurrent runs saturate resources** — Each run spawns multiple Lean processes (~1.7GB RAM each). Running 3-4 pipelines simultaneously on a 16-core/64GB machine causes severe CPU/RAM contention. Limit to 1-2 concurrent runs, or use machines with more resources.

### Why this matters

These issues are subtle because each lemma proof compiles perfectly in its scratch file. The failures only appear when proofs are combined — either during a post-merge check (which sees scratch file duplicates) or in Phase 06's Combined.lean (where Lean's elaboration context changes with more declarations in scope). The engine must carefully manage the workspace so that the context each Claude agent sees matches the eventual Combined.lean context as closely as possible. The trusted context sorry-stubs in scratch files (`build_lemma_scratch_text()`) help with this by giving Claude's proof the same namespace environment it will eventually live in.

## Artifact layout (per run)

```
.artifacts/lean_engine/<problem_id>/run_NNN/
  workspace/              # Working Lean project (Orthos/{Statements,Lemmas,Root}.lean)
  prompts/         ch
         # All Claude prompts sent
  claude_raw/             # Raw JSONL output from Claude
  diagnostics/            # Lean error logs
  summaries/              # Per-phase JSON summaries
  integrations/           # inventory.json, preflight.json, integration_usage.json
  lemmas/                 # Per-lemma artifacts (result.json, final_success.lean)
  trusted_context_manifest.json           # Phase 04 output
  trusted_context_semantic_manifest.json  # Phase 05 output
  pinned_signatures.json                  # Phase 03 output (immutable)
  final/                  # Terminal output
    result.json           # Structured result (success or fatal)
    summary.md            # Human-readable summary
    Combined.lean         # Single-file formalization (success only)
    project_manifest.json # File inventory (success only)
```
### Debugging Approach
- **When stuck on a bug, stop speculating and gather real information first**
- If a root cause isn't obvious from reading the code, add logging/instrumentation and ask the user to run it — don't keep re-theorising without data
- Avoid the "wait, actually the real issue is..." loop: form one clear hypothesis, test it, then reassess based on evidence
