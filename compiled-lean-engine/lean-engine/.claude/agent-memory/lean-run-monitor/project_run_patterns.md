---
name: lean-engine run patterns and timing
description: Observed timing, caching behavior, and common failure modes for lean-engine pipeline runs
type: project
---

## Run Timing Observations (from run_003 on a1.json Putnam problem)

**Phase 02 (workspace prep):** ~5s with cache hit (warm lake cache at `_lake_cache/3bef31004916d0b1/.lake`)

**Phase 03 (statement formalization):**
- Claude draft call: ~6.5 min (100s API call = 8 turns, workspace exploration + Write)
- Common syntax error on first attempt: `∏ i in Finset.range r, ...` must be `(Finset.range r).prod fun i => ...` in Lean 4.26.0
- Repair loop takes 1 additional pass (~1 min API call) to fix `in` token error
- Assembly check: **466 seconds** for cold-ish Mathlib cache (only 7365/7744 jobs were cached; needed to build FourierTransformDeriv, MFDeriv/NormedSpace, ContDiff.Basic, etc.)
- In fully warm reference run (run_005): assembly check took 13s
- **Total phase03 time:** ~15 minutes (dominated by assembly check Mathlib build)

**Phase 04 (lemma formalization, --parallel-lemmas):**
- All 7 lemmas launched in parallel at same timestamp
- Round 1 is scaffolding (creates sorry stubs), actual proof work begins in round 2
- Easy lemma (c6218886 = gcd=1 ↔ Coprime): solved in r1 round 1 with `Iff.rfl`
- Medium lemmas take 1-2 rounds, 1-3 minutes each
- Hard lemmas can take many rounds: `7360bff1` took 5 rounds over ~20 minutes
- Hardest lemma `8bcb0ad7` (fraction reduction + Euclid's lemma) took 103 JSONL lines in r1
- `124a8204` (exact difference identity with Int division) took 230+ lines in r2
- `import Orthos.Lemmas` in scratch files causes "already declared" conflict when promoted — repair rounds fix this by removing the import

**Why:** Phase 03 assembly check builds all of Mathlib that wasn't cached from workspace template init. This happens once per new workspace copy.

**How to apply:** Expect 10-15 min for phase 03 total on new runs. If assembly check is hanging >10 min, check `lake build Orthos.AssemblyCheck` process and see what Mathlib modules it's building. This is normal, not a bug.

## Caching Behavior

- Global lake cache at: `.artifacts/lean_engine/_lake_cache/3bef31004916d0b1/.lake` (12G)
- Phase 02 does a `lake build` (hits cache in 5s)
- Phase 03 assembly check triggers additional Mathlib modules not in cache (~466s for run_003)
- After first run, subsequent runs should be faster for assembly check (cache updated)

## Common Phase 04 Lemma Issues

1. `import Orthos.Lemmas` in scratch files → "already declared" when promoted → repair round needed
2. `∏ i in` vs `(Finset.range r).prod fun i =>` syntax variants keep breaking
3. `Nat.prime_two.coprime_iff_not_dvd` naming not always right across rounds
4. Hard arithmetic lemmas about Nat.gcd, divisibility, and Coprime require many MCP tool searches
5. `Int.sub_ediv_of_dvd_sub` is the key lemma for integer division identity proofs

## Phase Timing Summary (run_003 a1.json)

| Phase | Duration | Status | Notes |
|-------|----------|--------|-------|
| phase01_normalize | 0.001s | ok | |
| phase02_runtime | 13.7s | ok | cache hit |
| phase03_statement | 931s (15.5min) | ok | 466s = assembly check Mathlib build |
| phase04_lemmas | 5319s (88.6min) | ok | 7 lemmas parallel, 5 ok, 2 failed |
| phase05_semantic | 574s (9.6min) | fatal | false_lemma_suspected on 124a8204 |
| **Total** | **6839s (114min)** | **fatal** | |

## Phase 04 Lemma Failure Analysis

Two lemmas failed (exhausted 5-round budget):
1. `lem_20260315032944_124a8204` — exact difference identity with Int division
   - error_class: `false_lemma_suspected` (signature drift: pinned sig kept truncating)
   - Root cause: progress guard rejected candidate because declaration header "drifted" from pinned signature — the proof tried rewriting the `let d` binding in a way that changed the visible signature
   - Key issue: `Int.sub_ediv_of_dvd_sub` doesn't exist; agent searched 419 lines in r2 for alternatives; `Int.ediv_mul_cancel` approach works but the pinned-signature check kept failing
2. `lem_20260315033109_7360bff1` — odd products coprimality
   - error_class: `missing_library_fact`
   - Root cause: `Coprime.pow_right` / `Nat.Coprime.pow_right` naming issues + `import Orthos.Lemmas` conflict across 5 rounds

## Problem Characteristics: a1.json (Putnam)

- 7 lemmas about gcd sequence where gcd(2m+1, 2n+1) → 1 for all but finitely many k
- Problem ID: prob_20260315030719_fffb3b00
- Root theorem: ∃ K : ℕ, ∀ k ≥ K, Nat.Coprime (2 * seqM m₀ n₀ k + 1) (2 * seqN m₀ n₀ k + 1)
- Assembly lemmas: 5 (lem_8bcb0ad7, lem_bd72d344, lem_7360bff1, lem_415173a4, lem_c6218886)
- Non-assembly (support) lemmas: 2 (lem_124a8204, lem_b1ba019f)

## Problem Characteristics: a2.json (Putnam A2) - Observed 2026-03-16

- 7 lemmas about f(x) = sin(x)/(x*(π-x)) on (0,π): symmetry, endpoint limits, lower/upper bounds, inf/sup
- Problem ID: prob_20260315030804_a862b834
- Root theorem: a = 1/π and b = 4/π² are the optimal constants for the sandwich inequality
- Helper noncomputable def f introduced in Statements.lean
- Phase03 Claude time: 893s (14.9 min), 38 turns, $1.39 — used LSP to verify all 8 declarations
- Phase03 completed with assembly precheck ok (5.7s = warm cache), pinned all 7 lemmas + root
- Phase04 launched with --parallel-lemmas at 21:57 UTC
- First lemma solved: lem_4bc9cc21 (inf/sup characterize optimal bounds) in round_01, 85s, 9 turns — very easy (just csInf_le + le_csInf etc.)
- At checkpoint (22:07 UTC), only lem_4bc9cc21 has a summary; 6 scratch files remain
- Lemmas lem_1912f371 (equivalence), lem_d3b391e8 (symmetry), lem_8bfa0dc7 (limits), lem_f1fcee00 (lower bound), lem_1645b79b (upper bound) have lake setup-file processes running (started 21:58) — these are still in Phase04 repair loop
- lem_9b4e1f0b (inf/sup values from hypotheses) — the Claude agent called lean_goal at 22:03:17 and has been waiting 4.5+ min for LSP to respond (LSP rebuilding stale Mathlib files via lake)

## Phase04 LSP Stall Pattern (a2, 2026-03-16)

- At 22:02:59 MCP log: "Auto-rebuilding stale imports for Scratch_lemma_lem_9b4e1f0b.lean" (36.36s)
- At 22:03:17 Claude issued lean_goal; LSP started rebuilding Mathlib from partial cache
- Concurrent lake build triggered: 20-30 Lean compile workers consuming 1000-1400% CPU
- MCP log went silent — lean_goal is pending inside the LSP server
- 31 total Lean compile processes at peak; all rebuilding a2/run_002's .lake packages
- The runs for a3 (run_003) and a4 (run_002) also have lake builds running in parallel:
  - a3 run_003: started 22:03:25, at 7365/7747 modules at 22:07:48
  - a4 run_002: started 22:03:31, at 7365/7747 modules at 22:07:48
  - a2 run_003 (new retry): started 22:04:26, at 7401/7747 modules at 22:07:48

## Concurrent Load Pattern (Heavy Load, 2026-03-16 ~22:07 UTC)

- System: load avg 63, 38GB/62GB RAM used, 0 swap
- 5 concurrent lean_engine.cli processes: 2x a2, 2x a3, 1x a4
- 3 concurrent lake builds consuming ~1000-1400% CPU each
- Lake builds are NOT from scratch — they use .lake/build hardlinks to 25GB shared _lake_cache
- Each run's workspace .lake/build dir is only 27MB (project-level, not full Mathlib)
- The shared _lake_cache has grown from 12G (observed earlier) to 25G
- Expected: lake builds complete in ~10-20 min, then Phase03 Claude starts for a3/a4

## New Run Behavior: screen Sessions

- Runs launched in screen sessions (screen -S a2, screen -S a3) create parallel retries
- The "original" CLI (PID from background task) and the screen CLI both manage separate run dirs
- Example: a2 has run_002 (original) and run_003 (screen retry) running simultaneously
- This is intentional — multiple attempts in parallel

## Problem Characteristics: a3.json (Putnam A3) - Observed 2026-03-18

- 6 lemmas about Hamiltonian paths on {0,1,2}^n hypercube graph (H_n)
- Problem ID: prob_20260315224206_9f8facc7
- Root theorem: BobWins n for all n ≥ 1 (no-repetition game second player wins)
- Lemma IDs (with NL descriptions):
  - lem_20260315232011_3d908a3a (L1): Base case n=1: path (0)→(1)→(2) is Hamiltonian from zero
  - lem_20260315232044_137cba8d (L2): Path lifting: prepend digit to path gives H_{k+1} path (both directions)
  - lem_20260315232127_37cf2c0e (L3): Inductive step: snake pattern constructs H_{k+1} Hamiltonian path from H_k path
  - lem_20260315232216_4cebae53 (L4): Main existence: H_n has Hamiltonian path from zero for all n≥1
  - lem_20260315232302_10f794c0 (L5): Parity: coord sum parity equals index parity for zero-starting paths
  - lem_20260315232346_5554102e (L6): Bob's strategy validity given Hamiltonian path from zero with parity

## Phase Timing Summary (run_011 a3.json, 2026-03-18)

| Phase | Duration | Status | Notes |
|-------|----------|--------|-------|
| phase01_normalize | ~0s | ok | |
| phase02_runtime | 20s | ok | cache HIT (564125577f7fdbbe), lake build 2.7s |
| phase03_statement | 523s (8.7min) | ok | Claude draft 4min + repair round 0 + repair round 1 (0 diag) + olean rebuild ~3min + assembly precheck 3.6s |
| phase04_lemmas | 1013s (16.9min) | ok | 6 lemmas parallel; 2 succeeded (L1, L6), 4 failed (L2-L5) |
| phase05_semantic | 40.5s | fatal | false_lemma_suspected (pinned-signature guard false positive on L1) |
| **Total** | **~1597s (26.6min)** | **fatal** | |

## Run 011 Phase 04 Lemma Results

| Lemma | Rounds | Result | Error | Notes |
|-------|--------|--------|-------|-------|
| L1 (3d908a3a) | 1 | ok | — | Very easy: path proof ~30 lines; semicolon in `let` binding caused Phase 05 false positive |
| L2 (137cba8d) | 5 | failed | tactic_failure | `rewrite` tactic failures in reversed path proof |
| L3 (37cf2c0e) | 5 | failed | tactic_failure | `Fin.cons_eq_cons.mp` unknown; `unsolved goals` in snake path proof |
| L4 (4cebae53) | 5 | failed | type_mismatch | `Nat.eq_or_gt_of_le` unknown, `Fin.cons_injective` unknown |
| L5 (10f794c0) | 5 | failed | tactic_failure | `zeroVertex` rewrite fail, omega failure, returncode=135 (SIGABRT) on last attempt |
| L6 (5554102e) | 2 | ok | — | Fixed `Nat.Odd.pow` → correct API name in round 2 |

## Run 011 New Failure Mode: Pinned-Signature Guard False Positive

**Bug location:** `semantic_phase.py` _pinned_signature_guard_error + _collapse_ws function
**Root cause:** Phase 03 pins the signature by extracting the declaration header text without semicolons (`let P := ...\n IsHam...`). But Phase 04's proof uses `let P := ...; IsHam...` (with `;`). The guard was doing exact string comparison treating these as different signatures.
**Fix:** `_collapse_ws()` now does `text.replace(";", " ")` before collapsing whitespace. This fix is in the working directory (unstaged) as of 2026-03-18.
**Evidence:**
- Expected: `... := fun i _ => i IsHamiltonianPathFromZero P` (no semicolon)
- Actual:   `... := fun i _ => i; IsHamiltonianPathFromZero P` (with semicolon)

## Phase 05 Semantic Rejection: L6 (5554102e) - False Lemma Suspected

**Root cause:** `BobWins n` was defined as merely `Odd (3^n)`, which is trivially true arithmetic. The proof used only `Odd.pow` without ever invoking the hypotheses about the Hamiltonian path or parity property.
**Classifier verdict:** `bad_statement_translation` — the Lean formalization fundamentally misrepresents the game-theoretic content.
**Implication:** L6 needs a richer Lean formalization of `BobWins n` that actually encodes the game-theoretic content (Bob's strategy always finds a legal move, Bob makes the last move).

## Resume Run (run_012 from run_011) - Observed 2026-03-18

**Started:** 09:46 UTC, **Ended:** ~09:51 UTC (pipeline crashed)
**Resume behavior:**
1. Phase04 ran 5 rounds for all 4 failed lemmas (L2-L5) at 09:15-09:41 (appears to be first pass)
2. Phase05 ran at 09:41 — STILL rejected L1 with pinned-sig guard (semicolon fix not yet applied or not effective)
3. Phase07_summary at 09:41 echoed run_011's fatal result (run_root points to run_011)
4. A SECOND Phase04 pass started at 09:47, with all 4 lemmas succeeding in rounds 1-3:
   - L2 (137cba8d): succeeded in round_02 (09:49) — "proof compiles with no errors"
   - L3 (37cf2c0e): succeeded in round_02 (09:50) — "0 diagnostic items"
   - L4 (4cebae53): succeeded in round_02 (09:50) — "0 diagnostic items"
   - L5 (10f794c0): succeeded in round_02 (09:49) and round_03 (09:50) — induction proof
5. Pipeline process (PID 2299805) died at ~09:51 UTC
6. run_012 artifacts wiped — only `workspace/Orthos/Scratch_lemma_lem_20260315232302_10f794c0.lean` survived
7. Cause of death: unknown (no OOM events in journal; possibly a filesystem or Python exception during workspace reset for second pass)

**Key observation:** The resume appears to have a two-pass structure:
- First pass processes the result from the previous run's phase05 fatal (propagates run_011 data)
- Second pass starts a fresh Phase04 for the retried lemmas with improved proofs
- The pipeline crashed between the second-pass lemma successes and updating result.json / proceeding to Phase05

**Caching note:** run_012 was compiling Mathlib `Analysis/Calculus/ContDiff/Basic.lean` from source even though the cache had 7377 oleans. The MCP `lake setup-file` triggered this as a dependency of scratch files.

## A3 Outstanding Issues for Next Run

1. **L1 pinned-signature guard**: Semicolon fix is in `semantic_phase.py` working directory. Should work when next run evaluates L1.
2. **L6 bad_statement_translation**: `BobWins n` definition is too weak (just `Odd (3^n)`). Phase 03 statement formalization needs to capture the game-theoretic content. The Statements.lean `BobWins` definition is the root problem.
3. **L2-L5**: All 4 succeeded in run_012's second pass rounds 1-3. The proofs exist in scratch files. But the run crashed before writing result.json or proceeding to Phase05. Next resume from run_012 should be able to extract these proofs.
4. **Pipeline crash during workspace reset**: The run_012 artifacts (summaries/, lemmas/, etc.) were deleted mid-run. Likely the pipeline was resetting its own directory for the second phase04 pass. This suggests an unusual control flow in the resume logic.

## Run Observation: Resume Creates Summaries/ Then Wipes Them

The resume run_012 wrote complete summaries (phase04, phase05, phase07) at 09:41, then DELETED them all before starting a second Phase04 pass at 09:47. The pipeline died during this second pass. The summaries at 09:41 appeared to be re-echoing run_011's phase05 fatal result rather than containing fresh run_012 data.

**Why this matters:** A run_013 resuming from run_012 will find only `workspace/Orthos/Scratch_lemma_10f794c0.lean` in run_012. The resume logic must be robust enough to handle an incomplete resume run.
