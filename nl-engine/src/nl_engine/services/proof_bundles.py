from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nl_engine.artifacts.store import ArtifactStore
from nl_engine.domain.enums import NodeKind, ProofStatus
from nl_engine.domain.models import DecompositionORM, LemmaORM, ProblemORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
    AssemblyPlanRepository,
    DecompositionRepository,
    LemmaRepository,
    ProblemRepository,
    TheoremRepository,
    VetterReportRepository,
)


class ProofBundleService:
    def __init__(self, store: FileStore, artifacts: ArtifactStore | None = None) -> None:
        self.store = store
        self.artifacts = artifacts or ArtifactStore()
        self.problems = ProblemRepository(store)
        self.theorems = TheoremRepository(store)
        self.decompositions = DecompositionRepository(store)
        self.assembly_plans = AssemblyPlanRepository(store)
        self.lemmas = LemmaRepository(store)
        self.vetter_reports = VetterReportRepository(store)

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _root_running_key(problem_id: str) -> str:
        return f"problems/{problem_id}/proof_bundles/root/running.json"

    @staticmethod
    def _root_final_key(problem_id: str) -> str:
        return f"problems/{problem_id}/proof_bundles/root/final.json"

    @staticmethod
    def _root_legacy_key(problem_id: str) -> str:
        return f"problems/{problem_id}/proof_bundles/root/legacy_reconstructed.json"

    @staticmethod
    def _lemma_key(problem_id: str, lemma_id: str) -> str:
        return f"problems/{problem_id}/proof_bundles/lemmas/{lemma_id}.json"

    @staticmethod
    def _decomposition_key(problem_id: str, decomposition_id: str) -> str:
        return f"problems/{problem_id}/proof_bundles/decompositions/{decomposition_id}.json"

    @staticmethod
    def _proof_status_supports_direct_bundle(proof_status: str) -> bool:
        return proof_status in {
            ProofStatus.PROOF_FOUND.value,
            ProofStatus.PROOF_VETTED.value,
            ProofStatus.PROOF_FORMALIZED.value,
            ProofStatus.NL_ACCEPTED.value,
        }

    @staticmethod
    def _lemma_status_snapshot(lemma: LemmaORM) -> dict[str, Any]:
        return {
            "proof_status": lemma.proof_status,
            "routing_status": lemma.routing_status,
            "statement_status": lemma.statement_status,
            "truth_status": lemma.truth_status,
            "counterexample_status": lemma.counterexample_status,
            "active_counterexample_id": lemma.active_counterexample_id,
            "solver_attempt_count": lemma.solver_attempt_count,
            "consecutive_fatal_rejections": lemma.consecutive_fatal_rejections,
            "minor_rejection_count": lemma.minor_rejection_count,
            "decomposition_count": lemma.decomposition_count,
            "decomposition_round_count": lemma.decomposition_round_count,
            "lean_attempt_count": lemma.lean_attempt_count,
            "lean_identical_fatal_count": lemma.lean_identical_fatal_count,
            "consecutive_infrastructure_failures": lemma.consecutive_infrastructure_failures,
            "next_action": lemma.next_action,
            "last_terminal_worker_result": lemma.last_terminal_worker_result,
            "last_transition_reason": lemma.last_transition_reason,
            "latest_vetter_report_id": lemma.latest_vetter_report_id,
            "latest_lean_result_id": lemma.latest_lean_result_id,
            "proof_bundle_artifact_id": lemma.proof_bundle_artifact_id,
            "updated_at": lemma.updated_at.isoformat(),
            "last_activity_at": lemma.last_activity_at.isoformat(),
        }

    @staticmethod
    def _decomposition_status_snapshot(dec: DecompositionORM) -> dict[str, Any]:
        return {
            "logical_decomposition_id": dec.logical_decomposition_id,
            "revision_number": dec.revision_number,
            "llm_vetting_status": dec.llm_vetting_status,
            "lean_assembly_status": dec.lean_assembly_status,
            "controller_status": dec.controller_status,
            "final_check_passed": dec.final_check_passed,
            "final_check_job_id": dec.final_check_job_id,
            "invalidated_by_lemma_id": dec.invalidated_by_lemma_id,
            "invalidated_by_counterexample_id": dec.invalidated_by_counterexample_id,
            "proof_bundle_artifact_id": dec.proof_bundle_artifact_id,
            "updated_at": dec.updated_at.isoformat(),
        }

    @staticmethod
    def _problem_status_snapshot(problem: ProblemORM) -> dict[str, Any]:
        return {
            "status": problem.status,
            "verification_level": problem.verification_level,
            "active_decomposition_id": problem.active_decomposition_id,
            "standby_decomposition_id": problem.standby_decomposition_id,
            "running_final_proof_artifact_id": problem.running_final_proof_artifact_id,
            "final_proof_artifact_id": problem.final_proof_artifact_id,
            "updated_at": problem.updated_at.isoformat(),
        }

    @staticmethod
    def _plan_payload(plan: Any) -> dict[str, Any]:
        if plan is None:
            return {
                "assembly_plan_id": None,
                "steps": [],
                "proof_skeleton_nl": "",
                "is_trivially_composable": True,
            }
        return {
            "assembly_plan_id": plan.assembly_plan_id,
            "steps": list(plan.steps or []),
            "proof_skeleton_nl": str(plan.proof_skeleton_nl or ""),
            "is_trivially_composable": bool(plan.is_trivially_composable),
        }

    @staticmethod
    def _child_summary(child_bundles: list[dict[str, Any]]) -> str:
        if not child_bundles:
            return "No child lemmas were required."
        lines = []
        for idx, child in enumerate(child_bundles, start=1):
            proof_text = str(child.get("proof_nl") or "").strip()
            statement = str(child.get("statement_nl") or "")
            child_id = str(child.get("node_id") or f"child_{idx}")
            if proof_text:
                lines.append(f"{idx}. {child_id}: {statement}\n{proof_text}")
            else:
                lines.append(f"{idx}. {child_id}: {statement}\nProof text not yet materialized.")
        return "\n\n".join(lines)

    def _synthesize_assembled_proof(
        self,
        *,
        statement_nl: str,
        strategy_summary: str,
        plan_payload: dict[str, Any],
        child_bundles: list[dict[str, Any]],
        legacy: bool,
    ) -> tuple[str, bool]:
        proof_skeleton = str(plan_payload.get("proof_skeleton_nl") or "").strip()
        steps = plan_payload.get("steps") or []
        lines = [f"Target: {statement_nl}"]
        if strategy_summary:
            lines.append(f"Strategy summary: {strategy_summary}")
        if proof_skeleton:
            lines.append(f"Assembly skeleton: {proof_skeleton}")
        if steps:
            lines.append("Assembly steps:")
            for step in steps:
                lines.append(
                    f"- {step.get('step_id', 'step')}: derive {step.get('derives', '')} "
                    f"using lemmas {step.get('uses_lemmas', [])} and prior steps {step.get('uses_prior_steps', [])}."
                )
        lines.append("Recursive lemma proofs:")
        lines.append(self._child_summary(child_bundles))
        if legacy:
            lines.append("This assembled proof text was synthesized during legacy proof-bundle reconstruction.")
        return "\n\n".join(lines), True

    def _target_statement_and_sketch(self, problem_id: str, dec: DecompositionORM) -> tuple[str, dict[str, Any]]:
        if dec.node_kind == NodeKind.THEOREM.value:
            theorem = self.theorems.get_root_for_problem(problem_id)
            if theorem is None:
                return "", {}
            return theorem.statement_nl, dict(theorem.statement_semantic_sketch or {})
        lemma = self.lemmas.get(dec.node_id)
        if lemma is None:
            return "", {}
        return lemma.statement_nl, dict(lemma.statement_semantic_sketch or {})

    def _preferred_child_decomposition(self, problem_id: str, lemma_id: str) -> DecompositionORM | None:
        rows = self.decompositions.list_by_node(problem_id, lemma_id)
        if not rows:
            return None

        def score(row: DecompositionORM) -> tuple[int, int, float]:
            controller = 0
            if row.controller_status == "succeeded":
                controller = 3
            elif row.controller_status == "active":
                controller = 2
            elif row.llm_vetting_status == "accepted":
                controller = 1
            return (controller, 1 if row.final_check_passed else 0, row.created_at.timestamp())

        rows.sort(key=score, reverse=True)
        top = rows[0]
        if top.controller_status == "failed":
            return None
        return top

    def _persist_lemma_bundle(self, lemma: LemmaORM, bundle: dict[str, Any]) -> str:
        artifact_key = self._lemma_key(lemma.problem_id, lemma.lemma_id)
        self.artifacts.save_json(artifact_key, bundle)
        if lemma.proof_bundle_artifact_id != artifact_key:
            lemma.proof_bundle_artifact_id = artifact_key
            self.lemmas.save(lemma)
        return artifact_key

    def _persist_decomposition_bundle(self, dec: DecompositionORM, bundle: dict[str, Any]) -> str:
        artifact_key = self._decomposition_key(dec.problem_id, dec.decomposition_id)
        self.artifacts.save_json(artifact_key, bundle)
        if dec.proof_bundle_artifact_id != artifact_key:
            dec.proof_bundle_artifact_id = artifact_key
            self.decompositions.save(dec)
        return artifact_key

    def build_lemma_bundle(
        self,
        problem_id: str,
        lemma_id: str,
        *,
        legacy: bool = False,
        _stack: set[str] | None = None,
    ) -> dict[str, Any] | None:
        lemma = self.lemmas.get(lemma_id)
        if lemma is None or lemma.problem_id != problem_id:
            return None

        stack = set(_stack or set())
        stack_key = f"lemma:{lemma_id}"
        if stack_key in stack:
            return {
                "kind": "lemma",
                "node_id": lemma.lemma_id,
                "statement_nl": lemma.statement_nl,
                "semantic_sketch": dict(lemma.statement_semantic_sketch or {}),
                "resolution_mode": "legacy_reconstructed" if legacy else "direct_solver",
                "proof_nl": None,
                "status_snapshot": self._lemma_status_snapshot(lemma),
                "decomposition": None,
                "children": [],
                "source_artifacts": [],
                "provenance": {
                    "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                    "proof_nl_synthesized": False,
                    "cycle_detected": True,
                    "generated_at": self._utcnow_iso(),
                },
            }
        stack.add(stack_key)

        proof_text = str(lemma.latest_nl_proof or "").strip()
        if proof_text and self._proof_status_supports_direct_bundle(lemma.proof_status):
            bundle = {
                "kind": "lemma",
                "node_id": lemma.lemma_id,
                "statement_nl": lemma.statement_nl,
                "semantic_sketch": dict(lemma.statement_semantic_sketch or {}),
                "resolution_mode": "direct_solver",
                "proof_nl": proof_text,
                "status_snapshot": self._lemma_status_snapshot(lemma),
                "decomposition": None,
                "children": [],
                "source_artifacts": [
                    artifact
                    for artifact in (
                        lemma.latest_vetter_report_id,
                        lemma.latest_lean_result_id,
                    )
                    if artifact
                ],
                "provenance": {
                    "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                    "proof_nl_synthesized": False,
                    "generated_at": self._utcnow_iso(),
                },
            }
            self._persist_lemma_bundle(lemma, bundle)
            return bundle

        child = self._preferred_child_decomposition(problem_id, lemma.lemma_id)
        if child is not None:
            dec_bundle = self.build_decomposition_bundle(
                problem_id,
                child.decomposition_id,
                legacy=legacy,
                _stack=stack,
            )
            child_bundles = list(dec_bundle.get("children") or []) if dec_bundle else []
            plan_payload = dict((dec_bundle or {}).get("decomposition", {}).get("assembly_plan") or {})
            assembled_proof, synthesized = self._synthesize_assembled_proof(
                statement_nl=lemma.statement_nl,
                strategy_summary=child.strategy_summary,
                plan_payload=plan_payload,
                child_bundles=child_bundles,
                legacy=legacy,
            )
            bundle = {
                "kind": "lemma",
                "node_id": lemma.lemma_id,
                "statement_nl": lemma.statement_nl,
                "semantic_sketch": dict(lemma.statement_semantic_sketch or {}),
                "resolution_mode": "legacy_reconstructed" if legacy else "subdecomposition",
                "proof_nl": assembled_proof,
                "status_snapshot": self._lemma_status_snapshot(lemma),
                "decomposition": {
                    "decomposition_id": child.decomposition_id,
                    "node_id": child.node_id,
                    "node_kind": child.node_kind,
                    "strategy_summary": child.strategy_summary,
                    "shared_context": list(child.shared_context or []),
                    "assembly_plan": plan_payload,
                    "assembled_proof_nl": assembled_proof,
                    "ordered_child_lemma_ids": list(child.lemma_ids or []),
                },
                "children": child_bundles,
                "source_artifacts": [
                    artifact
                    for artifact in (
                        child.proof_bundle_artifact_id,
                        child.final_check_job_id,
                        lemma.latest_vetter_report_id,
                        lemma.latest_lean_result_id,
                    )
                    if artifact
                ],
                "provenance": {
                    "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                    "proof_nl_synthesized": synthesized,
                    "generated_at": self._utcnow_iso(),
                },
            }
            self._persist_lemma_bundle(lemma, bundle)
            return bundle

        bundle = {
            "kind": "lemma",
            "node_id": lemma.lemma_id,
            "statement_nl": lemma.statement_nl,
            "semantic_sketch": dict(lemma.statement_semantic_sketch or {}),
            "resolution_mode": "legacy_reconstructed" if legacy else "direct_solver",
            "proof_nl": proof_text or None,
            "status_snapshot": self._lemma_status_snapshot(lemma),
            "decomposition": None,
            "children": [],
            "source_artifacts": [
                artifact
                for artifact in (
                    lemma.latest_vetter_report_id,
                    lemma.latest_lean_result_id,
                )
                if artifact
            ],
            "provenance": {
                "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                "proof_nl_synthesized": False,
                "generated_at": self._utcnow_iso(),
            },
        }
        self._persist_lemma_bundle(lemma, bundle)
        return bundle

    def build_decomposition_bundle(
        self,
        problem_id: str,
        decomposition_id: str,
        *,
        legacy: bool = False,
        _stack: set[str] | None = None,
    ) -> dict[str, Any] | None:
        dec = self.decompositions.get(decomposition_id)
        if dec is None or dec.problem_id != problem_id:
            return None

        stack = set(_stack or set())
        stack_key = f"decomposition:{decomposition_id}"
        if stack_key in stack:
            statement_nl, semantic_sketch = self._target_statement_and_sketch(problem_id, dec)
            return {
                "kind": "decomposition",
                "node_id": dec.decomposition_id,
                "statement_nl": statement_nl,
                "semantic_sketch": semantic_sketch,
                "resolution_mode": "legacy_reconstructed" if legacy else "subdecomposition",
                "proof_nl": None,
                "status_snapshot": self._decomposition_status_snapshot(dec),
                "decomposition": None,
                "children": [],
                "source_artifacts": [],
                "provenance": {
                    "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                    "proof_nl_synthesized": False,
                    "cycle_detected": True,
                    "generated_at": self._utcnow_iso(),
                },
            }
        stack.add(stack_key)

        statement_nl, semantic_sketch = self._target_statement_and_sketch(problem_id, dec)
        plan = self.assembly_plans.get(dec.assembly_plan_id) if dec.assembly_plan_id else None
        child_bundles: list[dict[str, Any]] = []
        for lemma_id in dec.lemma_ids:
            child_bundle = self.build_lemma_bundle(problem_id, lemma_id, legacy=legacy, _stack=stack)
            if child_bundle is not None:
                child_bundles.append(child_bundle)
        plan_payload = self._plan_payload(plan)
        assembled_proof, synthesized = self._synthesize_assembled_proof(
            statement_nl=statement_nl,
            strategy_summary=dec.strategy_summary,
            plan_payload=plan_payload,
            child_bundles=child_bundles,
            legacy=legacy,
        )
        bundle = {
            "kind": "decomposition",
            "node_id": dec.decomposition_id,
            "statement_nl": statement_nl,
            "semantic_sketch": semantic_sketch,
            "resolution_mode": "legacy_reconstructed" if legacy else (
                "root_assembly" if dec.node_kind == NodeKind.THEOREM.value else "subdecomposition"
            ),
            "proof_nl": assembled_proof,
            "status_snapshot": self._decomposition_status_snapshot(dec),
            "decomposition": {
                "decomposition_id": dec.decomposition_id,
                "node_id": dec.node_id,
                "node_kind": dec.node_kind,
                "strategy_summary": dec.strategy_summary,
                "shared_context": list(dec.shared_context or []),
                "assembly_plan": plan_payload,
                "assembled_proof_nl": assembled_proof,
                "ordered_child_lemma_ids": list(dec.lemma_ids or []),
                "pinned_statement_signatures": dict(dec.pinned_statement_signatures or {}),
                "formalization_cost_estimate": dec.formalization_cost_estimate,
            },
            "children": child_bundles,
            "source_artifacts": [
                artifact
                for artifact in (
                    dec.assembly_plan_id,
                    dec.final_check_job_id,
                )
                if artifact
            ],
            "provenance": {
                "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                "proof_nl_synthesized": synthesized,
                "generated_at": self._utcnow_iso(),
            },
        }
        self._persist_decomposition_bundle(dec, bundle)
        return bundle

    def _select_root_decomposition(self, problem: ProblemORM, preferred_id: str | None = None) -> DecompositionORM | None:
        if preferred_id:
            selected = self.decompositions.get(preferred_id)
            if selected is not None:
                return selected
        if problem.active_decomposition_id:
            selected = self.decompositions.get(problem.active_decomposition_id)
            if selected is not None:
                return selected

        theorem = self.theorems.get_root_for_problem(problem.problem_id)
        if theorem is None:
            return None
        rows = [
            row
            for row in self.decompositions.list_by_node(problem.problem_id, theorem.theorem_id)
            if row.llm_vetting_status == "accepted"
        ]
        if not rows:
            return None

        def score(row: DecompositionORM) -> tuple[int, int, float]:
            return (
                1 if row.final_check_passed else 0,
                1 if row.controller_status in {"active", "succeeded", "standby"} else 0,
                row.created_at.timestamp(),
            )

        rows.sort(key=score, reverse=True)
        return rows[0]

    def build_root_bundle(
        self,
        problem_id: str,
        *,
        decomposition_id: str | None = None,
        final: bool = False,
        legacy: bool = False,
    ) -> dict[str, Any] | None:
        problem = self.problems.get(problem_id)
        theorem = self.theorems.get_root_for_problem(problem_id)
        if problem is None or theorem is None:
            return None
        dec = self._select_root_decomposition(problem, preferred_id=decomposition_id)
        if dec is None:
            return None
        dec_bundle = self.build_decomposition_bundle(problem_id, dec.decomposition_id, legacy=legacy)
        if dec_bundle is None:
            return None

        bundle = {
            "kind": "root",
            "node_id": theorem.theorem_id,
            "statement_nl": theorem.statement_nl,
            "semantic_sketch": dict(theorem.statement_semantic_sketch or {}),
            "resolution_mode": "legacy_reconstructed" if legacy else "root_assembly",
            "proof_nl": dec_bundle.get("proof_nl"),
            "status_snapshot": self._problem_status_snapshot(problem),
            "decomposition": dict(dec_bundle.get("decomposition") or {}),
            "children": list(dec_bundle.get("children") or []),
            "source_artifacts": [
                artifact
                for artifact in (
                    dec.proof_bundle_artifact_id,
                    theorem.artifact_ids[0] if theorem.artifact_ids else None,
                )
                if artifact
            ],
            "provenance": {
                "bundle_source": "legacy_reconstructed" if legacy else "runtime",
                "proof_nl_synthesized": bool(dec_bundle.get("provenance", {}).get("proof_nl_synthesized")),
                "generated_at": self._utcnow_iso(),
            },
        }

        if legacy:
            artifact_key = self._root_legacy_key(problem_id)
        elif final:
            artifact_key = self._root_final_key(problem_id)
        else:
            artifact_key = self._root_running_key(problem_id)
        self.artifacts.save_json(artifact_key, bundle)

        if not legacy:
            if final:
                if problem.final_proof_artifact_id != artifact_key:
                    problem.final_proof_artifact_id = artifact_key
                    self.problems.save(problem)
            else:
                if problem.running_final_proof_artifact_id != artifact_key:
                    problem.running_final_proof_artifact_id = artifact_key
                    self.problems.save(problem)
        return bundle

    def refresh_runtime_bundles(self, problem_id: str, *, root_decomposition_id: str | None = None) -> bool:
        problem = self.problems.get(problem_id)
        theorem = self.theorems.get_root_for_problem(problem_id)
        if problem is None or theorem is None:
            return False

        changed = False
        for lemma in self.lemmas.list_by_problem(problem_id):
            before = lemma.proof_bundle_artifact_id
            self.build_lemma_bundle(problem_id, lemma.lemma_id, legacy=False)
            refreshed = self.lemmas.get(lemma.lemma_id)
            if refreshed is not None and refreshed.proof_bundle_artifact_id != before:
                changed = True

        for dec in self.decompositions.list_by_problem(problem_id):
            before = dec.proof_bundle_artifact_id
            self.build_decomposition_bundle(problem_id, dec.decomposition_id, legacy=False)
            refreshed = self.decompositions.get(dec.decomposition_id)
            if refreshed is not None and refreshed.proof_bundle_artifact_id != before:
                changed = True

        before_running = problem.running_final_proof_artifact_id
        self.build_root_bundle(problem_id, decomposition_id=root_decomposition_id, final=False, legacy=False)
        refreshed_problem = self.problems.get(problem_id)
        if refreshed_problem is not None and refreshed_problem.running_final_proof_artifact_id != before_running:
            changed = True
        return changed

    def reconstruct_legacy_root_bundle(
        self,
        problem_id: str,
        *,
        root_decomposition_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self.build_root_bundle(
            problem_id,
            decomposition_id=root_decomposition_id,
            final=False,
            legacy=True,
        )
