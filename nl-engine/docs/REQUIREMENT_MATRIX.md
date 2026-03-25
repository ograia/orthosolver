# Requirement Matrix

Mapping from key spec invariants (`docs/nl_engine.tex`) to current implementation anchors.

## 1. Core Theorem/Lemma Invariants
| Requirement | Implementation Anchor |
|---|---|
| Root objective remains the only theorem | `ProblemORM.root_theorem_id`, theorem kind handling in controller |
| Non-root obligations are lemmas | lemma creation/decomposition logic in `orchestrator.py` |
| Root semantic sketch immutable | root theorem sketch created in `POST /v1/problems`; never rewritten |
| Every theorem/lemma has semantic sketch | Agent1 use for root and decomposition-candidate lemmas |

## 2. Verification and Routing Rules
| Requirement | Implementation Anchor |
|---|---|
| Formal success means Lean-verified unless NL-only | terminal route logic in `orchestrator.py` |
| NL-only outcomes tagged explicitly | `verification_level` on create and terminal paths |
| Major semantic drift blocks progress | `route_vetter_result` + controller blocked handling |
| Trusted context only from compiler-accepted Lean output | Lean terminal route + `TrustedContextRepository.create_if_absent` |
| Assembly plan must be trivial composition | decomposition vetting and assembly checks |

## 3. Config-Driven Limits
| Requirement | Implementation Anchor |
|---|---|
| No hardcoded controller caps | `ProblemConfig` consumption in orchestrator |
| Retry/depth/timeout values read from problem config | decomposition, lemma, and Lean dispatch sections in orchestrator |
| Per-agent model/thinking/timeout configurable per request | `ProblemConfig.llm` + `AgentService._resolve_agent_model_and_effort` |

## 4. Persistence and Replay
| Requirement | Implementation Anchor |
|---|---|
| Routing-critical state in first-class columns | ORM models + migrations (`problems`, `lemmas`, `decompositions`, jobs, reports) |
| Agent outputs valid JSON and persisted | `AgentService` parse/normalize pipeline + artifact writes |
| Persist prompts/raw outputs/key artifacts | `services/agents.py` and artifact store usage in API/controller |
| Event timeline for transitions | `EventRepository`, `EventLogger`, `/events`, `/events/stream` |

## 5. API Contract Surface
| Requirement | Implementation Anchor |
|---|---|
| Required `/v1/problems/*` endpoints | `api/main.py` |
| Monitoring endpoints (`progress`, `events`, SSE) | `api/main.py` |
| Structured error envelope | API exception handlers in `api/main.py` |
| Debug-only visibility and replay APIs | `api/debug.py` |

## 6. Lean Boundary
| Requirement | Implementation Anchor |
|---|---|
| Lean engine as external service boundary | `lean_client/client.py` |
| Asynchronous submit + poll + cancel model | Lean client methods + orchestration dispatch/polling |
| Mock Lean service for development | `mock_lean/main.py` |
| OIDC-ready auth mode | settings + lean client auth token path |

## 7. Operational Hardening
| Requirement | Implementation Anchor |
|---|---|
| Worker idempotency | `workers/facade.py` keyed by `job_id` |
| Cost/token observability | `llm_usage_records`, `run_cost_rollups`, `observability/costs.py` |
| Debug reconciliation for interrupted runs | `/v1/debug/problems/{id}/reconcile-incomplete-runs` |
| Cleanup/reset tooling for local debug cycles | `services/debug_cleaner.py` + debug endpoints |

## 8. Test Coverage Mapping
| Requirement Class | Test Coverage Anchor |
|---|---|
| config/routing/state behavior | `tests/unit/test_config.py`, `tests/unit/test_routing.py`, integration lifecycle tests |
| public API contract behavior | `tests/integration/test_api_nl_only.py`, `test_api_standard_mode.py` |
| decomposition/lemma progression | `tests/integration/test_decomposition_pipeline_progress.py` and related |
| debug API/UI observability | `tests/integration/test_debug_interface.py`, `tests/unit/test_debug_request_completion.py` |
| Lean boundary/auth/mock | `tests/integration/test_mock_lean.py`, `tests/unit/test_lean_client_auth.py` |
