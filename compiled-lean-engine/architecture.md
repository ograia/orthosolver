# Lean Engine Architecture

> Last updated: 2026-03-25

## What This System Does

This is an NL-proof-to-Lean compiler. It takes structured natural-language proofs and compiles them into Lean 4 code that actually type-checks. If the Lean compiler accepts the output, the proof is mathematically verified.

The system is NOT a theorem prover. It formalizes proofs that already exist in natural language.

---

## How Lean Verification Works

Lean 4 is a proof assistant — a compiler that checks mathematical proofs. There are two ways to run it:

### Cold check: `lake env lean <file>`
- Spawns a fresh Lean process
- Loads all imports from pre-compiled `.olean` files (~23K files for Mathlib, ~30s to load)
- Type-checks the file from scratch
- Exits
- **Authoritative** — always reflects current state of all source files and oleans
- Used by our engine as the acceptance gate

### Hot check: `lake serve` (LSP server)
- Long-running process, keeps Mathlib loaded in memory
- Receives file edits via Language Server Protocol (LSP)
- Returns diagnostics (errors/warnings) in seconds instead of 30s
- **Can have stale state** — if oleans on disk change, the in-memory server may not reflect them
- Used by Claude interactively during proof development (via our MCP server)

### The fundamental tension

Claude uses the fast LSP path for interactive proof writing. The engine validates with the slow authoritative path. When they disagree — the LSP says "no errors" but `lake env lean` finds real errors — Claude wastes a repair round believing it succeeded.

---

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Pipeline (phase07.py)                   │
│                                                                 │
│  Phase 01 ─→ Phase 02 ─→ Phase 03 ─→ Phase 04 ─→ Phase 05 ─→ Phase 06  │
│  Normalize   Runtime    Statements   Lemma       Semantic    Root        │
│              Setup      + Assembly   Proofs      Guards      Assembly    │
│                                        │                                │
│                                   ┌────┴────┐                           │
│                                   │ Workers  │ (parallel, 1 per lemma)  │
│                                   └────┬────┘                           │
│                                        │                                │
│                              ┌─────────┴──────────┐                     │
│                              │  Claude subprocess  │                    │
│                              │  (claude CLI)       │                    │
│                              │  cwd = workspace/   │                    │
│                              └─────────┬──────────┘                     │
│                                        │ reads .mcp.json                │
│                              ┌─────────┴──────────┐                     │
│                              │  MCP Server         │                    │
│                              │  (lean-lsp-mcp)     │                    │
│                              └─────────┬──────────┘                     │
│                                        │                                │
│                              ┌─────────┴──────────┐                     │
│                              │  lake serve (LSP)   │                    │
│                              │  Mathlib in memory  │                    │
│                              └────────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phase Pipeline

### Phase 01 — Input Normalization
- **Module:** `normalize.py`
- **Input:** Raw NL proof artifact (JSON)
- **Output:** `NormalizedProblemBundle` — structured representation with problem statement, lemma decomposition, proof sketches
- **Duration:** < 1s

### Phase 02 — Runtime Setup
- **Module:** `workspace.py`, `lean_checks.py`
- **What it does:**
  1. Creates workspace from `templates/lean_project/` (copies template)
  2. Links `.lake/` cache (pre-compiled Mathlib, ~12GB, via `cp -a`)
  3. Writes `.mcp.json` into workspace (tells Claude how to launch MCP server)
  4. Runs integration preflight (inventory + can-run checks for `lean-lsp-mcp-main`)
  5. Runs `lake build` to verify workspace compiles
  6. Updates `.lake/` cache on success
- **Duration:** 30-60s (mostly the cache copy)

### Phase 03 — Statement Formalization
- **Module:** `phase03.py`, `statement_phase.py`, `assembly_phase.py`
- **What it does:**
  1. Sends NL problem to Claude to formalize as Lean `theorem`/`axiom` signatures in `Orthos/Statements.lean`
  2. Repair loop: `lake env lean Statements.lean` → if errors, send diagnostics back to Claude → repeat (max 2 repair rounds)
  3. Extracts and pins all declaration signatures (`pinned_signatures.json`) — immutable from here
  4. Rebuilds `Orthos.Statements` olean via `lake build Orthos.Statements` (so Phase 04 scratch files can `import Orthos.Statements`)
  5. Assembly precheck: generates self-contained `AssemblyCheck.lean` that inlines all statement content (no cross-file imports), verifies the proof plan is composable
- **Key output:** `pinned_signatures.json` — frozen type signatures for every declaration
- **Duration:** 5-20 min (mostly Claude + Lean compilation)

### Phase 04 — Lemma Proving
- **Module:** `phase04.py`, `lemma_phase.py`
- **What it does:**
  1. Prepares workspace: neutralizes Statements.lean (comments out axioms), resets Lemmas.lean, rebuilds oleans
  2. For each lemma (parallel or sequential):
     - Creates scratch file (`Orthos/Scratch_lemma_xxx.lean`) with sorry-stubbed trusted context
     - Sends prompt to Claude subprocess with pinned signature + NL proof
     - Claude writes proof, checks via MCP `lean_diagnostic_messages`, iterates
     - Engine extracts candidate, runs progress guard (no sorry, signature match)
    - Engine runs an independent `lake env lean` cold check on the scratch file before acceptance
     - On success: deletes scratch file, merges proof into `Orthos/Lemmas.lean`
     - On failure: carries diagnostics + search discoveries to next round
  3. Max 5 rounds per lemma (default)
- **Key output:** `Orthos/Lemmas.lean` with proven theorem declarations, `trusted_context_manifest.json`
- **Duration:** 10 min - 3 hrs (depends on lemma count and difficulty)

### Phase 05 — Semantic Guards
- **Module:** `semantic_phase.py`
- **What it does:**
  1. For each proven lemma: sends Lean formalization + NL statement to Claude as equivalence judge
  2. Claude returns structured verdict: `{match: "yes"|"no"|"unknown", explanation, drift, classification_hint}`
  3. For unproven lemmas: classifies the failure (tactic_failure, major_proof_gap, false_lemma_suspected, bad_statement_translation)
  4. Fatal classes (`major_proof_gap`, `false_lemma_suspected`, `bad_statement_translation`) abort the pipeline
- **Key output:** `trusted_context_semantic_manifest.json`
- **Duration:** 1-5 min

### Phase 06 — Root Assembly
- **Module:** `phase06.py`
- **What it does:**
  1. Claude writes `Orthos/Root.lean` — the root theorem that composes all lemmas into the final proof
  2. Engine generates `Combined.lean`: single self-contained file with only `import Mathlib`, containing all statements + lemmas + root
  3. Final gate: `lake env lean Combined.lean` must pass with zero errors and zero sorry/admit
- **Key output:** `final/Combined.lean` — the deliverable
- **Duration:** 2-10 min

### Phase 07 — Pipeline Orchestration
- **Module:** `phase07.py`
- Runs phases 01-06 in sequence with timing, status tracking, integration inventory/preflight, pause/resume support (SIGUSR1), and stale process cleanup

### Phase 08 — HTTP Service
- **Module:** `service/app.py`, `service/jobs.py`, `service/store.py`
- POST `/v1/jobs`, GET `/v1/jobs/<id>`, GET `/v1/health`
- SQLite-backed job persistence

---

## Claude Subprocess System

### How Claude Is Invoked (`claude_runner.py`)

Claude is run as the `claude` CLI binary via `subprocess.Popen`:
```
claude -p --verbose --output-format stream-json --model <model>
  --permission-mode bypassPermissions
  --allowedTools "mcp,Bash,Read,Glob,Grep,Write,Edit,Agent"
  --max-turns 25
```

- **Prompt** is fed via stdin (not positional arg)
- **Working directory** is the workspace (contains `.mcp.json`)
- **Output** streams as JSONL to a file in `claude_raw/`
- **Process group** isolation via `start_new_session=True` for clean kill

### Stall Detection

Every 10 seconds, the engine checks Claude's JSONL output file:
- If output grew → reset timer. If last event is `tool_use` → mark `waiting_on_tool = True`
- If output did NOT grow AND not waiting on tool AND >900s since last activity → **SIGKILL the process group**
- Stall detection is **suppressed** while Claude is waiting for an MCP response

### Turn Limits
- Round 1: `max_turns=25`
- Rounds 2+: `max_turns=40` (repair rounds need more iteration)

### Candidate Extraction (`claude_candidate_selection.py`)

After Claude returns, the engine extracts the Lean proof from multiple sources (priority order):
1. **`tool_update`** — last file write captured from stream-json trace (most reliable)
2. **`result_text`** — final result text from Claude
3. **`assistant_text`** — concatenated assistant messages
4. **`workspace_fallback`** — read actual file on disk (catches MCP-mediated writes)

Each candidate is scored by `_lean_likeness_score()` (+3 for Lean keywords, +1 for comments/symbols, -2 for prose/markdown). Must score > 0. Then validated by a stage validator (correct declaration name, exactly one declaration block).

### Progress Guard (`lemma_phase.py:progress_guard_error()`)

Before running `lake env lean`, the engine checks:
1. Target declaration exists exactly once
2. No `sorry` or `admit` in the target declaration body
3. No unrelated declarations added
4. Pinned signature hasn't drifted

### Verification Gate

After progress guard passes: `check_lean_file()` runs `lake env lean Orthos/Scratch_lemma_xxx.lean` (120s timeout). This is a cold, authoritative check. If it fails, diagnostics go to the next round.

---

## MCP Server Architecture (`lean-lsp-mcp-main/`)

### What It Is

A Python MCP server that bridges Claude to the Lean LSP. Runs as a subprocess launched by the Claude CLI when it reads `.mcp.json`.

### Communication Chain
```
Claude ──(MCP stdio)──→ lean-lsp-mcp (Python) ──→ leanclient lib ──→ lake serve ──→ Lean LSP
```

### Single Lean Process

One `lake serve` process per MCP server instance. All tool calls share it. Mathlib loads into memory on first file open and stays resident. If the client is closed and recreated (e.g., after `lean_build`), Mathlib must be re-loaded.

### Key MCP Tools

| Tool | What it does | Timeout |
|------|-------------|---------|
| `lean_diagnostic_messages` | Get all errors/warnings for a file | 360s |
| `lean_goal` | Get proof state at a position | 600s |
| `lean_hover_info` | Get type/doc info at a position | 600s |
| `lean_completions` | Code completions | 120s |
| `lean_build` | Run `lake cache get` + `lake build`, restart LSP | 900s |
| `lean_local_search` | Ripgrep declaration search (fast, local) | 60s |
| `lean_loogle` | Search by name/type/subexpression (external API) | 60s |
| `lean_leandex` | Semantic search (external API) | 60s |
| `lean_leanfinder` | Semantic Mathlib search (external API) | 60s |
| `lean_state_search` | Goal-based theorem search (external API) | 120s |
| `lean_multi_attempt` | Try multiple code snippets at a line | 900s |
| `lean_run_code` | Run self-contained code snippet | 180s |

### Rate Limits (per MCP server instance)

| Tool | Limit | Reason |
|------|-------|--------|
| `lean_loogle` | 3 req / 30s | External API |
| `lean_leanfinder` | 10 req / 30s | External API |
| `lean_state_search` | 3 req / 30s | External API |
| `lean_hammer_premise` | 3 req / 30s | External API |
| `lean_leandex` | **none** (commented out) | — |

Rate limits are implemented as sliding-window counters in a Python decorator. They return a string error message (not an exception) when exceeded, so Claude sees it as tool output.

### How `lean_diagnostic_messages` Works (the critical tool)

1. Opens the file in the LSP with **`dependencyBuildMode: "never"`** — meaning: don't rebuild imports, use cached oleans
2. Waits for diagnostics with a 360s inactivity timeout (no new diagnostics for 360s = give up)
3. Checks for infrastructure failure patterns: `"lake setup-file"`, `"Build failed"`, `"exit code 1"`, `"fatalError"`, build progress patterns like `[N/M] Building...`
4. If infrastructure failure: returns `[INFRASTRUCTURE ERROR]` tag telling Claude to retry
5. If timeout: returns timeout message, closes and re-opens file
6. Truncates output to 10,000 chars max
7. Returns formatted diagnostic list

### Known False Negative Mechanisms

The MCP can return "no errors" when `lake env lean` finds real errors. Known causes:

| Mechanism | Description | Likelihood |
|-----------|-------------|-----------|
| **Stale oleans** | `dependencyBuildMode: "never"` means LSP uses cached oleans. If `Statements.lean` was rewritten but the olean wasn't fully rebuilt, LSP checks against old signatures. | **High** — this was the primary cause in run_013 |
| **Inactivity timeout** | Under CPU pressure, if Lean doesn't emit diagnostics for 360s, the tool returns empty list (= "no errors"). | **Medium** — happens with 6+ parallel workers |
| **Race conditions** | File written to disk + immediately opened in LSP. The LSP might have cached the old content. | **Low** — the LSP re-reads from disk on open |
| **Partial elaboration** | Lean may still be processing when timeout fires, returning incomplete diagnostics. | **Medium** |

---

## Workspace Architecture

### Template (`templates/lean_project/`)

```
lean-toolchain          # Pins Lean version (e.g., leanprover/lean4:v4.26.0)
lakefile.toml           # Single lean_lib "Orthos" with explicit globs
lake-manifest.json      # Pinned Mathlib revision
Orthos/
  Statements.lean       # Phase 03 writes theorem/axiom signatures here
  Lemmas.lean           # Phase 04 accumulates proven declarations here
  Root.lean             # Phase 06 writes the root theorem here
```

The `lakefile.toml` uses explicit `globs` restricting the Orthos library to only `Statements.lean`, `Lemmas.lean`, `Root.lean`. This prevents Lake from auto-discovering scratch files.

### Scratch File Lifecycle

During Phase 04, each lemma gets a scratch file: `Orthos/Scratch_lemma_xxx.lean`. These files:
- Import `Mathlib` and `Orthos.Statements`
- Include sorry-stubbed trusted context (previously-proven lemmas)
- Have the target declaration with `sorry` placeholder
- Are deleted after successful proof + merge into `Lemmas.lean`

### .lake/ Cache System

- **Cache key:** SHA256 of `lean-toolchain` + `lakefile.toml` + `lake-manifest.json` (16 hex chars)
- **Location:** `.artifacts/lean_engine/_lake_cache/<hash>/.lake/` (~12GB, 23K oleans)
- **Copy strategy:** `cp -a` (full copy, not hardlinks — prevents SIGBUS from concurrent `lake serve` processes)
- **Locking:** `fcntl.flock` — shared lock for reads, exclusive lock for writes
- **Post-copy fixup:** `git checkout -- .` in each package dir (restores symlinks), then touch all `.olean` files (ensures mtime > source mtime)

---

## Integration Repos

### Currently Active

| Repo | What it does | When it's used |
|------|-------------|----------------|
| **lean-lsp-mcp-main** | MCP server bridging Claude to Lean LSP | Every run — Claude's primary tool for proof checking |
| **lean4-skills-main** | Prompt references + script helpers (`solver_cascade`, `parse_lean_errors`, `check_axioms`) | References are loaded in statement/lemma/root prompting. Script helpers are invoked in core phases when configured. |

Current integration inventory/preflight is implemented for `lean-lsp-mcp-main` via `integrations/lean_lsp_mcp.py`. `lean4-skills-main` is consumed by direct helper modules rather than an `integrations/*` adapter. Other repos present in the workspace are not active runtime dependencies of the main lean-engine pipeline.

This is the full active integration scope for the main engine runtime.

---

## Configurable Parameters

### Claude Config
| Parameter | Default | CLI flag |
|-----------|---------|----------|
| `model` | `claude-sonnet-4-6` | `--model` |
| `fallback_model` | `claude-sonnet-4-6` | `--fallback-model` |
| `timeout_seconds` | 2700 | `--timeout` |
| `stall_timeout_seconds` | 2700 | (config file only) |

### Phase Limits
| Parameter | Default | CLI flag |
|-----------|---------|----------|
| `max_repair_rounds` (Phase 03) | 2 | `--max-repair-rounds` |
| `max_attempts_per_lemma` (Phase 04) | 5 | (config/code) |
| `parallel_lemmas` | False | `--parallel-lemmas` |
| `lemma_workers` | 1 (auto if parallel) | `--lemma-workers` |
| `max_semantic_repairs` (Phase 05) | 1 | `--max-semantic-repairs` |
| `max_root_attempts` (Phase 06) | 4 | `--max-root-attempts` |

### Lean Config
| Parameter | Default | CLI flag |
|-----------|---------|----------|
| `lake_jobs` | 0 (= nproc) | (config file only) |
| `imports` | `("Mathlib",)` | (config file only) |

---

## Artifact Layout (per run)

```
.artifacts/lean_engine/<problem_id>/run_NNN/
  workspace/                    # Working Lean project
    .mcp.json                   # MCP server config for Claude
    Orthos/Statements.lean      # Pinned signatures
    Orthos/Lemmas.lean          # Accumulated proofs
    Orthos/Root.lean            # Root theorem
    Combined.lean               # Final self-contained output
  normalized_problem.json       # Phase 01 output
  runtime_config.json           # Frozen config
  pinned_signatures.json        # Phase 03 output (immutable)
  assembly_precheck_summary.json
  trusted_context_manifest.json           # Phase 04 output
  trusted_context_semantic_manifest.json  # Phase 05 output
  prompts/                      # All prompts sent to Claude
  claude_raw/                   # Raw JSONL output from Claude
  diagnostics/                  # Lean error logs
  summaries/                    # Per-phase JSON summaries
  lemmas/                       # Per-lemma artifacts
    lemma_<id>/
      result.json               # Per-lemma result
      final_success.lean        # Extracted proof (on success)
      prompt_round_NN.md        # Round prompts
      scratch_round_NN.lean     # Scratch file snapshots
      diagnostics_round_NN.txt  # Lean error output
  semantic/                     # Phase 05 artifacts
  integrations/                 # Inventory, preflight, usage
  final/
    result.json                 # Structured result (success or fatal)
    summary.md                  # Human-readable summary
    Combined.lean               # Final deliverable (success only)
```

---

## Error Classification

| Class | Phase | Meaning |
|-------|-------|---------|
| `malformed_input_artifact` | 01 | Input validation failure |
| `statement_translation_failure` | 03 | Can't formalize statements |
| `assembly_invalid` | 03/06 | Assembly plan unsatisfiable |
| `tactic_failure` | 04 | Lean tactic didn't close the goal |
| `type_mismatch` | 04 | Type error in proof term |
| `major_proof_gap` | 04/05 | Real mathematical gap in NL proof |
| `false_lemma_suspected` | 05 | Semantic rejection — statement appears false |
| `bad_statement_translation` | 05 | Lean statement doesn't match NL intent |
| `assembly_composition_failure` | 06 | Root theorem can't compose lemmas |
| `unknown_fatal` | any | Catch-all |

Fatal classes in Phase 05 (`major_proof_gap`, `false_lemma_suspected`, `bad_statement_translation`) abort the pipeline immediately.
