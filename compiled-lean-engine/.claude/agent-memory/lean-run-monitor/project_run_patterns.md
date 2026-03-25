---
name: Lean Engine Run Patterns
description: Observed timing, caching behavior, and phase progression patterns from monitored pipeline runs
type: project
---

## Caching Behavior

- Cache key is based on workspace template hash (e.g., `3bef31004916d0b1`).
- Cache HIT skips `lake exe cache get`; cache is stored at `.artifacts/lean_engine/_lake_cache/<hash>/.lake`.
- When cache is WARM (hit), Phase 02 `lake build` still runs to validate — this takes ~231s (3.8 min) even on a warm cache, because it rebuilds the top 88 Mathlib modules not covered by the hardlink cache.
- When Claude triggers `lean_diagnostic_messages` on `Statements.lean` mid-Phase 03, it can trigger `lake setup-file` which spawns a second full incremental Mathlib build. This second build spawns ~10-15 parallel lean worker processes pegging CPU (40-100% each) and consuming ~1-2 GB RAM per worker.

**How to apply:** If you see lake build completing in Phase 02 and then another burst of lean worker processes starting in Phase 03 (from the MCP LSP server calling `lean_diagnostic_messages`), this is normal — it's the LSP re-checking Statements.lean imports. Expect another 5-15 minutes of compilation before Claude gets a response.

## Phase 03 Timing (Putnam A3 problem)

- Phase 02 `lake build`: ~231s on warm cache (hardlink hit)
- Phase 03 statement draft: Claude spawns at 07:00:33, does leandex queries at 07:01:25-07:01:38 (4 queries in ~13s), then silent for ~9 minutes before writing Statements.lean at 07:10:42
- First `lean_diagnostic_messages` call on Statements.lean: 07:10:52, returned 0 errors after 9.14s auto-rebuild
- Second tool call starts at 07:11:19 (unknown tool — log truncated)
- Total run elapsed at first check: ~17 minutes; still in Phase 03

## Problem: Putnam A3 (prob_20260315224206_9f8facc7)

- 6 lemmas: lem_20260315232011_3d908a3a (H_1 Hamiltonian path), lem_20260315232044_137cba8d (prepend digit), lem_20260315232127_37cf2c0e (3-block concatenation), lem_20260315232216_4cebae53 (H_n Hamiltonian path), lem_20260315232302_10f794c0 (coord-sum parity), lem_20260315232346_5554102e (Bob's strategy)
- Root theorem: `orthos_root_prob_20260315224206_9f8facc7` — Bob wins the no-repetition grid game on {0,1,2}^n
- Statements.lean compiled with 0 errors on first diagnostic check

## Secondary lake build triggered by MCP LSP

When Claude calls `lean_diagnostic_messages` on Statements.lean during Phase 03, the LSP spawns `lake setup-file` and then a parallel burst of lean worker processes (observed: ~10 workers running at 40-100% CPU, each consuming ~1-1.7 GB RAM). This is the LSP re-elaborating imports. It is NOT a hung state — it is expected background compilation. Duration unknown but likely 5-15 min.

## Phase 04 Parallel Lemma Behavior (Putnam A3, run_001)

### LSP Tool Timeout Pattern
- Phase 04 runs 6 parallel claude workers (PIDs spawned from the orchestrator).
- MCP tool `lean_hover_info` and `lean_goal` can block for up to 600s (10 min timeout). When this happens, 4 of 6 lemma workers stall simultaneously — their JSONL files stop updating, `stop_reason=None`, and last event stays as `assistant` (awaiting tool result).
- After the 600s MCP timeout fires, the timeout returns a fallback result and the worker resumes immediately. Workers continue as normal with `_wait_for_diagnostics timed out` also observed.
- `lean_local_search` and `lean_loogle` are fast (<1s typically) and do not cause stalls.
- `lean_diagnostic_messages` takes 70-80s when triggering an auto-rebuild of stale imports.

### Progress Indicators at ~13 min into Phase 04
- By 07:47 UTC (13 min into phase04 which started ~07:35), 2 of 6 scratch files had working proofs (no sorry):
  - `lem_20260315232011_3d908a3a` (H_1 Hamiltonian path): proof using `have h1`/`have h2` adj construction + `native_decide`
  - `lem_20260315232044_137cba8d` (prepend digit walk lifting): 34L proof using induction on Walk with lift_adj lemma
- 4 remaining scratch files still had sorry at 13 min mark.
- Trusted context manifest still empty at this point (proofs in scratch not yet verified/committed).

### Rate Limit Events
- All 6 lemma workers contain exactly 1 `rate_limit_event` each with `status=allowed`, `rateLimitType=five_hour`, `overageStatus=rejected`, `resetsAt=2026-03-16T12:00:00+00:00`. These are informational (not blocking) — rate limit is `allowed`, not throttled.

### JSONL Activity Pattern
- Each JSONL is written in chunks as Claude streams. Lines are JSONL events, not one per tool call.
- At 13 min in, lemma JSONLs ranged from 78KB (slower) to 192KB (more active). 25-40 events per file is typical for round_01 at this stage.
- All 6 claude processes stay alive (state `S`, sleeping/waiting) throughout LSP timeouts.

## Problem: Putnam A2 (prob_20260315030804_a862b834)

- 7 lemmas (all analysis/trig): lem_20260315031430_1912f371, lem_20260315031457_d3b391e8, lem_20260315031522_8bfa0dc7, lem_20260315031549_f1fcee00, lem_20260315031613_1645b79b, lem_20260315032024_9b4e1f0b, lem_20260315032055_4bc9cc21
- Problem involves real analysis / trig integrals (D(x)=x(π−x), infimum/supremum of f(x) over (0,π))
- Failed 3 times (run_001 to run_003) with `assembly_invalid` error in Phase 03
- Root cause: assembly plan had step `A1` that did NOT contribute to terminal step `A4`, triggering the connectivity check.
- Fix applied to `assembly_phase.py`: non-contributing steps are now silently skipped rather than treated as fatal (for plans with inter-step dependencies). Flat plans still reject vacuous steps.
- Run 004 was abandoned mid-lake-build (at 07:13, got to 7516/7746 before being killed). Run 005 started fresh at 07:37:41.
- Both run_004 and run_005 workspaces share the same hardlinked .lake (inode 1444500, 120 links) — so run_005's lake build benefits from run_004's partial work.
- run_005 lake build progress at ~3.3 min in: 7415/7746 (95.7%). Top ~330 modules NOT covered by warm cache.
- Estimated: ~5-8 min total for lake build (warm-but-incomplete cache scenario).

### Chronically Stuck Lemma: lem_20260315031430_1912f371 (A2)

FIRST lemma in Phase 04 order. In run_009 (and likely prior runs), failed ALL 5 rounds with `sorry` still in the scratch file — Claude NEVER produced a real proof. Root cause traced via JSONL:

1. Claude reads scratch file, then calls search tools (lean_loogle, lean_leanfinder, lean_local_search, lean_hammer_premise) — ALL return `Lean project path not set. Call a file-based tool first.` MCP LSP was not initialized with the project path.
2. Claude calls `lean_goal` on the file — gets `lean_goal timed out after 600s.` — elaboration spiral on the sorry-containing file with real division over (0,π).
3. After the timeout, a rate-limit event fires: `status=rejected, rateLimitType=five_hour, overageStatus=rejected`. Claude hits the 5-hour API rate limit.
4. Session ends: `You've hit your limit · resets 12pm (UTC)`. Claude stopped without writing a proof.
5. Since Claude never called file-editing tools, scratch stays as the initial sorry-template across all 5 rounds.

**Signature:** `theorem orthos_lem_20260315031430_1912f371 (a b : ℝ) : (∀ x ∈ Icc 0 π, a * (x * (π - x)) ≤ sin x ∧ sin x ≤ b * (x * (π - x))) ↔ ((∀ x ∈ Ioo 0 π, a ≤ sin x / (x * (π - x)) ∧ sin x / (x * (π - x)) ≤ b) ∧ sin 0 = 0 ∧ sin π = 0)`

**Key failure patterns for run_010 to avoid:**
- `Lean project path not set` = MCP LSP not initialized; Claude must call `lean_file_contents` on the scratch file BEFORE any search tools
- `lean_goal timed out after 600s` = elaboration spiral on real division goal; try `lean_term_goal` or `lean_diagnostic_messages` instead
- Rate limit at 12pm UTC = run_009 exhausted the 5-hour budget; run_010 starts fresh (resets at 12pm UTC)

### run_009 Other Lemma Phase 04/05 Results
- lem_20260315031457_d3b391e8: Phase 04 OK (proof found), Phase 05 `bad_statement_translation` (2 rounds, `unparsed response` from Claude)
- All other 5 lemmas: 5 rounds all sorry (likely rate-limit-cascade after lem_20260315031430_1912f371 burned the budget)

### run_010 Status (started 2026-03-16T18:52, ACTIVE as of 18:56 UTC)

- Cache "HIT" for cache key `564125577f7fdbbe` — same hollow cache as A3 (only 2 olean files in shared cache)
- `lake build` COLD BUILD from source; started 18:52:12Z; at 18:56:36Z (4m 24s in): 908/7755 modules (11.7%)
- Estimated: 30-40 min total for cold Mathlib build at current rate
- TWO simultaneous lake builds: A2 (run_010, PID 1038237) AND A3 (run_007, PID 1038361)
- System load avg 51 (extremely high) — both builds competing for CPU

## Warm-but-Incomplete Cache Pattern (A2 run_005)

When a previous run was abandoned mid-lake-build (e.g., run_004 was killed at ~7516/7746), the hardlinked workspace from that run benefits the next run's lake build. The new run sees many modules as already-built, rebuilding only the remaining modules. At 07:41 (3.3 min in), run_005 was at 7415/7746 — the first logged module (7370) took 103s to appear, suggesting it was building the hardest modules first. Total expected duration: 5-8 minutes.

## Run Pair: A3 run_002 + A4 run_001 (2026-03-16, concurrent monitoring)

### Caching
- Both runs hit the same warm cache key `564125577f7fdbbe`
- `lake build` completed in 9.7s (A3) and 7.9s (A4) — genuine warm cache hit
- A3 cache update WARNING after Phase 02: `No such file or directory: _lake_cache/564125577f7fdbbe/.lake` — race condition when both runs try to write the same cache simultaneously. Non-fatal.

### Phase 03 Timing
- Both Phase 03 Claude processes started at 21:17 UTC (A3) and 21:17 UTC (A4)
- A4 was significantly faster in Phase 03 (matrix/linear algebra domain = more Mathlib coverage):
  - A4: Searched Mathlib loogle/local_search 8 times, wrote Statements.lean at 21:24 (~7 min), repair round fixed `Matrix.dotProduct` -> `dotProduct` in 60s, Phase 03 complete at ~21:33 (16 min)
  - A3: Searched `SimpleGraph.IsHamiltonianPath`, `SimpleGraph.Walk`, `Hamiltonian` (graph theory) — all returned empty/sparse results; still generating response at 21:41 (>24 min in Phase 03)
- Graph theory formalization takes longer because Mathlib's Hamiltonian path API is sparse; Claude must do more searching

### A4 run_001: FATAL - assembly_invalid
- Phase 03 Statement Phase: OK (Statements.lean compiled clean after fixing dotProduct)
- Assembly Precheck: FATAL — `Unknown identifier 'orthos_root_prob_20260315031656_3725bb56'` in AssemblyCheck.lean line 33
- Root cause: The assembly precheck's `let _ := orthos_root_prob_20260315031656_3725bb56` triggered an "Unknown identifier" error even though Statements.lean contains this axiom. The `IsLeast {...} 3` type may cause elaboration issues when referenced as a term in the assembly skeleton.
- Total cost so far: $1.44 (Phase 03 draft) + $0.33 (repair) = $1.77
- Phase 03 statement_phase elapsed: 1051s (17.5 min)
- All 7 lemmas: 0 compiled (never reached Phase 04)

### A3 run_002: IN PROGRESS (Phase 03)
- At 24 min mark: Claude searching for Hamiltonian path Mathlib APIs, nothing found
- Graph theory problem (ternary grid game); 6 lemmas including Hamiltonian path construction
- Loogle/local_search returning empty for Hamiltonian, SimpleGraph.Walk queries

### Known Recurring Issue: Matrix.dotProduct
- When formalizing A4 (matrix commutativity), Claude used `Matrix.dotProduct` which doesn't exist in Mathlib — correct name is `dotProduct` (top-level). This required a repair round. Record this for future A4 or similar matrix problems.

## Problem: Putnam A4 (prob_20260315031656_3725bb56)

### A4 run_008: ALL 7 LEMMAS OK — Phase 05 started, not yet complete (2026-03-23)

- 7 lemmas (matrix commutativity, 2025 cyclic adjacency graph): all formalized with `status=ok`, `has_sorry=None`, zero round-failure, no private helpers in final_success.lean
- Lemma round counts: a6164cbd (1), 0386ddd1 (3), ec8ca1a1 (2), 1d533fda (2), 8f502a27 (1), 391235ec (2), da3e9cfe (1)
- **Max rounds was 3** (0386ddd1, a 2x2 non-scalar matrix centralizer lemma — the hardest algebraic structure lemma)
- Phase 02 `lake build` completed in 13.8s (warm cache)
- Phase 03 OK: 1 draft + 1 repair round. Assembly precheck OK (19.9s)
- Phase 04 OK: All 7 lemmas. Phase 04 summary lists them under `lemma_results` list (not `lemmas` dict). `lemma_order` field present.
- **Lemmas.lean is clean**: 7 theorems, 351 lines, no `sorry`, no `private`, no helper defs, no orphan scratch files in workspace/Orthos/. Workspace Orthos/ contains only Lemmas.lean, Root.lean, Statements.lean.
- Phase 05 (semantic): STARTED but incomplete. Only 1 artifact found: `semantic/lemma_lem_20260315032510_a6164cbd/equivalence_prompt_round_01.md` — the equivalence prompt was written but no `result.json` exists for any lemma. Phase05 summary not present. Run appears to have stopped here (no final/ directory, no phase05/06 summaries).
- Semantic dir mtime: 2026-03-23T09:36:21Z = same as phase04_summary.json mtime. Run stopped at start of Phase 05.
- **Extraction bug check (the specific question asked):** No lemmas used private helpers in their scratch files that were lost during extraction. All 7 final_success.lean files begin directly with the target theorem. Line delta between scratch and final: 0386ddd1 (80→71), 8f502a27 (165→153), da3e9cfe (51→37). The lost lines in da3e9cfe are the sorry-stub trusted context preamble (expected — not private helpers). Extraction was clean.

**To continue:** run `lean-engine run --resume .artifacts/lean_engine/a4__prob_20260315031656_3725bb56/run_008` — Phase 05 will re-run from the beginning (no per-lemma semantic results were committed).

## Problem: Putnam A5 (a5__prob_20260316042258_733e5db0)

### A5 run_012: Phase 04 ACTIVE — lem_d03c1e2a on round 03, lem_45b98c0b succeeded (2026-03-24T19:28 UTC)

- run_011 ended fatal: `tactic_failure` in Phase 05. 6 of 7 lemmas semantically rejected. `lem_20260316042657_d03c1e2a` was the failing lemma.
- run_012 started 2026-03-24T18:08 UTC. Phase 02 `lake build`: 2.55s (warm cache). Phase 03: OK.
- Only 2 of 7 lemmas are being processed in Phase 04 so far (`lem_d03c1e2a` and `lem_45b98c0b`). The other 5 have not been dispatched yet.
- **lem_45b98c0b (consecutive peaks are impossible):** SUCCEEDED in round 01 (678s). final_success.lean present. diagnostics_round_01.txt: clean (31 bytes — one line, no errors).
- **lem_d03c1e2a (fCount recurrence over peaks):** HARD LEMMA — still in progress at round 03 (started 18:49:39 UTC).
  - Round 01: 711s, 18 errors in scratch, `false_negative=true` (LSP said errors but candidate_source=tool_update suggests Claude thought it was clean), `progress_made=true`
  - Round 02: 817s, started with 18 errors, ended CLEAN (0 errors). `candidate_source=workspace_fallback`. `diagnostic_status=clean`. This is the most recent committed round.
  - Round 03: IN PROGRESS since 18:49:39 UTC. JSONL last updated 19:24:55 UTC (35 min ago). 98 JSONL events written. Lean worker (PID 1419157) still alive (0.5% CPU, 4.7% MEM = ~3GB, running 32 min). Scratch file last written at 19:28:14 UTC (15 sec ago = actively being worked on by Claude).
  - Round 02 diagnostics: `policy_violation: disallowed token 'admit'` — meaning the workspace_fallback candidate contained `admit` and was rejected. Engine then starts round 03 to get a clean candidate.
  - MCP log: `lean_diagnostic_messages` called at 19:18:04 UTC, returned 27 items (output truncated from 14,613 to 10,000 chars). Lean LSP snippet workers being terminated (normal cleanup). No errors in MCP server itself.
- **Trusted context:** 0 entries (empty manifest). Neither lemma's proof has been committed to trusted context yet.
- **Remaining 5 lemmas (not yet dispatched):** lem_f3c76a13, lem_48b7a7f1, lem_1b4343b3, lem_552e1b2d, lem_cbbf09d4 — all present in pinned_signatures.json but no lemma subdirectory yet.
- **lemma_workers=3, max_repair_rounds=3, max_attempts_per_lemma=5** — round 03 is the last repair round for this attempt.
- **Diagnosis:** HEALTHY but at a critical juncture. Round 03 is the final repair round; if it ends with errors or `admit`, the engine will start attempt 2 (new Claude session). lem_45b98c0b is already done. The big unknown is whether lem_d03c1e2a can produce a clean proof this round.

### A5 run_005: Phase 04 BLOCKED — provider_quota_exhausted (2026-03-23)

- 7 lemmas (sign sequences, peak counting, combinatorics): lem_d03c1e2a, lem_45b98c0b, lem_f3c76a13, lem_48b7a7f1, lem_1b4343b3, lem_552e1b2d, lem_cbbf09d4
- Root: `root_prob_20260316042258_733e5db0` — signCount ≤ altCount and equality iff s = altPlus or altMinus
- Phase 02: WARM cache — `lake build` completed in 3.79s
- Phase 03: OK — 1 draft attempt. Assembly precheck OK (4.84s). 7 pinned signatures written.
- Phase 04: BLOCKED. Round 01 attempted for all 4 initially dispatched lemmas in parallel. First batch (d03c1e2a, 45b98c0b) both failed with `provider_quota_exhausted` (wall: 1803s and 309s). Second batch (f3c76a13, 48b7a7f1) was dispatched at 21:35:17 UTC; their Claude streams had only 7 events (started, read scratch file, hit `rate_limit_event` with status=allowed and `resetsAt=2026-03-24T00:00:00Z`).
- Quota resets at 2026-03-24T00:00:00 UTC (midnight). Run is blocked waiting for quota.
- Pipeline process (PID 812174) is alive (state: S sleeping, 3 threads), waiting for Claude child processes (PIDs 889151, 889152).
- Scratch files present for all 4 dispatched lemmas. No result.json for any lemma. Lemmas 5-7 (lem_1b4343b3, lem_552e1b2d, lem_cbbf09d4) not yet dispatched.

**Key pattern:** Same `provider_quota_exhausted` pattern as A6 run_005. First batch took 309-1803s before hitting quota.

## Problem: Putnam A6 (a6__prob_20260315033724_f67141d9)

### A6 run_005: Phase 04 IN PROGRESS — provider_quota_exhausted pattern (2026-03-23)

- 6 lemmas: lem_20260315051602_d9ab3a00, lem_20260315051628_04c5b961, lem_20260315051701_131fbfcc, lem_20260315051727_f1150c1e, lem_20260315051812_eca9365d, lem_20260315051850_8c352334
- Phase 02: WARM cache hit — `lake build` completed in 3.88s (7747 jobs)
- Phase 03: OK — 1 draft + 1 repair. Assembly precheck OK (3.83s). Pinned signatures written.
- Phase 04: IN PROGRESS at time of check (21:42 UTC). First 5 lemmas ALL FAILED with `provider_quota_exhausted`. 6th lemma (8c352334) currently active (round_01, Claude stream last updated 21:41:50, still streaming).
- Round durations for failed lemmas: 346s, 903s, 2429s, 3205s, 2997s — ALL ended with `provider_quota_exhausted`, `progress_made=true`, `candidate_source=none`, `error_count_before=0`. This means Claude made progress in each attempt but ran out of quota before completing the proof.
- The 6th lemma's prompt was written at 21:39 and Claude is currently streaming a response.
- No result.json for any lemma (all failed/in-progress — no successes yet).

**Key pattern:** `provider_quota_exhausted` is distinct from `rate_limit_event` (which is informational). This kills the proof attempt outright. All 5 completed lemmas failed this way — Claude was working on proofs but the org quota was exhausted mid-attempt.

**Implication:** If the 6th lemma also fails with quota exhaustion, all 6 lemmas will need to be retried in a new run.

## Problem: Putnam B1 (b1__prob_20260317012640_e1d4625c)

### B1 run_007: Phase 04 ACTIVE — 1 of 5 lemmas in progress (2026-03-23)

- 5 lemmas (circumcenter coloring, Euclidean plane): lem_23457_af92e6d8, lem_23354_74d60854, lem_23552_38b8d43a, lem_23427_9860447b, lem_23625_951b418d
- Root: `root_prob_20260317012640_e1d4625c` — any 2-coloring of R^2 with circumcenter-color closure is monochromatic
- Phase 02: WARM cache — `lake build` completed in 3.24s
- Phase 03: OK — 1 draft (631s, 8 turns, $2.46). Assembly precheck OK (3.99s). 5 pinned signatures written.
- Phase 04: ACTIVE. Only 1 lemma dispatched so far (lem_23457_af92e6d8 — 5 circumcenter-pair construction).
  - round_01 started at ~21:23 UTC; JSONL last updated at 21:35 UTC (last event: context-compact summary injected, Claude continuing after context window hit).
  - As of 21:44 UTC: Claude process (PID 886581) alive, state S (sleeping, waiting for API response).
  - MCP log last activity: 21:23:47 (tool discovery). MCP LSP has been inactive since Phase 03 LSP check.
  - Only 3 tool calls so far in phase04 round_01: Read, Read, ToolSearch (ToolSearch returned empty — deferred tools not yet loaded).
  - 0 proofs succeeded, 0 trusted context entries, 4 remaining lemmas not yet dispatched.
- lemma_workers=3 but only 1 worker is active — likely sequential dispatch or others pending.

**Note:** B1 is significantly harder than A-series (circumcenter geometry in Euclidean plane). Expect longer proof search per lemma.

## Hollow Cache Scenario (A3 run_007)

**Problem:** Cache entry `564125577f7fdbbe` was reported as a HIT by workspace.py, but the cache only contains 2 oleans (lakefile configs) plus source package checkouts — no compiled Mathlib oleans. This causes what appears to be a warm-cache scenario but is actually a COLD BUILD from source.

**Observed:** run_007 started 18:52:13 UTC. At 3.3 minutes in: 738/7755 modules built (9.5%), rate ~3.7 modules/s. **Estimated completion: ~31 more minutes** (so ~35 min total from cold).

**Why hollow cache:** The cache hash `564125577f7fdbbe` matches a `.lake/` directory that was created by `lake exe cache get` which only downloaded source packages, not compiled oleans. The actual olean compilation is happening in the run_007 workspace itself, building all 7755 modules from scratch.

**Contrast:** Reference run `prob_20260315033724_f67141d9/run_005` with warm cache had `lake build` complete in 3.52s.

**Implication:** Pipeline blocks on `lake build` before Phase 03 can begin. All Orthos files still at Phase 02 placeholders as of 18:55 UTC.

**Also:** run_006 for this same problem failed with `error: no such file or directory` for `AssemblyCheck.lean` — this is a lakefile glob issue (the lakefile doesn't include AssemblyCheck in its globs, but some code tried to build it). run_007 appears to be the restart after that fix.
