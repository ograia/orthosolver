from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import hashlib
import json
import re
import threading
from typing import Any

from nl_engine.artifacts.store import ArtifactStore
from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.contracts import (
    Agent1Input,
    Agent1Output,
    Agent2AssemblyPlan,
    Agent2Candidate,
    Agent2Input,
    Agent2Lemma,
    Agent2Output,
    Agent3Input,
    Agent4Input,
    Agent4Output,
    Agent5Input,
    Agent5Output,
    Agent6Input,
    Agent6Output,
    Agent7Input,
    Agent7Output,
    Agent8Input,
    Agent8Output,
    DependencyManifestItem,
    SemanticSketch,
)
from nl_engine.domain.enums import (
    ControllerStatus,
    FailureReason,
    LeanAssemblyStatus,
    LeanJobMode,
    NodeKind,
    NodeStatus,
    ProblemStatus,
    ProofStatus,
    RoutingStatus,
    StatementStatus,
    VerificationLevel,
)
from nl_engine.domain.models import (
    AssemblyPlanORM,
    CounterexampleORM,
    DecompositionCandidateORM,
    DecompositionORM,
    FailureReportORM,
    LeanJobORM,
    LeanResultORM,
    LemmaORM,
    LemmaProofAttemptORM,
    ProblemORM,
    WorkerJobORM,
    VetterReportORM,
)
from nl_engine.lean_client.sessions import LeanSessionManager
from nl_engine.lean_client.client import LeanClient
from nl_engine.observability.events import EventLogger
from nl_engine.observability.costs import flush_buffered_usage_rows, record_usage_row
from nl_engine.observability.metrics import MetricsExporter
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
    AssemblyPlanRepository,
    CounterexampleRepository,
    DecompositionCandidateRepository,
    DecompositionRepository,
    EventRepository,
    FailureReportRepository,
    LeanJobRepository,
    LeanResultRepository,
    LemmaRepository,
    LemmaProofAttemptRepository,
    LlmUsageRepository,
    ProblemRepository,
    RunCostRollupRepository,
    TheoremRepository,
    TrustedContextRepository,
    VetterReportRepository,
    WorkerJobRepository,
)
from nl_engine.routing.policy import is_deterministic_lean_setup_error, route_lean_result, route_vetter_result
from nl_engine.services.agents import AgentExecutionError, AgentService
from nl_engine.services.ids import new_id
from nl_engine.services.proof_graphs import ProofGraphService
from nl_engine.services.proof_bundles import ProofBundleService
from nl_engine.settings import get_settings
from nl_engine.workers import WorkerFacade, WorkerJob


class Orchestrator:
    """Controller advancement loop for NL-only and standard-mode paths.

    Non-negotiable policies enforced here (docs/nl_engine.tex):
    - Root theorem sketch is immutable.
    - Major semantic drift blocks progress.
    - No hardcoded limits; all caps read from ProblemConfig.
    - Trusted context promotion only on compiler-accepted Lean success.
    - Root objective remains theorem; all non-root obligations are lemmas.
    - Assembly must remain trivial composition.
    """

    _DECOMPOSE_OUTCOME_CHILD_SELECTED = "child_selected"
    _DECOMPOSE_OUTCOME_CHILD_PENDING = "child_pending"
    _DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND = "no_accepted_this_round"
    _DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE = "cannot_decompose"

    def __init__(self, store: FileStore, *, execution_id: str | None = None):
        self.settings = get_settings()
        self.store = store
        self.execution_id = execution_id
        self.problems = ProblemRepository(store)
        self.theorems = TheoremRepository(store)
        self.decompositions = DecompositionRepository(store)
        self.decomposition_candidates = DecompositionCandidateRepository(store)
        self.assembly_plans = AssemblyPlanRepository(store)
        self.lemmas = LemmaRepository(store)
        self.counterexamples = CounterexampleRepository(store)
        self.proof_attempts = LemmaProofAttemptRepository(store)
        self.vetter_reports = VetterReportRepository(store)
        self.lean_jobs = LeanJobRepository(store)
        self.lean_results = LeanResultRepository(store)
        self.trusted_context = TrustedContextRepository(store)
        self.failure_reports = FailureReportRepository(store)
        self.events = EventRepository(store)
        self.worker_jobs = WorkerJobRepository(store)
        self.llm_usage = LlmUsageRepository(store)
        self.cost_rollups = RunCostRollupRepository(store)
        self.event_logger = EventLogger(store)
        self.proof_graphs = ProofGraphService(store)

        main_usage_mode = "disabled" if self.settings.openai_usage_persistence_mode == "disabled" else "immediate"
        try:
            self.agents = AgentService(
                db_session=store,
                usage_persistence_mode_override=main_usage_mode,
            )
        except TypeError:
            # Test doubles may not implement the optional db_session kwarg.
            self.agents = AgentService()
        if self.execution_id and hasattr(self.agents, "set_runtime_context"):
            self.agents.set_runtime_context(execution_id=self.execution_id)
        self.lean_sessions = LeanSessionManager(store)
        self.lean = None
        self.artifacts = ArtifactStore()
        self.proof_bundles = ProofBundleService(store, self.artifacts)
        self.worker_usage_buffer: list[dict[str, Any]] = []
        self.worker_usage_buffer_lock = threading.Lock()
        self._infrastructure_incident_added_this_tick = False
        self.workers = WorkerFacade(
            self.artifacts,
            agent_service_factory=AgentService,
            agent_service_kwargs={
                "usage_persistence_mode_override": self.settings.openai_usage_persistence_mode,
                "usage_buffer": self.worker_usage_buffer,
                "usage_buffer_lock": self.worker_usage_buffer_lock,
            },
        )
        self.metrics = MetricsExporter()

    def _resume_anchor_lemma(self, problem: ProblemORM) -> LemmaORM | None:
        lemma_id = str(problem.resume_anchor_lemma_id or "").strip()
        if not lemma_id:
            return None
        lemma = self.lemmas.get(lemma_id)
        if lemma is None or lemma.problem_id != problem.problem_id:
            return None
        return lemma

    def _consume_theorem_override_once(self, theorem_id: str, *, override_key: str | None) -> str | None:
        if override_key != "agent2_first_root":
            return override_key
        theorem = self.theorems.get(theorem_id)
        if theorem is None:
            return None
        if theorem.agent2_first_root_consumed:
            return None
        theorem.agent2_first_root_consumed = True
        self.theorems.save(theorem)
        return override_key

    def _consume_lemma_override_once(self, lemma_id: str, *, override_key: str | None) -> str | None:
        lemma = self.lemmas.get(lemma_id)
        if lemma is None:
            return None if override_key in {"agent2_first_lemma", "agent4_first"} else override_key
        if override_key == "agent2_first_lemma":
            if lemma.agent2_first_lemma_consumed:
                return None
            lemma.agent2_first_lemma_consumed = True
            self.lemmas.save(lemma)
            return override_key
        if override_key == "agent4_first":
            if lemma.agent4_first_consumed:
                return None
            lemma.agent4_first_consumed = True
            self.lemmas.save(lemma)
            return override_key
        return override_key

    @staticmethod
    def _continuation_generation(problem: ProblemORM) -> int:
        try:
            return int(problem.continuation_generation)
        except Exception:
            return 0

    def _clear_resume_anchor(self, problem: ProblemORM, *, reason: str) -> bool:
        if not problem.resume_anchor_lemma_id and not problem.resume_anchor_owner_decomposition_id:
            return False
        old_anchor = problem.resume_anchor_lemma_id
        problem.resume_anchor_lemma_id = None
        problem.resume_anchor_owner_decomposition_id = None
        self.problems.touch(problem)
        self.event_logger.transition(
            problem.problem_id,
            "problem.resume_anchor_cleared",
            old_anchor,
            None,
            target_node_id=old_anchor,
            reason=reason,
        )
        return True

    def _clear_resume_anchor_if_resolved(self, problem: ProblemORM) -> bool:
        if not problem.resume_anchor_lemma_id and not problem.resume_anchor_owner_decomposition_id:
            return False
        anchor = self._resume_anchor_lemma(problem)
        if anchor is None:
            return self._clear_resume_anchor(problem, reason="anchor lemma missing")
        if anchor.routing_status == RoutingStatus.DONE.value:
            return self._clear_resume_anchor(problem, reason="branch_resolved")
        return False

    def _refresh_proof_bundle_artifacts(self, problem: ProblemORM) -> bool:
        root_decomposition_id = problem.active_decomposition_id
        changed = self.proof_bundles.refresh_runtime_bundles(
            problem.problem_id,
            root_decomposition_id=root_decomposition_id,
        )
        refreshed = self.problems.get(problem.problem_id)
        if refreshed is not None:
            problem.running_final_proof_artifact_id = refreshed.running_final_proof_artifact_id
            problem.final_proof_artifact_id = refreshed.final_proof_artifact_id
        return changed

    def _clear_running_proof_bundle_refs(self, problem: ProblemORM, dec: DecompositionORM, lemma_ids: list[str]) -> bool:
        changed = False
        if problem.running_final_proof_artifact_id is not None:
            problem.running_final_proof_artifact_id = None
            changed = True
        if problem.final_proof_artifact_id is not None and problem.status != ProblemStatus.SUCCEEDED.value:
            problem.final_proof_artifact_id = None
            changed = True
        if dec.proof_bundle_artifact_id is not None:
            dec.proof_bundle_artifact_id = None
            changed = True
        for lemma_id in lemma_ids:
            lemma = self.lemmas.get(lemma_id)
            if lemma is None:
                continue
            if lemma.proof_bundle_artifact_id is not None:
                lemma.proof_bundle_artifact_id = None
                self.lemmas.save(lemma)
                changed = True
        if changed:
            self.decompositions.save(dec)
            self.problems.save(problem)
        return changed

    def _normalize_agent6_findings(
        self,
        problem: ProblemORM,
        dec: DecompositionORM,
        output: Agent6Output,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        buckets = (
            ("decomposition_findings", output.decomposition_findings, "decomposition"),
            ("assembly_findings", output.assembly_findings, "decomposition"),
            ("lemma_findings", output.lemma_findings, "lemma"),
        )
        for _, rows, default_scope in buckets:
            for item in rows:
                if not isinstance(item, dict):
                    continue
                finding = dict(item)
                lemma_id = str(finding.get("lemma_id") or "").strip()
                if lemma_id and not finding.get("target_id"):
                    finding["target_scope"] = "lemma"
                    finding["target_id"] = lemma_id
                target_scope = str(finding.get("target_scope") or default_scope).strip() or default_scope
                if target_scope not in {"problem", "decomposition", "lemma"}:
                    target_scope = default_scope
                finding["target_scope"] = target_scope
                target_id = str(finding.get("target_id") or "").strip()
                if not target_id:
                    if target_scope == "problem":
                        target_id = problem.problem_id
                    elif target_scope == "decomposition":
                        target_id = dec.decomposition_id
                    elif lemma_id:
                        target_id = lemma_id
                finding["target_id"] = target_id
                severity = str(finding.get("severity") or "warning").strip().lower()
                finding["severity"] = "fatal" if severity == "fatal" else "warning"
                recommended_action = str(finding.get("recommended_action") or "").strip().lower()
                if recommended_action not in {"retry_solver", "decompose_further"}:
                    recommended_action = "retry_solver" if target_scope == "lemma" else ""
                if recommended_action:
                    finding["recommended_action"] = recommended_action
                else:
                    finding.pop("recommended_action", None)
                normalized.append(finding)
        return normalized

    def _localize_final_check_repairs(
        self,
        problem: ProblemORM,
        dec: DecompositionORM,
        findings: list[dict[str, Any]],
        *,
        worker_job_id: str | None,
    ) -> bool:
        lemma_findings = [
            finding
            for finding in findings
            if finding.get("target_scope") == "lemma" and str(finding.get("target_id") or "").strip()
        ]
        if not lemma_findings:
            return False

        touched_lemma_ids: list[str] = []
        seen: set[str] = set()
        for finding in lemma_findings:
            lemma_id = str(finding.get("target_id") or "").strip()
            if not lemma_id or lemma_id in seen:
                continue
            seen.add(lemma_id)
            lemma = self.lemmas.get(lemma_id)
            if lemma is None:
                continue
            action = str(finding.get("recommended_action") or "retry_solver").strip().lower()
            lemma.proof_status = ProofStatus.PROOF_FLAWED.value
            if action == "decompose_further":
                lemma.routing_status = RoutingStatus.DECOMPOSE_FURTHER.value
            else:
                lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
            lemma.proof_bundle_artifact_id = None
            self.lemmas.save(lemma)
            touched_lemma_ids.append(lemma_id)
            self.event_logger.transition(
                problem.problem_id,
                "final_check.localized_lemma_repair",
                None,
                lemma.routing_status,
                target_node_id=lemma_id,
                worker_job_id=worker_job_id,
                reason=str(finding.get("description") or "")[:200],
            )

        if not touched_lemma_ids:
            return False

        dec.final_check_passed = False
        dec.final_check_job_id = None
        self._clear_running_proof_bundle_refs(problem, dec, touched_lemma_ids)
        self.event_logger.transition(
            problem.problem_id,
            "final_check.localized_repair_requested",
            None,
            "localized_repair",
            target_node_id=dec.decomposition_id,
            worker_job_id=worker_job_id,
            reason=f"lemmas={','.join(touched_lemma_ids)}",
        )
        return True

    def run_once(self, problem_id: str) -> ProblemORM:
        problem = self.problems.get(problem_id)
        if not problem:
            raise ValueError("problem not found")

        if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            return problem

        cfg = ProblemConfig.model_validate(problem.config)
        self._configure_problem_llm_overrides(cfg)
        root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
        if not root:
            raise ValueError("root theorem not found")

        def _flush_worker_usage() -> None:
            self._flush_worker_usage_buffer()

        changed = False
        self._infrastructure_incident_added_this_tick = False
        try:
            changed |= self._apply_mode_switch(problem, cfg)
            changed |= self._clear_resume_anchor_if_resolved(problem)
            if problem.status not in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                changed |= self._enforce_problem_budget_guardrail(problem, cfg)

            if problem.status == ProblemStatus.CREATED.value:
                old = problem.status
                problem.status = ProblemStatus.RUNNING.value
                self.event_logger.transition(
                    problem.problem_id,
                    "problem.start",
                    old,
                    problem.status,
                    reason="controller start",
                )
                changed = True

            # Harvest all in-flight Lean jobs so completed work is not stranded
            # behind a single unfair polling slot.
            changed |= self._poll_lean_jobs(problem, cfg)

            if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                self._commit(problem)
                _flush_worker_usage()
                problem = self.problems.get(problem.problem_id)
                return problem

            # Wait for semantic sketch before proceeding with decomposition.
            if not root.statement_semantic_sketch:
                changed |= self._ensure_root_semantic_sketch_job(problem, root)
                changed |= self._harvest_root_semantic_sketch_job(problem, root)
                root = self.theorems.get(root.theorem_id)
            if not root.statement_semantic_sketch:
                self._commit(problem)
                _flush_worker_usage()
                problem = self.problems.get(problem.problem_id)
                return problem

            root_decompositions = self.decompositions.list_by_node(problem.problem_id, root.theorem_id)
            if not root_decompositions:
                changed |= self._generate_root_decompositions(problem, root, cfg)
                self._commit(problem)
                _flush_worker_usage()
                problem = self.problems.get(problem.problem_id)
                return problem

            if self._current_root_track_count(problem.problem_id, root.theorem_id) < self._root_parallel_take_k(cfg):
                changed |= self._generate_root_decompositions(problem, root, cfg)

            if not cfg.mode.nl_only_mode:
                changed |= self._ensure_assembly_jobs(problem, root, cfg)

            if not problem.active_decomposition_id:
                changed |= self._select_active_or_fail(problem, root, cfg)
            else:
                # If the active decomposition was rejected by the final check,
                # clear it so a new one can be selected on the next tick.
                active_check = self.decompositions.get(problem.active_decomposition_id)
                if active_check and active_check.controller_status == ControllerStatus.FAILED.value:
                    anchor = self._resume_anchor_lemma(problem)
                    if anchor is not None:
                        self.event_logger.transition(
                            problem.problem_id,
                            "problem.root_replan_started",
                            None,
                            "root_replan",
                            target_node_id=anchor.lemma_id,
                            reason=f"branch exhausted; lemma={anchor.lemma_id}",
                        )
                        changed |= self._clear_resume_anchor(
                            problem,
                            reason="branch_exhausted_starting_root_replan",
                        )
                    problem.active_decomposition_id = None
                    self.problems.touch(problem)
                    changed = True

            if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                self._commit(problem)
                _flush_worker_usage()
                problem = self.problems.get(problem.problem_id)
                return problem

            if problem.active_decomposition_id:
                active = self.decompositions.get(problem.active_decomposition_id)
                if active:
                    changed |= self._process_decomposition(problem, root, active, cfg)
                    if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                        self._commit(problem)
                        _flush_worker_usage()
                        problem = self.problems.get(problem.problem_id)
                        return problem

            # Optional root-level parallel tracks: continue processing up to K
            # vetted root decompositions while preserving a single primary
            # active decomposition id.
            if self._root_parallel_take_k(cfg) > 1:
                changed |= self._process_parallel_root_decompositions(problem, root, cfg)

            changed |= self._refresh_proof_bundle_artifacts(problem)

            if problem.status == ProblemStatus.RUNNING.value:
                changed |= self._run_final_checks_on_solved_decompositions(problem, root, cfg)

            if problem.status == ProblemStatus.RUNNING.value:
                changed |= self._finalize_if_complete(problem, root, cfg)

            changed |= self._clear_resume_anchor_if_resolved(problem)
            if problem.status == ProblemStatus.RUNNING.value:
                changed |= self._maybe_fail_dead_frontier(problem, root, cfg)

            # Reset infrastructure failure counter on successful progress.
            if changed and problem.infrastructure_failure_count > 0 and not self._infrastructure_incident_added_this_tick:
                problem.infrastructure_failure_count = 0
        except AgentExecutionError as exc:
            if exc.error_class == "interrupted":
                with self.worker_usage_buffer_lock:
                    self.worker_usage_buffer.clear()
                refreshed = self.problems.get(problem_id)
                return refreshed or problem
            if exc.error_class == "infrastructure_transient":
                reason_payload = exc.as_dict()
                self.event_logger.transition(
                    problem.problem_id,
                    "agent.infrastructure_retry",
                    problem.status,
                    problem.status,
                    reason=json.dumps(reason_payload, sort_keys=True),
                )
                changed = True
            else:
                problem.infrastructure_failure_count += 1
                self._infrastructure_incident_added_this_tick = True
                reason_payload = exc.as_dict()
                max_failures = cfg.lemma_solving.max_infrastructure_failures
                if problem.infrastructure_failure_count >= max_failures:
                    self.event_logger.transition(
                        problem.problem_id,
                        "agent.infrastructure_fatal",
                        problem.status,
                        ProblemStatus.FAILED.value,
                        reason=json.dumps(reason_payload, sort_keys=True),
                    )
                    self._mark_failed(
                        problem,
                        FailureReason.UNKNOWN.value,
                        terminal_error_class=exc.error_class,
                        terminal_error_message=(
                            f"infrastructure failure count ({problem.infrastructure_failure_count}) "
                            f"exceeded threshold ({max_failures}): {exc.message}"
                        ),
                    )
                else:
                    self.event_logger.transition(
                        problem.problem_id,
                        "agent.infrastructure_retry",
                        problem.status,
                        problem.status,
                        reason=json.dumps({
                            **reason_payload,
                            "infrastructure_failure_count": problem.infrastructure_failure_count,
                            "max_infrastructure_failures": max_failures,
                        }, sort_keys=True),
                    )
                changed = True

        if changed:
            self._commit(problem)

        _flush_worker_usage()
        problem = self.problems.get(problem.problem_id)
        return problem

    def _frontier_has_runnable_action(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        if root is None or not root.statement_semantic_sketch:
            return True

        root_decompositions = self.decompositions.list_by_node(problem.problem_id, root.theorem_id)
        if not root_decompositions:
            return True

        if not problem.active_decomposition_id:
            return True

        active = self.decompositions.get(problem.active_decomposition_id)
        if active is None:
            return True
        if active.controller_status in {ControllerStatus.FAILED.value, ControllerStatus.SUCCEEDED.value}:
            return True

        lemma_rows = [self.lemmas.get(lemma_id) for lemma_id in active.lemma_ids]
        lemma_rows = [lemma for lemma in lemma_rows if lemma is not None]
        if not lemma_rows:
            return True

        for lemma in lemma_rows:
            if lemma.routing_status == RoutingStatus.DONE.value:
                continue
            if lemma.proof_status == ProofStatus.PROOF_FOUND.value:
                return True
            if lemma.proof_status == ProofStatus.PROOF_VETTED.value and not cfg.mode.nl_only_mode:
                return True
            if lemma.routing_status == RoutingStatus.SPLIT_EXISTING_PROOF.value:
                return True
            if lemma.routing_status == RoutingStatus.DECOMPOSE_FURTHER.value:
                return True
            if lemma.proof_status in {ProofStatus.OPEN.value, ProofStatus.PROOF_FLAWED.value, ProofStatus.FAILED.value}:
                return True
            if lemma.proof_status == ProofStatus.PROOF_EXHAUSTED.value:
                remaining = self._remaining_decomposition_slots(
                    problem.problem_id,
                    lemma.lemma_id,
                    NodeKind.LEMMA.value,
                    cfg,
                )
                if remaining > 0:
                    return True

            child_rows = self.decompositions.list_by_node(problem.problem_id, lemma.lemma_id)
            if any(row.controller_status in {ControllerStatus.ACTIVE.value, ControllerStatus.PENDING.value} for row in child_rows):
                return True
            if any(row.llm_vetting_status == "accepted" for row in child_rows):
                return True

        return False

    def _maybe_fail_dead_frontier(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        if problem.status != ProblemStatus.RUNNING.value:
            return False
        if root is None or not root.statement_semantic_sketch:
            return False

        continuation_generation = self._continuation_generation(problem)
        inflight_worker_jobs = [
            row
            for row in self.worker_jobs.list_by_problem(problem.problem_id)
            if row.status in {"queued", "running"}
            and row.superseded_at is None
            and int(row.continuation_generation or 0) == continuation_generation
        ]
        if inflight_worker_jobs:
            return False
        if self.lean_jobs.list_non_terminal(problem.problem_id):
            return False
        if self._frontier_has_runnable_action(problem, root, cfg):
            return False

        active_dec = self.decompositions.get(problem.active_decomposition_id) if problem.active_decomposition_id else None
        blocked_lemmas: list[dict[str, Any]] = []
        if active_dec is not None:
            for lemma_id in active_dec.lemma_ids:
                lemma = self.lemmas.get(lemma_id)
                if lemma is None or lemma.routing_status == RoutingStatus.DONE.value:
                    continue
                blocked_lemmas.append(
                    {
                        "lemma_id": lemma.lemma_id,
                        "proof_status": lemma.proof_status,
                        "routing_status": lemma.routing_status,
                        "remaining_decomposition_slots": self._remaining_decomposition_slots(
                            problem.problem_id,
                            lemma.lemma_id,
                            NodeKind.LEMMA.value,
                            cfg,
                        ),
                    }
                )

        detail_payload = {
            "active_decomposition_id": problem.active_decomposition_id,
            "continuation_generation": continuation_generation,
            "blocked_lemmas": blocked_lemmas,
            "worker_inflight_count": 0,
            "lean_inflight_count": 0,
        }
        self.event_logger.transition(
            problem.problem_id,
            "problem.dead_frontier_detected",
            ProblemStatus.RUNNING.value,
            ProblemStatus.RUNNING.value,
            target_node_id=problem.active_decomposition_id,
            reason=json.dumps(detail_payload, sort_keys=True),
        )
        self._mark_failed(
            problem,
            FailureReason.DEAD_FRONTIER.value,
            terminal_error_message=f"dead frontier detected: {json.dumps(detail_payload, sort_keys=True)}",
        )
        return True

    def _commit(self, problem: ProblemORM) -> None:
        latest = self.problems.get(problem.problem_id)
        if latest is not None:
            # Pause is authoritative: never let an in-flight stale running
            # orchestrator snapshot overwrite a persisted paused state.
            if latest.status == ProblemStatus.PAUSED.value and problem.status == ProblemStatus.RUNNING.value:
                problem.status = ProblemStatus.PAUSED.value
            if int(latest.continuation_generation or 0) > int(problem.continuation_generation or 0):
                problem.continuation_generation = int(latest.continuation_generation or 0)
        self.problems.touch(problem)

    def _flush_worker_usage_buffer(self) -> int:
        return flush_buffered_usage_rows(
            usage_buffer=self.worker_usage_buffer,
            usage_buffer_lock=self.worker_usage_buffer_lock,
            db_session=self.store,
        )

    def _configure_problem_llm_overrides(self, cfg: ProblemConfig) -> None:
        llm_overrides = cfg.llm.model_dump(exclude_none=True)
        if hasattr(self.agents, "set_llm_overrides"):
            self.agents.set_llm_overrides(llm_overrides)
        self.workers.agent_service_kwargs["llm_overrides"] = llm_overrides

    def _apply_mode_switch(self, problem: ProblemORM, cfg: ProblemConfig) -> bool:
        changed = False
        desired = cfg.mode.nl_only_mode
        if problem.nl_only_mode == desired:
            return False

        previous = problem.nl_only_mode
        problem.nl_only_mode = desired
        problem.verification_level = VerificationLevel.NL_ONLY.value if desired else VerificationLevel.FORMAL.value
        self.event_logger.transition(
            problem.problem_id,
            "problem.mode_switch",
            "nl_only" if previous else "standard",
            "nl_only" if desired else "standard",
            reason="config.mode.nl_only_mode changed",
        )
        changed = True

        if desired:
            # Standard -> NL-only: cancel running Lean jobs and promote vetted lemmas.
            cancelled_jobs = list(self.lean_jobs.list_non_terminal(problem.problem_id))
            self.lean_sessions.cancel_problem_lean_work(
                problem.problem_id,
                reason="mode switched to nl_only",
                terminate_session=True,
                clear_metadata=True,
            )
            for job in cancelled_jobs:
                self.event_logger.transition(
                    problem.problem_id,
                    "lean.cancel",
                    "running",
                    "cancelled",
                    worker_job_id=job.job_id,
                    reason="mode switched to nl_only",
                )
            for lemma in self.lemmas.list_by_problem(problem.problem_id):
                if lemma.proof_status == ProofStatus.PROOF_VETTED.value:
                    lemma.proof_status = ProofStatus.NL_ACCEPTED.value
                    lemma.routing_status = RoutingStatus.DONE.value
                    self.lemmas.save(lemma)
            changed = True
        else:
            # NL-only -> standard: demote nl_accepted back into Lean path.
            for lemma in self.lemmas.list_by_problem(problem.problem_id):
                if lemma.proof_status == ProofStatus.NL_ACCEPTED.value:
                    lemma.proof_status = ProofStatus.PROOF_VETTED.value
                    lemma.routing_status = RoutingStatus.READY_FOR_LEAN.value
                    self.lemmas.save(lemma)
            changed = True

        return changed

    def _trusted_context_summaries(self, problem_id: str, *, proof_graph_id: str | None = None) -> list[dict[str, str]]:
        summaries: list[dict[str, str]] = []
        if proof_graph_id:
            rows = self.trusted_context.list_for_graph(problem_id, proof_graph_id=proof_graph_id)
        else:
            rows = self.trusted_context.list_by_problem(problem_id)
        for row in rows:
            lean_code = row.lean_code or ""
            summary_line = lean_code.strip().splitlines()[0] if lean_code.strip() else row.decl_name
            summaries.append(
                {
                    "name": row.decl_name,
                    "statement_lean": lean_code,
                    "summary_nl": summary_line[:240],
                    "context_scope": row.context_scope,
                    "proof_graph_id": row.proof_graph_id,
                }
            )
        return summaries

    def _ancestry_summary(self, problem: ProblemORM, lemma: LemmaORM, root: Any) -> list[dict[str, Any]]:
        summary = [
            {
                "node_id": root.theorem_id,
                "kind": NodeKind.THEOREM.value,
                "statement_nl": root.statement_nl,
                "reason": "root theorem is forbidden as citable solver context",
            }
        ]
        summary.extend(self._build_local_branch_context(lemma, depth_cap=16))
        return summary

    def _dependency_manifest_bundle(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
    ) -> tuple[list[DependencyManifestItem], list[dict[str, Any]], list[dict[str, Any]], str | None]:
        return self.proof_graphs.build_dependency_manifest(problem=problem, lemma=lemma, root=root)

    def _load_decomposition_vetter_output(self, problem_id: str, decomposition_id: str) -> dict[str, Any] | None:
        # Preferred source: worker idempotency result payload.
        worker_key = f"worker_jobs/wrk_{decomposition_id}_vet/result.json"
        if self.artifacts.exists(worker_key):
            try:
                worker_payload = self.artifacts.load_json(worker_key)
                if isinstance(worker_payload, dict):
                    output = worker_payload.get("output")
                    if isinstance(output, dict):
                        return output
            except Exception:
                pass

        # Fallback source: raw parsed Agent3 outputs by attempt.
        for attempt in (2, 1):
            parsed_key = (
                f"problems/{problem_id}/decomposition_vetter/{decomposition_id}/agent3_parsed_output_attempt_{attempt}.json"
            )
            if not self.artifacts.exists(parsed_key):
                continue
            try:
                parsed_payload = self.artifacts.load_json(parsed_key)
                if isinstance(parsed_payload, dict):
                    return parsed_payload
            except Exception:
                continue
        return None

    def _counterexample_summary(self, counterexample_id: str | None) -> dict[str, Any] | None:
        counterexample_key = str(counterexample_id or "").strip()
        if not counterexample_key:
            return None
        row = self.counterexamples.get(counterexample_key)
        if row is None:
            return None
        return {
            "counterexample_id": row.counterexample_id,
            "lemma_id": row.lemma_id,
            "parent_decomposition_id": row.parent_decomposition_id,
            "status": row.status,
            "counterexample_text": row.counterexample_text,
            "summary": row.summary,
            "confidence": row.confidence,
            "source_agent": row.source_agent,
            "source_attempt_number": row.source_attempt_number,
            "accepted_by_report_id": row.accepted_by_report_id,
            "rejected_by_report_id": row.rejected_by_report_id,
        }

    @staticmethod
    def _false_lemma_constraint_text(
        *,
        local_id: str | None,
        counterexample_text: str | None,
        evidence: str | None,
    ) -> str:
        child_label = f"child lemma {local_id}" if local_id else "a child lemma"
        if counterexample_text:
            return (
                f"Do not reuse the previous decomposition pattern around {child_label}; "
                f"it was falsified by counterexample {counterexample_text}."
            )
        if evidence:
            return (
                f"Do not reuse the previous decomposition pattern around {child_label}; "
                f"it was flagged false because {evidence}."
            )
        return f"Do not reuse the previous decomposition pattern around {child_label}; it previously produced a false child."

    def _false_lemma_findings_summary(
        self,
        *,
        full_candidate: dict[str, Any] | None,
        vet_output: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        local_to_lemma_id: dict[str, str | None] = {}
        if isinstance(full_candidate, dict):
            lemmas = full_candidate.get("lemmas")
            if isinstance(lemmas, list):
                for item in lemmas:
                    if not isinstance(item, dict):
                        continue
                    local_id = str(item.get("local_id") or item.get("lemma_local_id") or "").strip()
                    if not local_id:
                        continue
                    lemma_id = str(item.get("lemma_id") or "").strip() or None
                    local_to_lemma_id[local_id] = lemma_id

        findings: list[dict[str, Any]] = []
        constraints: list[str] = []
        lemma_findings = vet_output.get("lemma_findings") if isinstance(vet_output, dict) else None
        if not isinstance(lemma_findings, list):
            return findings, constraints
        for item in lemma_findings:
            if not isinstance(item, dict):
                continue
            statement_status = str(item.get("statement_status") or "").strip().lower()
            counterexample_text = str(item.get("counterexample") or "").strip() or None
            if statement_status != "false" and not counterexample_text:
                continue
            local_id = str(item.get("local_id") or "").strip() or None
            evidence = str(item.get("evidence") or "").strip() or None
            finding_summary = {
                "local_id": local_id,
                "lemma_id": local_to_lemma_id.get(local_id or ""),
                "statement_status": statement_status or None,
                "evidence": evidence,
                "candidate_counterexample": counterexample_text,
                "do_not_repeat_pattern": self._false_lemma_constraint_text(
                    local_id=local_id,
                    counterexample_text=counterexample_text,
                    evidence=evidence,
                ),
            }
            findings.append(finding_summary)
            constraints.append(str(finding_summary["do_not_repeat_pattern"]))
        return findings, constraints

    def _node_previous_attempt_summaries(self, problem_id: str, node_id: str) -> list[dict[str, Any]]:
        rows = self.decompositions.list_by_node(problem_id, node_id)
        summaries: list[dict[str, Any]] = []
        for row in rows:
            failure_mode = "accepted"
            if row.llm_vetting_status != "accepted":
                failure_mode = row.llm_vetting_status
            if row.controller_status in {ControllerStatus.FAILED.value, ControllerStatus.PENDING.value} and row.llm_vetting_status != "accepted":
                failure_mode = f"{failure_mode}:{row.controller_status}"

            summary: dict[str, Any] = {
                "decomposition_id": row.decomposition_id,
                "strategy_summary": row.strategy_summary,
                "llm_vetting_status": row.llm_vetting_status,
                "lean_assembly_status": row.lean_assembly_status,
                "lean_v2_prepare_status": row.lean_v2_prepare_status,
                "controller_status": row.controller_status,
                "failure_mode": failure_mode,
                "equivalence_risk": row.equivalence_risk,
            }
            if row.lean_prepare_issue_kind:
                summary["lean_prepare_issue_kind"] = row.lean_prepare_issue_kind
            if row.lean_prepare_error_class:
                summary["lean_prepare_error_class"] = row.lean_prepare_error_class
            if row.lean_prepare_confidence is not None:
                summary["lean_prepare_confidence"] = row.lean_prepare_confidence
            if row.lean_prepare_fatality:
                summary["lean_prepare_fatality"] = row.lean_prepare_fatality
            if row.lean_bottlenecks:
                summary["recent_lean_bottlenecks"] = row.lean_bottlenecks[-5:]

            # Reconstruct the full Agent 2 candidate from stored data so
            # the next Agent 2 call can see exactly what was produced and
            # make targeted fixes rather than re-inventing from scratch.
            full_candidate = self._reconstruct_agent2_candidate(problem_id, row)
            if full_candidate:
                summary["full_candidate"] = full_candidate

            vet_output = self._load_decomposition_vetter_output(problem_id, row.decomposition_id)
            false_lemma_findings, false_constraints = self._false_lemma_findings_summary(
                full_candidate=full_candidate,
                vet_output=vet_output,
            )
            if isinstance(vet_output, dict):
                coverage = vet_output.get("coverage_check")
                drift = vet_output.get("drift_assessment")
                summary["decision"] = vet_output.get("decision")
                summary["vet_summary"] = vet_output.get("summary")
                summary["fixes_required"] = vet_output.get("fixes_required", [])
                summary["missing_coverage"] = (
                    coverage.get("missing_coverage", []) if isinstance(coverage, dict) else []
                )
                summary["drift_severity"] = (
                    drift.get("drift_severity") if isinstance(drift, dict) else None
                )
                summary["disguised_difficulty"] = (
                    coverage.get("disguised_difficulty", []) if isinstance(coverage, dict) else []
                )
            if false_lemma_findings:
                summary["false_lemma_findings"] = false_lemma_findings
            if row.failure_origin:
                summary["failure_origin"] = row.failure_origin
            # Load programmatic self-containment violations if persisted
            violations_key = f"problems/{problem_id}/decomposition_vetter/{row.decomposition_id}/self_containment_violations.json"
            if self.artifacts.exists(violations_key):
                try:
                    violations = self.artifacts.load_json(violations_key)
                    if isinstance(violations, list) and violations:
                        summary["self_containment_violations"] = violations
                except Exception:
                    pass
            # For pre-vet-rejected decompositions (final_step_yields_exact_root=False),
            # agent3 never ran so there is no vet output. Add an explicit rejection_reason
            # so agent2 understands why the attempt was rejected and what to fix.
            if row.failure_reason:
                summary["rejection_reason"] = row.failure_reason
            elif vet_output is None and row.llm_vetting_status == "rejected_fatal":
                summary["rejection_reason"] = (
                    "Rejected before vetting: assembly plan set final_step_yields_exact_root=false "
                    "(the assembly did not prove the root theorem). Fix: ensure every gap in the "
                    "assembly is covered by an explicit lemma so the assembly is logically complete "
                    "given the lemmas. final_step_yields_exact_root MUST be true."
                )
            if row.equivalence_risk == "high":
                summary["rejection_reason"] = (
                    "Rejected: at least one child lemma appears equivalent-strength to the parent theorem. "
                    "Fix: replace restatements with genuinely simplifying lemmas and keep only a clearly "
                    "labeled bottleneck when unavoidable."
                )
                fixes = summary.get("fixes_required")
                if not isinstance(fixes, list):
                    fixes = []
                fixes.append(
                    "A child lemma is equivalent to the parent theorem; rewrite decomposition with strictly simpler support lemmas."
                )
                summary["fixes_required"] = fixes
            invalidating_counterexample = self._counterexample_summary(row.invalidated_by_counterexample_id)
            if row.invalidated_by_lemma_id:
                summary["invalidated_by_lemma_id"] = row.invalidated_by_lemma_id
                lemma_repo = getattr(self, "lemmas", None)
                invalidating_lemma = lemma_repo.get(row.invalidated_by_lemma_id) if lemma_repo is not None else None
                if invalidating_lemma is not None:
                    summary["invalidated_by_statement_nl"] = invalidating_lemma.statement_nl
            if row.invalidated_by_counterexample_id:
                summary["invalidated_by_counterexample_id"] = row.invalidated_by_counterexample_id
            if invalidating_counterexample is not None:
                summary["invalidating_counterexample"] = invalidating_counterexample
                false_constraints.append(
                    self._false_lemma_constraint_text(
                        local_id=None,
                        counterexample_text=str(invalidating_counterexample.get("counterexample_text") or "").strip() or None,
                        evidence=str(row.failure_reason or "").strip() or None,
                    )
                )
            if false_constraints:
                summary["hard_negative_constraints"] = list(dict.fromkeys(false_constraints))
            summaries.append(
                summary
            )
        return summaries

    def _lineage_nodes(self, problem_id: str, node_id: str, *, depth_cap: int = 64) -> list[dict[str, Any]]:
        lineage: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        current_node_id = node_id
        lemma_repo = getattr(self, "lemmas", None)
        theorem_repo = getattr(self, "theorems", None)
        current_lemma = lemma_repo.get(node_id) if lemma_repo is not None else None
        current_kind = NodeKind.LEMMA.value if current_lemma is not None else NodeKind.THEOREM.value
        depth = 0

        while current_node_id and depth < depth_cap:
            seen_key = (current_kind, current_node_id)
            if seen_key in seen:
                break
            seen.add(seen_key)

            statement_nl = ""
            if current_kind == NodeKind.LEMMA.value:
                lemma = lemma_repo.get(current_node_id) if lemma_repo is not None else None
                if lemma is not None:
                    statement_nl = lemma.statement_nl
            else:
                theorem = theorem_repo.get(current_node_id) if theorem_repo is not None else None
                if theorem is not None:
                    statement_nl = theorem.statement_nl

            lineage.append(
                {
                    "node_id": current_node_id,
                    "node_kind": current_kind,
                    "statement_nl": statement_nl,
                    "depth": depth,
                }
            )

            if current_kind != NodeKind.LEMMA.value:
                break

            owner = self._find_owner_decomposition(problem_id, current_node_id)
            if owner is None:
                break
            current_node_id = owner.node_id
            current_kind = owner.node_kind
            depth += 1

        return lineage

    def _previous_attempt_summaries(self, problem_id: str, node_id: str) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lineage_row in self._lineage_nodes(problem_id, node_id):
            lineage_depth = int(lineage_row.get("depth") or 0)
            source_node_id = str(lineage_row.get("node_id") or "").strip()
            source_node_kind = str(lineage_row.get("node_kind") or "").strip()
            source_statement_nl = str(lineage_row.get("statement_nl") or "").strip()
            for item in self._node_previous_attempt_summaries(problem_id, source_node_id):
                summary = dict(item)
                summary["source_node_id"] = source_node_id
                summary["source_node_kind"] = source_node_kind
                summary["source_statement_nl"] = source_statement_nl
                summary["source_lineage_depth"] = lineage_depth
                summary["source_relation"] = "current_node" if lineage_depth == 0 else "ancestor"
                dedupe_key = (
                    str(summary.get("decomposition_id") or "").strip()
                    or f"{source_node_kind}:{source_node_id}:{summary.get('failure_mode')}:{summary.get('strategy_summary')}"
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                summaries.append(summary)
        return summaries

    @staticmethod
    def _risk_pattern_matches(text: str, patterns: list[tuple[str, str]]) -> list[dict[str, str]]:
        normalized = text.lower()
        matches: list[dict[str, str]] = []
        for label, pattern in patterns:
            if re.search(pattern, normalized):
                matches.append({"label": label, "evidence": text[:240]})
        return matches

    def _decomposition_risk_audit(
        self,
        *,
        theorem_nl: str,
        decomposition: dict[str, Any],
        previous_attempt_summaries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        patterns = [
            ("existence_claim", r"\bthere exists\b|\bexists\b|\bfind\b|\bchoose\b"),
            ("universal_integer_claim", r"\bfor all\b|\bfor every\b|\bfor each\b"),
            ("divisibility_or_counting", r"\bdivisib|\bmultiple\b|\bcount\b|\bnumber of\b"),
            ("bounded_certificate", r"\bbounded\b|\bcertificate\b|\bsupport\b|\bdegree\b"),
            ("regular_sequence", r"\bregular sequence\b"),
            ("coefficient_sign_claim", r"\bcoefficient\w*\b.*\b(nonnegative|positive)\b|\bnonnegative coefficients?\b"),
        ]
        matched_signals: list[dict[str, str]] = []
        text_rows = [theorem_nl]
        strategy_summary = str(decomposition.get("strategy_summary") or "").strip()
        if strategy_summary:
            text_rows.append(strategy_summary)
        for lemma in decomposition.get("lemmas", []) if isinstance(decomposition.get("lemmas"), list) else []:
            if not isinstance(lemma, dict):
                continue
            statement_nl = str(lemma.get("statement_nl") or "").strip()
            if statement_nl:
                text_rows.append(statement_nl)
        for text in text_rows:
            matched_signals.extend(self._risk_pattern_matches(text, patterns))

        previous_attempt_summaries = list(previous_attempt_summaries or [])
        prior_counterexamples: list[dict[str, Any]] = []
        for attempt in previous_attempt_summaries:
            invalidating_counterexample = attempt.get("invalidating_counterexample")
            if isinstance(invalidating_counterexample, dict):
                prior_counterexamples.append(invalidating_counterexample)
            for finding in attempt.get("false_lemma_findings", []) if isinstance(attempt.get("false_lemma_findings"), list) else []:
                if isinstance(finding, dict) and finding.get("candidate_counterexample"):
                    prior_counterexamples.append(
                        {
                            "counterexample_text": finding.get("candidate_counterexample"),
                            "summary": finding.get("evidence"),
                        }
                    )

        unique_labels = {item["label"] for item in matched_signals}
        if prior_counterexamples:
            search_priority = "high"
        elif len(unique_labels) >= 2:
            search_priority = "high"
        elif unique_labels:
            search_priority = "medium"
        else:
            search_priority = "low"

        recommended_checks = [
            "Run bounded small-instance search before marking risky lemmas plausible.",
            "Prefer direct counterexample search on bottleneck lemmas before auditing assembly details.",
        ]
        if prior_counterexamples:
            recommended_checks.append("Do not re-emit any lemma family contradicted by the supplied prior counterexamples.")

        return {
            "search_priority": search_priority,
            "matched_signals": matched_signals,
            "prior_counterexamples": prior_counterexamples,
            "recommended_checks": recommended_checks,
        }

    def _reconstruct_agent2_candidate(self, problem_id: str, dec: DecompositionORM) -> dict[str, Any] | None:
        """Reconstruct the full Agent 2 candidate from stored data.

        For accepted decompositions, reads from the persisted lemma records
        and assembly plan.  For rejected decompositions (where lemma records
        were never created), falls back to the Agent 3 vetter input artifact
        which contains the full candidate including lemma statements.
        """
        try:
            lemmas: list[dict[str, Any]] = []
            assembly_plan: dict[str, Any] | None = None

            if dec.raw_candidate_artifact_id and self.artifacts.exists(dec.raw_candidate_artifact_id):
                raw_candidate = self.artifacts.load_json(dec.raw_candidate_artifact_id)
                if isinstance(raw_candidate, dict):
                    return raw_candidate

            # Try persisted lemma records first (available for accepted decompositions).
            for lemma_id in dec.lemma_ids:
                lemma = self.lemmas.get(lemma_id)
                if lemma is None:
                    continue
                lemmas.append({
                    "lemma_id": lemma.lemma_id,
                    "statement_nl": lemma.statement_nl,
                    "role_in_assembly": lemma.role_in_parent or "",
                    "formalization_cost_estimate": lemma.formalization_cost_estimate,
                })

            # Try persisted assembly plan.
            plan = self.assembly_plans.get(dec.assembly_plan_id) if dec.assembly_plan_id else None
            if plan and plan.steps:
                assembly_plan = {
                    "steps": plan.steps,
                    "proof_skeleton_nl": plan.proof_skeleton_nl,
                }

            # For rejected decompositions, lemma records were never created.
            # Fall back to the Agent 3 vetter input artifact which has the
            # full candidate with all lemma statements and assembly plan.
            if not lemmas:
                agent3_input_key = f"problems/{problem_id}/decomposition_vetter/{dec.decomposition_id}/agent3_input.json"
                if self.artifacts.exists(agent3_input_key):
                    agent3_input = self.artifacts.load_json(agent3_input_key)
                    decomp_payload = agent3_input.get("decomposition", {})
                    for lem in decomp_payload.get("lemmas", []):
                        lemmas.append({
                            "local_id": lem.get("local_id", ""),
                            "statement_nl": lem.get("statement_nl", ""),
                            "role_in_assembly": lem.get("role_in_assembly", ""),
                            "formalization_cost_estimate": lem.get("formalization_cost_estimate"),
                        })
                    if assembly_plan is None:
                        artifact_plan = decomp_payload.get("assembly_plan", {})
                        if artifact_plan.get("steps"):
                            assembly_plan = {
                                "steps": artifact_plan["steps"],
                                "proof_skeleton_nl": artifact_plan.get("proof_skeleton_nl", ""),
                            }

            candidate: dict[str, Any] = {
                "strategy_summary": dec.strategy_summary,
                "shared_context": dec.shared_context,
                "lemmas": lemmas,
            }
            if assembly_plan:
                candidate["assembly_plan"] = assembly_plan

            return candidate
        except Exception:
            return None

    _CROSS_REF_RE = re.compile(
        r"(?:as |exactly as |defined |described |given |stated |specified |per |see |from )"
        r"\s*(?:in )?\s*(?:Lemma|lemma)\s+L\d+",
        re.IGNORECASE,
    )

    def _check_lemma_self_containment(self, lemmas: list[Any]) -> list[dict[str, Any]]:
        """Programmatically detect cross-lemma references in lemma statements."""
        violations: list[dict[str, Any]] = []
        for lemma in lemmas:
            statement = lemma.statement_nl if hasattr(lemma, "statement_nl") else (lemma.get("statement_nl", "") if isinstance(lemma, dict) else "")
            local_id = lemma.local_id if hasattr(lemma, "local_id") else (lemma.get("local_id", "") if isinstance(lemma, dict) else "")
            matches = self._CROSS_REF_RE.findall(statement)
            if matches:
                violations.append({
                    "local_id": str(local_id),
                    "violation": f"references another lemma — must inline all definitions. Found: {matches}",
                })
        return violations

    def _root_parallel_take_k(self, cfg: ProblemConfig) -> int:
        return max(1, min(cfg.decomposition.parallel_root_take_k, cfg.decomposition.parallel_root_decompositions_n))

    def _root_generation_materialized_key(self, problem_id: str, job_id: str) -> str:
        return f"problems/{problem_id}/decomposer/materialized/{job_id}.json"

    def _is_root_generation_materialized(self, problem_id: str, job_id: str) -> bool:
        return self.artifacts.exists(self._root_generation_materialized_key(problem_id, job_id))

    def _mark_root_generation_materialized(
        self,
        problem_id: str,
        job_id: str,
        *,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "job_id": job_id,
            "status": status,
            "materialized_at": datetime.now(UTC).isoformat(),
        }
        if details:
            payload["details"] = details
        self.artifacts.save_json(self._root_generation_materialized_key(problem_id, job_id), payload)

    def _mark_root_generation_attempt_queued(
        self,
        *,
        artifact_prefix: str,
        worker_job_id: str,
        cfg: ProblemConfig,
    ) -> None:
        state_key = f"{artifact_prefix}/agent2_request_state_attempt_1.json"
        if self.artifacts.exists(state_key):
            return

        agent2_cfg = cfg.llm.agent2
        model = agent2_cfg.model or self.settings.openai_model_agent2
        reasoning_effort = agent2_cfg.thinking_level or self.settings.openai_reasoning_effort
        text_verbosity = agent2_cfg.verbosity or self.settings.openai_text_verbosity
        timeout_seconds = (
            max(0, agent2_cfg.timeout_seconds)
            if isinstance(agent2_cfg.timeout_seconds, int)
            else max(0, int(self.settings.openai_timeout_seconds))
        )
        payload = {
            "agent_key": "agent2",
            "attempt": 1,
            "status": "queued",
            "updated_at": datetime.now(UTC).isoformat(),
            "worker_job_id": worker_job_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "text_verbosity": text_verbosity,
            "timeout_seconds": timeout_seconds,
        }
        self.artifacts.save_json(state_key, payload)

    def _list_root_generation_attempts(self, problem_id: str, *, continuation_generation: int | None = None) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for row in self.worker_jobs.list_by_problem(problem_id, worker_kind="decomposition_generation"):
            if row.request_source != "root_generation":
                continue
            if row.superseded_at is not None:
                continue
            if continuation_generation is not None and int(row.continuation_generation or 0) != int(continuation_generation):
                continue
            latest_state = row.status
            has_terminal_artifact = row.status in {"completed", "failed"}
            attempts.append(
                {
                    "artifact_prefix": row.artifact_prefix,
                    "worker_job_id": row.worker_job_id,
                    "latest_state": latest_state,
                    "worker_result_status": row.status,
                    "has_terminal_artifact": has_terminal_artifact,
                }
            )
        return attempts

    def _root_generation_inflight_attempt_count(self, problem_id: str, *, continuation_generation: int | None = None) -> int:
        inflight = 0
        for row in self._list_root_generation_attempts(problem_id, continuation_generation=continuation_generation):
            if row.get("latest_state") in {"queued", "running", "started", "deferred"} and not row.get("has_terminal_artifact", False):
                inflight += 1
        return inflight

    def _harvest_completed_root_generation_attempts(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        cfg: ProblemConfig,
    ) -> tuple[bool, int]:
        generated = False
        accepted_count = 0
        max_tracks = self._root_parallel_take_k(cfg)

        for row in self._list_root_generation_attempts(
            problem.problem_id,
            continuation_generation=self._continuation_generation(problem),
        ):
            job_id = row.get("worker_job_id")
            if not isinstance(job_id, str) or not job_id:
                continue
            if self._is_root_generation_materialized(problem.problem_id, job_id):
                continue

            worker_row = self.worker_jobs.get(job_id)
            if worker_row is None:
                continue

            if worker_row.status not in {"completed", "failed"}:
                continue

            result_payload = worker_row.result_payload or {}
            if worker_row.status != "completed":
                error_payload = worker_row.error_payload or {}
                error_class = str(error_payload.get("error_class") or "infrastructure")
                error_message = str(error_payload.get("message") or "root decomposition worker failed")
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="failed",
                    details={"reason": "worker_result_not_completed", "error": error_payload},
                )
                if (
                    error_class != "infrastructure_transient"
                    and self._current_root_track_count(problem.problem_id, root.theorem_id) == 0
                    and self._root_generation_inflight_attempt_count(
                        problem.problem_id,
                        continuation_generation=self._continuation_generation(problem),
                    ) == 0
                ):
                    self._mark_failed(
                        problem,
                        FailureReason.UNKNOWN.value,
                        terminal_error_class=error_class,
                        terminal_error_message=error_message,
                    )
                    return generated, accepted_count
                continue

            if self._current_root_track_count(problem.problem_id, root.theorem_id) >= max_tracks:
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="ignored",
                    details={"reason": "root_track_limit_reached"},
                )
                continue

            output_payload = result_payload if "status" in result_payload else result_payload.get("output")
            if not isinstance(output_payload, dict):
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="failed",
                    details={"reason": "missing_worker_output"},
                )
                continue

            prefix = row.get("artifact_prefix")
            if not isinstance(prefix, str) or not prefix:
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="failed",
                    details={"reason": "missing_attempt_prefix"},
                )
                continue

            input_key = f"{prefix}/agent2_input.json"
            if not self.artifacts.exists(input_key):
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="failed",
                    details={"reason": "missing_agent2_input"},
                )
                continue

            try:
                payload = Agent2Input.model_validate(self.artifacts.load_json(input_key))
                output = Agent2Output.model_validate(output_payload)
            except Exception as exc:
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="failed",
                    details={"reason": f"invalid_payload_or_output:{type(exc).__name__}"},
                )
                continue

            remaining_slots = max(
                0,
                max_tracks - self._current_root_track_count(problem.problem_id, root.theorem_id),
            )
            if remaining_slots <= 0:
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="ignored",
                    details={"reason": "root_track_limit_reached"},
                )
                continue

            # Write pre-materialization lock to prevent concurrent ticks
            # from materializing the same worker result.
            self._mark_root_generation_materialized(
                problem.problem_id,
                job_id,
                status="materializing",
                details={},
            )
            try:
                result_generated, result_accepted = self._materialize_decomposition_candidates(
                    problem=problem,
                    node_id=root.theorem_id,
                    node_kind=NodeKind.THEOREM.value,
                    request_tag=job_id,
                    theorem_nl=root.statement_nl,
                    theorem_semantic_sketch=root.statement_semantic_sketch,
                    parent_depth=0,
                    payload=payload,
                    decompose_job_id=job_id,
                    candidates=output.candidates,
                    max_candidates=remaining_slots,
                    cfg=cfg,
                )
                generated |= result_generated
                accepted_count += result_accepted
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="completed",
                    details={
                        "generated": result_generated,
                        "accepted_count": result_accepted,
                    },
                )
            except Exception:
                self._mark_root_generation_materialized(
                    problem.problem_id,
                    job_id,
                    status="failed",
                    details={"reason": "materialization_exception"},
                )
                raise

        return generated, accepted_count

    def _max_decompositions_for_node(self, node_kind: str, cfg: ProblemConfig) -> int:
        if node_kind == NodeKind.THEOREM.value:
            return cfg.decomposition.parallel_root_decompositions_n
        return cfg.decomposition.max_decompositions_per_failed_lemma

    def _current_root_track_count(self, problem_id: str, node_id: str) -> int:
        rows = self.decompositions.list_by_node(problem_id, node_id)
        return sum(
            1
            for row in rows
            if row.llm_vetting_status == "accepted" and row.controller_status != ControllerStatus.FAILED.value
        )

    def _false_frontier_hops_for_branch(self, problem_id: str, node_id: str) -> int:
        hops = 0
        for lineage_row in self._lineage_nodes(problem_id, node_id):
            lineage_node_id = str(lineage_row.get("node_id") or "").strip()
            hops += sum(
                1
                for row in self.decompositions.list_by_node(problem_id, lineage_node_id)
                if self._child_decomposition_is_false_frontier(row)
            )
            hops += sum(
                1
                for row in self.decomposition_candidates.list_by_node(problem_id, lineage_node_id)
                if self._child_decomposition_is_false_frontier(row)
            )
        return hops

    def _decomposition_slot_limit_reason(self, problem_id: str, node_id: str, node_kind: str, cfg: ProblemConfig) -> str:
        if node_kind == NodeKind.THEOREM.value:
            return "decomposition slot limit reached"
        lemma = self.lemmas.get(node_id)
        consumed_rounds = int(lemma.decomposition_round_count) if lemma is not None else len(self.decompositions.list_by_node(problem_id, node_id))
        per_node_remaining = max(0, self._max_decompositions_for_node(node_kind, cfg) - consumed_rounds)
        if per_node_remaining <= 0:
            return "decomposition slot limit reached"
        branch_remaining = max(
            0,
            int(cfg.decomposition.max_false_frontier_hops_per_branch)
            - self._false_frontier_hops_for_branch(problem_id, node_id),
        )
        if branch_remaining <= 0:
            return (
                "false-frontier branch redecomposition cap reached "
                f"({cfg.decomposition.max_false_frontier_hops_per_branch})"
            )
        return "decomposition slot limit reached"

    def _remaining_decomposition_slots(self, problem_id: str, node_id: str, node_kind: str, cfg: ProblemConfig) -> int:
        if node_kind == NodeKind.THEOREM.value:
            return max(0, cfg.decomposition.parallel_root_decompositions_n - self._current_root_track_count(problem_id, node_id))
        lemma = self.lemmas.get(node_id)
        consumed_rounds = int(lemma.decomposition_round_count) if lemma is not None else len(self.decompositions.list_by_node(problem_id, node_id))
        per_node_remaining = max(0, self._max_decompositions_for_node(node_kind, cfg) - consumed_rounds)
        branch_remaining = max(
            0,
            int(cfg.decomposition.max_false_frontier_hops_per_branch)
            - self._false_frontier_hops_for_branch(problem_id, node_id),
        )
        return min(per_node_remaining, branch_remaining)

    def _refresh_lemma_decomposition_counters(self, lemma: LemmaORM) -> None:
        candidate_rows = self.decomposition_candidates.list_by_node(lemma.problem_id, lemma.lemma_id)
        promoted_count = sum(1 for row in candidate_rows if row.promoted_decomposition_id)
        if lemma.materialized_candidate_count != len(candidate_rows):
            lemma.materialized_candidate_count = len(candidate_rows)
        if lemma.promoted_decomposition_count != promoted_count:
            lemma.promoted_decomposition_count = promoted_count
        self.lemmas.save(lemma)

    @staticmethod
    def _lemma_candidate_sort_key(candidate: DecompositionCandidateORM) -> tuple[float, int, datetime, str]:
        estimate = candidate.formalization_cost_estimate
        return (
            float(estimate) if estimate is not None else 999.0,
            int(candidate.candidate_index or 0),
            candidate.created_at,
            candidate.candidate_id,
        )

    def _accepted_lemma_candidates(self, problem_id: str, lemma_id: str) -> list[DecompositionCandidateORM]:
        rows = [
            row
            for row in self.decomposition_candidates.list_by_node(problem_id, lemma_id)
            if row.llm_vetting_status == "accepted"
        ]
        rows.sort(key=self._lemma_candidate_sort_key)
        return rows

    def _unpromoted_lemma_candidates(self, problem_id: str, lemma_id: str) -> list[DecompositionCandidateORM]:
        return [
            row
            for row in self._accepted_lemma_candidates(problem_id, lemma_id)
            if not row.promoted_decomposition_id
        ]

    def _lemma_candidate_bundle_key(self, problem_id: str, candidate_id: str) -> str:
        return f"problems/{problem_id}/decomposition_candidates/{candidate_id}/materialization_bundle.json"

    def _promote_lemma_candidate(
        self,
        *,
        problem: ProblemORM,
        candidate_row: DecompositionCandidateORM,
        cfg: ProblemConfig,
    ) -> DecompositionORM | None:
        if candidate_row.promoted_decomposition_id:
            existing = self.decompositions.get(candidate_row.promoted_decomposition_id)
            if existing is not None:
                return existing

        if not candidate_row.materialization_artifact_id or not self.artifacts.exists(candidate_row.materialization_artifact_id):
            candidate_row.selection_status = "rejected_fatal"
            candidate_row.failure_origin = candidate_row.failure_origin or "candidate_promotion:missing_materialization_artifact"
            candidate_row.failure_reason = candidate_row.failure_reason or "accepted candidate could not be promoted because its materialization bundle is missing"
            self.decomposition_candidates.save(candidate_row)
            return None

        bundle = self.artifacts.load_json(candidate_row.materialization_artifact_id)
        decomposition_id = new_id("dec")
        assembly_plan_id = new_id("asm")
        logical_decomposition_id = self._logical_decomposition_id(
            node_id=candidate_row.node_id,
            node_kind=candidate_row.node_kind,
        )
        lemma_rows = [
            LemmaORM.model_validate({**row, "proof_graph_id": None})
            for row in bundle.get("candidate_lemma_rows", [])
            if isinstance(row, dict)
        ]
        if not lemma_rows:
            candidate_row.selection_status = "rejected_fatal"
            candidate_row.failure_origin = candidate_row.failure_origin or "candidate_promotion:empty_bundle"
            candidate_row.failure_reason = candidate_row.failure_reason or "accepted candidate materialization bundle did not contain lemma rows"
            self.decomposition_candidates.save(candidate_row)
            return None

        decomp = DecompositionORM(
            decomposition_id=decomposition_id,
            problem_id=problem.problem_id,
            logical_decomposition_id=logical_decomposition_id,
            revision_number=self._next_decomposition_revision_number(
                problem_id=problem.problem_id,
                logical_decomposition_id=logical_decomposition_id,
            ),
            node_id=candidate_row.node_id,
            node_kind=candidate_row.node_kind,
            proof_graph_id=None,
            strategy_summary=str(bundle.get("strategy_summary") or candidate_row.strategy_summary or ""),
            shared_context=list(bundle.get("shared_context") or candidate_row.shared_context or []),
            lemma_ids=[row.lemma_id for row in lemma_rows],
            assembly_plan_id=assembly_plan_id,
            pinned_statement_signatures=None,
            formalization_cost_estimate=candidate_row.formalization_cost_estimate,
            llm_vetting_status="accepted",
            lean_assembly_status=(
                LeanAssemblyStatus.SKIPPED.value
                if cfg.mode.nl_only_mode
                else LeanAssemblyStatus.PENDING.value
            ),
            controller_status=ControllerStatus.ACTIVE.value,
            dependency_status="legacy_unknown",
            raw_candidate_artifact_id=candidate_row.raw_candidate_artifact_id,
            equivalence_risk="none",
            previous_attempt_summaries=list(candidate_row.previous_attempt_summaries or []),
            decomposition_origin=candidate_row.decomposition_origin,
            decomposition_origin_reason=candidate_row.decomposition_origin_reason,
            decomposition_origin_job_id=candidate_row.decomposition_origin_job_id,
        )
        plan = AssemblyPlanORM(
            assembly_plan_id=assembly_plan_id,
            problem_id=problem.problem_id,
            decomposition_id=decomposition_id,
            root_node_id=candidate_row.node_id,
            steps=list(bundle.get("accepted_steps") or []),
            proof_skeleton_nl=str(bundle.get("proof_skeleton_nl") or ""),
            is_trivially_composable=bool(bundle.get("is_trivially_composable", False)),
        )

        self.decompositions.create(decomp)
        self.assembly_plans.create(plan)
        graph = self.proof_graphs.ensure_graph_for_decomposition(problem, decomp)
        if graph is not None:
            decomp.proof_graph_id = graph.proof_graph_id
            decomp.dependency_status = "passed"
        for row in lemma_rows:
            row.proof_graph_id = decomp.proof_graph_id
        self.lemmas.create_many(lemma_rows)
        graph, _, reduction_check = self.proof_graphs.bootstrap_decomposition_graph(
            problem=problem,
            theorem=None,
            decomp=decomp,
            lemmas=lemma_rows,
        )
        for row in lemma_rows:
            self.lemmas.save(row)
        if graph is not None:
            self.proof_graphs.record_dependency_check(
                proof_graph_id=graph.proof_graph_id,
                target_node_id=decomp.decomposition_id,
                artifact_kind="decomposition",
                artifact_id=decomposition_id,
                check_status="passed" if not reduction_check else "retryable_violation",
                violations=reduction_check,
            )
        self.decompositions.save(decomp)

        candidate_row.selection_status = "promoted"
        candidate_row.promoted_decomposition_id = decomp.decomposition_id
        self.decomposition_candidates.save(candidate_row)

        owner_lemma = self.lemmas.get(candidate_row.node_id)
        if owner_lemma is not None:
            owner_lemma.promoted_decomposition_count = int(owner_lemma.promoted_decomposition_count) + 1
            self._refresh_lemma_decomposition_counters(owner_lemma)

        if not cfg.mode.nl_only_mode:
            self._submit_prepare_track_if_v2(problem, decomp, cfg)

        self.event_logger.transition(
            problem.problem_id,
            "decomposition.promoted",
            candidate_row.candidate_id,
            decomp.decomposition_id,
            target_node_id=decomp.decomposition_id,
            reason=f"promoted lemma candidate {candidate_row.candidate_id}",
        )
        return decomp

    def _build_decomposition_generation_payload(
        self,
        *,
        problem: ProblemORM,
        node_id: str,
        theorem_nl: str,
        theorem_semantic_sketch: dict[str, Any],
        num_candidates: int,
        previous_attempt_summaries: list[dict[str, Any]] | None = None,
        trusted_context_summaries: list[dict[str, str]] | None = None,
    ) -> Agent2Input:
        return Agent2Input(
            theorem_nl=theorem_nl,
            root_semantic_sketch=theorem_semantic_sketch,
            shared_context=self._collect_transitive_branch_shared_context(node_id),
            num_candidates=max(1, num_candidates),
            previous_attempt_summaries=(
                list(previous_attempt_summaries)
                if previous_attempt_summaries is not None
                else self._previous_attempt_summaries(problem.problem_id, node_id)
            ),
            trusted_context_summaries=(
                list(trusted_context_summaries)
                if trusted_context_summaries is not None
                else self._trusted_context_summaries(problem.problem_id)
            ),
        )

    @staticmethod
    def _definition_context_dedupe_key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("kind") or "").strip().lower(),
            str(item.get("label") or item.get("name") or "").strip(),
            str(item.get("content") or item.get("value") or "").strip(),
        )

    def _collect_transitive_branch_shared_context(self, node_id: str, *, depth_cap: int = 64) -> list[dict[str, Any]]:
        lemma = self.lemmas.get(node_id)
        if lemma is None:
            return []

        collected: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        current_parent_id = lemma.parent_id
        current_parent_kind = lemma.parent_kind
        depth = 0

        while depth < depth_cap and current_parent_id:
            if current_parent_kind == "decomposition":
                decomp = self.decompositions.get(current_parent_id)
                if decomp is None:
                    break
                for item in self.proof_graphs.normalize_definition_context(decomp.shared_context):
                    key = self._definition_context_dedupe_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(item)
                current_parent_id = decomp.node_id
                current_parent_kind = decomp.node_kind
                depth += 1
                continue

            if current_parent_kind == NodeKind.LEMMA.value:
                parent_lemma = self.lemmas.get(current_parent_id)
                if parent_lemma is None:
                    break
                current_parent_id = parent_lemma.parent_id
                current_parent_kind = parent_lemma.parent_kind
                depth += 1
                continue

            break

        return collected

    @staticmethod
    def _child_decomposition_is_false_frontier(row: Any) -> bool:
        return str(getattr(row, "failure_origin", "") or "").strip() in {
            "agent3:false_lemma",
            "child_lemma_false",
        }

    @staticmethod
    def _child_decomposition_is_live_accepted(row: Any) -> bool:
        if str(getattr(row, "llm_vetting_status", "") or "").strip() != "accepted":
            return False
        controller_status = getattr(row, "controller_status", None)
        return controller_status != ControllerStatus.FAILED.value

    @staticmethod
    def _lemma_waiting_on_child_decomposition_frontier(lemma: LemmaORM) -> bool:
        return str(lemma.next_action or "").strip() in {
            "process_child_decomposition",
            "wait_on_child_decomposition",
            "retry_decomposition",
            "evaluate_child_decompositions",
        }

    def _run_decomposition_generation_request(
        self,
        *,
        problem: ProblemORM,
        node_id: str,
        payload: Agent2Input,
        artifact_prefix: str,
        request_tag: str | None = None,
        override_key: str | None = None,
    ) -> tuple[WorkerJob, Agent2Input, Agent2Output]:
        decompose_job = self._build_decomposition_generation_job(
            problem_id=problem.problem_id,
            node_id=node_id,
            payload=payload,
            request_tag=request_tag,
        )
        try:
            output = self.workers.run_decomposition_generation(decompose_job, payload, artifact_prefix, override_key=override_key)
        except AgentExecutionError:
            raise
        except Exception as exc:
            raise AgentExecutionError(
                agent_key="agent2",
                error_class="infrastructure",
                message=f"decomposition generation failed: {type(exc).__name__}: {exc}",
                artifact_prefix=artifact_prefix,
            ) from exc
        return decompose_job, payload, output

    def _build_decomposition_generation_job(
        self,
        *,
        problem_id: str,
        node_id: str,
        payload: Agent2Input,
        request_tag: str | None = None,
    ) -> WorkerJob:
        tag = request_tag or new_id("req")
        return WorkerJob(
            job_id=f"wrk_{node_id}_decompose_{len(payload.previous_attempt_summaries)}_{tag}",
            problem_id=problem_id,
            worker_kind="decomposition_generation",
            payload=payload.model_dump(),
        )

    @staticmethod
    def _minimal_semantic_sketch(statement_nl: str) -> dict[str, Any]:
        return {
            "variables": [],
            "quantifier_order": [],
            "domain_restrictions": [],
            "witness_dependencies": [],
            "normalized_claim": statement_nl,
        }

    def _root_semantic_sketch_job_id(self, theorem_id: str, continuation_generation: int) -> str:
        return f"wrk_{theorem_id}_semantic_sketch_{continuation_generation}"

    def _ensure_root_semantic_sketch_job(self, problem: ProblemORM, root: Any) -> bool:
        continuation_generation = self._continuation_generation(problem)
        existing = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=root.theorem_id,
            request_source="root_semantic_sketch",
            attempt_number=1,
            continuation_generation=continuation_generation,
            statuses={"queued", "running", "completed", "failed"},
        )
        if existing is not None:
            return False
        payload = Agent1Input(statement_nl=root.statement_nl)
        job_id = self._root_semantic_sketch_job_id(root.theorem_id, continuation_generation)
        self.worker_jobs.enqueue_if_absent(
            WorkerJobORM(
                worker_job_id=job_id,
                problem_id=problem.problem_id,
                worker_kind="root_semantic_sketch",
                status="queued",
                target_id=root.theorem_id,
                target_kind="theorem",
                execution_id=self.execution_id,
                continuation_generation=continuation_generation,
                attempt_number=1,
                artifact_prefix=f"problems/{problem.problem_id}/root_theorem/{root.theorem_id}/semantic_sketch",
                handler_key="agent1_root_semantic_sketch",
                request_source="root_semantic_sketch",
                payload=payload.model_dump(),
                max_attempts=2,
            )
        )
        self.event_logger.transition(
            problem.problem_id,
            "problem.semantic_sketch_submitted",
            None,
            "queued",
            target_node_id=root.theorem_id,
            worker_job_id=job_id,
            reason="queued durable semantic sketch worker",
        )
        return True

    def _harvest_root_semantic_sketch_job(self, problem: ProblemORM, root: Any) -> bool:
        continuation_generation = self._continuation_generation(problem)
        worker_row = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=root.theorem_id,
            request_source="root_semantic_sketch",
            attempt_number=1,
            continuation_generation=continuation_generation,
            statuses={"completed", "failed"},
        )
        if worker_row is None:
            return False
        consumed_row, consumed = self.worker_jobs.consume_terminal(
            worker_row.worker_job_id,
            execution_id=self.execution_id,
        )
        if not consumed or consumed_row is None:
            return False

        theorem = self.theorems.get(root.theorem_id)
        if theorem is None:
            return False

        if consumed_row.status == "failed":
            theorem.statement_semantic_sketch = self._minimal_semantic_sketch(theorem.statement_nl)
            self.theorems.save(theorem)
            error_payload = consumed_row.error_payload or {}
            self.event_logger.transition(
                problem.problem_id,
                "problem.semantic_sketch_failed",
                None,
                "failed",
                target_node_id=theorem.theorem_id,
                worker_job_id=consumed_row.worker_job_id,
                reason=str(error_payload.get("message") or "semantic sketch worker failed"),
            )
            return True

        try:
            sketch_output = Agent1Output.model_validate(consumed_row.result_payload or {})
            theorem.statement_semantic_sketch = sketch_output.semantic_sketch.model_dump()
        except Exception as exc:
            theorem.statement_semantic_sketch = self._minimal_semantic_sketch(theorem.statement_nl)
            self.event_logger.transition(
                problem.problem_id,
                "problem.semantic_sketch_failed",
                None,
                "failed",
                target_node_id=theorem.theorem_id,
                worker_job_id=consumed_row.worker_job_id,
                reason=f"invalid semantic sketch output: {type(exc).__name__}",
            )
            self.theorems.save(theorem)
            return True

        self.theorems.save(theorem)
        self.event_logger.transition(
            problem.problem_id,
            "problem.semantic_sketch_ready",
            None,
            "ready",
            target_node_id=theorem.theorem_id,
            worker_job_id=consumed_row.worker_job_id,
            reason="durable semantic sketch worker completed",
        )
        return True

    def _decomposition_ready_for_selection(self, dec: DecompositionORM, cfg: ProblemConfig) -> bool:
        if dec.llm_vetting_status != "accepted":
            return False
        if dec.controller_status == ControllerStatus.FAILED.value:
            return False
        return True

    def _decomposition_pending_for_selection(self, dec: DecompositionORM, cfg: ProblemConfig) -> bool:
        if dec.llm_vetting_status != "accepted":
            return False
        if dec.controller_status == ControllerStatus.FAILED.value:
            return False
        return False

    def _materialize_decomposition_candidates(
        self,
        *,
        problem: ProblemORM,
        node_id: str,
        node_kind: str,
        request_tag: str | None,
        theorem_nl: str,
        theorem_semantic_sketch: dict[str, Any],
        parent_depth: int,
        payload: Agent2Input,
        decompose_job_id: str,
        candidates: list[Any],
        max_candidates: int,
        cfg: ProblemConfig,
    ) -> tuple[bool, int]:
        accepted_count = 0
        generated = False
        for candidate in candidates[: max(0, max_candidates)]:
            generated = True
            decomposition_id = new_id("dec")
            assembly_plan_id = new_id("asm")
            raw_candidate_artifact_id = (
                f"problems/{problem.problem_id}/decomposition_candidates/{decomposition_id}/agent2_candidate.json"
            )
            self.artifacts.save_json(raw_candidate_artifact_id, candidate.model_dump(mode="json"))

            # Catch agent-level errors (e.g. invalid JSON from Agent1
            # during lemma semantic sketch) at the candidate level so
            # that a single failing candidate does not kill the problem.
            try:
                materialized = self._materialize_single_candidate(
                    problem=problem,
                    node_id=node_id,
                    node_kind=node_kind,
                    request_tag=request_tag,
                    theorem_nl=theorem_nl,
                    theorem_semantic_sketch=theorem_semantic_sketch,
                    parent_depth=parent_depth,
                    payload=payload,
                    decompose_job_id=decompose_job_id,
                    candidate=candidate,
                    decomposition_id=decomposition_id,
                    assembly_plan_id=assembly_plan_id,
                    raw_candidate_artifact_id=raw_candidate_artifact_id,
                    cfg=cfg,
                )
            except AgentExecutionError as exc:
                if exc.error_class == "interrupted":
                    raise
                logical_decomposition_id = self._logical_decomposition_id(
                    node_id=node_id,
                    node_kind=node_kind,
                    request_tag=request_tag,
                )
                if node_kind == NodeKind.LEMMA.value:
                    candidate_row = DecompositionCandidateORM(
                        candidate_id=new_id("deccand"),
                        problem_id=problem.problem_id,
                        node_id=node_id,
                        node_kind=node_kind,
                        logical_decomposition_id=logical_decomposition_id,
                        candidate_index=int(getattr(candidate, "candidate_index", 0) or 0),
                        strategy_summary=getattr(candidate, "strategy_summary", "agent_error"),
                        shared_context=[],
                        formalization_cost_estimate=0,
                        llm_vetting_status="rejected_fatal",
                        selection_status="rejected_fatal",
                        raw_candidate_artifact_id=raw_candidate_artifact_id,
                        failure_origin=f"{exc.agent_key}:{exc.error_class}",
                        failure_reason=(
                            f"Candidate materialization failed before decomposition vetting because "
                            f"{exc.agent_key} returned {exc.error_class}. "
                            f"Retry the same candidate before replacing the decomposition strategy. "
                            f"Artifact: {exc.parse_error_artifact or exc.artifact_prefix}"
                        ),
                        previous_attempt_summaries=payload.previous_attempt_summaries,
                    )
                    self.decomposition_candidates.create(candidate_row)
                    owner_lemma = self.lemmas.get(node_id)
                    if owner_lemma is not None:
                        self._refresh_lemma_decomposition_counters(owner_lemma)
                    self.event_logger.transition(
                        problem.problem_id,
                        "decomposition.candidate_agent_error",
                        None,
                        "rejected_fatal",
                        target_node_id=candidate_row.candidate_id,
                        reason=json.dumps(exc.as_dict(), sort_keys=True),
                        worker_job_id=decompose_job_id,
                    )
                    continue

                decomp = DecompositionORM(
                    decomposition_id=decomposition_id,
                    problem_id=problem.problem_id,
                    logical_decomposition_id=logical_decomposition_id,
                    revision_number=self._next_decomposition_revision_number(
                        problem_id=problem.problem_id,
                        logical_decomposition_id=logical_decomposition_id,
                    ),
                    node_id=node_id,
                    node_kind=node_kind,
                    strategy_summary=getattr(candidate, "strategy_summary", "agent_error"),
                    shared_context=[],
                    lemma_ids=[],
                    assembly_plan_id=assembly_plan_id,
                    pinned_statement_signatures=None,
                    formalization_cost_estimate=0,
                    llm_vetting_status="rejected_fatal",
                    lean_assembly_status=LeanAssemblyStatus.PENDING.value,
                    controller_status=ControllerStatus.FAILED.value,
                    raw_candidate_artifact_id=raw_candidate_artifact_id,
                    failure_origin=f"{exc.agent_key}:{exc.error_class}",
                    failure_reason=(
                        f"Candidate materialization failed before decomposition vetting because "
                        f"{exc.agent_key} returned {exc.error_class}. "
                        f"Retry the same candidate before replacing the decomposition strategy. "
                        f"Artifact: {exc.parse_error_artifact or exc.artifact_prefix}"
                    ),
                    previous_attempt_summaries=payload.previous_attempt_summaries,
                )
                plan = AssemblyPlanORM(
                    assembly_plan_id=assembly_plan_id,
                    problem_id=problem.problem_id,
                    decomposition_id=decomposition_id,
                    root_node_id=node_id,
                    steps=[],
                    proof_skeleton_nl="",
                    is_trivially_composable=False,
                )
                self.decompositions.create(decomp)
                self.assembly_plans.create(plan)
                self.event_logger.transition(
                    problem.problem_id,
                    "decomposition.candidate_agent_error",
                    None,
                    "rejected_fatal",
                    target_node_id=decomposition_id,
                    reason=json.dumps(exc.as_dict(), sort_keys=True),
                    worker_job_id=decompose_job_id,
                )
                continue

            if materialized is not None:
                accepted_count += materialized

        return generated, accepted_count

    def _materialize_single_candidate(
        self,
        *,
        problem: ProblemORM,
        node_id: str,
        node_kind: str,
        request_tag: str | None,
        theorem_nl: str,
        theorem_semantic_sketch: dict[str, Any],
        parent_depth: int,
        payload: Agent2Input,
        decompose_job_id: str,
        candidate: Any,
        decomposition_id: str,
        assembly_plan_id: str,
        raw_candidate_artifact_id: str,
        cfg: ProblemConfig,
        pre_vetted_bundle: Agent8Output | None = None,
        decomposition_origin: str | None = None,
        decomposition_origin_reason: str | None = None,
        decomposition_origin_job_id: str | None = None,
    ) -> int | None:
        """Materialize a single decomposition candidate.

        Returns the number of accepted decompositions (0 or 1), or None
        if the candidate was skipped (e.g. truncated by cap).
        """
        local_to_global: dict[str, str] = {}
        local_semantic_by_id: dict[str, dict[str, Any]] = {}
        candidate_lemma_rows: list[LemmaORM] = []
        truncated_by_cap = False
        for lemma in candidate.lemmas:
            if len(self.lemmas.list_by_problem(problem.problem_id)) + len(candidate_lemma_rows) >= cfg.lemma_solving.max_total_lemma_nodes:
                truncated_by_cap = True
                break
            lemma_id = new_id("lem")
            local_to_global[lemma.local_id] = lemma_id
            # Always canonicalize lemma semantics via Agent1 before vetting/solving.
            sketch = self.agents.semantic_sketch(
                lemma.statement_nl,
                f"problems/{problem.problem_id}/lemmas/{lemma_id}/semantic_sketch",
            )
            sketch_payload = sketch.semantic_sketch.model_dump()
            local_semantic_by_id[lemma.local_id] = sketch_payload
            candidate_lemma_rows.append(
                LemmaORM(
                    lemma_id=lemma_id,
                    problem_id=problem.problem_id,
                    parent_id=node_id,
                    parent_kind=node_kind,
                    kind=NodeKind.LEMMA.value,
                    depth=parent_depth + 1,
                    statement_nl=lemma.statement_nl,
                    statement_semantic_sketch=sketch_payload,
                    role_in_parent=lemma.role_in_assembly,
                    formalization_cost_estimate=lemma.formalization_cost_estimate,
                    latest_nl_proof=lemma.proof_nl,
                    proof_status=(
                        ProofStatus.NL_ACCEPTED.value
                        if pre_vetted_bundle is not None and cfg.mode.nl_only_mode and lemma.proof_nl
                        else ProofStatus.PROOF_VETTED.value
                        if pre_vetted_bundle is not None and lemma.proof_nl
                        else ProofStatus.OPEN.value
                    ),
                    routing_status=(
                        RoutingStatus.DONE.value
                        if pre_vetted_bundle is not None and cfg.mode.nl_only_mode and lemma.proof_nl
                        else RoutingStatus.READY_FOR_LEAN.value
                        if pre_vetted_bundle is not None and lemma.proof_nl and not cfg.mode.nl_only_mode
                        else RoutingStatus.OPEN.value
                    ),
                    next_action=(
                        "done"
                        if pre_vetted_bundle is not None and cfg.mode.nl_only_mode and lemma.proof_nl
                        else "formalize_in_lean"
                        if pre_vetted_bundle is not None and lemma.proof_nl and not cfg.mode.nl_only_mode
                        else None
                    ),
                )
            )

        # Do not accept partially materialized candidates; they would break
        # assembly references and create hidden obligations.
        if truncated_by_cap or not candidate_lemma_rows or len(candidate_lemma_rows) != len(candidate.lemmas):
            return None

        vet_job_id = decomposition_origin_job_id or f"wrk_{decomposition_id}_vet"
        if pre_vetted_bundle is None:
            decomposition_payload = {
                "candidate_index": candidate.candidate_index,
                "strategy_summary": candidate.strategy_summary,
                "shared_context": candidate.context_items or candidate.shared_context,
                "lemmas": [
                    {
                        **lemma.model_dump(),
                        "semantic_sketch": local_semantic_by_id.get(
                            lemma.local_id,
                            lemma.semantic_sketch.model_dump(),
                        ),
                    }
                    for lemma in candidate.lemmas
                ],
                "assembly_plan": candidate.assembly_plan.model_dump(),
            }
            decision_payload = Agent3Input(
                theorem_nl=theorem_nl,
                root_semantic_sketch=theorem_semantic_sketch,
                decomposition=decomposition_payload,
                previous_attempt_summaries=payload.previous_attempt_summaries,
                risk_audit=self._decomposition_risk_audit(
                    theorem_nl=theorem_nl,
                    decomposition=decomposition_payload,
                    previous_attempt_summaries=payload.previous_attempt_summaries,
                ),
            )
            vet_job = WorkerJob(
                job_id=vet_job_id,
                problem_id=problem.problem_id,
                worker_kind="decomposition_vetting",
                payload=decision_payload.model_dump(),
            )
            vet = self.workers.run_decomposition_vetting(
                vet_job,
                decision_payload,
                f"problems/{problem.problem_id}/decomposition_vetter/{decomposition_id}",
            )
            has_false_lemma = any(item.get("statement_status") == "false" for item in vet.lemma_findings)
            drift_severity = vet.drift_assessment.get("drift_severity")
            vet_decision = vet.decision
            vet_summary = vet.summary
            vet_fatal_reason = vet.fatal_reason
            fixes_required = vet.fixes_required
            coverage_check = vet.coverage_check
        else:
            vet = None
            has_false_lemma = False
            drift_severity = None
            vet_decision = "accepted" if pre_vetted_bundle.decision == "approved" else "fatal"
            vet_summary = pre_vetted_bundle.summary
            vet_fatal_reason = pre_vetted_bundle.summary
            fixes_required = []
            coverage_check = {}

        is_major_drift = drift_severity == "major"
        final_step_yields_root = bool(candidate.assembly_plan.final_step_yields_exact_root)
        is_trivial_assembly = all(step.get("is_trivial", True) for step in candidate.assembly_plan.steps) and final_step_yields_root

        accepted_count = 0
        llm_status = "accepted"
        controller_status = ControllerStatus.PENDING.value
        failure_origin: str | None = None
        failure_reason: str | None = None
        if not final_step_yields_root:
            llm_status = "rejected_fatal"
            controller_status = ControllerStatus.FAILED.value
            failure_origin = "assembly_plan:final_step_yields_exact_root"
            failure_reason = (
                "Rejected before vetting: assembly plan set final_step_yields_exact_root=false "
                "(the assembly did not prove the root theorem). Fix: ensure every gap in the "
                "assembly is covered by an explicit lemma so the assembly is logically complete "
                "given the lemmas. final_step_yields_exact_root MUST be true."
            )
        elif vet_decision == "fatal" or has_false_lemma or is_major_drift:
            llm_status = "rejected_fatal"
            controller_status = ControllerStatus.FAILED.value
            failure_origin = "agent3:false_lemma" if has_false_lemma else "agent3:fatal"
            if pre_vetted_bundle is not None and pre_vetted_bundle.decision != "approved":
                failure_origin = decomposition_origin or "split_existing_proof"
            failure_reason = str(vet_fatal_reason or vet_summary or "Decomposition vetter rejected the candidate.")
        elif vet_decision == "minor_fix":
            # Only promote minor_fix to accepted when the issue is
            # specifically minor drift AND the config allows warning-only.
            if drift_severity == "minor" and cfg.drift.minor_drift_adds_warning_only:
                llm_status = "accepted"
                controller_status = ControllerStatus.PENDING.value
                accepted_count += 1
            else:
                llm_status = "rejected_minor"
                controller_status = ControllerStatus.FAILED.value
                failure_origin = "agent3:minor_fix"
                failure_reason = str(vet_summary or "Decomposition vetter requested minor fixes before acceptance.")
        else:
            accepted_count += 1

        accepted_steps = []
        for step in candidate.assembly_plan.steps:
            uses = [local_to_global.get(local_id, local_id) for local_id in step.get("uses_lemmas", [])]
            translated = dict(step)
            translated["uses_lemmas"] = uses
            accepted_steps.append(translated)

        is_accepted = llm_status == "accepted"
        logical_decomposition_id = self._logical_decomposition_id(
            node_id=node_id,
            node_kind=node_kind,
            request_tag=request_tag,
        )
        if node_kind == NodeKind.LEMMA.value:
            candidate_id = new_id("deccand")
            materialization_artifact_id = None
            selection_status = "pending_selection" if is_accepted else llm_status
            if is_accepted:
                materialization_artifact_id = self._lemma_candidate_bundle_key(problem.problem_id, candidate_id)
                self.artifacts.save_json(
                    materialization_artifact_id,
                    {
                        "candidate_id": candidate_id,
                        "node_id": node_id,
                        "node_kind": node_kind,
                        "strategy_summary": candidate.strategy_summary,
                        "shared_context": candidate.context_items or candidate.shared_context,
                        "formalization_cost_estimate": candidate.formalization_cost_estimate_total,
                        "proof_skeleton_nl": candidate.assembly_plan.proof_skeleton_nl,
                        "is_trivially_composable": is_trivial_assembly,
                        "accepted_steps": accepted_steps,
                        "candidate_lemma_rows": [row.model_dump(mode="json") for row in candidate_lemma_rows],
                    },
                )
            candidate_row = DecompositionCandidateORM(
                candidate_id=candidate_id,
                problem_id=problem.problem_id,
                node_id=node_id,
                node_kind=node_kind,
                logical_decomposition_id=logical_decomposition_id,
                candidate_index=int(getattr(candidate, "candidate_index", 0) or 0),
                strategy_summary=candidate.strategy_summary,
                shared_context=candidate.context_items or candidate.shared_context,
                formalization_cost_estimate=candidate.formalization_cost_estimate_total,
                llm_vetting_status=llm_status,
                selection_status=selection_status,
                raw_candidate_artifact_id=raw_candidate_artifact_id,
                materialization_artifact_id=materialization_artifact_id,
                failure_origin=failure_origin,
                failure_reason=failure_reason,
                previous_attempt_summaries=payload.previous_attempt_summaries,
                decomposition_origin=decomposition_origin,
                decomposition_origin_reason=decomposition_origin_reason,
                decomposition_origin_job_id=decomposition_origin_job_id,
            )
            self.decomposition_candidates.create(candidate_row)
            owner_lemma = self.lemmas.get(node_id)
            if owner_lemma is not None:
                self._refresh_lemma_decomposition_counters(owner_lemma)

            if drift_severity == "minor":
                self.event_logger.transition(
                    problem.problem_id,
                    "decomposition.drift_warning",
                    None,
                    "minor",
                    target_node_id=candidate_row.candidate_id,
                    reason=vet_summary,
                    worker_job_id=vet_job_id,
                )
            self.event_logger.transition(
                problem.problem_id,
                "decomposition.generated",
                None,
                llm_status,
                target_node_id=candidate_row.candidate_id,
                reason=(
                    failure_reason
                    if llm_status != "accepted" and failure_reason
                    else vet_summary
                    if final_step_yields_root
                    else "final assembly step does not yield root theorem"
                ),
                worker_job_id=decompose_job_id,
            )
            return accepted_count

        decomposition_lemma_ids = [row.lemma_id for row in candidate_lemma_rows] if is_accepted else []
        decomp = DecompositionORM(
            decomposition_id=decomposition_id,
            problem_id=problem.problem_id,
            logical_decomposition_id=logical_decomposition_id,
            revision_number=self._next_decomposition_revision_number(
                problem_id=problem.problem_id,
                logical_decomposition_id=logical_decomposition_id,
            ),
            node_id=node_id,
            node_kind=node_kind,
            proof_graph_id=None,
            strategy_summary=candidate.strategy_summary,
            shared_context=candidate.context_items or candidate.shared_context,
            lemma_ids=decomposition_lemma_ids,
            assembly_plan_id=assembly_plan_id,
            pinned_statement_signatures=None,
            formalization_cost_estimate=candidate.formalization_cost_estimate_total,
            llm_vetting_status=llm_status,
            lean_assembly_status=(
                LeanAssemblyStatus.SKIPPED.value if cfg.mode.nl_only_mode and llm_status == "accepted" else LeanAssemblyStatus.PENDING.value
            ),
            controller_status=controller_status,
            dependency_status="legacy_unknown",
            raw_candidate_artifact_id=raw_candidate_artifact_id,
            failure_origin=failure_origin,
            failure_reason=failure_reason,
            equivalence_risk="none",
            previous_attempt_summaries=payload.previous_attempt_summaries,
            decomposition_origin=decomposition_origin,
            decomposition_origin_reason=decomposition_origin_reason,
            decomposition_origin_job_id=decomposition_origin_job_id,
        )
        plan = AssemblyPlanORM(
            assembly_plan_id=assembly_plan_id,
            problem_id=problem.problem_id,
            decomposition_id=decomposition_id,
            root_node_id=node_id,
            steps=accepted_steps if is_accepted else [dict(step) for step in candidate.assembly_plan.steps],
            proof_skeleton_nl=candidate.assembly_plan.proof_skeleton_nl,
            is_trivially_composable=is_trivial_assembly,
        )

        self.decompositions.create(decomp)
        self.assembly_plans.create(plan)
        if is_accepted:
            graph = self.proof_graphs.ensure_graph_for_decomposition(problem, decomp)
            if graph is not None:
                decomp.proof_graph_id = graph.proof_graph_id
                decomp.dependency_status = "passed"
                if node_kind == NodeKind.THEOREM.value:
                    problem.active_proof_graph_id = graph.proof_graph_id
                    self.problems.save(problem)
            for row in candidate_lemma_rows:
                row.proof_graph_id = decomp.proof_graph_id
            self.lemmas.create_many(candidate_lemma_rows)
            theorem_row = self.theorems.get(problem.root_theorem_id) if node_kind == NodeKind.THEOREM.value and problem.root_theorem_id else None
            graph, _, reduction_check = self.proof_graphs.bootstrap_decomposition_graph(
                problem=problem,
                theorem=theorem_row,
                decomp=decomp,
                lemmas=candidate_lemma_rows,
            )
            for row in candidate_lemma_rows:
                self.lemmas.save(row)
            if graph is not None:
                target_node_id = decomp.decomposition_id
                self.proof_graphs.record_dependency_check(
                    proof_graph_id=graph.proof_graph_id,
                    target_node_id=target_node_id,
                    artifact_kind="decomposition",
                    artifact_id=decomposition_id,
                    check_status="passed" if not reduction_check else "retryable_violation",
                    violations=reduction_check,
                )
            self.decompositions.save(decomp)
            if not cfg.mode.nl_only_mode:
                self._submit_prepare_track_if_v2(problem, decomp, cfg)

        if drift_severity == "minor":
            self.event_logger.transition(
                problem.problem_id,
                "decomposition.drift_warning",
                None,
                "minor",
                target_node_id=decomposition_id,
                reason=vet_summary,
                worker_job_id=vet_job_id,
            )
        self.event_logger.transition(
            problem.problem_id,
            "decomposition.generated",
            None,
            llm_status,
            target_node_id=decomposition_id,
            reason=(
                failure_reason
                if llm_status != "accepted" and failure_reason
                else vet_summary
                if final_step_yields_root
                else "final assembly step does not yield root theorem"
            ),
            worker_job_id=decompose_job_id,
        )

        return accepted_count

    def _generate_decompositions_for_node(
        self,
        *,
        problem: ProblemORM,
        node_id: str,
        node_kind: str,
        theorem_nl: str,
        theorem_semantic_sketch: dict[str, Any],
        parent_depth: int,
        artifact_prefix: str,
        cfg: ProblemConfig,
    ) -> tuple[bool, int]:
        remaining_slots = self._remaining_decomposition_slots(problem.problem_id, node_id, node_kind, cfg)
        if remaining_slots <= 0:
            return False, 0

        local_previous_attempt_summaries = self._node_previous_attempt_summaries(problem.problem_id, node_id)
        if node_kind == NodeKind.THEOREM.value:
            num_candidates = remaining_slots
        else:
            num_candidates = min(cfg.decomposition.max_candidates_generated, remaining_slots)

        payload = self._build_decomposition_generation_payload(
            problem=problem,
            node_id=node_id,
            theorem_nl=theorem_nl,
            theorem_semantic_sketch=theorem_semantic_sketch,
            num_candidates=num_candidates,
        )
        # Use the first-attempt lemma model override when this is
        # the first decomposition of a lemma (no previous attempts).
        override_key: str | None = None
        if node_kind == NodeKind.LEMMA.value and not local_previous_attempt_summaries:
            override_key = self._consume_lemma_override_once(
                node_id,
                override_key="agent2_first_lemma",
            )
        decompose_job, payload, output = self._run_decomposition_generation_request(
            problem=problem,
            node_id=node_id,
            payload=payload,
            artifact_prefix=artifact_prefix,
            override_key=override_key,
        )
        return self._materialize_decomposition_candidates(
            problem=problem,
            node_id=node_id,
            node_kind=node_kind,
            request_tag=None,
            theorem_nl=theorem_nl,
            theorem_semantic_sketch=theorem_semantic_sketch,
            parent_depth=parent_depth,
            payload=payload,
            decompose_job_id=decompose_job.job_id,
            candidates=output.candidates,
            max_candidates=remaining_slots,
            cfg=cfg,
        )

    def _generate_root_decompositions(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        generated = False
        accepted_count = 0
        max_tracks = self._root_parallel_take_k(cfg)

        harvested_generated, harvested_accepted = self._harvest_completed_root_generation_attempts(
            problem=problem,
            root=root,
            cfg=cfg,
        )
        generated |= harvested_generated
        accepted_count += harvested_accepted

        current_tracks = self._current_root_track_count(problem.problem_id, root.theorem_id)
        if current_tracks >= max_tracks:
            return generated

        attempts = self._list_root_generation_attempts(
            problem.problem_id,
            continuation_generation=self._continuation_generation(problem),
        )
        inflight_count = sum(
            1
            for row in attempts
            if row.get("latest_state") in {"queued", "running", "started", "deferred"}
            and not row.get("has_terminal_artifact", False)
        )
        request_budget = max(0, cfg.decomposition.parallel_root_decompositions_n - inflight_count)
        if not attempts:
            request_count = max(1, request_budget)
        else:
            needed_tracks = max(0, max_tracks - current_tracks)
            request_count = min(needed_tracks, request_budget)

        if request_count <= 0:
            return generated

        if request_count > 0:
            prior_summaries = self._previous_attempt_summaries(problem.problem_id, root.theorem_id)
            local_prior_summaries = self._node_previous_attempt_summaries(problem.problem_id, root.theorem_id)
            trusted_summaries = self._trusted_context_summaries(problem.problem_id)
            for idx in range(request_count):
                payload = self._build_decomposition_generation_payload(
                    problem=problem,
                    node_id=root.theorem_id,
                    theorem_nl=root.statement_nl,
                    theorem_semantic_sketch=root.statement_semantic_sketch,
                    num_candidates=1,
                    previous_attempt_summaries=prior_summaries,
                    trusted_context_summaries=trusted_summaries,
                )
                attempt_prefix = f"problems/{problem.problem_id}/decomposer/{new_id('attempt')}_track_{idx + 1}"
                decompose_job = self._build_decomposition_generation_job(
                    problem_id=problem.problem_id,
                    node_id=root.theorem_id,
                    payload=payload,
                    request_tag=f"track_{idx + 1}_{new_id('req')}",
                )
                self.artifacts.save_json(f"{attempt_prefix}/agent2_input.json", payload.model_dump())
                self._mark_root_generation_attempt_queued(
                    artifact_prefix=attempt_prefix,
                    worker_job_id=decompose_job.job_id,
                    cfg=cfg,
                )
                # Use the first-root model override only when there are
                # no previous attempts (first decomposition of the root).
                root_override_key = None
                if not local_prior_summaries:
                    root_override_key = self._consume_theorem_override_once(
                        root.theorem_id,
                        override_key="agent2_first_root",
                    )
                root_llm_profile = (
                    cfg.llm.agent2_first_root
                    if root_override_key == "agent2_first_root" and cfg.llm.agent2_first_root is not None
                    else cfg.llm.agent2
                )
                root_job_max_attempts = (
                    max(1, int(root_llm_profile.max_attempts))
                    if isinstance(getattr(root_llm_profile, "max_attempts", None), int)
                    else 2
                )
                self.worker_jobs.enqueue_if_absent(
                    WorkerJobORM(
                        worker_job_id=decompose_job.job_id,
                        problem_id=problem.problem_id,
                        worker_kind="decomposition_generation",
                        status="queued",
                        target_id=root.theorem_id,
                        target_kind=NodeKind.THEOREM.value,
                        execution_id=self.execution_id,
                        continuation_generation=self._continuation_generation(problem),
                        attempt_number=max(1, len(prior_summaries) + 1),
                        artifact_prefix=attempt_prefix,
                        handler_key="agent2_root_generation",
                        request_source="root_generation",
                        payload=payload.model_dump(),
                        llm_override_key=root_override_key,
                        max_attempts=root_job_max_attempts,
                    )
                )
                generated = True
            self.event_logger.transition(
                problem.problem_id,
                "decomposition.parallel_generation_enqueued",
                None,
                "queued",
                target_node_id=root.theorem_id,
                reason=f"request_count={request_count}",
            )

        if not generated:
            return False

        if accepted_count == 0 and self._remaining_decomposition_slots(
            problem.problem_id,
            root.theorem_id,
            NodeKind.THEOREM.value,
            cfg,
        ) <= 0 and self._root_generation_inflight_attempt_count(
            problem.problem_id,
            continuation_generation=self._continuation_generation(problem),
        ) == 0:
            self._mark_failed(
                problem,
                FailureReason.PROOF_EXHAUSTED.value,
                terminal_error_message="All decomposition candidates rejected.",
            )
        return True

    def _ensure_assembly_jobs(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        return self._ensure_assembly_jobs_for_node(
            problem,
            node_id=root.theorem_id,
            theorem_nl=root.statement_nl,
            theorem_semantic_sketch=root.statement_semantic_sketch,
            cfg=cfg,
        )

    def _ensure_assembly_jobs_for_node(
        self,
        problem: ProblemORM,
        *,
        node_id: str,
        theorem_nl: str,
        theorem_semantic_sketch: dict[str, Any],
        cfg: ProblemConfig,
    ) -> bool:
        if cfg.mode.nl_only_mode:
            return False
        changed = False
        for dec in self.decompositions.list_by_node(problem.problem_id, node_id):
            if dec.llm_vetting_status != "accepted":
                continue
            if dec.controller_status == ControllerStatus.FAILED.value:
                continue
            if dec.lean_v2_track_id and dec.lean_v2_prepare_status == "success":
                changed |= self._activate_ready_for_lean_lemmas(problem=problem, dec=dec, cfg=cfg)
                continue
            changed |= self._submit_prepare_track_if_v2(problem, dec, cfg)

        return changed

    def _select_active_or_fail(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        candidates: list[DecompositionORM] = []
        node_rows = self.decompositions.list_by_node(problem.problem_id, root.theorem_id)
        for dec in node_rows:
            if self._decomposition_ready_for_selection(dec, cfg):
                candidates.append(dec)

        if not candidates:
            # Check rejection count for root-level decompositions.
            # Without this, the system infinitely retries when all candidates
            # are rejected, since _remaining_decomposition_slots for root
            # only counts accepted tracks.  For root nodes, count all
            # consecutive rejections (minor + fatal), not just fatal.
            total_rejections = sum(
                1
                for dec in node_rows
                if dec.llm_vetting_status in {"rejected_fatal", "rejected_minor"}
            )
            streak_exceeded = total_rejections > cfg.decomposition.max_consecutive_fatal_rejections_per_node

            if not streak_exceeded and self._remaining_decomposition_slots(
                problem.problem_id,
                root.theorem_id,
                NodeKind.THEOREM.value,
                cfg,
            ) > 0:
                return self._generate_root_decompositions(problem, root, cfg)

            if cfg.mode.nl_only_mode:
                self._mark_failed(
                    problem,
                    FailureReason.PROOF_EXHAUSTED.value,
                    terminal_error_message="No decomposition survived NL vetting.",
                )
                return True

            all_terminal = all(
                not self._decomposition_pending_for_selection(dec, cfg)
                for dec in node_rows
                if dec.llm_vetting_status == "accepted"
            )
            if all_terminal:
                self._mark_failed(
                    problem,
                    FailureReason.ASSEMBLY_COMPOSITION_FAILURE.value,
                    terminal_error_message="No decomposition survived Lean track preparation.",
                )
                return True
            return False

        scoped_ids = {
            dec.decomposition_id
            for dec in sorted(candidates, key=lambda row: row.created_at)[: self._root_parallel_take_k(cfg)]
        }
        return self._select_active_decomposition_for_node(
            problem,
            root.theorem_id,
            NodeKind.THEOREM.value,
            cfg,
            candidate_scope_ids=scoped_ids,
        )

    def _root_parallel_workset(self, problem: ProblemORM, cfg: ProblemConfig) -> list[DecompositionORM]:
        if not problem.root_theorem_id:
            return []

        rows = self.decompositions.list_by_node(problem.problem_id, problem.root_theorem_id)
        ready: list[DecompositionORM] = []
        for dec in rows:
            if self._decomposition_ready_for_selection(dec, cfg):
                ready.append(dec)
        ready.sort(key=lambda row: (row.created_at, row.decomposition_id))
        return ready[: self._root_parallel_take_k(cfg)]

    def _process_parallel_root_decompositions(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        changed = False
        active_id = problem.active_decomposition_id
        for dec in self._root_parallel_workset(problem, cfg):
            if dec.decomposition_id == active_id:
                continue
            if dec.controller_status in {ControllerStatus.SUCCEEDED.value, ControllerStatus.FAILED.value}:
                continue
            changed |= self._process_decomposition(problem, root, dec, cfg)
            if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                break
        return changed

    def _select_active_decomposition_for_node(
        self,
        problem: ProblemORM,
        node_id: str,
        node_kind: str,
        cfg: ProblemConfig,
        candidate_scope_ids: set[str] | None = None,
    ) -> bool:
        if node_kind == NodeKind.LEMMA.value:
            active = next(
                (
                    dec
                    for dec in self.decompositions.list_by_node(problem.problem_id, node_id)
                    if dec.controller_status == ControllerStatus.ACTIVE.value
                    and dec.controller_status != ControllerStatus.FAILED.value
                ),
                None,
            )
            if active is not None:
                return True

            candidates = self._unpromoted_lemma_candidates(problem.problem_id, node_id)
            if candidate_scope_ids is not None:
                candidates = [row for row in candidates if row.candidate_id in candidate_scope_ids]
            if not candidates:
                return False
            chosen = sorted(candidates, key=self._lemma_candidate_sort_key)[0]
            chosen.selection_status = "selected"
            self.decomposition_candidates.save(chosen)
            promoted = self._promote_lemma_candidate(problem=problem, candidate_row=chosen, cfg=cfg)
            if promoted is None:
                return False
            self.event_logger.transition(
                problem.problem_id,
                "decomposition.selected",
                chosen.candidate_id,
                promoted.decomposition_id,
                target_node_id=promoted.decomposition_id,
                reason=f"selected lemma candidate for node={node_id} by minimum formalization_cost_estimate",
            )
            return True

        node_decs = self.decompositions.list_by_node(problem.problem_id, node_id)
        accepted = [
            dec
            for dec in node_decs
            if dec.llm_vetting_status == "accepted" and dec.controller_status != ControllerStatus.FAILED.value
        ]
        if not accepted:
            return False

        candidates = [dec for dec in accepted if self._decomposition_ready_for_selection(dec, cfg)]

        if candidate_scope_ids is not None:
            candidates = [dec for dec in candidates if dec.decomposition_id in candidate_scope_ids]

        if not candidates:
            return False

        candidates.sort(key=lambda d: d.formalization_cost_estimate or 999.0)
        active = candidates[0]
        standby = candidates[1] if len(candidates) > 1 else None

        active_ids = {dec.decomposition_id for dec in candidates}
        for dec in accepted:
            if dec.decomposition_id not in active_ids and dec.controller_status != ControllerStatus.FAILED.value:
                dec.controller_status = ControllerStatus.PENDING.value
                self.decompositions.save(dec)

        for idx, dec in enumerate(candidates):
            if idx == 0:
                dec.controller_status = ControllerStatus.ACTIVE.value
            elif idx == 1:
                dec.controller_status = ControllerStatus.STANDBY.value
            else:
                dec.controller_status = ControllerStatus.PENDING.value
            self.decompositions.save(dec)

        if node_kind == NodeKind.THEOREM.value:
            problem.active_decomposition_id = active.decomposition_id
            problem.standby_decomposition_id = standby.decomposition_id if standby else None
            self.problems.save(problem)
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if root:
                root.active_decomposition_id = active.decomposition_id
                self.theorems.save(root)

        self.event_logger.transition(
            problem.problem_id,
            "decomposition.selected",
            None,
            active.decomposition_id,
            target_node_id=active.decomposition_id,
            reason=f"selected for node={node_id} by minimum formalization_cost_estimate",
        )
        return True

    def _promote_standby_decomposition(self, problem: ProblemORM, node_id: str, node_kind: str) -> bool:
        if node_kind == NodeKind.LEMMA.value:
            node_decs = self.decompositions.list_by_node(problem.problem_id, node_id)
            active = next((d for d in node_decs if d.controller_status == ControllerStatus.ACTIVE.value), None)
            if active is not None and active.controller_status != ControllerStatus.FAILED.value:
                active.controller_status = ControllerStatus.FAILED.value
                self.decompositions.save(active)
            return self._select_active_decomposition_for_node(problem, node_id, node_kind, ProblemConfig.model_validate(problem.config))

        node_decs = self.decompositions.list_by_node(problem.problem_id, node_id)
        active = next((d for d in node_decs if d.controller_status == ControllerStatus.ACTIVE.value), None)
        if active:
            active.controller_status = ControllerStatus.FAILED.value
            self.decompositions.save(active)

        ready = [
            d
            for d in node_decs
            if self._decomposition_ready_for_selection(d, ProblemConfig.model_validate(problem.config))
            and d.controller_status in {ControllerStatus.STANDBY.value, ControllerStatus.PENDING.value}
        ]
        ready.sort(key=lambda d: d.formalization_cost_estimate or 999.0)
        if not ready:
            return False

        promoted = ready[0]
        promoted.controller_status = ControllerStatus.ACTIVE.value
        self.decompositions.save(promoted)

        next_standby = ready[1] if len(ready) > 1 else None
        if next_standby:
            next_standby.controller_status = ControllerStatus.STANDBY.value
            self.decompositions.save(next_standby)

        if node_kind == NodeKind.THEOREM.value:
            problem.active_decomposition_id = promoted.decomposition_id
            problem.standby_decomposition_id = next_standby.decomposition_id if next_standby else None
            self.problems.save(problem)
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if root:
                root.active_decomposition_id = promoted.decomposition_id
                self.theorems.save(root)

        self.event_logger.transition(
            problem.problem_id,
            "decomposition.promoted",
            active.decomposition_id if active else None,
            promoted.decomposition_id,
            target_node_id=promoted.decomposition_id,
            reason=f"standby promotion for node={node_id}",
        )
        return True

    def _process_child_decomposition_for_lemma(
        self,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> bool:
        node_decs = self.decompositions.list_by_node(problem.problem_id, lemma.lemma_id)
        candidate_rows = self.decomposition_candidates.list_by_node(problem.problem_id, lemma.lemma_id)
        if not node_decs:
            false_candidate = next((row for row in candidate_rows if self._child_decomposition_is_false_frontier(row)), None)
            if false_candidate is not None:
                self._invalidate_parent_decomposition(
                    problem,
                    lemma,
                    cfg,
                    reason=false_candidate.failure_reason or "child decomposition candidate determined lemma is false",
                )
                return True
            if candidate_rows and self._select_active_decomposition_for_node(problem, lemma.lemma_id, NodeKind.LEMMA.value, cfg):
                return True
            if any(self._child_decomposition_is_live_accepted(row) for row in candidate_rows):
                if (
                    lemma.routing_status != RoutingStatus.BLOCKED.value
                    or lemma.next_action != "wait_on_child_decomposition"
                ):
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.PROOF_FLAWED.value,
                        routing_status=RoutingStatus.BLOCKED.value,
                        next_action="wait_on_child_decomposition",
                        reason="accepted child decomposition candidate exists but promotion is not yet complete",
                        clear_solver_series=True,
                    )
                    return True
            remaining_slots = self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            )
            if (
                candidate_rows
                and remaining_slots > 0
                and lemma.next_action in {"retry_decomposition", "evaluate_child_decompositions"}
            ):
                outcome, detail = self._decompose_current_lemma_result(problem, lemma, cfg)
                if outcome in {
                    self._DECOMPOSE_OUTCOME_CHILD_SELECTED,
                    self._DECOMPOSE_OUTCOME_CHILD_PENDING,
                }:
                    self.event_logger.transition(
                        problem.problem_id,
                        "lemma.decomposition_retry_scheduled",
                        None,
                        "queued",
                        target_node_id=lemma.lemma_id,
                        reason=f"rejected child candidates only; remaining_slots={remaining_slots}",
                    )
                    return True
                if outcome == self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND and self._remaining_decomposition_slots(
                    problem.problem_id,
                    lemma.lemma_id,
                    NodeKind.LEMMA.value,
                    cfg,
                ) > 0:
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.PROOF_FLAWED.value,
                        routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                        next_action="retry_decomposition",
                        reason=detail,
                        clear_solver_series=True,
                    )
                    self.event_logger.transition(
                        problem.problem_id,
                        "lemma.decomposition_retry_scheduled",
                        None,
                        "retry_decomposition",
                        target_node_id=lemma.lemma_id,
                        reason=f"{detail}; remaining_slots={self._remaining_decomposition_slots(problem.problem_id, lemma.lemma_id, NodeKind.LEMMA.value, cfg)}",
                    )
                    return True
            if lemma.proof_status == ProofStatus.PROOF_EXHAUSTED.value and lemma.routing_status == RoutingStatus.BLOCKED.value:
                if remaining_slots > 0:
                    outcome, detail = self._decompose_current_lemma_result(problem, lemma, cfg)
                    if outcome in {
                        self._DECOMPOSE_OUTCOME_CHILD_SELECTED,
                        self._DECOMPOSE_OUTCOME_CHILD_PENDING,
                    }:
                        self.event_logger.transition(
                            problem.problem_id,
                            "lemma.decomposition_retry_scheduled",
                            None,
                            "queued",
                            target_node_id=lemma.lemma_id,
                            reason=f"blocked exhausted lemma had no child decompositions; remaining_slots={remaining_slots}",
                        )
                        return True
                    if outcome == self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND and self._remaining_decomposition_slots(
                        problem.problem_id,
                        lemma.lemma_id,
                        NodeKind.LEMMA.value,
                        cfg,
                    ) > 0:
                        self._save_lemma_transition(
                            lemma,
                            proof_status=ProofStatus.PROOF_FLAWED.value,
                            routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                            next_action="retry_decomposition",
                            reason=detail,
                            clear_solver_series=True,
                        )
                        return True
            return False

        false_child = next((d for d in node_decs if self._child_decomposition_is_false_frontier(d)), None)
        if false_child is None:
            false_candidate = next((row for row in candidate_rows if self._child_decomposition_is_false_frontier(row)), None)
            if false_candidate is not None:
                self._invalidate_parent_decomposition(
                    problem,
                    lemma,
                    cfg,
                    reason=false_candidate.failure_reason or "child decomposition candidate determined lemma is false",
                    counterexample_id=false_candidate.invalidated_by_counterexample_id,
                )
                return True
        if false_child is not None:
            self._invalidate_parent_decomposition(
                problem,
                lemma,
                cfg,
                reason=false_child.failure_reason or "child decomposition determined lemma is false",
                counterexample_id=false_child.invalidated_by_counterexample_id,
            )
            return True

        active = next((d for d in node_decs if d.controller_status == ControllerStatus.ACTIVE.value), None)
        if not active:
            if self._select_active_decomposition_for_node(problem, lemma.lemma_id, NodeKind.LEMMA.value, cfg):
                return True
            has_accepted_child = any(self._child_decomposition_is_live_accepted(d) for d in node_decs) or any(
                self._child_decomposition_is_live_accepted(row) for row in candidate_rows
            )
            if not cfg.mode.nl_only_mode and any(self._decomposition_pending_for_selection(d, cfg) for d in node_decs):
                if (
                    lemma.routing_status != RoutingStatus.BLOCKED.value
                    or lemma.next_action != "wait_on_child_decomposition"
                ):
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.PROOF_FLAWED.value,
                        routing_status=RoutingStatus.BLOCKED.value,
                        next_action="wait_on_child_decomposition",
                        reason="child decomposition exists but Lean track preparation is not yet complete",
                        clear_solver_series=True,
                    )
                return True
            if has_accepted_child:
                # Accepted child decomposition exists but is not ready for promotion yet.
                if (
                    lemma.routing_status != RoutingStatus.BLOCKED.value
                    or lemma.next_action != "wait_on_child_decomposition"
                ):
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.PROOF_FLAWED.value,
                        routing_status=RoutingStatus.BLOCKED.value,
                        next_action="wait_on_child_decomposition",
                        reason="accepted child decomposition exists but Lean track preparation is not yet complete",
                        clear_solver_series=True,
                    )
                    return True
                return True

            remaining_slots = self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            )
            if remaining_slots > 0:
                outcome, detail = self._decompose_current_lemma_result(problem, lemma, cfg)
                if outcome in {
                    self._DECOMPOSE_OUTCOME_CHILD_SELECTED,
                    self._DECOMPOSE_OUTCOME_CHILD_PENDING,
                }:
                    self.event_logger.transition(
                        problem.problem_id,
                        "lemma.decomposition_retry_scheduled",
                        None,
                        "queued",
                        target_node_id=lemma.lemma_id,
                        reason=f"no active child decomposition; remaining_slots={remaining_slots}",
                    )
                    return True
                if outcome == self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND and self._remaining_decomposition_slots(
                    problem.problem_id,
                    lemma.lemma_id,
                    NodeKind.LEMMA.value,
                    cfg,
                ) > 0:
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.PROOF_FLAWED.value,
                        routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                        next_action="retry_decomposition",
                        reason=detail,
                        clear_solver_series=True,
                    )
                    self.event_logger.transition(
                        problem.problem_id,
                        "lemma.decomposition_retry_scheduled",
                        None,
                        "retry_decomposition",
                        target_node_id=lemma.lemma_id,
                        reason=f"{detail}; remaining_slots={self._remaining_decomposition_slots(problem.problem_id, lemma.lemma_id, NodeKind.LEMMA.value, cfg)}",
                    )
                    return True

            terminal_reason = "child decomposition frontier exhausted with no accepted child decomposition"
            owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
            if owner is not None:
                self._handle_lemma_terminal_failure(
                    problem,
                    owner,
                    lemma,
                    failure_reason=FailureReason.PROOF_EXHAUSTED.value,
                    terminal_error_message=terminal_reason,
                    force_problem_failure=True,
                )
            else:
                self._mark_failed(
                    problem,
                    FailureReason.PROOF_EXHAUSTED.value,
                    terminal_lemma_id=lemma.lemma_id,
                    terminal_error_message=terminal_reason,
                )
            return True

        if active.controller_status == ControllerStatus.SUCCEEDED.value:
            if cfg.mode.nl_only_mode:
                if lemma.proof_status != ProofStatus.NL_ACCEPTED.value or lemma.routing_status != RoutingStatus.DONE.value:
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.NL_ACCEPTED.value,
                        routing_status=RoutingStatus.DONE.value,
                        next_action="done",
                        reason=f"child decomposition {active.decomposition_id} succeeded",
                        clear_solver_series=True,
                    )
                    return True
                return False
            if lemma.proof_status != ProofStatus.PROOF_VETTED.value:
                self._mark_current_proof_as_lean_ready(lemma)
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_VETTED.value,
                    routing_status=RoutingStatus.READY_FOR_LEAN.value,
                    next_action="formalize_in_lean",
                    reason=f"child decomposition {active.decomposition_id} succeeded",
                    clear_solver_series=True,
                )
                return True
            return False

        if active.controller_status == ControllerStatus.FAILED.value:
            if self._promote_standby_decomposition(problem, lemma.lemma_id, NodeKind.LEMMA.value):
                return True
            if self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            ) > 0:
                outcome, detail = self._decompose_current_lemma_result(problem, lemma, cfg)
                if outcome in {
                    self._DECOMPOSE_OUTCOME_CHILD_SELECTED,
                    self._DECOMPOSE_OUTCOME_CHILD_PENDING,
                    self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND,
                }:
                    if outcome == self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND:
                        self._save_lemma_transition(
                            lemma,
                            proof_status=ProofStatus.PROOF_FLAWED.value,
                            routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                            next_action="retry_decomposition",
                            reason=detail,
                            clear_solver_series=True,
                        )
                    return True
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_EXHAUSTED.value,
                routing_status=RoutingStatus.BLOCKED.value,
                next_action="terminal_failure",
                reason="active child decomposition failed and no decomposition slots remain",
                clear_solver_series=True,
            )
            return True

        return self._process_decomposition(problem, root, active, cfg)

    def _build_local_branch_context(self, lemma: LemmaORM, depth_cap: int) -> list[dict[str, Any]]:
        context: list[dict[str, Any]] = []
        current_parent_id = lemma.parent_id
        current_parent_kind = lemma.parent_kind
        depth = 0
        while depth < depth_cap and current_parent_id:
            if current_parent_kind == NodeKind.LEMMA.value:
                parent_lemma = self.lemmas.get(current_parent_id)
                if not parent_lemma:
                    break
                context.append(
                    {
                        "node_id": parent_lemma.lemma_id,
                        "kind": NodeKind.LEMMA.value,
                        "statement_nl": parent_lemma.statement_nl,
                        "semantic_sketch": parent_lemma.statement_semantic_sketch,
                        "role_in_parent": parent_lemma.role_in_parent,
                    }
                )
                current_parent_id = parent_lemma.parent_id
                current_parent_kind = parent_lemma.parent_kind
                depth += 1
                continue

            theorem = self.theorems.get(current_parent_id)
            if theorem:
                context.append(
                    {
                        "node_id": theorem.theorem_id,
                        "kind": NodeKind.THEOREM.value,
                        "statement_nl": theorem.statement_nl,
                        "semantic_sketch": theorem.statement_semantic_sketch,
                    }
                )
            break
        return context

    @staticmethod
    def _normalize_finding_severity(raw: Any) -> str:
        label = str(raw or "").strip().lower()
        normalized = {
            "none": "none",
            "": "none",
            "low": "low",
            "minor": "low",
            "info": "low",
            "pass": "low",
            "medium": "medium",
            "moderate": "medium",
            "warning": "medium",
            "warn": "medium",
            "high": "high",
            "fatal": "high",
            "critical": "high",
            "severe": "high",
            "major": "high",
            "blocker": "high",
        }
        return normalized.get(label, "none")

    @staticmethod
    def _max_finding_severity(detailed_findings: Any) -> str:
        rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
        max_level = "none"
        if not isinstance(detailed_findings, list):
            return max_level
        for item in detailed_findings:
            if not isinstance(item, dict):
                continue
            level = Orchestrator._normalize_finding_severity(item.get("severity"))
            if rank[level] > rank[max_level]:
                max_level = level
        return max_level

    def _current_vetter_issue_severity(self, vet: Agent5Output) -> str:
        finding_level = self._max_finding_severity(vet.detailed_findings)
        if vet.proof_status in {"major_gap", "wrong_strategy"}:
            return "high"
        if vet.recommended_action == "decompose_current_lemma":
            return "high"
        if vet.proof_status == "localized_gap":
            return "high" if finding_level == "high" else "medium"
        return finding_level

    def _prior_report_issue_severity(self, report: VetterReportORM | None) -> str:
        if report is None:
            return "none"
        finding_level = self._max_finding_severity(report.detailed_findings)
        if report.proof_status in {"major_gap", "wrong_strategy"}:
            return "high"
        if report.recommended_action == "decompose_current_lemma":
            return "high"
        if report.proof_status == "localized_gap":
            return "high" if finding_level == "high" else "medium"
        return finding_level

    def _solver_feedback_from_report(self, report: VetterReportORM | None) -> str | None:
        if report is None:
            return None
        if report.feedback_for_solver:
            return report.feedback_for_solver

        findings = []
        if isinstance(report.detailed_findings, list):
            findings = [item for item in report.detailed_findings if isinstance(item, dict)]
        if findings:
            lines = []
            for item in findings[:5]:
                finding = str(item.get("finding") or item.get("description") or "").strip()
                severity = self._normalize_finding_severity(item.get("severity"))
                code = str(item.get("code", "")).strip()
                location = str(item.get("location", "")).strip()
                if not finding:
                    continue
                prefix_parts: list[str] = []
                if severity != "none":
                    prefix_parts.append(f"[{severity}]")
                if code and code.lower() != "none":
                    prefix_parts.append(f"[{code}]")
                if location and location.lower() != "none":
                    prefix_parts.append(f"@ {location}")
                prefix = " ".join(prefix_parts)
                lines.append(f"{prefix} {finding}".strip())
            if lines:
                joined = "\n".join(f"- {line}" for line in lines)
                return f"Previous vetter findings to fix:\n{joined}"

        if report.reason:
            return f"Previous vetter reason: {report.reason}"
        return None

    @staticmethod
    def _solver_retry_quality(
        attempt: LemmaProofAttemptORM,
        report: VetterReportORM | None,
    ) -> int:
        proof_nl = str(attempt.proof_nl or "").strip()
        if not proof_nl:
            return 0
        if str(attempt.terminal_disposition or "").strip() == "accepted":
            return 4
        if report is not None:
            if report.statement_status == "plausible" and report.proof_status == "complete":
                return 3
            if report.proof_status == "localized_gap":
                return 2
            if report.proof_status in {"major_gap", "wrong_strategy"}:
                return 1
        if str(attempt.solver_status or "").strip() == "proved":
            return 3
        return 1

    def _best_solver_retry_context(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
    ) -> dict[str, Any]:
        attempt_rows = self.proof_attempts.list_by_lemma(problem.problem_id, lemma.lemma_id)
        chosen_attempt: LemmaProofAttemptORM | None = None
        chosen_report: VetterReportORM | None = None
        chosen_quality = -1
        for attempt in attempt_rows:
            report = self.vetter_reports.get(attempt.vetter_report_id) if attempt.vetter_report_id else None
            quality = self._solver_retry_quality(attempt, report)
            if quality <= 0:
                continue
            is_better = quality > chosen_quality
            if not is_better and quality == chosen_quality and chosen_attempt is not None:
                is_better = (
                    int(attempt.attempt_number),
                    attempt.updated_at,
                    attempt.proof_attempt_id,
                ) > (
                    int(chosen_attempt.attempt_number),
                    chosen_attempt.updated_at,
                    chosen_attempt.proof_attempt_id,
                )
            if is_better:
                chosen_attempt = attempt
                chosen_report = report
                chosen_quality = quality

        latest_report = self.vetter_reports.latest_for_target(problem.problem_id, lemma.lemma_id)
        previous_proof_nl = None
        if chosen_attempt is not None:
            proof_nl = str(chosen_attempt.proof_nl or "").strip()
            previous_proof_nl = proof_nl or None
        elif isinstance(lemma.latest_nl_proof, str) and lemma.latest_nl_proof.strip():
            previous_proof_nl = lemma.latest_nl_proof.strip()

        feedback_source = "none"
        previous_feedback = self._solver_feedback_from_report(chosen_report)
        if previous_feedback:
            feedback_source = "selected_attempt_report"
        else:
            previous_feedback = self._solver_feedback_from_report(latest_report)
            if previous_feedback:
                feedback_source = "latest_report"

        return {
            "previous_proof_nl": previous_proof_nl,
            "previous_feedback": previous_feedback,
            "selected_attempt_number": int(chosen_attempt.attempt_number) if chosen_attempt is not None else None,
            "selected_proof_attempt_id": chosen_attempt.proof_attempt_id if chosen_attempt is not None else None,
            "selected_vetter_report_id": chosen_report.report_id if chosen_report is not None else None,
            "feedback_source": feedback_source,
            "quality_rank": chosen_quality if chosen_quality > 0 else None,
        }

    @staticmethod
    def _agent4_infra_override_key(
        *,
        attempt_number: int,
        consecutive_infra_failures: int,
    ) -> str | None:
        if attempt_number == 1:
            return "agent4_first"
        if consecutive_infra_failures >= 3:
            return "agent4_infra_retry_3"
        if consecutive_infra_failures >= 2:
            return "agent4_infra_retry_2"
        return None

    def _apply_vetter_route_override(
        self,
        *,
        base_route: str,
        vet: Agent5Output,
        prior_report: VetterReportORM | None,
    ) -> tuple[str, str | None]:
        # Preserve hard-stops and false-statement routing.
        if base_route in {"blocked", "flag_suspected_false"}:
            return base_route, None
        if vet.statement_status != "plausible":
            return base_route, None

        current_severity = self._current_vetter_issue_severity(vet)
        if current_severity == "medium":
            return "retry_solver", "medium_severity_retry"
        if current_severity == "high":
            return "retry_solver", "high_severity_retry"
        return base_route, None

    def _reset_consecutive_fatal_rejections(self, lemma: LemmaORM) -> bool:
        if int(lemma.consecutive_fatal_rejections) <= 0:
            return False
        lemma.consecutive_fatal_rejections = 0
        self.lemmas.save(lemma)
        return True

    def _problem_total_estimated_cost_usd(self, problem_id: str) -> float:
        return float(sum(row.total_estimated_cost_usd for row in self.cost_rollups.list_by_problem(problem_id)))

    def _lemma_total_estimated_cost_usd(self, problem_id: str, lemma_id: str) -> float:
        return float(
            sum(
                row.estimated_cost_usd
                for row in self.llm_usage.list_by_problem(problem_id)
                if row.lemma_id == lemma_id
            )
        )

    def _clear_solver_series(self, lemma: LemmaORM) -> None:
        lemma.solver_series_started_at = None
        lemma.consecutive_infrastructure_failures = 0

    def _ensure_solver_series_started(self, lemma: LemmaORM) -> None:
        if lemma.solver_series_started_at is None:
            lemma.solver_series_started_at = datetime.now(UTC)

    def _save_lemma_transition(
        self,
        lemma: LemmaORM,
        *,
        proof_status: str | None = None,
        routing_status: str | None = None,
        next_action: str | None = None,
        terminal_worker_result: str | None = None,
        reason: str | None = None,
        ensure_solver_series: bool = False,
        clear_solver_series: bool = False,
    ) -> None:
        if proof_status is not None:
            lemma.proof_status = proof_status
        if routing_status is not None:
            lemma.routing_status = routing_status
        if ensure_solver_series:
            self._ensure_solver_series_started(lemma)
        if clear_solver_series:
            self._clear_solver_series(lemma)
        lemma.next_action = next_action
        if terminal_worker_result is not None:
            lemma.last_terminal_worker_result = terminal_worker_result
        if reason is not None:
            lemma.last_transition_reason = reason
        candidate_rows = self.decomposition_candidates.list_by_node(lemma.problem_id, lemma.lemma_id)
        if candidate_rows:
            lemma.materialized_candidate_count = len(candidate_rows)
            lemma.promoted_decomposition_count = sum(1 for row in candidate_rows if row.promoted_decomposition_id)
        self.lemmas.save(lemma)

    def _statement_fingerprint(self, statement_nl: str, semantic_sketch: dict[str, Any]) -> str:
        normalized_statement = re.sub(r"\s+", " ", str(statement_nl or "").strip())
        sketch_json = json.dumps(semantic_sketch or {}, sort_keys=True)
        return f"{normalized_statement}::{sketch_json}"

    def _lemma_proof_attempt_artifact_key(self, problem_id: str, lemma_id: str, attempt_number: int) -> str:
        return f"problems/{problem_id}/lemmas/{lemma_id}/proof_attempts/attempt_{attempt_number}.json"

    def _ensure_attempt_row(
        self,
        *,
        problem_id: str,
        lemma_id: str,
        attempt_number: int,
    ) -> LemmaProofAttemptORM:
        existing = self.proof_attempts.get_by_lemma_attempt(problem_id, lemma_id, attempt_number)
        if existing is not None:
            return existing
        row = LemmaProofAttemptORM(
            proof_attempt_id=new_id("proof_attempt"),
            problem_id=problem_id,
            lemma_id=lemma_id,
            attempt_number=attempt_number,
        )
        return self.proof_attempts.create(row)

    def _persist_attempt_artifact(
        self,
        *,
        lemma: LemmaORM,
        attempt: LemmaProofAttemptORM,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        artifact_key = self._lemma_proof_attempt_artifact_key(
            lemma.problem_id,
            lemma.lemma_id,
            int(attempt.attempt_number),
        )
        payload: dict[str, Any] = {
            "proof_attempt_id": attempt.proof_attempt_id,
            "lemma_id": lemma.lemma_id,
            "attempt_number": attempt.attempt_number,
            "statement_nl": lemma.statement_nl,
            "truth_status": lemma.truth_status,
            "counterexample_status": lemma.counterexample_status,
            "solver_worker_job_id": attempt.solver_worker_job_id,
            "solver_status": attempt.solver_status,
            "solver_summary": attempt.solver_summary,
            "solver_artifact_prefix": attempt.solver_artifact_prefix,
            "proof_nl": attempt.proof_nl,
            "solver_candidate_counterexample": attempt.solver_candidate_counterexample,
            "counterexample_record_id": attempt.counterexample_record_id,
            "counterexample_vetter_worker_job_id": attempt.counterexample_vetter_worker_job_id,
            "counterexample_vetter_status": attempt.counterexample_vetter_status,
            "vetter_worker_job_id": attempt.vetter_worker_job_id,
            "vetter_report_id": attempt.vetter_report_id,
            "vetter_statement_status": attempt.vetter_statement_status,
            "vetter_proof_status": attempt.vetter_proof_status,
            "vetter_reason": attempt.vetter_reason,
            "retry_context_attempt_number": attempt.retry_context_attempt_number,
            "retry_context_proof_attempt_id": attempt.retry_context_proof_attempt_id,
            "retry_context_vetter_report_id": attempt.retry_context_vetter_report_id,
            "retry_context_feedback_source": attempt.retry_context_feedback_source,
            "solver_llm_override_key": attempt.solver_llm_override_key,
            "terminal_disposition": attempt.terminal_disposition,
            "created_at": attempt.created_at.isoformat(),
            "updated_at": attempt.updated_at.isoformat(),
        }
        if extra_payload:
            payload.update(extra_payload)
        self.artifacts.save_json(artifact_key, payload)
        if attempt.artifact_key != artifact_key:
            attempt.artifact_key = artifact_key
            self.proof_attempts.save(attempt)

    def _record_solver_attempt(
        self,
        *,
        lemma: LemmaORM,
        attempt_number: int,
        worker_row: WorkerJobORM,
        solver_status: str,
        solver_summary: str | None,
        proof_nl: str | None,
        candidate_counterexample: str | None,
        terminal_disposition: str | None = None,
    ) -> LemmaProofAttemptORM:
        attempt = self._ensure_attempt_row(
            problem_id=lemma.problem_id,
            lemma_id=lemma.lemma_id,
            attempt_number=attempt_number,
        )
        attempt.solver_worker_job_id = worker_row.worker_job_id
        attempt.solver_status = solver_status
        attempt.solver_summary = solver_summary
        attempt.solver_artifact_prefix = worker_row.artifact_prefix
        attempt.proof_nl = proof_nl
        attempt.solver_candidate_counterexample = candidate_counterexample
        payload = worker_row.payload if isinstance(worker_row.payload, dict) else {}
        retry_context = payload.get("retry_context")
        if isinstance(retry_context, dict):
            raw_attempt_number = retry_context.get("attempt_number")
            if isinstance(raw_attempt_number, int):
                attempt.retry_context_attempt_number = raw_attempt_number
            raw_proof_attempt_id = retry_context.get("proof_attempt_id")
            if isinstance(raw_proof_attempt_id, str) and raw_proof_attempt_id.strip():
                attempt.retry_context_proof_attempt_id = raw_proof_attempt_id.strip()
            raw_report_id = retry_context.get("vetter_report_id")
            if isinstance(raw_report_id, str) and raw_report_id.strip():
                attempt.retry_context_vetter_report_id = raw_report_id.strip()
            raw_feedback_source = retry_context.get("feedback_source")
            if isinstance(raw_feedback_source, str) and raw_feedback_source.strip():
                attempt.retry_context_feedback_source = raw_feedback_source.strip()
        raw_override_key = payload.get("solver_llm_override_key", worker_row.llm_override_key)
        if isinstance(raw_override_key, str) and raw_override_key.strip():
            attempt.solver_llm_override_key = raw_override_key.strip()
        elif not raw_override_key:
            attempt.solver_llm_override_key = None
        if terminal_disposition is not None:
            attempt.terminal_disposition = terminal_disposition
        self.proof_attempts.save(attempt)
        self._persist_attempt_artifact(
            lemma=lemma,
            attempt=attempt,
            extra_payload={
                "solver_payload": worker_row.payload,
                "solver_result": worker_row.result_payload,
                "solver_error": worker_row.error_payload,
            },
        )
        return attempt

    def _record_vetter_attempt(
        self,
        *,
        lemma: LemmaORM,
        attempt_number: int,
        worker_row: WorkerJobORM,
        report: VetterReportORM,
        terminal_disposition: str | None = None,
    ) -> LemmaProofAttemptORM:
        attempt = self._ensure_attempt_row(
            problem_id=lemma.problem_id,
            lemma_id=lemma.lemma_id,
            attempt_number=attempt_number,
        )
        attempt.vetter_worker_job_id = worker_row.worker_job_id
        attempt.vetter_report_id = report.report_id
        attempt.vetter_statement_status = report.statement_status
        attempt.vetter_proof_status = report.proof_status
        attempt.vetter_reason = report.reason
        if terminal_disposition is not None:
            attempt.terminal_disposition = terminal_disposition
        self.proof_attempts.save(attempt)
        self._persist_attempt_artifact(
            lemma=lemma,
            attempt=attempt,
            extra_payload={
                "vetter_payload": worker_row.payload,
                "vetter_result": worker_row.result_payload,
                "vetter_error": worker_row.error_payload,
            },
        )
        return attempt

    def _record_counterexample_vetter_attempt(
        self,
        *,
        lemma: LemmaORM,
        attempt_number: int,
        worker_row: WorkerJobORM,
        disposition: str,
    ) -> LemmaProofAttemptORM:
        attempt = self._ensure_attempt_row(
            problem_id=lemma.problem_id,
            lemma_id=lemma.lemma_id,
            attempt_number=attempt_number,
        )
        attempt.counterexample_vetter_worker_job_id = worker_row.worker_job_id
        attempt.counterexample_vetter_status = disposition
        attempt.terminal_disposition = disposition
        self.proof_attempts.save(attempt)
        self._persist_attempt_artifact(
            lemma=lemma,
            attempt=attempt,
            extra_payload={
                "counterexample_vetter_payload": worker_row.payload,
                "counterexample_vetter_result": worker_row.result_payload,
                "counterexample_vetter_error": worker_row.error_payload,
            },
        )
        return attempt

    def _root_track_key(self, request_tag: str | None) -> str:
        match = re.search(r"track_(\d+)", str(request_tag or ""))
        if match is None:
            return "track_1"
        return f"track_{match.group(1)}"

    def _logical_decomposition_id(
        self,
        *,
        node_id: str,
        node_kind: str,
        request_tag: str | None = None,
    ) -> str:
        if node_kind == NodeKind.THEOREM.value:
            return f"logdec_{node_id}_{self._root_track_key(request_tag)}"
        return f"logdec_{node_id}"

    def _next_decomposition_revision_number(
        self,
        *,
        problem_id: str,
        logical_decomposition_id: str,
    ) -> int:
        rows = self.decompositions.list_by_logical_id(problem_id, logical_decomposition_id)
        if not rows:
            return 1
        return max(int(row.revision_number or 1) for row in rows) + 1

    def _register_counterexample(
        self,
        *,
        lemma: LemmaORM,
        source_agent: str,
        source_worker_job_id: str,
        source_attempt_number: int,
        counterexample_text: str,
        summary: str | None,
        confidence: float | None = None,
        status: str = "pending_vet",
    ) -> CounterexampleORM:
        owner = self._find_owner_decomposition(lemma.problem_id, lemma.lemma_id)
        row = CounterexampleORM(
            counterexample_id=new_id("cex"),
            problem_id=lemma.problem_id,
            lemma_id=lemma.lemma_id,
            parent_decomposition_id=(owner.decomposition_id if owner else None),
            source_agent=source_agent,
            source_worker_job_id=source_worker_job_id,
            source_attempt_number=source_attempt_number,
            status=status,
            statement_fingerprint=self._statement_fingerprint(lemma.statement_nl, lemma.statement_semantic_sketch),
            counterexample_text=counterexample_text,
            summary=summary,
            confidence=confidence,
        )
        self.counterexamples.create(row)
        lemma.active_counterexample_id = row.counterexample_id
        lemma.counterexample_status = status
        self.lemmas.save(lemma)
        return row

    def _problem_budget_cap_reason(self, problem: ProblemORM, cfg: ProblemConfig) -> str | None:
        cap = cfg.budget.max_estimated_cost_usd_per_problem
        if cap is None:
            return None
        spent = self._problem_total_estimated_cost_usd(problem.problem_id)
        if spent >= float(cap):
            return f"problem estimated cost budget reached ({spent:.4f} >= {float(cap):.4f})"
        return None

    def _lemma_solver_guardrail_reason(self, problem: ProblemORM, lemma: LemmaORM, cfg: ProblemConfig) -> str | None:
        total_attempt_cap = cfg.lemma_solving.max_solver_attempts_per_lemma_total
        if total_attempt_cap is not None and int(lemma.solver_attempt_count) >= int(total_attempt_cap):
            return f"max solver attempts per lemma reached ({int(total_attempt_cap)})"

        infra_cap = cfg.lemma_solving.max_consecutive_infrastructure_failures_per_lemma
        if infra_cap is not None and int(lemma.consecutive_infrastructure_failures) >= int(infra_cap):
            return (
                "max consecutive infrastructure failures per lemma reached "
                f"({int(infra_cap)})"
            )

        series_cap = cfg.lemma_solving.max_solver_series_wall_clock_seconds_per_lemma
        started_at = lemma.solver_series_started_at
        if series_cap is not None and started_at is not None:
            elapsed_seconds = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
            if elapsed_seconds >= float(series_cap):
                return (
                    "max solver series wall-clock reached "
                    f"({int(series_cap)}s)"
                )

        lemma_budget = cfg.budget.max_estimated_cost_usd_per_lemma
        if lemma_budget is not None:
            spent = self._lemma_total_estimated_cost_usd(problem.problem_id, lemma.lemma_id)
            if spent >= float(lemma_budget):
                return f"lemma estimated cost budget reached ({spent:.4f} >= {float(lemma_budget):.4f})"

        return None

    def _enforce_problem_budget_guardrail(self, problem: ProblemORM, cfg: ProblemConfig) -> bool:
        reason = self._problem_budget_cap_reason(problem, cfg)
        if reason is None:
            return False
        self.event_logger.transition(
            problem.problem_id,
            "problem.budget_exhausted",
            problem.status,
            ProblemStatus.FAILED.value,
            reason=reason,
        )
        self._mark_failed(
            problem,
            FailureReason.PROOF_EXHAUSTED.value,
            terminal_error_message=reason,
        )
        return True

    def _apply_immediate_lemma_follow_up(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        default_route: str,
        reason: str,
        terminal_worker_result: str | None,
        owner_decomposition: DecompositionORM | None = None,
    ) -> tuple[bool, bool]:
        if self._enforce_problem_budget_guardrail(problem, cfg):
            return True, True

        cap_reason = self._lemma_rejection_cap_reason(lemma, cfg)
        guardrail_reason = self._lemma_solver_guardrail_reason(problem, lemma, cfg)
        effective_route = default_route
        if cap_reason or guardrail_reason:
            effective_route = RoutingStatus.DECOMPOSE_FURTHER.value

        if effective_route == RoutingStatus.RETRY_SOLVER.value:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.RETRY_SOLVER.value,
                next_action="retry_solver",
                terminal_worker_result=terminal_worker_result,
                reason=reason,
                ensure_solver_series=True,
            )
            return True, False

        if effective_route != RoutingStatus.DECOMPOSE_FURTHER.value:
            self._save_lemma_transition(
                lemma,
                terminal_worker_result=terminal_worker_result,
                reason=reason,
            )
            return True, False

        owner = owner_decomposition or self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
        if owner is None:
            terminal_reason = guardrail_reason or cap_reason or reason
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_EXHAUSTED.value,
                routing_status=RoutingStatus.BLOCKED.value,
                next_action="terminal_failure",
                terminal_worker_result=terminal_worker_result,
                reason=terminal_reason,
                clear_solver_series=True,
            )
            self._mark_failed(
                problem,
                FailureReason.PROOF_EXHAUSTED.value,
                terminal_lemma_id=lemma.lemma_id,
                terminal_error_message=terminal_reason,
            )
            return True, True
        terminal = self._handle_rejection_cap_for_lemma(
            problem,
            owner,
            lemma,
            cfg,
            cap_reason=guardrail_reason or cap_reason or reason,
        )
        return True, terminal or problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}

    def _increment_fatal_rejection(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        reason: str,
    ) -> None:
        old_count = int(lemma.consecutive_fatal_rejections)
        lemma.consecutive_fatal_rejections = old_count + 1
        self.lemmas.save(lemma)
        self.event_logger.transition(
            problem.problem_id,
            "lemma.fatal_rejection_count_incremented",
            str(old_count),
            str(lemma.consecutive_fatal_rejections),
            target_node_id=lemma.lemma_id,
            reason=reason,
        )

    def _increment_minor_rejection(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        reason: str,
    ) -> None:
        old_minor = int(lemma.minor_rejection_count)
        old_fatal = int(lemma.consecutive_fatal_rejections)
        lemma.minor_rejection_count = old_minor + 1
        lemma.consecutive_fatal_rejections = 0
        self.lemmas.save(lemma)
        self.event_logger.transition(
            problem.problem_id,
            "lemma.minor_rejection_count_incremented",
            str(old_minor),
            str(lemma.minor_rejection_count),
            target_node_id=lemma.lemma_id,
            reason=reason,
        )
        if old_fatal > 0:
            self.event_logger.transition(
                problem.problem_id,
                "lemma.fatal_rejection_count_reset",
                str(old_fatal),
                "0",
                target_node_id=lemma.lemma_id,
                reason="minor rejection resets consecutive fatal counter",
            )

    def _lemma_rejection_cap_reason(self, lemma: LemmaORM, cfg: ProblemConfig) -> str | None:
        if int(lemma.consecutive_fatal_rejections) >= cfg.lemma_solving.max_consecutive_fatal_rejections_per_lemma:
            return (
                "max consecutive fatal rejections reached "
                f"({cfg.lemma_solving.max_consecutive_fatal_rejections_per_lemma})"
            )
        if int(lemma.minor_rejection_count) >= cfg.lemma_solving.max_minor_rejections_per_lemma:
            return (
                "max minor rejections reached "
                f"({cfg.lemma_solving.max_minor_rejections_per_lemma})"
            )
        return None

    def _consecutive_fatal_decomposition_rejections(self, problem_id: str, node_id: str) -> int:
        candidate_rows = self.decomposition_candidates.list_by_node(problem_id, node_id)
        if candidate_rows:
            streak = 0
            for row in candidate_rows:
                if row.llm_vetting_status == "rejected_fatal":
                    streak += 1
                    continue
                if row.llm_vetting_status in {"rejected_minor", "accepted"}:
                    streak = 0
            return streak

        streak = 0
        for dec in self.decompositions.list_by_node(problem_id, node_id):
            if dec.llm_vetting_status == "rejected_fatal":
                streak += 1
                continue
            if dec.llm_vetting_status in {"rejected_minor", "accepted"}:
                streak = 0
        return streak

    def _lemma_decomposition_fatal_streak_exceeded(
        self,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> bool:
        streak = self._consecutive_fatal_decomposition_rejections(problem.problem_id, lemma.lemma_id)
        return streak > cfg.decomposition.max_consecutive_fatal_rejections_per_node

    def _decompose_current_lemma_result(
        self,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> tuple[str, str]:
        if lemma.depth >= cfg.lemma_solving.max_recursive_decomposition_depth:
            return self._DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE, "max recursive decomposition depth reached"
        if len(self.lemmas.list_by_problem(problem.problem_id)) >= cfg.lemma_solving.max_total_lemma_nodes:
            return self._DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE, "max total lemma node cap reached"
        if self._lemma_decomposition_fatal_streak_exceeded(problem, lemma, cfg):
            return self._DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE, (
                "fatal decomposition rejection streak exceeded cap "
                f"({cfg.decomposition.max_consecutive_fatal_rejections_per_node})"
            )

        existing = self.decompositions.list_by_node(problem.problem_id, lemma.lemma_id)
        existing_candidates = self.decomposition_candidates.list_by_node(problem.problem_id, lemma.lemma_id)
        if existing or existing_candidates:
            if not cfg.mode.nl_only_mode and existing:
                self._ensure_assembly_jobs_for_node(
                    problem,
                    node_id=lemma.lemma_id,
                    theorem_nl=lemma.statement_nl,
                    theorem_semantic_sketch=lemma.statement_semantic_sketch,
                    cfg=cfg,
                )
            selected = self._select_active_decomposition_for_node(problem, lemma.lemma_id, NodeKind.LEMMA.value, cfg)
            if selected:
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_FLAWED.value,
                    routing_status=RoutingStatus.BLOCKED.value,
                    next_action="process_child_decomposition",
                    reason="reusing previously generated decomposition candidate",
                    clear_solver_series=True,
                )
                self.event_logger.transition(
                    problem.problem_id,
                    "lemma.decomposed",
                    None,
                    "existing_decomposition_selected",
                    target_node_id=lemma.lemma_id,
                    reason="reusing previously generated decomposition candidate",
                )
                return self._DECOMPOSE_OUTCOME_CHILD_SELECTED, "existing decomposition selected"
            if any(row.llm_vetting_status == "accepted" for row in existing_candidates) and self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            ) <= 0:
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_FLAWED.value,
                    routing_status=RoutingStatus.BLOCKED.value,
                    next_action="wait_on_child_decomposition",
                    reason="accepted child decomposition candidate pending promotion",
                    clear_solver_series=True,
                )
                return self._DECOMPOSE_OUTCOME_CHILD_PENDING, "accepted child decomposition candidate pending promotion"
            if self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            ) <= 0:
                return self._DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE, self._decomposition_slot_limit_reason(
                    problem.problem_id,
                    lemma.lemma_id,
                    NodeKind.LEMMA.value,
                    cfg,
                )

        generated, accepted_count = self._generate_decompositions_for_node(
            problem=problem,
            node_id=lemma.lemma_id,
            node_kind=NodeKind.LEMMA.value,
            theorem_nl=lemma.statement_nl,
            theorem_semantic_sketch=lemma.statement_semantic_sketch,
            parent_depth=lemma.depth,
            artifact_prefix=f"problems/{problem.problem_id}/lemma_decomposer/{lemma.lemma_id}/attempt_{lemma.decomposition_round_count + 1}",
            cfg=cfg,
        )
        if not generated:
            if self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            ) <= 0:
                return self._DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE, self._decomposition_slot_limit_reason(
                    problem.problem_id,
                    lemma.lemma_id,
                    NodeKind.LEMMA.value,
                    cfg,
                )
            return self._DECOMPOSE_OUTCOME_CANNOT_DECOMPOSE, "decomposition generation produced no candidates"
        lemma.decomposition_round_count = int(lemma.decomposition_round_count) + 1
        lemma.materialized_candidate_count = len(self.decomposition_candidates.list_by_node(problem.problem_id, lemma.lemma_id))
        lemma.promoted_decomposition_count = sum(
            1
            for row in self.decomposition_candidates.list_by_node(problem.problem_id, lemma.lemma_id)
            if row.promoted_decomposition_id
        )
        self._save_lemma_transition(
            lemma,
            next_action="evaluate_child_decompositions",
            reason="child decomposition round generated",
            clear_solver_series=True,
        )
        if accepted_count == 0:
            return self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND, "no accepted decomposition candidates"

        if not cfg.mode.nl_only_mode:
            self._ensure_assembly_jobs_for_node(
                problem,
                node_id=lemma.lemma_id,
                theorem_nl=lemma.statement_nl,
                theorem_semantic_sketch=lemma.statement_semantic_sketch,
                cfg=cfg,
            )

        selected = self._select_active_decomposition_for_node(problem, lemma.lemma_id, NodeKind.LEMMA.value, cfg)
        if selected:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.BLOCKED.value,
                next_action="process_child_decomposition",
                reason="route=decompose_further",
                clear_solver_series=True,
            )
            self.event_logger.transition(
                problem.problem_id,
                "lemma.decomposed",
                None,
                "active_child_decomposition_selected",
                target_node_id=lemma.lemma_id,
                reason="route=decompose_further",
            )
            return self._DECOMPOSE_OUTCOME_CHILD_SELECTED, "active child decomposition selected"

        self._save_lemma_transition(
            lemma,
            proof_status=ProofStatus.PROOF_FLAWED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="wait_on_child_decomposition",
            reason="decomposition accepted but child not yet selected",
            clear_solver_series=True,
        )
        return self._DECOMPOSE_OUTCOME_CHILD_PENDING, "decomposition accepted but child not yet selected"

    def _decompose_current_lemma(self, problem: ProblemORM, lemma: LemmaORM, cfg: ProblemConfig) -> bool:
        outcome, _reason = self._decompose_current_lemma_result(problem, lemma, cfg)
        return outcome in {
            self._DECOMPOSE_OUTCOME_CHILD_SELECTED,
            self._DECOMPOSE_OUTCOME_CHILD_PENDING,
        }

    def _handle_rejection_cap_for_lemma(
        self,
        problem: ProblemORM,
        dec: DecompositionORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        *,
        cap_reason: str,
    ) -> bool:
        outcome, detail = self._decompose_current_lemma_result(problem, lemma, cfg)

        if outcome in {
            self._DECOMPOSE_OUTCOME_CHILD_SELECTED,
            self._DECOMPOSE_OUTCOME_CHILD_PENDING,
        }:
            lemma.consecutive_fatal_rejections = 0
            lemma.minor_rejection_count = 0
            self._save_lemma_transition(
                lemma,
                next_action="process_child_decomposition",
                reason="accepted decomposition candidate exists",
                clear_solver_series=True,
            )
            self.event_logger.transition(
                problem.problem_id,
                "lemma.rejection_counters_reset",
                None,
                "0/0",
                target_node_id=lemma.lemma_id,
                reason="accepted decomposition candidate exists",
            )
            return problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}

        if outcome == self._DECOMPOSE_OUTCOME_NO_ACCEPTED_THIS_ROUND:
            remaining_slots = self._remaining_decomposition_slots(
                problem.problem_id,
                lemma.lemma_id,
                NodeKind.LEMMA.value,
                cfg,
            )
            if remaining_slots > 0:
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_FLAWED.value,
                    routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                    next_action="retry_decomposition",
                    reason=(
                        f"{cap_reason}; {detail}; "
                        f"remaining_slots={remaining_slots}"
                    ),
                    clear_solver_series=True,
                )
                self.event_logger.transition(
                    problem.problem_id,
                    "lemma.decomposition_attempt_rejected",
                    None,
                    "retry_decomposition",
                    target_node_id=lemma.lemma_id,
                    reason=(
                        f"{cap_reason}; {detail}; "
                        f"remaining_slots={remaining_slots}"
                    ),
                )
                return problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}

            detail = f"{detail}; decomposition slots exhausted"

        terminal_reason = (
            "lemma decomposition attempts exhausted with no accepted decomposition; "
            f"{cap_reason}; {detail}"
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.decomposition_exhausted",
            None,
            ProofStatus.PROOF_EXHAUSTED.value,
            target_node_id=lemma.lemma_id,
            reason=terminal_reason,
        )
        self._save_lemma_transition(
            lemma,
            proof_status=ProofStatus.PROOF_EXHAUSTED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="terminal_failure",
            reason=terminal_reason,
            clear_solver_series=True,
        )
        self._handle_lemma_terminal_failure(
            problem,
            dec,
            lemma,
            failure_reason=FailureReason.PROOF_EXHAUSTED.value,
            terminal_error_message=terminal_reason,
            force_problem_failure=True,
        )
        return problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}

    def _invalidate_parent_decomposition(
        self,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        *,
        reason: str,
        counterexample_id: str | None = None,
    ) -> None:
        owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
        if not owner:
            self._mark_failed(
                problem,
                FailureReason.LEMMA_FALSE.value,
                terminal_lemma_id=lemma.lemma_id,
                terminal_error_message=reason,
            )
            return

        old_status = owner.controller_status
        owner.controller_status = ControllerStatus.FAILED.value
        owner.failure_origin = owner.failure_origin or "child_lemma_false"
        owner.failure_reason = reason
        owner.invalidated_by_lemma_id = lemma.lemma_id
        owner.invalidated_by_counterexample_id = counterexample_id
        self.decompositions.save(owner)
        lemma.truth_status = "false"
        lemma.statement_status = StatementStatus.FALSE.value
        lemma.counterexample_status = "accepted" if counterexample_id else lemma.counterexample_status
        lemma.active_counterexample_id = counterexample_id or lemma.active_counterexample_id
        self._save_lemma_transition(
            lemma,
            proof_status=ProofStatus.FAILED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="invalidate_parent_decomposition",
            reason=reason,
            clear_solver_series=True,
        )
        self.event_logger.transition(
            problem.problem_id,
            "decomposition.invalidated",
            old_status,
            ControllerStatus.FAILED.value,
            target_node_id=owner.decomposition_id,
            reason=reason,
        )

        if owner.node_kind == NodeKind.THEOREM.value:
            old_problem_status = problem.status
            problem.status = ProblemStatus.PAUSED.value
            problem.active_decomposition_id = None
            problem.standby_decomposition_id = None
            self.problems.save(problem)
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if root is not None:
                root.active_decomposition_id = None
                self.theorems.save(root)
            self.event_logger.transition(
                problem.problem_id,
                "problem.paused_for_false_root_child",
                old_problem_status,
                ProblemStatus.PAUSED.value,
                target_node_id=lemma.lemma_id,
                reason=reason,
            )
            return

        parent = self.lemmas.get(owner.node_id)
        if parent:
            self._save_lemma_transition(
                parent,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="retry_decomposition",
                reason=f"child decomposition invalidated by false lemma {lemma.lemma_id}",
                clear_solver_series=True,
            )
            self.event_logger.transition(
                problem.problem_id,
                "lemma.retry_parent_decomposition",
                None,
                RoutingStatus.DECOMPOSE_FURTHER.value,
                target_node_id=parent.lemma_id,
                reason=reason,
            )
            return

        self._mark_failed(
            problem,
            FailureReason.LEMMA_FALSE.value,
            terminal_lemma_id=lemma.lemma_id,
            terminal_error_message=reason,
        )

    def _handle_lemma_terminal_failure(
        self,
        problem: ProblemORM,
        dec: DecompositionORM,
        lemma: LemmaORM,
        *,
        failure_reason: str,
        terminal_error_message: str,
        terminal_error_class: str | None = None,
        force_problem_failure: bool = False,
    ) -> None:
        self._save_lemma_transition(
            lemma,
            proof_status=ProofStatus.PROOF_EXHAUSTED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="terminal_failure",
            reason=terminal_error_message,
            clear_solver_series=True,
        )
        dec.controller_status = ControllerStatus.FAILED.value
        self.decompositions.save(dec)

        self.event_logger.transition(
            problem.problem_id,
            "lemma.terminal_failure",
            None,
            lemma.proof_status,
            target_node_id=lemma.lemma_id,
            reason=terminal_error_message,
        )

        if not force_problem_failure and self._promote_standby_decomposition(problem, dec.node_id, dec.node_kind):
            return

        if dec.node_kind == NodeKind.LEMMA.value:
            parent = self.lemmas.get(dec.node_id)
            if parent:
                self._save_lemma_transition(
                    parent,
                    proof_status=ProofStatus.PROOF_EXHAUSTED.value,
                    routing_status=RoutingStatus.BLOCKED.value,
                    next_action="terminal_failure",
                    reason=terminal_error_message,
                    clear_solver_series=True,
                )

        self._mark_failed(
            problem,
            failure_reason,
            terminal_lemma_id=lemma.lemma_id,
            terminal_error_class=terminal_error_class,
            terminal_error_message=terminal_error_message,
        )

    def _solve_and_vet_lemma_task(
        self,
        *,
        problem_id: str,
        lemma_id: str,
        statement_nl: str,
        semantic_sketch: dict[str, Any],
        role_in_assembly: str,
        root_semantic_sketch: dict[str, Any],
        definition_context: list[dict[str, Any]],
        trusted_context_summaries: list[dict[str, str]],
        allowed_dependency_manifest: list[DependencyManifestItem],
        forbidden_claims: list[dict[str, Any]],
        proof_attempt_node_id: str | None,
        ancestry_summary: list[dict[str, Any]],
        previous_feedback: str | None,
        previous_proof_nl: str | None,
        attempt_number: int,
    ) -> tuple[str, int, Agent4Output, Agent5Output | None, str, str | None]:
        solve_job_id = f"wrk_{lemma_id}_solve_{attempt_number}"
        solve_payload = Agent4Input(
            lemma_id=lemma_id,
            statement_nl=statement_nl,
            semantic_sketch=semantic_sketch,
            root_theorem_nl="",
            root_semantic_sketch={},
            role_in_assembly=role_in_assembly,
            shared_context=[],
            definition_context=definition_context,
            trusted_context_summaries=trusted_context_summaries,
            allowed_dependency_manifest=allowed_dependency_manifest,
            forbidden_claims=forbidden_claims,
            proof_attempt_node_id=proof_attempt_node_id,
            previous_feedback=previous_feedback,
            previous_proof_nl=previous_proof_nl,
            attempt_number=attempt_number,
        )
        solver_override_key = None
        if attempt_number == 1:
            solver_override_key = self._consume_lemma_override_once(
                lemma_id,
                override_key="agent4_first",
            )
        solved = self.workers.run_lemma_solver(
            WorkerJob(
                job_id=solve_job_id,
                problem_id=problem_id,
                worker_kind="lemma_solver",
                payload=solve_payload.model_dump(),
            ),
            solve_payload,
            f"problems/{problem_id}/lemmas/{lemma_id}/solver_attempt_{attempt_number}",
            override_key=solver_override_key,
        )
        if solved.status != "proved" or not solved.proof_nl:
            return lemma_id, attempt_number, solved, None, solve_job_id, None

        vet_job_id = f"wrk_{lemma_id}_vet_{attempt_number}"
        vet_payload = Agent5Input(
            lemma_id=lemma_id,
            statement_nl=statement_nl,
            semantic_sketch=semantic_sketch,
            root_semantic_sketch=root_semantic_sketch,
            proof_nl=solved.proof_nl,
            role_in_assembly=role_in_assembly,
            lean_diagnostics=None,
            attempt_number=attempt_number,
            allowed_dependency_manifest=allowed_dependency_manifest,
            citations=solved.citations,
            ancestry_summary=ancestry_summary,
        )
        vet = self.workers.run_lemma_vetter(
            WorkerJob(
                job_id=vet_job_id,
                problem_id=problem_id,
                worker_kind="lemma_vetter",
                payload=vet_payload.model_dump(),
            ),
            vet_payload,
            f"problems/{problem_id}/lemmas/{lemma_id}/vetter_attempt_{attempt_number}",
        )
        return lemma_id, attempt_number, solved, vet, solve_job_id, vet_job_id

    # Maximum time (seconds) to wait for a single lemma solver+vetter future
    # inside the batch.  Individual agent calls have their own timeout_seconds
    # but this caps the total wall-clock time we block on any single future.
    _BATCH_FUTURE_TIMEOUT_SECONDS: int = 1800  # 30 minutes

    def _run_solver_vetter_batch(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        cfg: ProblemConfig,
        batch: list[tuple[LemmaORM, str | None, str | None]],
    ) -> tuple[list[tuple[str, int, Agent4Output, Agent5Output | None, str, str | None]], AgentExecutionError | None]:
        if not batch:
            return [], None

        max_workers = max(1, len(batch))
        results_by_lemma: dict[str, tuple[int, Agent4Output, Agent5Output | None, str, str | None]] = {}
        fatal_error: AgentExecutionError | None = None

        pool = ThreadPoolExecutor(max_workers=max_workers)
        futures = {}
        for lemma, previous_feedback, previous_proof_nl in batch:
            allowed_manifest, definition_context, forbidden_claims, proof_attempt_node_id = self._dependency_manifest_bundle(
                problem=problem,
                root=root,
                lemma=lemma,
            )
            trusted_context_summaries = self._trusted_context_summaries(
                problem.problem_id,
                proof_graph_id=lemma.proof_graph_id or problem.active_proof_graph_id,
            )
            future = pool.submit(
                self._solve_and_vet_lemma_task,
                problem_id=problem.problem_id,
                lemma_id=lemma.lemma_id,
                statement_nl=lemma.statement_nl,
                semantic_sketch=lemma.statement_semantic_sketch,
                role_in_assembly=lemma.role_in_parent or "",
                root_semantic_sketch=root.statement_semantic_sketch,
                definition_context=definition_context,
                trusted_context_summaries=trusted_context_summaries,
                allowed_dependency_manifest=allowed_manifest,
                forbidden_claims=forbidden_claims,
                proof_attempt_node_id=proof_attempt_node_id,
                ancestry_summary=self._ancestry_summary(problem, lemma, root),
                previous_feedback=previous_feedback,
                previous_proof_nl=previous_proof_nl,
                attempt_number=lemma.solver_attempt_count + 1,
            )
            futures[future] = lemma.lemma_id
        try:
            for future in as_completed(futures, timeout=self._BATCH_FUTURE_TIMEOUT_SECONDS):
                lemma_id = futures[future]
                try:
                    out_lemma_id, attempt_number, solved, vet, solver_job_id, vetter_job_id = future.result(timeout=60)
                    results_by_lemma[out_lemma_id] = (attempt_number, solved, vet, solver_job_id, vetter_job_id)
                except AgentExecutionError as exc:
                    fatal_error = exc
                    break
                except Exception as exc:
                    fatal_error = AgentExecutionError(
                        agent_key="agent4_or_agent5",
                        error_class="infrastructure",
                        message=f"parallel worker execution failed for lemma {lemma_id}: {exc}",
                        artifact_prefix=f"problems/{problem.problem_id}/lemmas/{lemma_id}",
                    )
                    break
        except TimeoutError:
            timed_out_lemmas = [lid for f, lid in futures.items() if not f.done()]
            fatal_error = AgentExecutionError(
                agent_key="agent4_or_agent5",
                error_class="timeout",
                message=f"batch solver timed out after {self._BATCH_FUTURE_TIMEOUT_SECONDS}s; stuck lemmas: {timed_out_lemmas}",
                artifact_prefix=f"problems/{problem.problem_id}",
            )
        finally:
            # Cancel any remaining futures and shut down without waiting
            for future in futures:
                if not future.done():
                    future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

        ordered: list[tuple[str, int, Agent4Output, Agent5Output | None, str, str | None]] = []
        for lemma, _, __ in batch:
            data = results_by_lemma.get(lemma.lemma_id)
            if data is None:
                continue
            attempt_number, solved, vet, solver_job_id, vetter_job_id = data
            ordered.append((lemma.lemma_id, attempt_number, solved, vet, solver_job_id, vetter_job_id))
        return ordered, fatal_error

    def _apply_solver_vetter_results(
        self,
        *,
        problem: ProblemORM,
        dec: DecompositionORM,
        cfg: ProblemConfig,
        solver_results: list[tuple[str, int, Agent4Output, Agent5Output | None, str, str | None]],
    ) -> tuple[bool, bool]:
        """Process and persist solver/vetter results. Returns (changed, terminal)."""
        changed = False
        for lemma_id, attempt_number, solved, vet, solver_job_id, vetter_job_id in solver_results:
            lemma = self.lemmas.get(lemma_id)
            if lemma is None or lemma.routing_status == RoutingStatus.DONE.value:
                continue

            lemma.solver_attempt_count = max(lemma.solver_attempt_count, attempt_number)
            if solved.proof_nl:
                lemma.latest_nl_proof = solved.proof_nl
            self.lemmas.save(lemma)
            changed = True

            self.event_logger.transition(
                problem.problem_id,
                "lemma.solver_attempt",
                None,
                solved.status,
                target_node_id=lemma.lemma_id,
                reason=solved.proof_summary,
                worker_job_id=solver_job_id,
            )

            if solved.status != "proved" or not solved.proof_nl or vet is None:
                lemma.proof_status = ProofStatus.PROOF_FLAWED.value
                lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
                self.lemmas.save(lemma)
                continue

            prior_report = self.vetter_reports.latest_for_target(problem.problem_id, lemma.lemma_id)
            report = VetterReportORM(
                report_id=new_id("rep"),
                problem_id=problem.problem_id,
                target_id=lemma.lemma_id,
                target_kind="lemma",
                statement_status=vet.statement_status,
                proof_status=vet.proof_status,
                drift_assessment=vet.drift_assessment,
                recommended_action=vet.recommended_action,
                reason=vet.reason,
                confidence=vet.confidence,
                feedback_for_solver=vet.feedback_for_solver,
                detailed_findings=vet.detailed_findings,
            )
            self.vetter_reports.create(report)

            lemma.latest_vetter_report_id = report.report_id
            lemma.statement_status = vet.statement_status

            drift_severity = vet.drift_assessment.get("drift_severity")
            if drift_severity == "minor":
                self.event_logger.transition(
                    problem.problem_id,
                    "drift.warning",
                    None,
                    "minor",
                    target_node_id=lemma.lemma_id,
                    reason=vet.reason,
                )

            route = route_vetter_result(
                vet.statement_status,
                vet.proof_status,
                drift_severity,
                cfg,
            )
            route, route_override = self._apply_vetter_route_override(
                base_route=route,
                vet=vet,
                prior_report=prior_report,
            )

            route_reason = vet.reason
            if route_override:
                route_reason = f"{vet.reason} | override={route_override}"

            self.event_logger.transition(
                problem.problem_id,
                "lemma.vetter_route",
                None,
                route,
                target_node_id=lemma.lemma_id,
                reason=route_reason,
                worker_job_id=vetter_job_id,
            )

            if route == "blocked":
                # When major drift comes from a proof (statement is plausible),
                # give the solver one retry with the drift feedback before
                # blocking.  The vetter often provides actionable feedback that
                # a fresh solver attempt can incorporate.
                if vet.statement_status == "plausible" and not lemma.drift_retry_used:
                    lemma.drift_retry_used = True
                    lemma.proof_status = ProofStatus.PROOF_FLAWED.value
                    lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
                    self.lemmas.save(lemma)
                    self.event_logger.transition(
                        problem.problem_id,
                        "lemma.drift_retry",
                        None,
                        "retry_solver",
                        target_node_id=lemma.lemma_id,
                        reason="major drift on plausible statement; retrying solver with drift feedback",
                    )
                    continue

                self._handle_lemma_terminal_failure(
                    problem,
                    dec,
                    lemma,
                    failure_reason=FailureReason.PROOF_EXHAUSTED.value,
                    terminal_error_message="major semantic drift",
                )
                changed = True
                if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                    return changed, True
                continue

            if vet.recommended_action == "flag_suspected_false" and (
                vet.confidence >= cfg.routing.invalidation_confidence_threshold
            ):
                if cfg.mode.nl_only_mode:
                    self._invalidate_parent_decomposition(
                        problem,
                        lemma,
                        cfg,
                        reason="nl-only suspected false with high confidence",
                    )
                    changed = True
                    if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                        return changed, True
                    continue
                if self._submit_plausibility_job(problem, lemma, cfg):
                    lemma.routing_status = RoutingStatus.BLOCKED.value
                    self.lemmas.save(lemma)
                    changed = True
                continue

            if route == "retry_solver":
                lemma.proof_status = ProofStatus.PROOF_FLAWED.value
                lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
                self.lemmas.save(lemma)
                continue

            if route == "decompose_further":
                if not self._decompose_current_lemma(problem, lemma, cfg):
                    if self._lemma_decomposition_fatal_streak_exceeded(problem, lemma, cfg):
                        terminal_reason = (
                            "fatal decomposition rejection streak exceeded cap "
                            f"({cfg.decomposition.max_consecutive_fatal_rejections_per_node})"
                        )
                    else:
                        terminal_reason = "decomposition cap or depth reached"
                    self._handle_lemma_terminal_failure(
                        problem,
                        dec,
                        lemma,
                        failure_reason=FailureReason.PROOF_EXHAUSTED.value,
                        terminal_error_message=terminal_reason,
                    )
                changed = True
                if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                    return changed, True
                continue

            if route == "send_to_lean":
                if cfg.mode.nl_only_mode:
                    lemma.proof_status = ProofStatus.NL_ACCEPTED.value
                    lemma.routing_status = RoutingStatus.DONE.value
                else:
                    lemma.proof_status = ProofStatus.PROOF_VETTED.value
                    lemma.routing_status = RoutingStatus.READY_FOR_LEAN.value
                    if self._ensure_formalize_job(problem, lemma, cfg):
                        changed = True
                self.lemmas.save(lemma)
                continue

            lemma.proof_status = ProofStatus.PROOF_FLAWED.value
            lemma.routing_status = RoutingStatus.RETRY_SOLVER.value
            self.lemmas.save(lemma)

        return changed, False

    @staticmethod
    def _lemma_solver_job_id(lemma_id: str, attempt_number: int, continuation_generation: int) -> str:
        return f"wrk_{lemma_id}_g{max(0, int(continuation_generation))}_solve_{attempt_number}"

    @staticmethod
    def _lemma_vetter_job_id(
        lemma_id: str,
        attempt_number: int,
        continuation_generation: int,
        *,
        suffix: str | None = None,
    ) -> str:
        base = f"wrk_{lemma_id}_g{max(0, int(continuation_generation))}_vet_{attempt_number}"
        if suffix:
            return f"{base}_{suffix}"
        return base

    @staticmethod
    def _proof_split_generation_job_id(lemma_id: str, attempt_number: int, continuation_generation: int) -> str:
        return f"wrk_{lemma_id}_g{max(0, int(continuation_generation))}_split_{attempt_number}"

    @staticmethod
    def _proof_split_vetter_job_id(lemma_id: str, attempt_number: int, continuation_generation: int) -> str:
        return f"wrk_{lemma_id}_g{max(0, int(continuation_generation))}_split_vet_{attempt_number}"

    @staticmethod
    def _solver_attempt_number_from_worker_row(worker_row: WorkerJobORM, fallback: int) -> int:
        payload_attempt = None
        if isinstance(worker_row.payload, dict):
            raw = worker_row.payload.get("attempt_number")
            if isinstance(raw, int):
                payload_attempt = raw
            elif isinstance(raw, str):
                try:
                    payload_attempt = int(raw.strip())
                except Exception:
                    payload_attempt = None
        if isinstance(payload_attempt, int) and payload_attempt > 0:
            return payload_attempt
        match = re.search(r"_solve_(\d+)$", worker_row.worker_job_id)
        if match is not None:
            try:
                parsed = int(match.group(1))
                if parsed > 0:
                    return parsed
            except Exception:
                pass
        return max(1, fallback)

    def _ensure_lemma_solver_job(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> bool:
        continuation_generation = self._continuation_generation(problem)
        attempt_number = max(1, lemma.solver_attempt_count + 1)
        job_id = self._lemma_solver_job_id(lemma.lemma_id, attempt_number, continuation_generation)
        existing = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_solver",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
        )
        if existing is not None:
            return False

        retry_context = self._best_solver_retry_context(problem=problem, lemma=lemma)

        allowed_manifest, definition_context, forbidden_claims, proof_attempt_node_id = self._dependency_manifest_bundle(
            problem=problem,
            root=root,
            lemma=lemma,
        )

        payload = Agent4Input(
            lemma_id=lemma.lemma_id,
            statement_nl=lemma.statement_nl,
            semantic_sketch=lemma.statement_semantic_sketch,
            root_theorem_nl="",
            root_semantic_sketch={},
            role_in_assembly=lemma.role_in_parent or "",
            shared_context=[],
            definition_context=definition_context,
            trusted_context_summaries=self._trusted_context_summaries(
                problem.problem_id,
                proof_graph_id=lemma.proof_graph_id or problem.active_proof_graph_id,
            ),
            allowed_dependency_manifest=allowed_manifest,
            forbidden_claims=forbidden_claims,
            proof_attempt_node_id=proof_attempt_node_id,
            previous_feedback=retry_context["previous_feedback"],
            previous_proof_nl=retry_context["previous_proof_nl"],
            attempt_number=attempt_number,
        )
        override_key = self._agent4_infra_override_key(
            attempt_number=attempt_number,
            consecutive_infra_failures=int(lemma.consecutive_infrastructure_failures),
        )
        if override_key == "agent4_first":
            override_key = self._consume_lemma_override_once(
                lemma.lemma_id,
                override_key="agent4_first",
            )
        payload_dict = payload.model_dump()
        payload_dict["retry_context"] = {
            "attempt_number": retry_context["selected_attempt_number"],
            "proof_attempt_id": retry_context["selected_proof_attempt_id"],
            "vetter_report_id": retry_context["selected_vetter_report_id"],
            "feedback_source": retry_context["feedback_source"],
            "quality_rank": retry_context["quality_rank"],
        }
        payload_dict["solver_llm_override_key"] = override_key
        solver_llm_profile = (
            cfg.llm.agent4_first
            if override_key == "agent4_first" and cfg.llm.agent4_first is not None
            else cfg.llm.agent4
        )
        solver_job_max_attempts = (
            max(1, int(solver_llm_profile.max_attempts))
            if isinstance(getattr(solver_llm_profile, "max_attempts", None), int)
            else 2
        )
        self.worker_jobs.enqueue_if_absent(
            WorkerJobORM(
                worker_job_id=job_id,
                problem_id=problem.problem_id,
                worker_kind="lemma_solver",
                status="queued",
                target_id=lemma.lemma_id,
                target_kind="lemma",
                execution_id=self.execution_id,
                continuation_generation=continuation_generation,
                attempt_number=attempt_number,
                artifact_prefix=f"problems/{problem.problem_id}/lemmas/{lemma.lemma_id}/solver_attempt_{attempt_number}",
                handler_key="agent4_lemma_solver",
                request_source="lemma_solver",
                payload=payload_dict,
                llm_override_key=override_key,
                max_attempts=solver_job_max_attempts,
            )
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.solver_submitted",
            None,
            "queued",
            target_node_id=lemma.lemma_id,
            worker_job_id=job_id,
            reason=f"attempt={attempt_number}",
        )
        self._save_lemma_transition(
            lemma,
            next_action="wait_on_solver",
            reason=f"solver attempt {attempt_number} submitted",
            ensure_solver_series=True,
        )
        lemma.last_submitted_solver_job_id = job_id
        lemma.last_submitted_solver_attempt_number = attempt_number
        self.lemmas.save(lemma)
        return True

    def _ensure_lemma_vetter_job(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
    ) -> bool:
        continuation_generation = self._continuation_generation(problem)
        attempt_number = max(1, lemma.solver_attempt_count)
        job_id = self._lemma_vetter_job_id(lemma.lemma_id, attempt_number, continuation_generation)
        existing = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_vetter",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
        )
        if existing is not None:
            return False
        if not lemma.latest_nl_proof:
            return False
        latest_solver_job = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_solver",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
        )
        solver_payload = latest_solver_job.payload if latest_solver_job is not None and isinstance(latest_solver_job.payload, dict) else {}
        solver_result = latest_solver_job.result_payload if latest_solver_job is not None and isinstance(latest_solver_job.result_payload, dict) else {}
        payload = Agent5Input(
            lemma_id=lemma.lemma_id,
            statement_nl=lemma.statement_nl,
            semantic_sketch=lemma.statement_semantic_sketch,
            root_semantic_sketch=root.statement_semantic_sketch,
            proof_nl=lemma.latest_nl_proof,
            role_in_assembly=lemma.role_in_parent or "",
            lean_diagnostics=None,
            attempt_number=attempt_number,
            allowed_dependency_manifest=solver_payload.get("allowed_dependency_manifest", []),
            citations=solver_result.get("citations", []),
            ancestry_summary=self._ancestry_summary(problem, lemma, root),
        )
        self.worker_jobs.enqueue_if_absent(
            WorkerJobORM(
                worker_job_id=job_id,
                problem_id=problem.problem_id,
                worker_kind="lemma_vetter",
                status="queued",
                target_id=lemma.lemma_id,
                target_kind="lemma",
                execution_id=self.execution_id,
                continuation_generation=continuation_generation,
                attempt_number=attempt_number,
                artifact_prefix=f"problems/{problem.problem_id}/lemmas/{lemma.lemma_id}/vetter_attempt_{attempt_number}",
                handler_key="agent5_lemma_vetter",
                request_source="lemma_vetter",
                payload=payload.model_dump(),
            )
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.vetter_submitted",
            None,
            "queued",
            target_node_id=lemma.lemma_id,
            worker_job_id=job_id,
            reason=f"attempt={attempt_number}",
        )
        self._save_lemma_transition(
            lemma,
            next_action="wait_on_vetter",
            reason=f"vetter attempt {attempt_number} submitted",
        )
        return True

    def _ensure_split_existing_proof_job(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
    ) -> bool:
        if not lemma.latest_nl_proof:
            return False
        continuation_generation = self._continuation_generation(problem)
        prior_rows = [
            row
            for row in self.worker_jobs.list_by_problem(problem.problem_id, worker_kind="proof_split_generation")
            if row.target_id == lemma.lemma_id
            and row.request_source == "split_existing_proof_generation"
            and int(row.continuation_generation or 0) == continuation_generation
            and row.superseded_at is None
        ]
        if any(row.status in {"queued", "running"} for row in prior_rows):
            return False
        attempt_number = max((int(row.attempt_number or 0) for row in prior_rows), default=0) + 1
        job_id = self._proof_split_generation_job_id(lemma.lemma_id, attempt_number, continuation_generation)
        owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
        lean_failure = owner.lean_bottlenecks[-1] if owner and owner.lean_bottlenecks else {}
        payload = Agent7Input(
            lemma_id=lemma.lemma_id,
            parent_statement_nl=lemma.statement_nl,
            parent_semantic_sketch=lemma.statement_semantic_sketch,
            parent_proof_nl=str(lemma.latest_nl_proof or ""),
            role_in_parent=lemma.role_in_parent,
            root_theorem_nl=root.statement_nl if root is not None else "",
            root_semantic_sketch=getattr(root, "statement_semantic_sketch", {}) or {},
            ancestry_summary=self._ancestry_summary(problem, lemma, root),
            trusted_context_summaries=self._trusted_context_summaries(
                problem.problem_id,
                proof_graph_id=lemma.proof_graph_id or problem.active_proof_graph_id,
            ),
            lean_failure=lean_failure if isinstance(lean_failure, dict) else {},
            previous_attempt_summaries=self._previous_attempt_summaries(problem.problem_id, lemma.lemma_id),
        )
        self.worker_jobs.enqueue_if_absent(
            WorkerJobORM(
                worker_job_id=job_id,
                problem_id=problem.problem_id,
                worker_kind="proof_split_generation",
                status="queued",
                target_id=lemma.lemma_id,
                target_kind="lemma",
                execution_id=self.execution_id,
                continuation_generation=continuation_generation,
                attempt_number=attempt_number,
                artifact_prefix=f"problems/{problem.problem_id}/lemmas/{lemma.lemma_id}/split_existing_proof_attempt_{attempt_number}",
                handler_key="agent7_split_existing_proof",
                request_source="split_existing_proof_generation",
                payload=payload.model_dump(),
                max_attempts=2,
            )
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.split_existing_proof_submitted",
            None,
            "queued",
            target_node_id=lemma.lemma_id,
            worker_job_id=job_id,
            reason=f"attempt={attempt_number}",
        )
        self._save_lemma_transition(
            lemma,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="wait_on_split_existing_proof",
            reason=f"split_existing_proof attempt {attempt_number} submitted",
            ensure_solver_series=True,
        )
        return True

    def _ensure_split_existing_proof_vetter_job(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        split_row: WorkerJobORM,
        split_output: Agent7Output,
    ) -> bool:
        continuation_generation = self._continuation_generation(problem)
        attempt_number = max(1, int(split_row.attempt_number or 1))
        existing = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="split_existing_proof_vetting",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
        )
        if existing is not None:
            return False
        owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
        lean_failure = owner.lean_bottlenecks[-1] if owner and owner.lean_bottlenecks else {}
        payload = Agent8Input(
            lemma_id=lemma.lemma_id,
            parent_statement_nl=lemma.statement_nl,
            parent_semantic_sketch=lemma.statement_semantic_sketch,
            parent_proof_nl=str(lemma.latest_nl_proof or ""),
            lean_failure=lean_failure if isinstance(lean_failure, dict) else {},
            proposed_split=split_output.model_dump(mode="json"),
        )
        job_id = self._proof_split_vetter_job_id(lemma.lemma_id, attempt_number, continuation_generation)
        self.worker_jobs.enqueue_if_absent(
            WorkerJobORM(
                worker_job_id=job_id,
                problem_id=problem.problem_id,
                worker_kind="proof_split_vetting",
                status="queued",
                target_id=lemma.lemma_id,
                target_kind="lemma",
                execution_id=self.execution_id,
                continuation_generation=continuation_generation,
                attempt_number=attempt_number,
                artifact_prefix=f"problems/{problem.problem_id}/lemmas/{lemma.lemma_id}/split_existing_proof_vetter_{attempt_number}",
                handler_key="agent8_split_bundle_vetter",
                request_source="split_existing_proof_vetting",
                payload=payload.model_dump(),
                max_attempts=2,
            )
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.split_existing_proof_vetter_submitted",
            None,
            "queued",
            target_node_id=lemma.lemma_id,
            worker_job_id=job_id,
            reason=f"attempt={attempt_number}",
        )
        self._save_lemma_transition(
            lemma,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="wait_on_split_existing_proof_vetter",
            reason=f"split_existing_proof vetter attempt {attempt_number} submitted",
            ensure_solver_series=True,
        )
        return True

    def _harvest_split_existing_proof_job(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
    ) -> tuple[bool, bool]:
        rows = [
            row
            for row in self.worker_jobs.list_by_problem(problem.problem_id, worker_kind="proof_split_generation")
            if row.target_id == lemma.lemma_id
            and row.request_source == "split_existing_proof_generation"
            and row.superseded_at is None
            and row.controller_consumed_at is None
        ]
        rows.sort(key=lambda row: (int(row.attempt_number or 0), row.created_at))
        if not rows:
            return False, False
        row = rows[-1]
        consumed_row, consumed = self.worker_jobs.consume_terminal(row.worker_job_id, execution_id=self.execution_id)
        if not consumed or consumed_row is None:
            return False, False
        if consumed_row.status == "failed":
            reason = str((consumed_row.error_payload or {}).get("message") or "split_existing_proof generation failed")
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason=reason,
                ensure_solver_series=True,
            )
            return True, False
        try:
            split_output = Agent7Output.model_validate(consumed_row.result_payload or {})
        except Exception:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason="split_existing_proof output was invalid",
                ensure_solver_series=True,
            )
            return True, False
        if split_output.decision != "split" or not split_output.lemmas:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason=split_output.summary or "existing proof could not be split coherently",
                ensure_solver_series=True,
            )
            return True, False
        changed = self._ensure_split_existing_proof_vetter_job(
            problem=problem,
            lemma=lemma,
            split_row=consumed_row,
            split_output=split_output,
        )
        return changed or True, False

    def _harvest_split_existing_proof_vetter_job(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> tuple[bool, bool]:
        rows = [
            row
            for row in self.worker_jobs.list_by_problem(problem.problem_id, worker_kind="proof_split_vetting")
            if row.target_id == lemma.lemma_id
            and row.request_source == "split_existing_proof_vetting"
            and row.superseded_at is None
            and row.controller_consumed_at is None
        ]
        rows.sort(key=lambda row: (int(row.attempt_number or 0), row.created_at))
        if not rows:
            return False, False
        row = rows[-1]
        consumed_row, consumed = self.worker_jobs.consume_terminal(row.worker_job_id, execution_id=self.execution_id)
        if not consumed or consumed_row is None:
            return False, False
        if consumed_row.status == "failed":
            reason = str((consumed_row.error_payload or {}).get("message") or "split_existing_proof vetting failed")
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason=reason,
                ensure_solver_series=True,
            )
            return True, False
        try:
            vet_output = Agent8Output.model_validate(consumed_row.result_payload or {})
        except Exception:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason="split_existing_proof vetter output was invalid",
                ensure_solver_series=True,
            )
            return True, False
        if vet_output.decision != "approved" or not vet_output.parent_reassembly_valid:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason=vet_output.summary or "split_existing_proof bundle was rejected",
                ensure_solver_series=True,
            )
            return True, False

        generation = self._continuation_generation(problem)
        split_generation_rows = [
            candidate_row
            for candidate_row in self.worker_jobs.list_by_problem(problem.problem_id, worker_kind="proof_split_generation")
            if candidate_row.target_id == lemma.lemma_id
            and candidate_row.request_source == "split_existing_proof_generation"
            and int(candidate_row.continuation_generation or 0) == generation
            and int(candidate_row.attempt_number or 0) == int(consumed_row.attempt_number or 1)
            and candidate_row.result_payload is not None
        ]
        if not split_generation_rows:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                next_action="decompose_further",
                reason="split_existing_proof generator result missing at vet acceptance time",
                ensure_solver_series=True,
            )
            return True, False
        split_output = Agent7Output.model_validate(split_generation_rows[-1].result_payload or {})
        if self._materialize_split_existing_proof(
            problem=problem,
            root=root,
            lemma=lemma,
            cfg=cfg,
            split_output=split_output,
            vet_output=vet_output,
            source_job_id=consumed_row.worker_job_id,
        ):
            return True, False
        self._save_lemma_transition(
            lemma,
            proof_status=ProofStatus.PROOF_FLAWED.value,
            routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
            next_action="decompose_further",
            reason="split_existing_proof was approved but no child decomposition was materialized",
            ensure_solver_series=True,
        )
        return True, False

    def _ensure_counterexample_vetter_job(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
        counterexample: CounterexampleORM,
    ) -> bool:
        attempt_number = max(1, int(counterexample.source_attempt_number or lemma.solver_attempt_count or 1))
        continuation_generation = self._continuation_generation(problem)
        job_id = self._lemma_vetter_job_id(lemma.lemma_id, attempt_number, continuation_generation, suffix="counterexample")
        existing = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_counterexample_vetter",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
        )
        if existing is not None:
            return False
        payload = Agent5Input(
            lemma_id=lemma.lemma_id,
            statement_nl=lemma.statement_nl,
            semantic_sketch=lemma.statement_semantic_sketch,
            root_semantic_sketch=root.statement_semantic_sketch,
            proof_nl=str(lemma.latest_nl_proof or ""),
            role_in_assembly=lemma.role_in_parent or "",
            lean_diagnostics=None,
            attempt_number=attempt_number,
            vetting_mode="counterexample",
            candidate_counterexample=counterexample.counterexample_text,
            ancestry_summary=self._ancestry_summary(problem, lemma, root),
        )
        self.worker_jobs.enqueue_if_absent(
            WorkerJobORM(
                worker_job_id=job_id,
                problem_id=problem.problem_id,
                worker_kind="lemma_vetter",
                status="queued",
                target_id=lemma.lemma_id,
                target_kind="lemma",
                execution_id=self.execution_id,
                continuation_generation=continuation_generation,
                attempt_number=attempt_number,
                artifact_prefix=f"problems/{problem.problem_id}/lemmas/{lemma.lemma_id}/counterexample_attempt_{attempt_number}",
                handler_key="agent5_counterexample_vetter",
                request_source="lemma_counterexample_vetter",
                payload=payload.model_dump(),
            )
        )
        self._save_lemma_transition(
            lemma,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="wait_on_counterexample_vetter",
            terminal_worker_result="agent4:counterexample_flagged",
            reason=f"counterexample {counterexample.counterexample_id} queued for vetting",
            clear_solver_series=True,
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.counterexample_vetter_submitted",
            None,
            "queued",
            target_node_id=lemma.lemma_id,
            worker_job_id=job_id,
            reason=f"counterexample_id={counterexample.counterexample_id}; attempt={attempt_number}",
        )
        return True

    def _handle_terminal_lemma_worker_failure(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        agent_key: str,
        worker_job_id: str,
        artifact_prefix: str,
        error_payload: dict[str, Any] | None,
    ) -> tuple[bool, bool]:
        payload = error_payload or {}
        error_class = str(payload.get("error_class") or payload.get("class") or "infrastructure")
        message = str(payload.get("message") or f"{agent_key} worker failed")

        if error_class == "interrupted":
            return False, False

        is_infrastructure = error_class in {"infrastructure", "infrastructure_transient", "timeout"}
        if is_infrastructure:
            lemma.consecutive_infrastructure_failures = int(lemma.consecutive_infrastructure_failures) + 1
            self._ensure_solver_series_started(lemma)
            infra_cap = cfg.lemma_solving.max_consecutive_infrastructure_failures_per_lemma
            if infra_cap is not None and int(lemma.consecutive_infrastructure_failures) >= int(infra_cap):
                self.event_logger.transition(
                    problem.problem_id,
                    "lemma.infrastructure_cap_reached",
                    None,
                    RoutingStatus.DECOMPOSE_FURTHER.value,
                    target_node_id=lemma.lemma_id,
                    worker_job_id=worker_job_id,
                    reason=(
                        f"{error_class}: {message} | "
                        f"max consecutive infrastructure failures per lemma reached ({int(infra_cap)})"
                    ),
                )
                return self._apply_immediate_lemma_follow_up(
                    problem=problem,
                    lemma=lemma,
                    cfg=cfg,
                    default_route=RoutingStatus.RETRY_SOLVER.value,
                    reason=f"{error_class}: {message}",
                    terminal_worker_result=f"{agent_key}:failed_infra",
                )

            problem.infrastructure_failure_count += 1
            self._infrastructure_incident_added_this_tick = True
            reason_payload = {
                "agent_key": agent_key,
                "artifact_prefix": artifact_prefix,
                "error_class": error_class,
                "message": message,
                "worker_job_id": worker_job_id,
                "parse_error_artifact": payload.get("parse_error_artifact"),
            }
            max_failures = cfg.lemma_solving.max_infrastructure_failures
            if problem.infrastructure_failure_count >= max_failures:
                self.event_logger.transition(
                    problem.problem_id,
                    "agent.infrastructure_fatal",
                    problem.status,
                    ProblemStatus.FAILED.value,
                    reason=json.dumps(reason_payload, sort_keys=True),
                )
                self._mark_failed(
                    problem,
                    FailureReason.UNKNOWN.value,
                    terminal_error_class=error_class,
                    terminal_error_message=(
                        f"infrastructure failure count ({problem.infrastructure_failure_count}) "
                        f"exceeded threshold ({max_failures}): {message}"
                    ),
                )
                return True, True

            self.event_logger.transition(
                problem.problem_id,
                "agent.infrastructure_retry",
                problem.status,
                problem.status,
                reason=json.dumps(
                    {
                        **reason_payload,
                        "infrastructure_failure_count": problem.infrastructure_failure_count,
                        "max_infrastructure_failures": max_failures,
                    },
                    sort_keys=True,
                ),
            )
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.RETRY_SOLVER.value,
                reason=f"{error_class}: {message}",
                terminal_worker_result=f"{agent_key}:failed_infra",
            )

        self.event_logger.transition(
            problem.problem_id,
            "lemma.worker_failed",
            None,
            "retry_solver",
            target_node_id=lemma.lemma_id,
            worker_job_id=worker_job_id,
            reason=f"{error_class}: {message}",
        )
        lemma.consecutive_infrastructure_failures = 0
        return self._apply_immediate_lemma_follow_up(
            problem=problem,
            lemma=lemma,
            cfg=cfg,
            default_route=RoutingStatus.RETRY_SOLVER.value,
            reason=f"{error_class}: {message}",
            terminal_worker_result=f"{agent_key}:failed",
        )

    def _harvest_lemma_solver_job(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> tuple[bool, bool]:
        continuation_generation = self._continuation_generation(problem)
        next_attempt = max(1, lemma.solver_attempt_count + 1)
        worker_row = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_solver",
            attempt_number=next_attempt,
            continuation_generation=continuation_generation,
            statuses={"completed", "failed"},
        )
        if worker_row is None:
            return False, False

        job_id = worker_row.worker_job_id
        consumed_row, consumed = self.worker_jobs.consume_terminal(job_id, execution_id=self.execution_id)
        if not consumed or consumed_row is None:
            return False, False
        consumed_attempt = self._solver_attempt_number_from_worker_row(consumed_row, next_attempt)
        attempt_bumped = False
        lemma.last_submitted_solver_job_id = None
        lemma.last_submitted_solver_attempt_number = None
        if lemma.solver_attempt_count < consumed_attempt:
            lemma.solver_attempt_count = consumed_attempt
            attempt_bumped = True

        if consumed_row.status == "failed":
            if attempt_bumped:
                self.lemmas.save(lemma)
            handled, terminal = self._handle_terminal_lemma_worker_failure(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                agent_key="agent4",
                worker_job_id=job_id,
                artifact_prefix=consumed_row.artifact_prefix or f"worker_jobs/{job_id}",
                error_payload=consumed_row.error_payload,
            )
            return handled or attempt_bumped, terminal

        try:
            solved = Agent4Output.model_validate(consumed_row.result_payload or {})
        except Exception as exc:
            self.event_logger.transition(
                problem.problem_id,
                "lemma.solver_output_invalid",
                None,
                "retry_solver",
                target_node_id=lemma.lemma_id,
                worker_job_id=job_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
            lemma.consecutive_infrastructure_failures = 0
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.RETRY_SOLVER.value,
                reason=f"invalid solver output: {type(exc).__name__}: {exc}",
                terminal_worker_result="agent4:invalid_output",
            )

        payload_dict = consumed_row.payload if isinstance(consumed_row.payload, dict) else {}
        manifest_rows = payload_dict.get("allowed_dependency_manifest", [])
        allowed_manifest = [
            DependencyManifestItem.model_validate(item)
            for item in manifest_rows
            if isinstance(item, dict)
        ]
        forbidden_claims = payload_dict.get("forbidden_claims", [])
        proof_attempt_node_id = payload_dict.get("proof_attempt_node_id")
        dependency_status, dependency_violations = self.proof_graphs.validate_solver_output(
            problem=problem,
            lemma=lemma,
            proof_attempt_node_id=proof_attempt_node_id,
            allowed_manifest=allowed_manifest,
            forbidden_claims=forbidden_claims if isinstance(forbidden_claims, list) else [],
            output_proof_nl=solved.proof_nl,
            citations=solved.citations,
            used_forbidden_claim=solved.used_forbidden_claim,
            artifact_id=job_id,
        )
        lemma.dependency_status = dependency_status
        if lemma.proof_graph_id and lemma.claim_node_id:
            latest_dep = self.proof_graphs.checks.latest_for_target(lemma.proof_graph_id, target_node_id=lemma.claim_node_id, artifact_kind="solver")
            if latest_dep is not None:
                lemma.last_dependency_check_id = latest_dep.dependency_check_id
        if dependency_violations:
            self._increment_fatal_rejection(
                problem=problem,
                lemma=lemma,
                reason=f"source=dependency_solver; violations={json.dumps(dependency_violations, sort_keys=True)}",
            )
            lemma.consecutive_infrastructure_failures = 0
            route = RoutingStatus.DECOMPOSE_FURTHER.value if lemma.consecutive_fatal_rejections >= 2 else RoutingStatus.RETRY_SOLVER.value
            self.event_logger.transition(
                problem.problem_id,
                "lemma.dependency_violation",
                None,
                route,
                target_node_id=lemma.lemma_id,
                worker_job_id=job_id,
                reason=json.dumps(dependency_violations, sort_keys=True)[:500],
            )
            owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=route,
                reason=json.dumps(dependency_violations, sort_keys=True)[:500],
                terminal_worker_result="agent4:dependency_violation",
                owner_decomposition=owner,
            )

        lemma.solver_attempt_count = max(lemma.solver_attempt_count, int(solved.attempt_number), consumed_attempt)
        if solved.proof_nl:
            lemma.latest_nl_proof = solved.proof_nl
        lemma.consecutive_infrastructure_failures = 0
        attempt_row = self._record_solver_attempt(
            lemma=lemma,
            attempt_number=lemma.solver_attempt_count,
            worker_row=consumed_row,
            solver_status=solved.status,
            solver_summary=solved.proof_summary,
            proof_nl=solved.proof_nl,
            candidate_counterexample=solved.candidate_counterexample,
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.solver_attempt",
            None,
            solved.status,
            target_node_id=lemma.lemma_id,
            reason=solved.proof_summary,
            worker_job_id=job_id,
        )
        if solved.status == "proved" and solved.proof_nl:
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FOUND.value,
                routing_status=RoutingStatus.OPEN.value,
                next_action="vet_proof",
                terminal_worker_result="agent4:proved",
                reason=solved.proof_summary,
                clear_solver_series=True,
            )
        elif solved.candidate_counterexample:
            counterexample = self._register_counterexample(
                lemma=lemma,
                source_agent="agent4",
                source_worker_job_id=job_id,
                source_attempt_number=lemma.solver_attempt_count,
                counterexample_text=solved.candidate_counterexample,
                summary=solved.proof_summary,
                status="pending_vet",
            )
            attempt_row.counterexample_record_id = counterexample.counterexample_id
            attempt_row.terminal_disposition = "counterexample_pending_vet"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if root is not None:
                self._ensure_counterexample_vetter_job(
                    problem=problem,
                    root=root,
                    lemma=lemma,
                    counterexample=counterexample,
                )
            return True, False
        else:
            self._increment_fatal_rejection(
                problem=problem,
                lemma=lemma,
                reason=f"source=solver_not_proved; summary={solved.proof_summary}",
            )
            attempt_row.terminal_disposition = "solver_failed"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.RETRY_SOLVER.value,
                reason=solved.proof_summary or "solver did not prove lemma",
                terminal_worker_result=f"agent4:{solved.status}",
                owner_decomposition=owner,
            )
        return True, False

    def _harvest_lemma_vetter_job(
        self,
        *,
        problem: ProblemORM,
        dec: DecompositionORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> tuple[bool, bool]:
        continuation_generation = self._continuation_generation(problem)
        attempt_number = max(1, lemma.solver_attempt_count)
        worker_row = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_vetter",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
            statuses={"completed", "failed"},
        )
        if worker_row is None:
            return False, False

        job_id = worker_row.worker_job_id
        consumed_row, consumed = self.worker_jobs.consume_terminal(job_id, execution_id=self.execution_id)
        if not consumed or consumed_row is None:
            return False, False

        if consumed_row.status == "failed":
            return self._handle_terminal_lemma_worker_failure(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                agent_key="agent5",
                worker_job_id=job_id,
                artifact_prefix=consumed_row.artifact_prefix or f"worker_jobs/{job_id}",
                error_payload=consumed_row.error_payload,
            )

        try:
            vet = Agent5Output.model_validate(consumed_row.result_payload or {})
        except Exception as exc:
            self.event_logger.transition(
                problem.problem_id,
                "lemma.vetter_output_invalid",
                None,
                "retry_solver",
                target_node_id=lemma.lemma_id,
                worker_job_id=job_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.RETRY_SOLVER.value,
                reason=f"invalid vetter output: {type(exc).__name__}: {exc}",
                terminal_worker_result="agent5:invalid_output",
                owner_decomposition=dec,
            )

        prior_report = self.vetter_reports.latest_for_target(problem.problem_id, lemma.lemma_id)
        report = VetterReportORM(
            report_id=new_id("rep"),
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            target_kind="lemma",
            statement_status=vet.statement_status,
            proof_status=vet.proof_status,
            drift_assessment=vet.drift_assessment,
            recommended_action=vet.recommended_action,
            reason=vet.reason,
            confidence=vet.confidence,
            feedback_for_solver=vet.feedback_for_solver,
            detailed_findings=vet.detailed_findings,
        )
        self.vetter_reports.create(report)
        attempt_row = self._record_vetter_attempt(
            lemma=lemma,
            attempt_number=attempt_number,
            worker_row=consumed_row,
            report=report,
        )

        if lemma.proof_graph_id and lemma.claim_node_id:
            dependency_status, dependency_violations = self.proof_graphs.validate_vetter_consistency(
                proof_graph_id=lemma.proof_graph_id,
                target_node_id=lemma.claim_node_id,
                artifact_id=job_id,
                dependency_assessment=vet.dependency_assessment,
                forbidden_findings=vet.forbidden_dependency_findings,
                undeclared_findings=vet.undeclared_citation_findings,
            )
            lemma.dependency_status = dependency_status
            latest_dep = self.proof_graphs.checks.latest_for_target(lemma.proof_graph_id, target_node_id=lemma.claim_node_id, artifact_kind="vetter")
            if latest_dep is not None:
                lemma.last_dependency_check_id = latest_dep.dependency_check_id
            if dependency_violations:
                self._increment_fatal_rejection(
                    problem=problem,
                    lemma=lemma,
                    reason=f"source=dependency_vetter; violations={json.dumps(dependency_violations, sort_keys=True)}",
                )
                route = RoutingStatus.DECOMPOSE_FURTHER.value if lemma.consecutive_fatal_rejections >= 2 else RoutingStatus.RETRY_SOLVER.value
                self.event_logger.transition(
                    problem.problem_id,
                    "lemma.dependency_violation",
                    None,
                    route,
                    target_node_id=lemma.lemma_id,
                    worker_job_id=job_id,
                    reason=json.dumps(dependency_violations, sort_keys=True)[:500],
                )
                return self._apply_immediate_lemma_follow_up(
                    problem=problem,
                    lemma=lemma,
                    cfg=cfg,
                    default_route=route,
                    reason=json.dumps(dependency_violations, sort_keys=True)[:500],
                    terminal_worker_result="agent5:dependency_violation",
                    owner_decomposition=dec,
                )

        lemma.latest_vetter_report_id = report.report_id
        lemma.statement_status = vet.statement_status

        if vet.statement_status == StatementStatus.FALSE.value and vet.candidate_counterexample:
            counterexample = self._register_counterexample(
                lemma=lemma,
                source_agent="agent5",
                source_worker_job_id=job_id,
                source_attempt_number=attempt_number,
                counterexample_text=vet.candidate_counterexample,
                summary=vet.reason,
                confidence=vet.confidence,
                status="accepted",
            )
            counterexample.accepted_by_report_id = report.report_id
            self.counterexamples.save(counterexample)
            lemma.truth_status = "false"
            lemma.counterexample_status = "accepted"
            lemma.active_counterexample_id = counterexample.counterexample_id
            self.lemmas.save(lemma)
            attempt_row.counterexample_record_id = counterexample.counterexample_id
            attempt_row.terminal_disposition = "counterexample_accepted_by_vetter"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            self._invalidate_parent_decomposition(
                problem,
                lemma,
                cfg,
                reason=vet.reason or "vetted counterexample",
                counterexample_id=counterexample.counterexample_id,
            )
            return True, problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value, ProblemStatus.PAUSED.value}

        drift_severity = vet.drift_assessment.get("drift_severity")
        if drift_severity == "minor":
            self.event_logger.transition(
                problem.problem_id,
                "drift.warning",
                None,
                "minor",
                target_node_id=lemma.lemma_id,
                reason=vet.reason,
            )

        route = route_vetter_result(
            vet.statement_status,
            vet.proof_status,
            drift_severity,
            cfg,
        )
        route, route_override = self._apply_vetter_route_override(
            base_route=route,
            vet=vet,
            prior_report=prior_report,
        )
        current_severity = self._current_vetter_issue_severity(vet)
        counter_override: str | None = None

        if route not in {"blocked", "flag_suspected_false"} and vet.statement_status == "plausible":
            if current_severity == "medium":
                self._increment_minor_rejection(
                    problem=problem,
                    lemma=lemma,
                    reason=f"source=vetter; severity=medium; reason={vet.reason}",
                )
                cap_reason = self._lemma_rejection_cap_reason(lemma, cfg)
                if cap_reason:
                    route = "decompose_further"
                    counter_override = f"minor_cap_reached: {cap_reason}"
                else:
                    route = "retry_solver"
                    counter_override = "minor_rejection_retry"
            elif current_severity == "high":
                self._increment_fatal_rejection(
                    problem=problem,
                    lemma=lemma,
                    reason=f"source=vetter; severity=high; reason={vet.reason}",
                )
                cap_reason = self._lemma_rejection_cap_reason(lemma, cfg)
                if cap_reason:
                    route = "decompose_further"
                    counter_override = f"fatal_cap_reached: {cap_reason}"
                else:
                    route = "retry_solver"
                    counter_override = "fatal_rejection_retry"

        route_reason = vet.reason
        if route_override:
            route_reason = f"{route_reason} | override={route_override}"
        if counter_override:
            route_reason = f"{route_reason} | counter={counter_override}"

        self.event_logger.transition(
            problem.problem_id,
            "lemma.vetter_route",
            None,
            route,
            target_node_id=lemma.lemma_id,
            reason=route_reason,
            worker_job_id=job_id,
        )

        if route == "blocked":
            if vet.statement_status == "plausible" and not lemma.drift_retry_used:
                lemma.drift_retry_used = True
                self.event_logger.transition(
                    problem.problem_id,
                    "lemma.drift_retry",
                    None,
                    "retry_solver",
                    target_node_id=lemma.lemma_id,
                    reason="major drift on plausible statement; retrying solver with drift feedback",
                )
                return self._apply_immediate_lemma_follow_up(
                    problem=problem,
                    lemma=lemma,
                    cfg=cfg,
                    default_route=RoutingStatus.RETRY_SOLVER.value,
                    reason="major drift on plausible statement; retrying solver with drift feedback",
                    terminal_worker_result="agent5:major_drift_retry",
                    owner_decomposition=dec,
                )
            attempt_row.terminal_disposition = "blocked_major_drift"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            self._handle_lemma_terminal_failure(
                problem,
                dec,
                lemma,
                failure_reason=FailureReason.PROOF_EXHAUSTED.value,
                terminal_error_message="major semantic drift",
            )
            return True, problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}

        if vet.recommended_action == "flag_suspected_false" and (
            vet.confidence >= cfg.routing.invalidation_confidence_threshold
        ):
            if cfg.mode.nl_only_mode:
                self._invalidate_parent_decomposition(
                    problem,
                    lemma,
                    cfg,
                    reason="nl-only suspected false with high confidence",
                )
                attempt_row.terminal_disposition = "suspected_false_invalidated"
                self.proof_attempts.save(attempt_row)
                self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
                return True, problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value, ProblemStatus.PAUSED.value}
            if self._submit_plausibility_job(problem, lemma, cfg):
                self._save_lemma_transition(
                    lemma,
                    routing_status=RoutingStatus.BLOCKED.value,
                    next_action="wait_on_plausibility_check",
                    terminal_worker_result="agent5:plausibility_check_submitted",
                    reason="suspected false; waiting on plausibility check",
                    clear_solver_series=True,
                )
            attempt_row.terminal_disposition = "suspected_false_plausibility"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            return True, False

        if route == "retry_solver":
            attempt_row.terminal_disposition = "retry_solver"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.RETRY_SOLVER.value,
                reason=route_reason,
                terminal_worker_result="agent5:retry_solver",
                owner_decomposition=dec,
            )

        if route == "decompose_further":
            attempt_row.terminal_disposition = "decompose_further"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            changed, terminal = self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.DECOMPOSE_FURTHER.value,
                reason=self._lemma_rejection_cap_reason(lemma, cfg) or "vetter requested decomposition",
                terminal_worker_result="agent5:decompose_further",
                owner_decomposition=dec,
            )
            return changed, terminal or problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}

        if route == "send_to_lean":
            self._reset_consecutive_fatal_rejections(lemma)
            if cfg.mode.nl_only_mode:
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.NL_ACCEPTED.value,
                    routing_status=RoutingStatus.DONE.value,
                    next_action="done",
                    terminal_worker_result="agent5:accepted",
                    reason=route_reason,
                    clear_solver_series=True,
                )
            else:
                self._mark_current_proof_as_lean_ready(lemma)
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_VETTED.value,
                    routing_status=RoutingStatus.READY_FOR_LEAN.value,
                    next_action="formalize_in_lean",
                    terminal_worker_result="agent5:accepted",
                    reason=route_reason,
                    clear_solver_series=True,
                )
                self._ensure_formalize_job(problem, lemma, cfg)
            attempt_row.terminal_disposition = "accepted"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            self.proof_bundles.build_lemma_bundle(
                problem.problem_id,
                lemma.lemma_id,
                legacy=False,
            )
            return True, False

        attempt_row.terminal_disposition = "retry_solver_fallback"
        self.proof_attempts.save(attempt_row)
        self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
        return self._apply_immediate_lemma_follow_up(
            problem=problem,
            lemma=lemma,
            cfg=cfg,
            default_route=RoutingStatus.RETRY_SOLVER.value,
            reason=route_reason,
            terminal_worker_result="agent5:retry_solver_fallback",
            owner_decomposition=dec,
        )

    def _harvest_counterexample_vetter_job(
        self,
        *,
        problem: ProblemORM,
        dec: DecompositionORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
    ) -> tuple[bool, bool]:
        counterexample_id = str(lemma.active_counterexample_id or "").strip()
        if not counterexample_id:
            return False, False
        counterexample = self.counterexamples.get(counterexample_id)
        if counterexample is None or counterexample.status != "pending_vet":
            return False, False

        continuation_generation = self._continuation_generation(problem)
        attempt_number = max(1, int(counterexample.source_attempt_number or lemma.solver_attempt_count or 1))
        worker_row = self.worker_jobs.find_by_target_request_attempt(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            request_source="lemma_counterexample_vetter",
            attempt_number=attempt_number,
            continuation_generation=continuation_generation,
            statuses={"completed", "failed"},
        )
        if worker_row is None:
            return False, False

        job_id = worker_row.worker_job_id
        consumed_row, consumed = self.worker_jobs.consume_terminal(job_id, execution_id=self.execution_id)
        if not consumed or consumed_row is None:
            return False, False

        if consumed_row.status == "failed":
            self._record_counterexample_vetter_attempt(
                lemma=lemma,
                attempt_number=attempt_number,
                worker_row=consumed_row,
                disposition="counterexample_vetter_failed",
            )
            return self._handle_terminal_lemma_worker_failure(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                agent_key="agent5",
                worker_job_id=job_id,
                artifact_prefix=consumed_row.artifact_prefix or f"worker_jobs/{job_id}",
                error_payload=consumed_row.error_payload,
            )

        try:
            vet = Agent5Output.model_validate(consumed_row.result_payload or {})
        except Exception as exc:
            self.event_logger.transition(
                problem.problem_id,
                "lemma.counterexample_vetter_output_invalid",
                None,
                "retry_solver",
                target_node_id=lemma.lemma_id,
                worker_job_id=job_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
            return self._apply_immediate_lemma_follow_up(
                problem=problem,
                lemma=lemma,
                cfg=cfg,
                default_route=RoutingStatus.RETRY_SOLVER.value,
                reason=f"invalid counterexample vetter output: {type(exc).__name__}: {exc}",
                terminal_worker_result="agent5:counterexample_invalid_output",
                owner_decomposition=dec,
            )

        report = VetterReportORM(
            report_id=new_id("rep"),
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            target_kind="lemma",
            statement_status=vet.statement_status,
            proof_status=vet.proof_status,
            drift_assessment=vet.drift_assessment,
            recommended_action=vet.recommended_action,
            reason=vet.reason,
            confidence=vet.confidence,
            feedback_for_solver=vet.feedback_for_solver,
            detailed_findings=vet.detailed_findings,
        )
        self.vetter_reports.create(report)
        attempt_row = self._record_vetter_attempt(
            lemma=lemma,
            attempt_number=attempt_number,
            worker_row=consumed_row,
            report=report,
        )
        attempt_row.counterexample_record_id = counterexample.counterexample_id

        accepted = (
            vet.counterexample_status == "accepted"
            or (vet.statement_status == StatementStatus.FALSE.value and bool(vet.candidate_counterexample or counterexample.counterexample_text))
        )
        if accepted:
            counterexample.status = "accepted"
            counterexample.accepted_by_report_id = report.report_id
            counterexample.summary = vet.reason
            counterexample.confidence = vet.confidence
            self.counterexamples.save(counterexample)
            lemma.truth_status = "false"
            lemma.statement_status = StatementStatus.FALSE.value
            lemma.counterexample_status = "accepted"
            lemma.active_counterexample_id = counterexample.counterexample_id
            self.lemmas.save(lemma)
            attempt_row.counterexample_vetter_status = "accepted"
            attempt_row.terminal_disposition = "counterexample_accepted"
            self.proof_attempts.save(attempt_row)
            self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
            self._invalidate_parent_decomposition(
                problem,
                lemma,
                cfg,
                reason=vet.reason or counterexample.summary or "counterexample accepted",
                counterexample_id=counterexample.counterexample_id,
            )
            return True, problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value, ProblemStatus.PAUSED.value}

        counterexample.status = "rejected"
        counterexample.rejected_by_report_id = report.report_id
        counterexample.summary = vet.reason
        counterexample.confidence = vet.confidence
        self.counterexamples.save(counterexample)
        lemma.counterexample_status = "rejected"
        self.lemmas.save(lemma)
        attempt_row.counterexample_vetter_status = "rejected"
        attempt_row.terminal_disposition = "counterexample_rejected"
        self.proof_attempts.save(attempt_row)
        self._persist_attempt_artifact(lemma=lemma, attempt=attempt_row)
        return self._apply_immediate_lemma_follow_up(
            problem=problem,
            lemma=lemma,
            cfg=cfg,
            default_route=RoutingStatus.RETRY_SOLVER.value,
            reason=vet.reason or "counterexample rejected by vetter",
            terminal_worker_result="agent5:counterexample_rejected",
            owner_decomposition=dec,
        )

    def _process_decomposition(self, problem: ProblemORM, root: Any, dec: DecompositionORM, cfg: ProblemConfig) -> bool:
        lemma_rows = [self.lemmas.get(lemma_id) for lemma_id in dec.lemma_ids]
        lemma_rows = [lem for lem in lemma_rows if lem is not None]
        changed = False
        max_parallel = max(1, cfg.lemma_solving.max_parallel_lemmas)
        continuation_generation = self._continuation_generation(problem)
        inflight_solver_jobs = [
            row for row in self.worker_jobs.list_by_problem(problem.problem_id, worker_kind="lemma_solver")
            if row.status in {"queued", "running"}
            and row.superseded_at is None
            and int(row.continuation_generation or 0) == continuation_generation
        ]
        available_solver_slots = max(0, max_parallel - len(inflight_solver_jobs))

        for lemma in lemma_rows:
            if lemma.routing_status == RoutingStatus.DONE.value:
                continue

            # Recursive decomposition handling: if this lemma has an active child
            # decomposition, process that branch before solving this node again.
            if self._process_child_decomposition_for_lemma(problem, root, lemma, cfg):
                changed = True
                if problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value, ProblemStatus.PAUSED.value}:
                    return True
                continue

            if lemma.routing_status == RoutingStatus.DONE.value:
                continue

            if lemma.routing_status == RoutingStatus.SPLIT_EXISTING_PROOF.value:
                if self._ensure_split_existing_proof_job(problem=problem, root=root, lemma=lemma):
                    changed = True
                consumed, terminal = self._harvest_split_existing_proof_job(
                    problem=problem,
                    lemma=lemma,
                )
                changed |= consumed
                if terminal:
                    return True
                continue

            if lemma.next_action == "wait_on_split_existing_proof":
                consumed, terminal = self._harvest_split_existing_proof_job(
                    problem=problem,
                    lemma=lemma,
                )
                changed |= consumed
                if terminal:
                    return True
                continue

            if lemma.next_action == "wait_on_split_existing_proof_vetter":
                consumed, terminal = self._harvest_split_existing_proof_vetter_job(
                    problem=problem,
                    root=root,
                    lemma=lemma,
                    cfg=cfg,
                )
                changed |= consumed
                if terminal:
                    return True
                continue

            if (
                lemma.proof_status == ProofStatus.PROOF_VETTED.value
                and lemma.routing_status in self._formalize_routing_states()
                and not cfg.mode.nl_only_mode
            ):
                if self._ensure_formalize_job(problem, lemma, cfg):
                    changed = True
                continue

            if lemma.proof_status == ProofStatus.PROOF_FOUND.value:
                if self._ensure_lemma_vetter_job(problem=problem, root=root, lemma=lemma):
                    changed = True
                consumed, terminal = self._harvest_lemma_vetter_job(
                    problem=problem,
                    dec=dec,
                    lemma=lemma,
                    cfg=cfg,
                )
                changed |= consumed
                if terminal:
                    return True
                continue

            if lemma.counterexample_status == "pending_vet":
                counterexample = self.counterexamples.get(str(lemma.active_counterexample_id or ""))
                if counterexample is not None and self._ensure_counterexample_vetter_job(
                    problem=problem,
                    root=root,
                    lemma=lemma,
                    counterexample=counterexample,
                ):
                    changed = True
                consumed, terminal = self._harvest_counterexample_vetter_job(
                    problem=problem,
                    dec=dec,
                    lemma=lemma,
                    cfg=cfg,
                )
                changed |= consumed
                if terminal:
                    return True
                continue

            if lemma.proof_status in {ProofStatus.OPEN.value, ProofStatus.PROOF_FLAWED.value, ProofStatus.FAILED.value}:
                if self._lemma_waiting_on_child_decomposition_frontier(lemma):
                    continue
                cap_reason = self._lemma_rejection_cap_reason(lemma, cfg)
                guardrail_reason = self._lemma_solver_guardrail_reason(problem, lemma, cfg)
                if cap_reason or guardrail_reason:
                    terminal = self._handle_rejection_cap_for_lemma(
                        problem,
                        dec,
                        lemma,
                        cfg,
                        cap_reason=guardrail_reason or cap_reason or "lemma solver guardrail reached",
                    )
                    changed = True
                    if terminal or problem.status in {ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value, ProblemStatus.PAUSED.value}:
                        return True
                    continue

                if available_solver_slots > 0:
                    if self._ensure_lemma_solver_job(problem=problem, root=root, lemma=lemma, cfg=cfg):
                        available_solver_slots = max(0, available_solver_slots - 1)
                        changed = True
                consumed, terminal = self._harvest_lemma_solver_job(
                    problem=problem,
                    lemma=lemma,
                    cfg=cfg,
                )
                changed |= consumed
                if terminal:
                    return True

                refreshed = self.lemmas.get(lemma.lemma_id)
                if refreshed is not None and refreshed.proof_status == ProofStatus.PROOF_FOUND.value:
                    if self._ensure_lemma_vetter_job(problem=problem, root=root, lemma=refreshed):
                        changed = True
                    consumed, terminal = self._harvest_lemma_vetter_job(
                        problem=problem,
                        dec=dec,
                        lemma=refreshed,
                        cfg=cfg,
                    )
                    changed |= consumed
                    if terminal:
                        return True

        # Decomposition-level terminal status update.
        if lemma_rows and all(lemma.routing_status == RoutingStatus.DONE.value for lemma in lemma_rows):
            if dec.controller_status not in {ControllerStatus.SUCCEEDED.value, ControllerStatus.FAILED.value}:
                dec.controller_status = ControllerStatus.SUCCEEDED.value
                self.decompositions.save(dec)
                self.event_logger.transition(
                    problem.problem_id,
                    "decomposition.succeeded",
                    ControllerStatus.ACTIVE.value,
                    ControllerStatus.SUCCEEDED.value,
                    target_node_id=dec.decomposition_id,
                    reason=f"all lemmas done for node={dec.node_id}",
                )
            if dec.node_kind == NodeKind.LEMMA.value:
                parent = self.lemmas.get(dec.node_id)
                if parent:
                    if cfg.mode.nl_only_mode:
                        self._save_lemma_transition(
                            parent,
                            proof_status=ProofStatus.NL_ACCEPTED.value,
                            routing_status=RoutingStatus.DONE.value,
                            next_action="done",
                            reason=f"child decomposition {dec.decomposition_id} succeeded",
                            clear_solver_series=True,
                        )
                    else:
                        self._mark_current_proof_as_lean_ready(parent)
                        self._save_lemma_transition(
                            parent,
                            proof_status=ProofStatus.PROOF_VETTED.value,
                            routing_status=RoutingStatus.READY_FOR_LEAN.value,
                            next_action="formalize_in_lean",
                            reason=f"child decomposition {dec.decomposition_id} succeeded",
                            clear_solver_series=True,
                        )
                        self._ensure_formalize_job(problem, parent, cfg)
                    self.proof_bundles.build_lemma_bundle(
                        problem.problem_id,
                        parent.lemma_id,
                        legacy=False,
                    )
                    self.event_logger.transition(
                        problem.problem_id,
                        "lemma.promoted_from_subdecomposition",
                        None,
                        parent.proof_status,
                        target_node_id=parent.lemma_id,
                        reason=f"child decomposition {dec.decomposition_id} succeeded",
                    )
                    return True
            changed = True

        return changed

    @staticmethod
    def _lean_mode_cfg(cfg: ProblemConfig) -> Any:
        return getattr(getattr(cfg, "mode", None), "lean", None)

    def _lean_v2_prepare_enabled(self, cfg: ProblemConfig) -> bool:
        return bool(not cfg.mode.nl_only_mode and getattr(cfg.mode, "lean_mode", True))

    def _lean_v2_operations_enabled(self, cfg: ProblemConfig) -> bool:
        return self._lean_v2_prepare_enabled(cfg)

    def _should_split_existing_proof(
        self,
        *,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        error_class: str | None = None,
    ) -> bool:
        lean_cfg = self._lean_mode_cfg(cfg)
        auto_split_enabled = bool(getattr(lean_cfg, "auto_split_sublemmas", False)) if lean_cfg else False
        if not auto_split_enabled:
            return False
        if not lemma.latest_nl_proof:
            return False
        if error_class == "false_lemma_suspected":
            return False
        if is_deterministic_lean_setup_error(error_class):
            return False
        return True

    @staticmethod
    def _formalize_routing_states() -> set[str]:
        return {
            RoutingStatus.READY_FOR_LEAN.value,
            RoutingStatus.SEND_TO_LEAN.value,
        }

    @staticmethod
    def _proof_fingerprint_for_lean(lemma: LemmaORM) -> str | None:
        proof_nl = str(lemma.latest_nl_proof or "").strip()
        if not proof_nl:
            return None
        payload = {
            "lemma_id": lemma.lemma_id,
            "statement_nl": lemma.statement_nl,
            "proof_nl": proof_nl,
            "latest_vetter_report_id": lemma.latest_vetter_report_id,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _mark_current_proof_as_lean_ready(lemma: LemmaORM) -> str | None:
        fingerprint = Orchestrator._proof_fingerprint_for_lean(lemma)
        lemma.latest_vetted_proof_fingerprint = fingerprint
        return fingerprint

    def _active_formalize_jobs(self, problem_id: str) -> list[LeanJobORM]:
        return [
            job
            for job in self.lean_jobs.list_non_terminal(problem_id)
            if (job.operation or job.mode) == "formalize_lemma_from_nl"
        ]

    def _activate_ready_for_lean_lemmas(
        self,
        *,
        problem: ProblemORM,
        dec: DecompositionORM,
        cfg: ProblemConfig,
    ) -> bool:
        changed = False
        for lemma_id in dec.lemma_ids:
            lemma = self.lemmas.get(lemma_id)
            if lemma is None:
                continue
            if lemma.proof_status != ProofStatus.PROOF_VETTED.value:
                continue
            if lemma.routing_status not in self._formalize_routing_states():
                continue
            if self._ensure_formalize_job(problem, lemma, cfg):
                changed = True
        return changed

    def _submit_prepare_track_if_v2(self, problem: ProblemORM, dec: DecompositionORM, cfg: ProblemConfig) -> bool:
        if not self._lean_v2_prepare_enabled(cfg):
            return False
        if dec.lean_v2_track_id:
            return False

        existing_jobs = [
            job
            for job in self.lean_jobs.list_by_target(problem.problem_id, dec.decomposition_id)
            if (job.operation or job.mode) == "prepare_track"
        ]
        if any(job.status in {"queued", "running"} for job in existing_jobs):
            return False

        lean_cfg = self._lean_mode_cfg(cfg)
        max_track_attempts = max(1, int(getattr(lean_cfg, "max_track_attempts", 3) or 3))
        counted_existing_jobs = [job for job in existing_jobs if job.status != "cancelled"]
        if len(counted_existing_jobs) >= max_track_attempts:
            dec.lean_v2_prepare_status = "exhausted"
            dec.controller_status = ControllerStatus.FAILED.value
            self.decompositions.save(dec)
            return False

        if dec.node_kind == NodeKind.THEOREM.value:
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if not root:
                return False
            theorem_nl = root.statement_nl
            theorem_semantic_sketch = root.statement_semantic_sketch
        else:
            owner_lemma = self.lemmas.get(dec.node_id)
            if not owner_lemma:
                return False
            theorem_nl = owner_lemma.statement_nl
            theorem_semantic_sketch = owner_lemma.statement_semantic_sketch

        plan = self.assembly_plans.get(dec.assembly_plan_id) if dec.assembly_plan_id else None
        lemma_rows = [self.lemmas.get(lemma_id) for lemma_id in dec.lemma_ids]
        source_payload = self._build_lean_source_for_decomposition(
            problem=problem,
            decomposition=dec,
            theorem_nl=theorem_nl,
            theorem_semantic_sketch=theorem_semantic_sketch,
            lemma_rows=[lemma for lemma in lemma_rows if lemma is not None],
            plan=plan,
        )

        attempt_index = self._next_attempt_index_for_lean_operation(
            problem_id=problem.problem_id,
            target_id=dec.decomposition_id,
            operations={"prepare_track"},
        )
        track_id = f"track_{dec.decomposition_id}"
        payload = {
            "track_id": track_id,
            "decomposition_id": dec.decomposition_id,
            "source": source_payload,
            "source_kind": "json",
            "source_name": f"{problem.problem_id}:{dec.decomposition_id}:prepare_track:{attempt_index}",
        }

        try:
            submitted_job = self._submit_lean_job(
                problem,
                target_id=dec.decomposition_id,
                target_kind="assembly",
                mode="prepare_track",
                operation="prepare_track",
                payload=payload,
                attempt_index=attempt_index,
                options=self._lean_job_options(
                    cfg,
                    timeout_seconds=cfg.lean_engine.assembly_check_timeout_seconds,
                    extra={"max_repair_rounds": cfg.decomposition.assembly_check_repair_rounds},
                ),
                prefer_operation_endpoint=self._lean_v2_operations_enabled(cfg),
                fallback_to_job_endpoint=False,
            )
        except Exception as exc:
            dec.lean_v2_prepare_status = "failed"
            self.decompositions.save(dec)
            self.event_logger.transition(
                problem.problem_id,
                "lean_v2.prepare_track_failed",
                None,
                "failed",
                target_node_id=dec.decomposition_id,
                reason=f"prepare_track error: {str(exc)[:200]}",
            )
            return False

        dec.lean_v2_prepare_status = "queued"
        dec.latest_prepare_track_job_id = submitted_job.job_id
        self.decompositions.save(dec)
        self.event_logger.transition(
            problem.problem_id,
            "lean_v2.prepare_track_submitted",
            None,
            "queued",
            target_node_id=dec.decomposition_id,
            reason=f"attempt={attempt_index}",
        )
        return True

    def _lean_client_for_problem(self, problem: ProblemORM, *, create_if_missing: bool = True):
        override = getattr(self, "lean", None)
        if override is not None:
            return override
        return self.lean_sessions.client_for_problem(problem, create_if_missing=create_if_missing)

    def _next_attempt_index_for_lean_operation(
        self,
        *,
        problem_id: str,
        target_id: str,
        operations: set[str],
    ) -> int:
        prior = [
            job
            for job in self.lean_jobs.list_by_target(problem_id, target_id)
            if str(job.operation or job.mode or "").strip() in operations
        ]
        return max((int(job.attempt_index or 0) for job in prior), default=0) + 1

    def _ensure_formalize_job(self, problem: ProblemORM, lemma: LemmaORM, cfg: ProblemConfig) -> bool:
        existing = [job for job in self.lean_jobs.list_by_target(problem.problem_id, lemma.lemma_id) if job.mode == LeanJobMode.FORMALIZE_LEMMA.value]
        non_terminal = [job for job in existing if job.status in {"queued", "running"}]
        if non_terminal:
            return False

        current_fingerprint = lemma.latest_vetted_proof_fingerprint or self._mark_current_proof_as_lean_ready(lemma)
        if not current_fingerprint:
            return False
        if lemma.last_submitted_lean_proof_fingerprint == current_fingerprint:
            return False

        latest_attempt = max((job.attempt_index for job in existing), default=0)
        owner_decomp = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
        if not self._lean_v2_prepare_enabled(cfg) or owner_decomp is None:
            return False
        if owner_decomp.lean_v2_prepare_status != "success" or not owner_decomp.lean_v2_track_id:
            return False
        if len(self._active_formalize_jobs(problem.problem_id)) >= cfg.lean_engine.effective_max_parallel_lean_jobs:
            return False

        run_dir = owner_decomp.lean_run_dir
        lemma_handle = owner_decomp.lean_v2_lemma_handles.get(lemma.lemma_id)
        if not run_dir or not lemma_handle:
            return False

        pinned_signature = None
        if owner_decomp.pinned_statement_signatures:
            pinned_signature = owner_decomp.pinned_statement_signatures.get(lemma.lemma_id)
        payload = {
            "run_dir": run_dir,
            "track_run_dir": run_dir,
            "track_id": owner_decomp.lean_v2_track_id,
            "lemma_id": lemma.lemma_id,
            "lemma_handle": lemma_handle,
            "statement_nl": lemma.statement_nl,
            "semantic_sketch": lemma.statement_semantic_sketch,
            "proof_nl": lemma.latest_nl_proof,
            "proof_fingerprint": current_fingerprint,
            "pinned_statement_signature": pinned_signature,
            "trusted_context": [
                {"decl_name": row.decl_name, "lean_code": row.lean_code}
                for row in self.trusted_context.list_for_graph(
                    problem.problem_id,
                    proof_graph_id=lemma.proof_graph_id or problem.active_proof_graph_id,
                )
            ],
            "imports": ["Mathlib"],
            "max_repair_rounds": cfg.lean_engine.max_repair_rounds,
            "max_tool_calls": cfg.lean_engine.max_tool_calls_per_job,
            "timeout_seconds": cfg.lean_engine.lean_job_timeout_seconds,
            "internal_packaging_retry_count": cfg.lean_engine.internal_packaging_retry_count,
            "repair_context_token_budget": cfg.lean_engine.repair_context_token_budget,
            "model": cfg.lean_engine.model,
            "proof_issue_class": None,
            "lean_issue_class": lemma.latest_lean_issue_class,
        }

        submitted = self._submit_lean_job(
            problem,
            target_id=lemma.lemma_id,
            target_kind="lemma",
            mode=LeanJobMode.FORMALIZE_LEMMA.value,
            operation="formalize_lemma_from_nl",
            payload=payload,
            attempt_index=latest_attempt + 1,
            options=self._lean_job_options(
                cfg,
                timeout_seconds=cfg.lean_engine.lean_job_timeout_seconds,
                extra={"max_attempts_per_lemma": cfg.lean_engine.max_repair_rounds},
            ),
            prefer_operation_endpoint=True,
            fallback_to_job_endpoint=False,
        )
        lemma.lean_attempt_count += 1
        lemma.last_submitted_lean_proof_fingerprint = current_fingerprint
        lemma.last_submitted_lean_job_id = submitted.job_id
        self._save_lemma_transition(
            lemma,
            routing_status=RoutingStatus.READY_FOR_LEAN.value,
            next_action="wait_on_lean_formalization",
            reason="formalize_lemma_from_nl submitted",
            clear_solver_series=True,
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.formalize_submitted",
            None,
            "queued",
            target_node_id=lemma.lemma_id,
            reason="formalize_lemma_from_nl submitted",
        )
        return True

    def _find_owner_decomposition(self, problem_id: str, lemma_id: str) -> DecompositionORM | None:
        for dec in self.decompositions.list_by_problem(problem_id):
            if lemma_id in dec.lemma_ids:
                return dec
        return None

    def _build_lean_source_for_decomposition(
        self,
        *,
        problem: ProblemORM,
        decomposition: DecompositionORM,
        theorem_nl: str,
        theorem_semantic_sketch: dict[str, Any],
        lemma_rows: list[LemmaORM],
        plan: AssemblyPlanORM | None,
    ) -> dict[str, Any]:
        root_theorem_id = (
            decomposition.node_id
            if decomposition.node_kind == NodeKind.THEOREM.value
            else f"thm_{decomposition.decomposition_id}"
        )
        assembly_plan_id = plan.assembly_plan_id if plan else f"asm_{decomposition.decomposition_id}"
        assembly_steps = plan.steps if plan else []
        proof_skeleton_nl = plan.proof_skeleton_nl if plan else ""

        return {
            "problem_id": problem.problem_id,
            "title": problem.title,
            "verification_level": "formal",
            "root_theorem": {
                "theorem_id": root_theorem_id,
                "statement_nl": theorem_nl,
                "semantic_sketch": theorem_semantic_sketch,
            },
            "selected_decomposition": {
                "decomposition_id": decomposition.decomposition_id,
                "assembly_plan": {
                    "assembly_plan_id": assembly_plan_id,
                    "steps": assembly_steps,
                    "proof_skeleton_nl": proof_skeleton_nl,
                    "is_trivially_composable": True,
                },
            },
            "lemmas": [
                {
                    "lemma_id": lemma.lemma_id,
                    "statement_nl": lemma.statement_nl,
                    "semantic_sketch": lemma.statement_semantic_sketch,
                    "proof_nl": lemma.latest_nl_proof or "Proof sketch unavailable.",
                    "proof_status": "nl_accepted",
                    "routing_status": "done",
                }
                for lemma in lemma_rows
            ],
            "all_visible_lemmas_nl_accepted": True,
        }

    def _submit_plausibility_job(self, problem: ProblemORM, lemma: LemmaORM, cfg: ProblemConfig) -> bool:
        existing = [
            job
            for job in self.lean_jobs.list_by_target(problem.problem_id, lemma.lemma_id)
            if job.mode == LeanJobMode.CHECK_STATEMENT_PLAUSIBILITY.value and job.status in {"queued", "running"}
        ]
        if existing:
            return False

        payload = {
            "statement_nl": lemma.statement_nl,
            "semantic_sketch": lemma.statement_semantic_sketch,
            "imports": ["Mathlib"],
            "timeout_seconds": cfg.lean_engine.plausibility_check_timeout_seconds,
        }
        attempt_index = self._next_attempt_index_for_lean_operation(
            problem_id=problem.problem_id,
            target_id=lemma.lemma_id,
            operations={LeanJobMode.CHECK_STATEMENT_PLAUSIBILITY.value},
        )
        self._submit_lean_job(
            problem,
            target_id=lemma.lemma_id,
            target_kind="lemma",
            mode=LeanJobMode.CHECK_STATEMENT_PLAUSIBILITY.value,
            payload=payload,
            attempt_index=attempt_index,
            options=self._lean_job_options(
                cfg,
                timeout_seconds=cfg.lean_engine.plausibility_check_timeout_seconds,
            ),
        )
        return True

    def _submit_split_job(self, problem: ProblemORM, lemma: LemmaORM, cfg: ProblemConfig) -> bool:
        existing = [
            job
            for job in self.lean_jobs.list_by_target(problem.problem_id, lemma.lemma_id)
            if (job.operation or job.mode) == "split_proof_into_sublemmas" and job.status in {"queued", "running"}
        ]
        if existing:
            return False

        prior = [
            job
            for job in self.lean_jobs.list_by_target(problem.problem_id, lemma.lemma_id)
            if (job.operation or job.mode) == "split_proof_into_sublemmas"
        ]
        attempt_index = len(prior) + 1
        lean_cfg = self._lean_mode_cfg(cfg)
        payload = {
            "lemma_id": lemma.lemma_id,
            "statement_nl": lemma.statement_nl,
            "proof_nl": lemma.latest_nl_proof,
        }
        self._submit_lean_job(
            problem,
            target_id=lemma.lemma_id,
            target_kind="lemma",
            mode="split_proof_into_sublemmas",
            operation="split_proof_into_sublemmas",
            payload=payload,
            attempt_index=attempt_index,
            options=self._lean_job_options(
                cfg,
                timeout_seconds=cfg.lean_engine.lean_job_timeout_seconds,
            ),
            prefer_operation_endpoint=self._lean_v2_operations_enabled(cfg),
            fallback_to_job_endpoint=False,
        )
        return True

    def _record_lean_bottleneck(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        job: LeanJobORM,
        result_payload: dict[str, Any],
        result_row: LeanResultORM,
    ) -> dict[str, Any]:
        artifact_index = result_payload.get("artifact_index") if isinstance(result_payload.get("artifact_index"), dict) else result_row.artifact_index
        summary = {
            "job_id": job.job_id,
            "issue_kind": result_payload.get("issue_kind") or result_row.issue_kind,
            "error_class": result_payload.get("error_class") or result_row.error_class,
            "confidence": (
                float(result_payload.get("confidence"))
                if isinstance(result_payload.get("confidence"), (int, float))
                else result_row.confidence
            ),
            "fatality": result_payload.get("fatality") or result_row.fatality,
            "artifact_keys": artifact_index or {},
            "recommended_next_step": result_payload.get("recommended_next_step") or result_row.recommended_next_step,
        }

        owner = self._find_owner_decomposition(problem.problem_id, lemma.lemma_id)
        if owner is not None:
            owner.lean_bottlenecks = [*owner.lean_bottlenecks[-9:], summary]
            self.decompositions.save(owner)

        if lemma.proof_graph_id and lemma.claim_node_id:
            self.proof_graphs.annotate_node_metadata(
                proof_graph_id=lemma.proof_graph_id,
                node_id=lemma.claim_node_id,
                metadata={
                    "issue_kind": summary.get("issue_kind"),
                    "error_class": summary.get("error_class"),
                    "confidence": summary.get("confidence"),
                    "fatality": summary.get("fatality"),
                    "job_id": summary.get("job_id"),
                    "artifact_keys": summary.get("artifact_keys"),
                },
                node_status="blocked",
            )
        return summary

    def _build_proof_split_candidate(
        self,
        *,
        lemma: LemmaORM,
        split_output: Agent7Output,
        source_job_id: str,
    ) -> Agent2Candidate:
        context_items = list(split_output.context_items)
        context_items.append(
            {
                "kind": "lean_failure_subproof_decomposition",
                "label": "Lean failure subproof decomposition",
                "content": f"Generated from accepted proof split for lemma `{lemma.lemma_id}` via {source_job_id}.",
            }
        )
        return Agent2Candidate(
            candidate_index=1,
            strategy_summary=split_output.strategy_summary,
            shared_context=split_output.shared_context,
            context_items=context_items,
            lemmas=split_output.lemmas,
            assembly_plan=split_output.assembly_plan,
            formalization_cost_estimate_total=sum(float(item.formalization_cost_estimate) for item in split_output.lemmas),
            drift_self_check={
                "source": "split_existing_proof",
                "summary": split_output.summary,
                "parent_reassembly_explanation": split_output.parent_reassembly_explanation,
            },
        )

    def _materialize_split_existing_proof(
        self,
        *,
        problem: ProblemORM,
        root: Any,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        split_output: Agent7Output,
        vet_output: Agent8Output,
        source_job_id: str,
    ) -> bool:
        remaining_slots = self._remaining_decomposition_slots(
            problem.problem_id,
            lemma.lemma_id,
            NodeKind.LEMMA.value,
            cfg,
        )
        if remaining_slots <= 0:
            return False
        candidate = self._build_proof_split_candidate(
            lemma=lemma,
            split_output=split_output,
            source_job_id=source_job_id,
        )
        if not candidate.lemmas:
            return False
        prior_summaries = self._previous_attempt_summaries(problem.problem_id, lemma.lemma_id)
        prior_summaries.append(
            {
                "source": "split_existing_proof",
                "worker_job_id": source_job_id,
                "child_count": len(candidate.lemmas),
                "summary": split_output.summary,
                "confidence": split_output.confidence,
            }
        )
        payload = self._build_decomposition_generation_payload(
            problem=problem,
            node_id=lemma.lemma_id,
            theorem_nl=lemma.statement_nl,
            theorem_semantic_sketch=lemma.statement_semantic_sketch,
            num_candidates=1,
            previous_attempt_summaries=prior_summaries,
        )
        decomposition_id = new_id("dec")
        assembly_plan_id = new_id("asm")
        raw_candidate_artifact_id = f"worker_jobs/{source_job_id}/result.json"
        accepted_count = self._materialize_single_candidate(
            problem=problem,
            node_id=lemma.lemma_id,
            node_kind=NodeKind.LEMMA.value,
            request_tag=f"split_existing_proof_{source_job_id}",
            theorem_nl=lemma.statement_nl,
            theorem_semantic_sketch=lemma.statement_semantic_sketch,
            parent_depth=max(1, int(lemma.depth)),
            payload=payload,
            decompose_job_id=source_job_id,
            candidate=candidate,
            decomposition_id=decomposition_id,
            assembly_plan_id=assembly_plan_id,
            raw_candidate_artifact_id=raw_candidate_artifact_id,
            cfg=cfg,
            pre_vetted_bundle=vet_output,
            decomposition_origin="lean_failure_subproof_decomposition",
            decomposition_origin_reason=vet_output.summary,
            decomposition_origin_job_id=source_job_id,
        )
        if not accepted_count:
            return False
        self._select_active_decomposition_for_node(problem, lemma.lemma_id, NodeKind.LEMMA.value, cfg)
        self._save_lemma_transition(
            lemma,
            proof_status=ProofStatus.PROOF_FLAWED.value,
            routing_status=RoutingStatus.BLOCKED.value,
            next_action="process_child_decomposition",
            reason=vet_output.summary or f"split_existing_proof created {accepted_count} child decomposition(s)",
            clear_solver_series=True,
        )
        self.event_logger.transition(
            problem.problem_id,
            "lemma.split_existing_proof_materialized",
            None,
            "accepted",
            target_node_id=lemma.lemma_id,
            worker_job_id=source_job_id,
            reason=f"accepted_count={accepted_count}",
        )
        return True

    def _build_split_candidate(
        self,
        *,
        lemma: LemmaORM,
        sublemmas: list[dict[str, Any]],
    ) -> Agent2Candidate:
        split_lemmas: list[Agent2Lemma] = []
        for index, item in enumerate(sublemmas, start=1):
            statement_nl = str(item.get("statement_nl") or "").strip()
            if not statement_nl:
                continue
            local_id = str(item.get("local_id") or f"S{index}")
            proof_hint = str(item.get("proof_hint") or "").strip()
            source = str(item.get("source") or "lean_auto_split").strip()
            split_lemmas.append(
                Agent2Lemma(
                    local_id=local_id,
                    statement_nl=statement_nl,
                    semantic_sketch=SemanticSketch.model_validate(self._minimal_semantic_sketch(statement_nl)),
                    role_in_assembly=proof_hint or f"Prove subgoal {index} for `{lemma.statement_nl}`.",
                    lemma_relation_to_parent="bottleneck",
                    strictly_easier_reason="Lean identified this as a smaller intermediate target.",
                    bottleneck_reason=source,
                    formalization_cost_estimate=0.5,
                    self_check_true=True,
                    self_check_notes="Generated from Lean auto-split output.",
                )
            )

        local_ids = [row.local_id for row in split_lemmas]
        proof_skeleton_lines = [f"- {row.statement_nl}" for row in split_lemmas]
        return Agent2Candidate(
            candidate_index=1,
            strategy_summary="Lean-directed sublemma split",
            shared_context=[],
            context_items=[
                {
                    "kind": "lean_bottleneck",
                    "label": "Lean auto-split",
                    "content": f"Generated from Lean feedback for lemma `{lemma.lemma_id}`.",
                }
            ],
            lemmas=split_lemmas,
            assembly_plan=Agent2AssemblyPlan(
                steps=[
                    {
                        "step_id": "split_assembly",
                        "description": "Combine Lean-generated sublemmas back into the parent lemma.",
                        "uses_lemmas": local_ids,
                        "is_trivial": True,
                    }
                ],
                proof_skeleton_nl="\n".join(proof_skeleton_lines) if proof_skeleton_lines else lemma.statement_nl,
                final_step_yields_exact_root=True,
            ),
            formalization_cost_estimate_total=float(len(split_lemmas)) * 0.5,
            drift_self_check={"source": "lean_auto_split", "status": "external_guidance"},
        )

    def _materialize_split_sublemmas(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        cfg: ProblemConfig,
        sublemmas: list[dict[str, Any]],
        source_job_id: str,
    ) -> bool:
        remaining_slots = self._remaining_decomposition_slots(
            problem.problem_id,
            lemma.lemma_id,
            NodeKind.LEMMA.value,
            cfg,
        )
        if remaining_slots <= 0:
            return False

        candidate = self._build_split_candidate(lemma=lemma, sublemmas=sublemmas)
        if not candidate.lemmas:
            return False

        prior_summaries = self._previous_attempt_summaries(problem.problem_id, lemma.lemma_id)
        prior_summaries.append(
            {
                "source": "lean_auto_split",
                "worker_job_id": source_job_id,
                "sublemma_count": len(candidate.lemmas),
            }
        )
        payload = self._build_decomposition_generation_payload(
            problem=problem,
            node_id=lemma.lemma_id,
            theorem_nl=lemma.statement_nl,
            theorem_semantic_sketch=lemma.statement_semantic_sketch,
            num_candidates=1,
            previous_attempt_summaries=prior_summaries,
        )

        generated, accepted_count = self._materialize_decomposition_candidates(
            problem=problem,
            node_id=lemma.lemma_id,
            node_kind=NodeKind.LEMMA.value,
            request_tag=f"lean_split_{source_job_id}",
            theorem_nl=lemma.statement_nl,
            theorem_semantic_sketch=lemma.statement_semantic_sketch,
            parent_depth=max(1, int(lemma.depth)),
            payload=payload,
            decompose_job_id=source_job_id,
            candidates=[candidate],
            max_candidates=min(1, remaining_slots),
            cfg=cfg,
        )
        if accepted_count > 0:
            self._select_active_decomposition_for_node(problem, lemma.lemma_id, NodeKind.LEMMA.value, cfg)
            self._save_lemma_transition(
                lemma,
                proof_status=ProofStatus.PROOF_FLAWED.value,
                routing_status=RoutingStatus.BLOCKED.value,
                next_action="process_child_decomposition",
                reason=f"Lean auto-split produced {accepted_count} accepted child decomposition(s)",
                clear_solver_series=True,
            )
            self.event_logger.transition(
                problem.problem_id,
                "lean.split.materialized",
                None,
                "accepted",
                target_node_id=lemma.lemma_id,
                worker_job_id=source_job_id,
                reason=f"accepted_count={accepted_count}",
            )
            return True
        return generated

    def _submit_assembly_retry(
        self,
        problem: ProblemORM,
        dec: DecompositionORM,
        cfg: ProblemConfig,
        *,
        attempt_index: int,
    ) -> bool:
        if dec.node_kind == NodeKind.THEOREM.value:
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if not root:
                return False
            theorem_nl = root.statement_nl
            theorem_sketch = root.statement_semantic_sketch
        else:
            parent_lemma = self.lemmas.get(dec.node_id)
            if not parent_lemma:
                return False
            theorem_nl = parent_lemma.statement_nl
            theorem_sketch = parent_lemma.statement_semantic_sketch

        plan = self.assembly_plans.get(dec.assembly_plan_id) if dec.assembly_plan_id else None
        lemma_rows = [self.lemmas.get(lemma_id) for lemma_id in dec.lemma_ids]
        source_payload = self._build_lean_source_for_decomposition(
            problem=problem,
            decomposition=dec,
            theorem_nl=theorem_nl,
            theorem_semantic_sketch=theorem_sketch,
            lemma_rows=[lemma for lemma in lemma_rows if lemma is not None],
            plan=plan,
        )
        payload = {
            "source": source_payload,
            "source_kind": "json",
            "source_name": f"{problem.problem_id}:{dec.decomposition_id}:retry{attempt_index}",
        }

        self._submit_lean_job(
            problem,
            target_id=dec.decomposition_id,
            target_kind="assembly",
            mode=LeanJobMode.CHECK_ASSEMBLY.value,
            payload=payload,
            attempt_index=attempt_index,
            options=self._lean_job_options(
                cfg,
                timeout_seconds=cfg.lean_engine.assembly_check_timeout_seconds,
                extra={"max_repair_rounds": cfg.decomposition.assembly_check_repair_rounds},
            ),
        )
        return True

    def _lean_job_options(
        self,
        cfg: ProblemConfig,
        *,
        timeout_seconds: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = dict(extra or {})
        if timeout_seconds is not None:
            options["timeout_seconds"] = timeout_seconds
        if cfg.lean_engine.model is not None:
            options["model"] = cfg.lean_engine.model
        if cfg.lean_engine.no_lean4_refs:
            options["no_lean4_refs"] = True
        options["claude_activity_timeout_seconds"] = cfg.lean_engine.claude_activity_timeout_seconds
        if cfg.lean_engine.claude_init_timeout_seconds >= 0:
            options["claude_init_timeout_seconds"] = cfg.lean_engine.claude_init_timeout_seconds
        options["internal_packaging_retry_count"] = cfg.lean_engine.internal_packaging_retry_count
        return options

    def _submit_lean_job(
        self,
        problem: ProblemORM,
        *,
        target_id: str,
        target_kind: str,
        mode: str,
        operation: str | None = None,
        payload: dict[str, Any],
        attempt_index: int,
        options: dict[str, Any] | None = None,
        prefer_operation_endpoint: bool = False,
        fallback_to_job_endpoint: bool = True,
    ) -> LeanJobORM:
        operation_name = operation or mode
        job_id = f"lean_job_{target_id}_{operation_name}_{attempt_index}"
        existing = self.lean_jobs.get(job_id)
        if existing:
            return existing
        lean_client = self._lean_client_for_problem(problem)

        request_body = {
            "job_id": job_id,
            "problem_id": problem.problem_id,
            "target_id": target_id,
            "target_kind": target_kind,
            "mode": mode,
            "operation": operation_name,
            "operation_id": job_id,
            "lean_image_tag": problem.lean_image_tag,
            "callback_url": None,
            "payload": payload,
            "options": options or {},
        }

        req_artifact = self.artifacts.save_json(
            f"problems/{problem.problem_id}/lean_jobs/{job_id}/request.json",
            request_body,
        )

        job = LeanJobORM(
            job_id=job_id,
            problem_id=problem.problem_id,
            target_id=target_id,
            target_kind=target_kind,
            mode=mode,
            operation=operation_name,
            status="queued",
            attempt_index=attempt_index,
            proof_fingerprint=payload.get("proof_fingerprint") if isinstance(payload.get("proof_fingerprint"), str) else None,
            lean_image_tag=problem.lean_image_tag,
            request_artifact_id=req_artifact,
        )
        stored = self.lean_jobs.create_if_absent(job)

        if prefer_operation_endpoint and hasattr(lean_client, "submit_operation"):
            try:
                submit_response = lean_client.submit_operation(
                    operation_name,
                    request_body,
                    request_id=new_id("req"),
                    version="v2",
                )
            except Exception:
                if not fallback_to_job_endpoint:
                    raise
                submit_response = lean_client.submit_job(request_body, request_id=new_id("req"))
        else:
            submit_response = lean_client.submit_job(request_body, request_id=new_id("req"))

        remote_operation_id = submit_response.get("operation_id")
        if isinstance(remote_operation_id, str) and remote_operation_id.strip():
            stored.remote_operation_id = remote_operation_id.strip()
        self.artifacts.save_json(
            f"problems/{problem.problem_id}/lean_jobs/{job_id}/submit_response.json",
            submit_response,
        )
        record_usage_row(
            problem_id=problem.problem_id,
            lemma_id=target_id if target_kind == "lemma" else None,
            worker_job_id=job_id,
            stage=f"lean.{operation_name}.submit",
            provider="lean_engine",
            model=problem.lean_image_tag,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            raw_usage=None,
            db_session=self.store,
        )
        stored.status = submit_response.get("status", "queued")
        progress_snapshot = submit_response.get("progress_snapshot")
        if isinstance(progress_snapshot, dict):
            stored.progress_snapshot = progress_snapshot
        self.lean_jobs.save(stored)
        self.event_logger.transition(
            problem.problem_id,
            "lean.job_submitted",
            None,
            stored.status,
            target_node_id=target_id,
            worker_job_id=stored.job_id,
            reason=operation_name,
        )

        return stored

    @staticmethod
    def _parse_remote_utc(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            return None

    @classmethod
    def _timing_summary_from_lean_job(
        cls,
        *,
        submitted_at: datetime | None,
        started_at: datetime | None,
        completed_at: datetime | None,
        harvested_at: datetime | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary = dict(extra or {})
        if submitted_at and started_at:
            summary["queue_duration_seconds"] = max((started_at - submitted_at).total_seconds(), 0.0)
        if started_at and completed_at:
            summary["service_run_duration_seconds"] = max((completed_at - started_at).total_seconds(), 0.0)
        if completed_at and harvested_at:
            summary["controller_handoff_lag_seconds"] = max((harvested_at - completed_at).total_seconds(), 0.0)
        if submitted_at and harvested_at:
            summary["end_to_end_duration_seconds"] = max((harvested_at - submitted_at).total_seconds(), 0.0)
        return summary

    def _poll_lean_jobs(self, problem: ProblemORM, cfg: ProblemConfig) -> bool:
        non_terminal = self.lean_jobs.list_non_terminal(problem.problem_id)
        if not non_terminal:
            return False
        lean_client = self._lean_client_for_problem(problem)

        changed = False
        terminal_rows: list[tuple[LeanJobORM, str, dict[str, Any]]] = []
        for job in non_terminal:
            if job.remote_operation_id and hasattr(lean_client, "get_operation"):
                try:
                    polled = lean_client.get_operation(job.remote_operation_id, version="v2")
                except Exception:
                    polled = lean_client.get_job(job.job_id)
            else:
                polled = lean_client.get_job(job.job_id)

            old_status = job.status
            job.status = polled.get("status", job.status)
            polled_progress = polled.get("progress_snapshot")
            if isinstance(polled_progress, dict):
                job.progress_snapshot = polled_progress
            started_at = self._parse_remote_utc(polled.get("started_at"))
            completed_at = self._parse_remote_utc(polled.get("completed_at"))
            if started_at is not None:
                job.started_at = started_at
            if completed_at is not None:
                job.completed_at = completed_at

            if job.status in {"queued", "running"}:
                if (job.operation or job.mode) == "prepare_track":
                    dec = self.decompositions.get(job.target_id)
                    if dec is not None and dec.lean_v2_prepare_status != job.status:
                        dec.lean_v2_prepare_status = job.status
                        self.decompositions.save(dec)
                self.lean_jobs.save(job)
                self.event_logger.transition(
                    problem.problem_id,
                    "lean.job_polled",
                    old_status,
                    job.status,
                    target_node_id=job.target_id,
                    worker_job_id=job.job_id,
                    reason=f"mode={job.mode} operation={job.operation or job.mode}",
                )
                changed = True
                continue

            terminal_rows.append((job, old_status, polled))

        for job, old_status, polled in terminal_rows:
            result_payload = polled.get("result")
            if isinstance(result_payload, dict):
                if isinstance(result_payload.get("progress_snapshot"), dict):
                    job.progress_snapshot = result_payload.get("progress_snapshot")
                job.issue_kind = result_payload.get("issue_kind")
                if isinstance(result_payload.get("confidence"), (int, float)):
                    job.confidence = float(result_payload.get("confidence"))
                job.fatality = result_payload.get("fatality")
                job.last_error = result_payload.get("error_message")

            res_artifact = self.artifacts.save_json(
                f"problems/{problem.problem_id}/lean_jobs/{job.job_id}/response.json",
                polled,
            )
            harvest_now = datetime.now(UTC)
            job.result_artifact_id = res_artifact
            job.controller_harvested_at = harvest_now
            self.lean_jobs.save(job)

            result = self.lean_results.get_for_job(job.job_id)
            normalized_diagnostics = self._normalize_lean_diagnostics(
                result_payload.get("diagnostics", []) if isinstance(result_payload, dict) else []
            )
            timing_breakdown = self._timing_summary_from_lean_job(
                submitted_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                harvested_at=harvest_now,
                extra=(
                    result_payload.get("timing_breakdown")
                    if isinstance(result_payload, dict) and isinstance(result_payload.get("timing_breakdown"), dict)
                    else {}
                ),
            )
            if not result:
                result = LeanResultORM(
                    result_id=new_id("lean_res"),
                    job_id=job.job_id,
                    status=job.status,
                    error_class=result_payload.get("error_class") if isinstance(result_payload, dict) else None,
                    issue_kind=result_payload.get("issue_kind") if isinstance(result_payload, dict) else None,
                    artifact_check_status=result_payload.get("artifact_check_status") if isinstance(result_payload, dict) else None,
                    artifact_check_diagnostics=self._normalize_lean_diagnostics(
                        result_payload.get("artifact_check_diagnostics", []) if isinstance(result_payload, dict) else []
                    ),
                    integration_check_status=result_payload.get("integration_check_status") if isinstance(result_payload, dict) else None,
                    integration_check_diagnostics=self._normalize_lean_diagnostics(
                        result_payload.get("integration_check_diagnostics", []) if isinstance(result_payload, dict) else []
                    ),
                    confidence=(
                        float(result_payload.get("confidence"))
                        if isinstance(result_payload, dict) and isinstance(result_payload.get("confidence"), (int, float))
                        else None
                    ),
                    fatality=result_payload.get("fatality") if isinstance(result_payload, dict) else None,
                    error_scope=result_payload.get("error_scope") if isinstance(result_payload, dict) else None,
                    error_message=result_payload.get("error_message") if isinstance(result_payload, dict) else None,
                    progress_snapshot=(
                        result_payload.get("progress_snapshot") if isinstance(result_payload, dict) else None
                    ),
                    diagnostics=normalized_diagnostics,
                    decl_name=result_payload.get("decl_name") if isinstance(result_payload, dict) else None,
                    lean_code_artifact_id=None,
                    compiler_log_artifact_id=None,
                    artifact_index=(
                        result_payload.get("artifact_index") if isinstance(result_payload, dict) and isinstance(result_payload.get("artifact_index"), dict) else {}
                    ),
                    timing_breakdown=timing_breakdown,
                    recommended_next_step=result_payload.get("recommended_next_step") if isinstance(result_payload, dict) else None,
                    routing_confidence=result_payload.get("routing_confidence") if isinstance(result_payload, dict) else None,
                    controller_harvested_at=harvest_now,
                )
                self.lean_results.create(result)
            else:
                result.status = job.status
                result.controller_harvested_at = harvest_now
                if isinstance(result_payload, dict):
                    result.error_class = result_payload.get("error_class")
                    result.issue_kind = result_payload.get("issue_kind")
                    result.artifact_check_status = result_payload.get("artifact_check_status")
                    result.artifact_check_diagnostics = self._normalize_lean_diagnostics(result_payload.get("artifact_check_diagnostics", []))
                    result.integration_check_status = result_payload.get("integration_check_status")
                    result.integration_check_diagnostics = self._normalize_lean_diagnostics(result_payload.get("integration_check_diagnostics", []))
                    if isinstance(result_payload.get("confidence"), (int, float)):
                        result.confidence = float(result_payload.get("confidence"))
                    result.fatality = result_payload.get("fatality")
                    result.error_scope = result_payload.get("error_scope")
                    result.error_message = result_payload.get("error_message")
                    if isinstance(result_payload.get("progress_snapshot"), dict):
                        result.progress_snapshot = result_payload.get("progress_snapshot")
                    result.diagnostics = normalized_diagnostics
                    result.decl_name = result_payload.get("decl_name")
                    artifact_index = result_payload.get("artifact_index")
                    if isinstance(artifact_index, dict):
                        result.artifact_index = artifact_index
                    result.timing_breakdown = timing_breakdown
                    result.recommended_next_step = result_payload.get("recommended_next_step")
                    result.routing_confidence = result_payload.get("routing_confidence")
                self.lean_results.save(result)

            self.event_logger.transition(
                problem.problem_id,
                "lean.job_terminal",
                old_status,
                job.status,
                target_node_id=job.target_id,
                worker_job_id=job.job_id,
                reason=job.operation or job.mode,
            )
            self._route_terminal_lean_result(problem, job, result_payload or {}, cfg, result)
            changed = True

        return changed

    def _poll_one_lean_job(self, problem: ProblemORM, cfg: ProblemConfig) -> bool:
        return self._poll_lean_jobs(problem, cfg)

    @staticmethod
    def _normalize_lean_diagnostics(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                normalized.append(item)
                continue
            if item is None:
                continue
            message = str(item).strip()
            if message:
                normalized.append({"message": message})
        return normalized

    def _route_terminal_lean_result(
        self,
        problem: ProblemORM,
        job: LeanJobORM,
        result_payload: dict[str, Any],
        cfg: ProblemConfig,
        result_row: LeanResultORM,
    ) -> None:
        if (job.operation or job.mode) == "prepare_track":
            dec = self.decompositions.get(job.target_id)
            if not dec:
                return
            dec.latest_prepare_track_job_id = job.job_id
            issue_kind = result_payload.get("issue_kind") if isinstance(result_payload, dict) else None
            error_class = result_payload.get("error_class") if isinstance(result_payload, dict) else None
            confidence_raw = result_payload.get("confidence") if isinstance(result_payload, dict) else None
            confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
            fatality = result_payload.get("fatality") if isinstance(result_payload, dict) else None
            artifact_index = result_payload.get("artifact_index") if isinstance(result_payload.get("artifact_index"), dict) else {}
            if job.status == "success":
                dec.lean_v2_prepare_status = "success"
                track_id = result_payload.get("track_id")
                if isinstance(track_id, str) and track_id.strip():
                    dec.lean_v2_track_id = track_id.strip()
                run_dir = result_payload.get("run_dir")
                if isinstance(run_dir, str) and run_dir.strip():
                    dec.lean_run_dir = run_dir.strip()
                lemma_handles = result_payload.get("lemma_handles")
                if isinstance(lemma_handles, dict):
                    dec.lean_v2_lemma_handles = {
                        str(key): str(value)
                        for key, value in lemma_handles.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
                pinned_signatures = result_payload.get("pinned_statement_signatures")
                if isinstance(pinned_signatures, dict):
                    dec.pinned_statement_signatures = pinned_signatures
                dec.lean_prepare_issue_kind = None
                dec.lean_prepare_error_class = None
                dec.lean_prepare_confidence = None
                dec.lean_prepare_fatality = None
                dec.lean_artifact_index = artifact_index or {}
                self.decompositions.save(dec)
                self._activate_ready_for_lean_lemmas(problem=problem, dec=dec, cfg=cfg)
                return
            else:
                dec.lean_v2_prepare_status = job.status
                dec.lean_prepare_issue_kind = issue_kind if isinstance(issue_kind, str) else None
                dec.lean_prepare_error_class = error_class if isinstance(error_class, str) else None
                dec.lean_prepare_confidence = confidence
                dec.lean_prepare_fatality = fatality if isinstance(fatality, str) else None
                dec.lean_artifact_index = artifact_index or {}
                if issue_kind == "lean_issue":
                    max_track_attempts = max(1, int(getattr(self._lean_mode_cfg(cfg), "max_track_attempts", 3) or 3))
                    if job.attempt_index < max_track_attempts:
                        dec.lean_v2_prepare_status = "pending"
                        self.decompositions.save(dec)
                        self._submit_prepare_track_if_v2(problem, dec, cfg)
                        self.event_logger.transition(
                            problem.problem_id,
                            "lean_v2.prepare_track_retry",
                            None,
                            "queued",
                            target_node_id=dec.decomposition_id,
                            worker_job_id=job.job_id,
                            reason=f"attempt={job.attempt_index + 1}; error_class={error_class}",
                        )
                        return
                dec.lean_v2_prepare_status = "lean_blocked"
                dec.failure_origin = (
                    "lean_prepare:proof_issue"
                    if issue_kind == "proof_issue"
                    else "lean_prepare:lean_issue"
                )
                dec.failure_reason = (
                    str(result_payload.get("error_message") or error_class or "Lean track preparation failed")
                )
            self.decompositions.save(dec)
            return

        if job.mode == LeanJobMode.CHECK_ASSEMBLY.value:
            dec = self.decompositions.get(job.target_id)
            if not dec:
                return
            if job.status == "success":
                dec.lean_assembly_status = LeanAssemblyStatus.SUCCESS.value
                pinned_signatures = result_payload.get("pinned_statement_signatures")
                if isinstance(pinned_signatures, dict):
                    dec.pinned_statement_signatures = pinned_signatures
                run_dir = result_payload.get("run_dir")
                if isinstance(run_dir, str) and run_dir.strip():
                    dec.lean_run_dir = run_dir.strip()
                self.decompositions.save(dec)
            else:
                error_class = result_payload.get("error_class")
                if error_class == "bad_statement_translation" and job.attempt_index < 2:
                    dec.lean_assembly_status = LeanAssemblyStatus.PENDING.value
                    self.decompositions.save(dec)
                    self._submit_assembly_retry(problem, dec, cfg, attempt_index=job.attempt_index + 1)
                    self.event_logger.transition(
                        problem.problem_id,
                        "decomposition.assembly_retry",
                        None,
                        "queued",
                        target_node_id=dec.decomposition_id,
                        worker_job_id=job.job_id,
                        reason="bad_statement_translation retry",
                    )
                    return
                dec.lean_assembly_status = LeanAssemblyStatus.FATAL.value
                dec.controller_status = ControllerStatus.FAILED.value
                self.decompositions.save(dec)
            return

        if job.mode == LeanJobMode.FORMALIZE_LEMMA.value:
            lemma = self.lemmas.get(job.target_id)
            if not lemma:
                return
            lemma.latest_lean_result_id = result_row.result_id
            issue_kind = result_payload.get("issue_kind") if isinstance(result_payload, dict) else None
            if issue_kind is None:
                issue_kind = result_row.issue_kind
            confidence_raw = result_payload.get("confidence") if isinstance(result_payload, dict) else None
            confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else result_row.confidence
            self.lemmas.save(lemma)

            if (
                job.status == "success"
                and result_payload.get("compiler_ok")
                and result_row.integration_check_status == "success"
                and result_row.artifact_check_status == "success"
            ):
                lemma.latest_lean_issue_class = None
                lemma.latest_lean_issue_kind = None
                lemma.proof_status = ProofStatus.PROOF_FORMALIZED.value
                lemma.statement_status = StatementStatus.FORMALIZED.value
                self._save_lemma_transition(
                    lemma,
                    routing_status=RoutingStatus.DONE.value,
                    next_action="done",
                    reason="formalization succeeded",
                    clear_solver_series=True,
                )
                self.proof_bundles.build_lemma_bundle(
                    problem.problem_id,
                    lemma.lemma_id,
                    legacy=False,
                )

                decl_name = result_payload.get("decl_name")
                lean_code = result_payload.get("lean_code")
                if decl_name and lean_code and self.proof_graphs.can_promote_trusted_context(lemma):
                    stored_trusted = self.trusted_context.create_if_absent(
                        problem.problem_id,
                        decl_name,
                        lean_code,
                        source_lemma_id=lemma.lemma_id,
                        source_job_id=job.job_id,
                        proof_graph_id=lemma.proof_graph_id,
                        context_scope="graph_local",
                    )
                    if lemma.proof_graph_id:
                        self.proof_graphs.record_trusted_decl_node(
                            proof_graph_id=lemma.proof_graph_id,
                            decl_name=decl_name,
                            lean_code=lean_code,
                            owner_id=str(stored_trusted.id),
                            metadata={"source_lemma_id": lemma.lemma_id, "source_job_id": job.job_id},
                        )
                return

            error_class = result_payload.get("error_class")
            lemma.latest_lean_issue_class = error_class if isinstance(error_class, str) else None
            lemma.latest_lean_issue_kind = issue_kind if isinstance(issue_kind, str) else None
            self.lemmas.save(lemma)
            bottleneck = self._record_lean_bottleneck(
                problem=problem,
                lemma=lemma,
                job=job,
                result_payload=result_payload,
                result_row=result_row,
            )

            route = route_lean_result(job.status, error_class, cfg, issue_kind=issue_kind)
            lean_cfg = self._lean_mode_cfg(cfg)
            strict_fail_fast = bool(getattr(lean_cfg, "strict_proof_issue_fail_fast", False)) if lean_cfg else False
            confidence_threshold = float(getattr(lean_cfg, "proof_issue_confidence_threshold", 0.8)) if lean_cfg else 0.8
            if issue_kind == "proof_issue" and strict_fail_fast and confidence is not None and confidence >= confidence_threshold:
                route = "retry_nl_proof" if error_class == "false_lemma_suspected" else "decompose_further"

            self.event_logger.transition(
                problem.problem_id,
                "lean.result_classified",
                None,
                route,
                target_node_id=lemma.lemma_id,
                worker_job_id=job.job_id,
                reason=f"issue_kind={issue_kind or '-'} error_class={error_class or '-'} confidence={confidence}; bottleneck={json.dumps(bottleneck, sort_keys=True)[:300]}",
            )

            if route == "retry_lean_only" and self._should_split_existing_proof(lemma=lemma, cfg=cfg, error_class=error_class):
                route = "split_existing_proof"
            if route == "retry_lean_only":
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_FLAWED.value,
                    routing_status=RoutingStatus.RETRY_SOLVER.value,
                    next_action="retry_solver",
                    reason="Lean exhausted the current vetted NL proof version; waiting for a new NL proof before another Lean run",
                    ensure_solver_series=True,
                )
                return
            if route == "retry_nl_proof":
                if error_class == "false_lemma_suspected":
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.PROOF_FLAWED.value,
                        routing_status=RoutingStatus.RETRY_SOLVER.value,
                        next_action="retry_solver",
                        reason="Lean suspected the lemma statement/proof is false; returning to NL re-proof",
                        ensure_solver_series=True,
                    )
                    return
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_FLAWED.value,
                    routing_status=RoutingStatus.RETRY_SOLVER.value,
                    next_action="retry_solver",
                    reason="lean requested NL proof retry",
                    ensure_solver_series=True,
                )
                return
            if route == "check_statement_plausibility":
                self._submit_plausibility_job(problem, lemma, cfg)
                self._save_lemma_transition(
                    lemma,
                    routing_status=RoutingStatus.BLOCKED.value,
                    next_action="wait_on_plausibility_check",
                    reason="lean requested statement plausibility check",
                    clear_solver_series=True,
                )
                return

            if route == "decompose_further":
                if self._should_split_existing_proof(lemma=lemma, cfg=cfg, error_class=error_class):
                    route = "split_existing_proof"
                else:
                    lemma.lean_identical_fatal_count += 1
                    self._save_lemma_transition(
                        lemma,
                        proof_status=ProofStatus.FAILED.value,
                        routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
                        next_action="decompose_further",
                        reason="lean fatal result routed back to decomposition",
                        clear_solver_series=True,
                    )
                    if lemma.lean_identical_fatal_count >= cfg.routing.max_identical_fatal_class_repeats:
                        self._mark_failed(
                            problem,
                            FailureReason.LEAN_DIFFICULTY.value,
                            terminal_lemma_id=lemma.lemma_id,
                            terminal_error_class=result_payload.get("error_class"),
                            terminal_error_message=result_payload.get("error_message"),
                        )
                    return

            if route == "split_existing_proof":
                if self._ensure_split_existing_proof_job(problem=problem, root=self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None, lemma=lemma):
                    return
                self._save_lemma_transition(
                    lemma,
                    routing_status=RoutingStatus.SPLIT_EXISTING_PROOF.value,
                    next_action="split_existing_proof",
                    reason="Lean formalization stalled; processing proof-preserving split",
                    ensure_solver_series=True,
                )
            return

        if job.mode == LeanJobMode.CHECK_STATEMENT_PLAUSIBILITY.value:
            lemma = self.lemmas.get(job.target_id)
            if not lemma:
                return
            verdict = result_payload.get("verdict")
            if verdict == "suspected_false":
                self._invalidate_parent_decomposition(
                    problem,
                    lemma,
                    cfg,
                    reason="plausibility check suspected false",
                )
            else:
                self._save_lemma_transition(
                    lemma,
                    proof_status=ProofStatus.PROOF_FLAWED.value,
                    routing_status=RoutingStatus.RETRY_SOLVER.value,
                    next_action="retry_solver",
                    reason="plausibility check cleared lemma for another solver attempt",
                    ensure_solver_series=True,
                )
            return

        if job.mode == LeanJobMode.ASSEMBLE_ROOT.value:
            root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
            if job.status == "success" and result_payload.get("compiler_ok"):
                old_problem_status = problem.status
                problem.status = ProblemStatus.SUCCEEDED.value
                problem.verification_level = VerificationLevel.FORMAL.value
                if root:
                    root.status = NodeStatus.SUCCEEDED.value
                    root.final_decl_name = result_payload.get("decl_name")
                    self.theorems.save(root)
                self.event_logger.transition(
                    problem.problem_id,
                    "problem.succeeded",
                    old_problem_status,
                    ProblemStatus.SUCCEEDED.value,
                    target_node_id=root.theorem_id if root else None,
                    worker_job_id=job.job_id,
                    reason="assemble_root compiler_ok",
                )
                self.metrics.emit("problem_terminal", 1, {"status": "succeeded", "verification_level": "formal"})
            else:
                self._mark_failed(
                    problem,
                    FailureReason.ASSEMBLY_COMPOSITION_FAILURE.value,
                    terminal_error_class=result_payload.get("error_class"),
                    terminal_error_message=result_payload.get("error_message"),
                )

    def _run_final_checks_on_solved_decompositions(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        """Submit Agent 6 final checks for decompositions where all lemmas are solved."""
        changed = False
        workset = self._root_parallel_workset(problem, cfg)
        if problem.active_decomposition_id:
            active = self.decompositions.get(problem.active_decomposition_id)
            if active and all(d.decomposition_id != active.decomposition_id for d in workset):
                workset.insert(0, active)

        nl_only = cfg.mode.nl_only_mode
        target_proof_status = ProofStatus.NL_ACCEPTED.value if nl_only else ProofStatus.PROOF_FORMALIZED.value

        for dec in workset:
            if dec.final_check_passed:
                continue
            if dec.controller_status == ControllerStatus.FAILED.value:
                continue

            # Check if all lemmas are solved
            lemma_rows = [self.lemmas.get(lid) for lid in dec.lemma_ids]
            lemma_rows = [lem for lem in lemma_rows if lem is not None]
            if not lemma_rows or not all(lem.proof_status == target_proof_status for lem in lemma_rows):
                continue

            graph_summary: dict[str, Any] = {}
            dependency_checks_summary: list[dict[str, Any]] = []
            graph_status = "legacy_unknown"
            if dec.proof_graph_id:
                graph_status, graph_violations, graph_summary = self.proof_graphs.final_graph_integrity(
                    dec.proof_graph_id,
                    artifact_id=f"{dec.decomposition_id}:pre_agent6",
                )
                dependency_checks_summary = [
                    row.model_dump(mode="json")
                    for row in self.proof_graphs.checks.list_by_graph(dec.proof_graph_id)[-10:]
                ]
                if graph_status != "passed":
                    dec.final_check_passed = False
                    dec.controller_status = ControllerStatus.FAILED.value
                    self.decompositions.save(dec)
                    self.event_logger.transition(
                        problem.problem_id,
                        "final_check.graph_integrity_failed",
                        None,
                        "failed",
                        target_node_id=dec.decomposition_id,
                        reason=json.dumps(graph_violations, sort_keys=True)[:500],
                    )
                    self._mark_failed(
                        problem,
                        FailureReason.ASSEMBLY_COMPOSITION_FAILURE.value,
                        terminal_error_class="proof_graph_integrity_failed",
                        terminal_error_message=json.dumps(graph_violations, sort_keys=True)[:1000],
                    )
                    changed = True
                    continue

            # Check if a final_check job already exists
            if dec.final_check_job_id:
                # Harvest completed final check
                worker_row = self.worker_jobs.get(dec.final_check_job_id)
                if worker_row is None or worker_row.status == "superseded" or worker_row.superseded_at is not None:
                    dec.final_check_job_id = None
                    self.decompositions.save(dec)
                    changed = True
                    continue
                if worker_row.status not in {"completed", "failed"}:
                    continue  # Still running

                if worker_row.status == "failed":
                    failed_job_id = dec.final_check_job_id
                    if graph_status == "passed":
                        dec.final_check_passed = True
                        self.decompositions.save(dec)
                        self.event_logger.transition(
                            problem.problem_id, "final_check.infrastructure_error", None, "approved",
                            target_node_id=dec.decomposition_id,
                            worker_job_id=failed_job_id,
                            reason="Agent 6 failed; structural graph integrity passed",
                        )
                    else:
                        dec.final_check_job_id = None
                        self.decompositions.save(dec)
                        self.event_logger.transition(
                            problem.problem_id, "final_check.infrastructure_error", None, "retry",
                            target_node_id=dec.decomposition_id,
                            worker_job_id=failed_job_id,
                            reason="Agent 6 failed and no structural pass is available",
                        )
                    changed = True
                    continue

                result_payload = worker_row.result_payload or {}
                try:
                    output = Agent6Output.model_validate(result_payload)
                except Exception:
                    dec.final_check_job_id = None
                    self.decompositions.save(dec)
                    changed = True
                    continue

                if dec.proof_graph_id:
                    graph_status, graph_violations, graph_summary = self.proof_graphs.final_graph_integrity(
                        dec.proof_graph_id,
                        artifact_id=f"{dec.decomposition_id}:post_agent6",
                    )
                    if graph_status != "passed":
                        dec.final_check_passed = False
                        dec.controller_status = ControllerStatus.FAILED.value
                        self.decompositions.save(dec)
                        self.event_logger.transition(
                            problem.problem_id,
                            "final_check.graph_integrity_failed",
                            None,
                            "failed",
                            target_node_id=dec.decomposition_id,
                            worker_job_id=dec.final_check_job_id,
                            reason=json.dumps(graph_violations, sort_keys=True)[:500],
                        )
                        self._mark_failed(
                            problem,
                            FailureReason.ASSEMBLY_COMPOSITION_FAILURE.value,
                            terminal_error_class="proof_graph_integrity_failed",
                            terminal_error_message=json.dumps(graph_violations, sort_keys=True)[:1000],
                        )
                        changed = True
                        continue

                findings = self._normalize_agent6_findings(problem, dec, output)
                fatal_findings = [finding for finding in findings if finding.get("severity") == "fatal"]
                fatal_findings_are_localized = bool(fatal_findings) and all(
                    finding.get("target_scope") == "lemma"
                    and self.lemmas.get(str(finding.get("target_id") or "")) is not None
                    for finding in fatal_findings
                )

                if output.verdict == "approved":
                    dec.final_check_passed = True
                    self.decompositions.save(dec)
                    self.event_logger.transition(
                        problem.problem_id, "final_check.approved", None, "approved",
                        target_node_id=dec.decomposition_id,
                        worker_job_id=dec.final_check_job_id,
                        reason=output.summary[:200],
                    )
                    changed = True

                elif (
                    not cfg.final_check.fail_problem_on_fatal
                    and (output.verdict in {"localized_lemma_repair", "lemma_issues"} or fatal_findings_are_localized)
                    and self._localize_final_check_repairs(
                        problem,
                        dec,
                        findings,
                        worker_job_id=dec.final_check_job_id,
                    )
                ):
                    changed = True
                elif output.verdict == "lemma_issues" and self._localize_final_check_repairs(
                    problem,
                    dec,
                    findings,
                    worker_job_id=dec.final_check_job_id,
                ):
                    changed = True
                else:
                    dec.final_check_passed = False
                    self.decompositions.save(dec)
                    self.event_logger.transition(
                        problem.problem_id,
                        "final_check.failed_problem",
                        None,
                        "failed",
                        target_node_id=dec.decomposition_id,
                        worker_job_id=dec.final_check_job_id,
                        reason=f"{output.verdict}: {output.summary[:200]}",
                    )
                    self._mark_failed(
                        problem,
                        FailureReason.ASSEMBLY_COMPOSITION_FAILURE.value,
                        terminal_error_class=output.verdict,
                        terminal_error_message=output.summary,
                    )
                    changed = True
                continue

            # No final_check job yet — submit one
            proof_bundle = self.proof_bundles.build_root_bundle(
                problem.problem_id,
                decomposition_id=dec.decomposition_id,
                final=False,
                legacy=False,
            )
            payload = Agent6Input(
                problem_id=problem.problem_id,
                proof_bundle=proof_bundle or {},
                proof_graph_summary=graph_summary,
                dependency_checks_summary=dependency_checks_summary,
                track_identity={
                    "proof_graph_id": dec.proof_graph_id,
                    "decomposition_id": dec.decomposition_id,
                    "node_id": dec.node_id,
                },
            )
            continuation_generation = self._continuation_generation(problem)
            existing_final_checks = [
                row
                for row in self.worker_jobs.list_by_problem(problem.problem_id, worker_kind="final_check")
                if row.target_id == dec.decomposition_id
                and row.superseded_at is None
                and int(row.continuation_generation or 0) == continuation_generation
            ]
            attempt_number = max((int(row.attempt_number or 0) for row in existing_final_checks), default=0) + 1
            job_id = f"wrk_{dec.decomposition_id}_g{continuation_generation}_final_check_{attempt_number}"
            self.worker_jobs.enqueue_if_absent(
                WorkerJobORM(
                    worker_job_id=job_id,
                    problem_id=problem.problem_id,
                    worker_kind="final_check",
                    status="queued",
                    target_id=dec.decomposition_id,
                    target_kind="decomposition",
                    execution_id=self.execution_id,
                    continuation_generation=continuation_generation,
                    attempt_number=attempt_number,
                    artifact_prefix=f"problems/{problem.problem_id}/final_check/{dec.decomposition_id}",
                    handler_key="agent6_final_check",
                    request_source="final_check",
                    payload=payload.model_dump(),
                )
            )
            dec.final_check_job_id = job_id
            self.decompositions.save(dec)
            self.event_logger.transition(
                problem.problem_id, "final_check.submitted", None, "queued",
                target_node_id=dec.decomposition_id,
                worker_job_id=job_id,
                reason="Agent 6 final check submitted",
            )
            changed = True

        return changed

    def _finalize_if_complete(self, problem: ProblemORM, root: Any, cfg: ProblemConfig) -> bool:
        completion_scope = self._root_parallel_workset(problem, cfg)
        if problem.active_decomposition_id:
            active = self.decompositions.get(problem.active_decomposition_id)
            if active and all(row.decomposition_id != active.decomposition_id for row in completion_scope):
                completion_scope.insert(0, active)
        if not completion_scope:
            return False

        required_solutions = max(
            1,
            min(
                cfg.decomposition.root_solutions_required_for_termination,
                self._root_parallel_take_k(cfg),
            ),
        )

        if cfg.mode.nl_only_mode:
            solved: list[DecompositionORM] = []
            for dec in completion_scope:
                if not dec.final_check_passed:
                    continue
                lemma_rows = [self.lemmas.get(lemma_id) for lemma_id in dec.lemma_ids]
                lemma_rows = [lemma for lemma in lemma_rows if lemma is not None]
                if all(lemma.proof_status == ProofStatus.NL_ACCEPTED.value for lemma in lemma_rows):
                    solved.append(dec)

            if len(solved) < required_solutions:
                return False

            winner = solved[0]
            winner.controller_status = ControllerStatus.SUCCEEDED.value
            self.decompositions.save(winner)
            problem.active_decomposition_id = winner.decomposition_id
            old_problem_status = problem.status
            problem.status = ProblemStatus.SUCCEEDED.value
            problem.verification_level = VerificationLevel.NL_ONLY.value
            root.status = NodeStatus.SUCCEEDED.value
            root.active_decomposition_id = winner.decomposition_id
            self.theorems.save(root)
            self.problems.save(problem)
            self.proof_bundles.build_root_bundle(
                problem.problem_id,
                decomposition_id=winner.decomposition_id,
                final=True,
                legacy=False,
            )
            refreshed_problem = self.problems.get(problem.problem_id)
            if refreshed_problem is not None:
                problem.final_proof_artifact_id = refreshed_problem.final_proof_artifact_id
                problem.running_final_proof_artifact_id = refreshed_problem.running_final_proof_artifact_id
            solved_ids = [row.decomposition_id for row in solved[:required_solutions]]
            self.event_logger.transition(
                problem.problem_id,
                "problem.succeeded",
                old_problem_status,
                ProblemStatus.SUCCEEDED.value,
                reason=(
                    f"nl solutions reached {len(solved_ids)}/{required_solutions}; "
                    f"decompositions={','.join(solved_ids)}"
                ),
            )
            self.metrics.emit("problem_terminal", 1, {"status": "succeeded", "verification_level": "nl_only"})
            return True

        solved = []
        for dec in completion_scope:
            if not dec.final_check_passed:
                continue
            lemma_rows = [self.lemmas.get(lemma_id) for lemma_id in dec.lemma_ids]
            lemma_rows = [lemma for lemma in lemma_rows if lemma is not None]
            if all(lemma.proof_status == ProofStatus.PROOF_FORMALIZED.value for lemma in lemma_rows):
                solved.append(dec)

        if len(solved) < required_solutions:
            return False

        winner = solved[0]
        problem.active_decomposition_id = winner.decomposition_id
        root.active_decomposition_id = winner.decomposition_id
        self.theorems.save(root)
        self.problems.save(problem)
        self.proof_bundles.build_root_bundle(
            problem.problem_id,
            decomposition_id=winner.decomposition_id,
            final=True,
            legacy=False,
        )
        refreshed_problem = self.problems.get(problem.problem_id)
        if refreshed_problem is not None:
            problem.final_proof_artifact_id = refreshed_problem.final_proof_artifact_id
            problem.running_final_proof_artifact_id = refreshed_problem.running_final_proof_artifact_id

        existing = [
            job
            for job in self.lean_jobs.list_by_target(problem.problem_id, root.theorem_id)
            if job.mode == LeanJobMode.ASSEMBLE_ROOT.value
        ]
        if not existing:
            if not winner.lean_v2_track_id or not winner.lean_run_dir:
                return False
            payload = {
                "run_dir": winner.lean_run_dir,
                "track_run_dir": winner.lean_run_dir,
                "track_id": winner.lean_v2_track_id,
                "infer_dependencies": True,
            }
            attempt_index = self._next_attempt_index_for_lean_operation(
                problem_id=problem.problem_id,
                target_id=root.theorem_id,
                operations={"assemble_root_from_track", LeanJobMode.ASSEMBLE_ROOT.value},
            )
            self._submit_lean_job(
                problem,
                target_id=root.theorem_id,
                target_kind="theorem",
                mode=LeanJobMode.ASSEMBLE_ROOT.value,
                operation="assemble_root_from_track",
                payload=payload,
                attempt_index=attempt_index,
                options=self._lean_job_options(
                    cfg,
                    timeout_seconds=cfg.lean_engine.assemble_root_timeout_seconds,
                    extra={"max_root_attempts": cfg.lean_engine.assemble_root_repair_rounds},
                ),
                prefer_operation_endpoint=True,
                fallback_to_job_endpoint=False,
            )
            return True

        return False

    def _mark_failed(
        self,
        problem: ProblemORM,
        reason: str,
        *,
        terminal_lemma_id: str | None = None,
        terminal_error_class: str | None = None,
        terminal_error_message: str | None = None,
    ) -> None:
        if problem.status == ProblemStatus.FAILED.value:
            return

        old = problem.status
        problem.status = ProblemStatus.FAILED.value

        root = self.theorems.get(problem.root_theorem_id) if problem.root_theorem_id else None
        if root:
            root.status = NodeStatus.FAILED.value
            self.theorems.save(root)

        partial_tree = {
            "root": {
                "id": problem.root_theorem_id,
                "kind": NodeKind.THEOREM.value,
                "status": "failed",
                "children": [
                    {
                        "id": lemma.lemma_id,
                        "kind": NodeKind.LEMMA.value,
                        "status": lemma.proof_status,
                        "children": [],
                    }
                    for lemma in self.lemmas.list_by_problem(problem.problem_id)
                ],
            }
        }

        trusted = [row.decl_name for row in self.trusted_context.list_by_problem(problem.problem_id)]
        decomp_attempts = [
            {
                "decomposition_id": dec.decomposition_id,
                "failure_mode": dec.controller_status,
                "failed_lemma_id": terminal_lemma_id,
                "retry_count": 0,
                "last_error_class": terminal_error_class,
            }
            for dec in self.decompositions.list_by_problem(problem.problem_id)
        ]
        routing_summary = [
            {
                "event_id": event.event_id,
                "stage": event.stage,
                "old_status": event.old_status,
                "new_status": event.new_status,
                "reason": event.reason,
            }
            for event in self.events.list_for_problem(problem.problem_id, limit=500)
        ]

        failure = FailureReportORM(
            failure_report_id=new_id("fail"),
            problem_id=problem.problem_id,
            failure_reason=reason,
            terminal_lemma_id=terminal_lemma_id,
            terminal_error_class=terminal_error_class,
            terminal_error_message=terminal_error_message,
            partial_tree=partial_tree,
            trusted_context_at_failure=trusted,
            all_decomposition_attempts=decomp_attempts,
            routing_log_summary=routing_summary,
        )
        self.failure_reports.create_or_replace(failure)

        artifact_id = self.artifacts.save_json(
            f"problems/{problem.problem_id}/failure_report.json",
            {
                "failure_report_id": failure.failure_report_id,
                "problem_id": problem.problem_id,
                "failure_reason": reason,
                "terminal_lemma_id": terminal_lemma_id,
                "terminal_error_class": terminal_error_class,
                "terminal_error_message": terminal_error_message,
                "partial_tree": partial_tree,
                "trusted_context_at_failure": trusted,
                "all_decomposition_attempts": decomp_attempts,
                "routing_log_summary": routing_summary,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        problem.failure_report_artifact_id = artifact_id

        self.event_logger.transition(
            problem.problem_id,
            "problem.failed",
            old,
            ProblemStatus.FAILED.value,
            target_node_id=terminal_lemma_id,
            reason=terminal_error_message or reason,
        )
        self.metrics.emit("problem_failures", 1, {"failure_reason": reason})
        if reason == FailureReason.GLOBAL_TIMEOUT.value:
            self.metrics.emit("global_timeouts", 1, {})
