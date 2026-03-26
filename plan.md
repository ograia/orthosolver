## Plan: Unified Lean Mode Through Standardized APIs

Integrate NL and Lean as one runtime by keeping Lean as an external service boundary, adding a versioned v2 Lean API for decomposition-prep + per-lemma formalization, and wiring NL orchestrator to call Lean continuously (not only at the end). Preserve existing NL backend/run organization and existing Lean full-pipeline capability by keeping v1 contracts in place. Add explicit Lean classification (`proof_issue` vs `lean_issue`) so routing can distinguish real NL flaws from Lean search/engineering difficulty.

**Steps**
1. Phase 1 - Contract and mode alignment (foundation)
2. Define authoritative interface docs for NL↔Lean v2 and status taxonomy in NL contracts/docs and Lean docs. Include fields for `issue_kind` (`proof_issue`/`lean_issue`), `error_class`, `confidence`, `fatality`, and artifact pointers. *Blocks all downstream implementation.*
3. Introduce config compatibility policy in NL: add `mode.lean_mode` while preserving `mode.nl_only_mode` backward compatibility and explicit conflict resolution rules. Keep default behavior stable for existing clients. *Depends on step 1.*
4. Define operation-level feature flags in API payload/config for: (a) Lean-driven auto sublemma split, (b) progress streaming payloads, (c) strict proof-issue fail-fast thresholds. *Parallel with step 3.*

5. Phase 2 - Lean service v2 (while preserving v1)
6. Implement Lean API v2 surface on top of current Phase 08 service with backward-compatible v1 retention:
7. `prepare_track` operation: after accepted decomposition, formalize all lemma statements in one Lean track/session and return stable handles per lemma plus run metadata.
8. `formalize_lemma_from_nl` operation: consume lemma handle + NL proof + dependency context and produce Lean result with classification and artifact links.
9. `assemble_root_from_track` operation: preserve final theorem assembly path for full formal success.
10. Optional `split_proof_into_sublemmas` operation behind setting/flag.
11. Keep v1 endpoints/modes for existing full-bundle workflows (`run_full_pipeline` and current modes) unchanged. *Depends on step 1.*
12. Extend service job store/result payloads with progress snapshots (`phase`, `round`, `attempt`, `last_error`) and terminal classification fields. *Parallel with step 6, blocks step 13.*
13. Ensure service emits durable artifact index references (run root + key files) without moving artifacts out of Lean-native run directories. *Depends on step 6.*

14. Phase 3 - NL orchestrator integration
15. Add NL-side v2 Lean client methods and contract models for new operations while preserving current submit/poll compatibility path. Route by configured API version. *Depends on step 6.*
16. Wire decomposition acceptance hook in orchestrator to submit `prepare_track` for each accepted decomposition and persist returned track/lemma-handle mapping. *Depends on steps 3 and 15.*
17. Wire lemma lifecycle so that once Agent5 approves a lemma (`send_to_lean`), orchestrator submits `formalize_lemma_from_nl` immediately using the prepared handle. *Depends on step 16.*
18. Replace current lean-result routing assumptions with classification-aware routing:
19. `proof_issue` with high confidence routes to NL decomposition/statement plausibility paths.
20. `lean_issue` routes through lean-only retries/repair budgets before NL rollback.
21. Apply explicit hard cap escalation policy to avoid infinite loops.
22. *Depends on step 17.*
23. Inject Lean failure/bottleneck signals into proof graph metadata and decomposition inputs. If auto-split setting is enabled, call split operation and feed generated sublemmas into decomposition flow under existing caps. *Depends on steps 4 and 17.*

24. Phase 4 - Data model and persistence hardening
25. Add NL migrations for any new persisted fields needed for track/session mapping, per-lemma Lean classification summary, and progress snapshots (only where not already representable in existing artifacts). *Depends on step 16.*
26. Update repositories/ORM serialization and debug snapshot payload composition to include new Lean metadata and artifact pointers.
27. Maintain compatibility with existing rows by nullable defaults and fallback parsing. *Depends on step 25.*

28. Phase 5 - Observability and UI
29. Extend `GET /v1/problems/{id}/progress` with per-lemma Lean status summaries and explicit fatal-vs-hard indicators derived from classification.
30. Add `GET /v1/problems/{id}/lean-jobs` (or equivalent) for focused Lean monitoring without requiring full debug snapshot parsing.
31. Enrich SSE stream with Lean lifecycle events (submitted, running, retry, classified proof_issue/lean_issue, terminal).
32. Extend debug snapshot/API payloads with direct links to Lean-native artifact keys (source code, compiler logs, run summaries).
33. Update existing `/debug` UI (no new separate Lean UI) in static assets to show:
34. Per-lemma Lean progress/attempt timeline
35. Classification badges (`proof issue`, `lean issue`, confidence)
36. Artifact quick-links for Lean code/logs
37. Auto-split enabled/disabled state visibility
38. *Depends on steps 13 and 29-32.*

39. Phase 6 - Compatibility, rollout, and verification
40. Keep full JSON->full Lean output workflow intact through existing Lean full-pipeline path and NL compatibility behavior; add regression tests to guarantee no breakage. *Depends on step 11.*
41. Add unit tests for classification mapping, config compatibility (`lean_mode` + `nl_only_mode`), and routing policy transitions.
42. Add integration tests for:
43. decomposition accepted -> prepare_track -> per-lemma formalization loop
44. proof_issue vs lean_issue divergence routing
45. auto-split feature-flag on/off
46. existing standard mode and NL-only mode non-regression
47. Add staging/contract tests against real Lean v2 API and maintain mock Lean parity for local CI.
48. Publish runbook updates for operators describing new statuses, artifacts, and troubleshooting flows.

**Relevant files**
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/controller/orchestrator.py - Core insertion points for decomposition acceptance, formalize dispatch, poll/route handling, and mode transitions.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/routing/policy.py - Lean result routing update for classification-aware decisions.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/domain/contracts.py - API + Lean job + agent contracts; add v2 payload/response models and progress schema extensions.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/domain/config.py - `lean_mode` compatibility and feature-flag config.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/domain/models.py - Persisted fields for track/session mapping and classification summaries (if needed).
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/lean_client/client.py - Lean API v2 methods and version-aware request handling.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/api/main.py - Progress/events endpoint expansion and optional lean-jobs endpoint.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/api/debug.py - Snapshot/debug payload enrichments and artifact-link exposure.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/api/static/debug/app.js - UI updates for Lean progress and classification badges.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/src/nl_engine/services/proof_graphs.py - Lean feedback injection and bottleneck metadata updates.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/database/migrations - Schema migration scripts for new persisted Lean integration fields.
- /home/admin_hgraia_altostrat_com/orthosolver/nl-engine/contracts/lean_engine/http_contract.md - Publicized boundary contract updates with v1/v2 coexistence.
- /home/admin_hgraia_altostrat_com/orthosolver/compiled-lean-engine/lean-engine/src/lean_engine/service/app.py - New v2 route handling and compatibility preservation.
- /home/admin_hgraia_altostrat_com/orthosolver/compiled-lean-engine/lean-engine/src/lean_engine/service/jobs.py - Operation dispatch for `prepare_track`, `formalize_lemma_from_nl`, `assemble_root_from_track`, optional split.
- /home/admin_hgraia_altostrat_com/orthosolver/compiled-lean-engine/lean-engine/src/lean_engine/service/store.py - Progress/terminal classification persistence.
- /home/admin_hgraia_altostrat_com/orthosolver/compiled-lean-engine/lean-engine/docs/runbook_phase08.md - Service API/runbook updates.
- /home/admin_hgraia_altostrat_com/orthosolver/compiled-lean-engine/architecture.md - Architecture narrative update reflecting integrated lean-mode lifecycle.

**Verification**
1. Contract tests: validate NL Lean client requests against Lean v2 schema and ensure v1 requests still pass unchanged.
2. End-to-end integration test: root theorem -> accepted decomposition -> immediate statement formalization for all lemmas -> first vetted lemma triggers Lean formalization -> status visible in progress/debug.
3. Routing tests: simulate Lean `proof_issue` and `lean_issue` outcomes and verify distinct controller actions and counters.
4. Auto-split tests: with feature flag off, no split calls; with flag on, split calls occur only on configured bottleneck classes.
5. UI verification: `/debug` shows per-lemma Lean status timeline, classification badges, and artifact links that resolve.
6. Regression suite: existing NL-only and standard-mode tests still pass; existing full-bundle Lean pipeline still produces final `Combined.lean` when called through legacy path.
7. Staging sanity: run real Lean service with v2 endpoints and verify idempotency, polling monotonicity, and failure-class stability.

**Decisions**
- Keep Lean as an external API service boundary.
- Introduce v2 Lean APIs while retaining v1 legacy compatibility.
- Start Lean statement formalization immediately for all lemmas once a decomposition is accepted.
- Lean engine is the authority for `proof_issue` vs `lean_issue` classification.
- Auto sublemma split is enabled as an API/config setting (feature-flagged), not forced globally.
- Extend existing `/debug` and current progress/events surfaces; no separate Lean UI.
- Preserve Lean-native run directory organization and expose links from NL artifacts/UI.

**Further Considerations**
1. Classification confidence policy: define minimum confidence threshold that allows Lean `proof_issue` to trigger immediate decomposition versus requiring one confirmation retry.
2. Resource policy: decide default concurrency/budget partition between NL workers and Lean jobs to prevent starvation when many lemmas are active.
3. API lifecycle policy: define sunset timeline and compatibility guarantees for v1 once v2 adoption is stable.
