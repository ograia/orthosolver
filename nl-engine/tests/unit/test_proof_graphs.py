from nl_engine.domain.models import DecompositionORM, LemmaORM, ProblemORM, ProofGraphEdgeORM, ProofGraphNodeORM, ProofGraphORM, TheoremORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import DecompositionRepository, LemmaRepository, ProblemRepository, ProofGraphEdgeRepository, ProofGraphNodeRepository, ProofGraphRepository, TheoremRepository, TrustedContextRepository
from nl_engine.services.ids import new_id
from nl_engine.services.proof_graphs import ProofGraphService


def test_final_graph_integrity_detects_claim_dependency_cycle(tmp_path) -> None:
    store = FileStore(str(tmp_path / "data"))
    graphs = ProofGraphRepository(store)
    nodes = ProofGraphNodeRepository(store)
    edges = ProofGraphEdgeRepository(store)
    service = ProofGraphService(store)

    graph = graphs.create(
        ProofGraphORM(
            proof_graph_id="pgraph_test",
            problem_id="prob_test",
            root_theorem_id="thm_root",
            root_decomposition_id="dec_root",
            graph_status="active",
            verification_status="retry_enforced",
        )
    )
    left = nodes.create(
        ProofGraphNodeORM(
            graph_node_id="claim_left",
            proof_graph_id=graph.proof_graph_id,
            node_kind="claim",
            owner_kind="lemma",
            owner_id="lem_left",
            statement_nl="Left claim",
            semantic_sketch_json={"normalized_claim": "left"},
            normalized_claim_hash="left",
            node_status="active",
        )
    )
    right = nodes.create(
        ProofGraphNodeORM(
            graph_node_id="claim_right",
            proof_graph_id=graph.proof_graph_id,
            node_kind="claim",
            owner_kind="lemma",
            owner_id="lem_right",
            statement_nl="Right claim",
            semantic_sketch_json={"normalized_claim": "right"},
            normalized_claim_hash="right",
            node_status="active",
        )
    )
    edges.create(
        ProofGraphEdgeORM(
            edge_id=new_id("edge"),
            proof_graph_id=graph.proof_graph_id,
            from_node_id=left.graph_node_id,
            to_node_id=right.graph_node_id,
            edge_kind="proof_depends_on_claim",
            dependency_scope="allowed",
        )
    )
    edges.create(
        ProofGraphEdgeORM(
            edge_id=new_id("edge"),
            proof_graph_id=graph.proof_graph_id,
            from_node_id=right.graph_node_id,
            to_node_id=left.graph_node_id,
            edge_kind="proof_depends_on_claim",
            dependency_scope="allowed",
        )
    )

    status, violations, summary = service.final_graph_integrity(graph.proof_graph_id, artifact_id="cycle_test")
    assert status == "fatal_violation"
    assert summary["cycle_found"] is True
    assert any(item.get("type") == "claim_dependency_cycle" for item in violations)


def test_validate_solver_output_rejects_forbidden_claim_citation(tmp_path) -> None:
    store = FileStore(str(tmp_path / "data"))
    graphs = ProofGraphRepository(store)
    service = ProofGraphService(store)
    graphs.create(
        ProofGraphORM(
            proof_graph_id="pgraph_test",
            problem_id="prob_test",
            root_theorem_id="thm_root",
            root_decomposition_id="dec_root",
            graph_status="active",
            verification_status="retry_enforced",
        )
    )
    problem = ProblemORM(problem_id="prob_test", root_theorem_id="thm_root", active_proof_graph_id="pgraph_test")
    lemma = type(
        "LemmaStub",
        (),
        {"lemma_id": "lem_test", "proof_graph_id": "pgraph_test", "claim_node_id": "claim_lemma"},
    )()
    citation = {
        "citation_kind": "claim",
        "item_id": "claim_parent",
        "label": "lem_parent",
        "detail": "used directly",
    }

    status, violations = service.validate_solver_output(
        problem=problem,
        lemma=lemma,
        proof_attempt_node_id=None,
        allowed_manifest=[],
        forbidden_claims=[{"item_id": "claim_parent", "label": "lem_parent", "statement_nl": "Parent claim"}],
        output_proof_nl="Use lem_parent directly.",
        citations=[citation],  # type: ignore[list-item]
        used_forbidden_claim=False,
        artifact_id="solver_test",
    )
    assert status == "retryable_violation"
    assert any(item.get("type") == "forbidden_citation" for item in violations)


def test_build_dependency_manifest_uses_graph_node_ids(tmp_path) -> None:
    store = FileStore(str(tmp_path / "data"))
    service = ProofGraphService(store)
    problems = ProblemRepository(store)
    theorems = TheoremRepository(store)
    decomps = DecompositionRepository(store)
    lemmas = LemmaRepository(store)
    trusted = TrustedContextRepository(store)
    nodes = ProofGraphNodeRepository(store)

    problem = problems.create(
        ProblemORM(
            problem_id="prob_graph_manifest",
            root_theorem_id="thm_root_manifest",
            active_proof_graph_id=None,
        )
    )
    root = theorems.create(
        TheoremORM(
            theorem_id="thm_root_manifest",
            problem_id=problem.problem_id,
            statement_nl="For all n, n = n",
            statement_semantic_sketch={"normalized_claim": "forall n, n = n"},
        )
    )
    decomp = DecompositionORM(
        decomposition_id="dec_root_manifest",
        problem_id=problem.problem_id,
        node_id=root.theorem_id,
        node_kind="theorem",
        shared_context=[{"kind": "definition", "label": "Eq", "content": "x = y means ..."}],
        llm_vetting_status="accepted",
    )
    graph = service.ensure_graph_for_decomposition(problem, decomp)
    assert graph is not None
    decomp.proof_graph_id = graph.proof_graph_id
    decomps.create(decomp)

    lemma = lemmas.create(
        LemmaORM(
            lemma_id="lem_manifest",
            problem_id=problem.problem_id,
            parent_id=decomp.decomposition_id,
            parent_kind="decomposition",
            statement_nl="For all n, n = n",
            statement_semantic_sketch={"normalized_claim": "forall n, n = n"},
            proof_graph_id=graph.proof_graph_id,
        )
    )
    trusted.create_if_absent(
        problem.problem_id,
        decl_name="eq_refl",
        lean_code="theorem eq_refl : x = x := rfl",
        source_lemma_id="initial",
        source_job_id="initial",
        context_scope="problem_external",
    )

    allowed, _, _, _ = service.build_dependency_manifest(problem=problem, lemma=lemma, root=root)
    graph_nodes = {row.graph_node_id for row in nodes.list_by_graph(graph.proof_graph_id)}

    assert allowed
    assert all(item.item_id in graph_nodes for item in allowed)
    assert all(not item.item_id.startswith("def_") for item in allowed)
    assert all(not item.item_id.startswith("trusted_") for item in allowed)


def test_final_graph_integrity_projects_proof_attempt_dependencies_to_claim_cycles(tmp_path) -> None:
    store = FileStore(str(tmp_path / "data"))
    graphs = ProofGraphRepository(store)
    nodes = ProofGraphNodeRepository(store)
    edges = ProofGraphEdgeRepository(store)
    service = ProofGraphService(store)

    graph = graphs.create(
        ProofGraphORM(
            proof_graph_id="pgraph_projected_cycle",
            problem_id="prob_projected_cycle",
            root_theorem_id="thm_root",
            root_decomposition_id="dec_root",
            graph_status="active",
            verification_status="retry_enforced",
        )
    )
    claim_a = nodes.create(
        ProofGraphNodeORM(
            graph_node_id="claim_a",
            proof_graph_id=graph.proof_graph_id,
            node_kind="claim",
            owner_kind="lemma",
            owner_id="lem_a",
            statement_nl="A",
            semantic_sketch_json={"normalized_claim": "A"},
            normalized_claim_hash="a",
            node_status="active",
        )
    )
    claim_b = nodes.create(
        ProofGraphNodeORM(
            graph_node_id="claim_b",
            proof_graph_id=graph.proof_graph_id,
            node_kind="claim",
            owner_kind="lemma",
            owner_id="lem_b",
            statement_nl="B",
            semantic_sketch_json={"normalized_claim": "B"},
            normalized_claim_hash="b",
            node_status="active",
        )
    )
    attempt_a = nodes.create(
        ProofGraphNodeORM(
            graph_node_id="attempt_a",
            proof_graph_id=graph.proof_graph_id,
            node_kind="proof_attempt",
            owner_kind="lemma",
            owner_id="lem_a",
            statement_nl="attempt A",
            semantic_sketch_json={},
            normalized_claim_hash=None,
            node_status="completed",
        )
    )
    attempt_b = nodes.create(
        ProofGraphNodeORM(
            graph_node_id="attempt_b",
            proof_graph_id=graph.proof_graph_id,
            node_kind="proof_attempt",
            owner_kind="lemma",
            owner_id="lem_b",
            statement_nl="attempt B",
            semantic_sketch_json={},
            normalized_claim_hash=None,
            node_status="completed",
        )
    )
    edges.create(
        ProofGraphEdgeORM(
            edge_id=new_id("edge"),
            proof_graph_id=graph.proof_graph_id,
            from_node_id=attempt_a.graph_node_id,
            to_node_id=claim_b.graph_node_id,
            edge_kind="proof_depends_on_claim",
            dependency_scope="allowed",
        )
    )
    edges.create(
        ProofGraphEdgeORM(
            edge_id=new_id("edge"),
            proof_graph_id=graph.proof_graph_id,
            from_node_id=attempt_b.graph_node_id,
            to_node_id=claim_a.graph_node_id,
            edge_kind="proof_depends_on_claim",
            dependency_scope="allowed",
        )
    )

    status, violations, summary = service.final_graph_integrity(graph.proof_graph_id, artifact_id="projected_cycle")
    assert status == "fatal_violation"
    assert summary["cycle_found"] is True
    assert any(item.get("type") == "claim_dependency_cycle" for item in violations)
