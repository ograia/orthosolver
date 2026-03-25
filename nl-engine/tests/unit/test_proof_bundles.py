from __future__ import annotations

from nl_engine.artifacts.store import ArtifactStore
from nl_engine.domain.models import AssemblyPlanORM, DecompositionORM, LemmaORM, ProblemORM, TheoremORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
    AssemblyPlanRepository,
    DecompositionRepository,
    LemmaRepository,
    ProblemRepository,
    TheoremRepository,
)
from nl_engine.services.proof_bundles import ProofBundleService


def test_recursive_proof_bundle_assembles_parent_from_child_decomposition() -> None:
    store = FileStore()
    artifacts = ArtifactStore()
    problems = ProblemRepository(store)
    theorems = TheoremRepository(store)
    decompositions = DecompositionRepository(store)
    assembly_plans = AssemblyPlanRepository(store)
    lemmas = LemmaRepository(store)

    problem_id = "prob_recursive_bundle"
    root_theorem_id = "thm_recursive_bundle"
    root_decomposition_id = "dec_root_recursive_bundle"
    child_decomposition_id = "dec_child_recursive_bundle"
    parent_lemma_id = "lem_parent_recursive_bundle"
    leaf_lemma_id = "lem_leaf_recursive_bundle"

    problems.create(
        ProblemORM(
            problem_id=problem_id,
            title="recursive bundle",
            root_theorem_id=root_theorem_id,
            active_decomposition_id=root_decomposition_id,
            nl_only_mode=True,
            verification_level="nl_only",
            status="running",
            config={"mode": {"nl_only_mode": True}},
        )
    )
    theorems.create(
        TheoremORM(
            theorem_id=root_theorem_id,
            problem_id=problem_id,
            statement_nl="Root theorem",
            statement_semantic_sketch={"normalized_claim": "Root theorem"},
        )
    )

    assembly_plans.create(
        AssemblyPlanORM(
            assembly_plan_id="ap_root_recursive_bundle",
            problem_id=problem_id,
            decomposition_id=root_decomposition_id,
            root_node_id=root_theorem_id,
            steps=[
                {
                    "step_id": "A1",
                    "uses_lemmas": ["L1"],
                    "uses_prior_steps": [],
                    "derives": "Root theorem",
                    "is_trivial": True,
                    "trivial_justification": "root assembly",
                }
            ],
            proof_skeleton_nl="Apply the parent lemma.",
        )
    )
    assembly_plans.create(
        AssemblyPlanORM(
            assembly_plan_id="ap_child_recursive_bundle",
            problem_id=problem_id,
            decomposition_id=child_decomposition_id,
            root_node_id=parent_lemma_id,
            steps=[
                {
                    "step_id": "B1",
                    "uses_lemmas": ["L1"],
                    "uses_prior_steps": [],
                    "derives": "Parent lemma",
                    "is_trivial": True,
                    "trivial_justification": "child assembly",
                }
            ],
            proof_skeleton_nl="Apply the leaf lemma.",
        )
    )

    decompositions.create(
        DecompositionORM(
            decomposition_id=root_decomposition_id,
            problem_id=problem_id,
            node_id=root_theorem_id,
            node_kind="theorem",
            strategy_summary="root strategy",
            lemma_ids=[parent_lemma_id],
            assembly_plan_id="ap_root_recursive_bundle",
            llm_vetting_status="accepted",
            controller_status="active",
        )
    )
    decompositions.create(
        DecompositionORM(
            decomposition_id=child_decomposition_id,
            problem_id=problem_id,
            node_id=parent_lemma_id,
            node_kind="lemma",
            strategy_summary="child strategy",
            lemma_ids=[leaf_lemma_id],
            assembly_plan_id="ap_child_recursive_bundle",
            llm_vetting_status="accepted",
            controller_status="succeeded",
        )
    )

    lemmas.create(
        LemmaORM(
            lemma_id=parent_lemma_id,
            problem_id=problem_id,
            parent_id=root_theorem_id,
            parent_kind="theorem",
            statement_nl="Parent lemma",
            statement_semantic_sketch={"normalized_claim": "Parent lemma"},
            proof_status="nl_accepted",
            routing_status="done",
        )
    )
    lemmas.create(
        LemmaORM(
            lemma_id=leaf_lemma_id,
            problem_id=problem_id,
            parent_id=parent_lemma_id,
            parent_kind="lemma",
            statement_nl="Leaf lemma",
            statement_semantic_sketch={"normalized_claim": "Leaf lemma"},
            proof_status="nl_accepted",
            routing_status="done",
            latest_nl_proof="Direct proof of the leaf lemma.",
        )
    )

    service = ProofBundleService(store, artifacts)
    bundle = service.build_root_bundle(problem_id, decomposition_id=root_decomposition_id, final=False, legacy=False)

    assert bundle is not None
    assert bundle["kind"] == "root"
    assert bundle["decomposition"]["decomposition_id"] == root_decomposition_id
    assert len(bundle["children"]) == 1

    parent_bundle = bundle["children"][0]
    assert parent_bundle["node_id"] == parent_lemma_id
    assert parent_bundle["resolution_mode"] == "subdecomposition"
    assert "Parent lemma" in parent_bundle["proof_nl"]
    assert len(parent_bundle["children"]) == 1
    assert parent_bundle["children"][0]["node_id"] == leaf_lemma_id
    assert parent_bundle["children"][0]["proof_nl"] == "Direct proof of the leaf lemma."

    refreshed_problem = problems.get(problem_id)
    refreshed_parent = lemmas.get(parent_lemma_id)
    refreshed_root = decompositions.get(root_decomposition_id)
    assert refreshed_problem is not None and refreshed_problem.running_final_proof_artifact_id is not None
    assert refreshed_parent is not None and refreshed_parent.proof_bundle_artifact_id is not None
    assert refreshed_root is not None and refreshed_root.proof_bundle_artifact_id is not None
    assert artifacts.exists(refreshed_problem.running_final_proof_artifact_id)
