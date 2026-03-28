from __future__ import annotations

from nl_engine.controller.orchestrator import Orchestrator
from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.contracts import Agent2AssemblyPlan, Agent2Candidate, Agent2Input, Agent2Lemma, Agent3Output, Agent7Output, Agent8Output, SemanticSketch
from nl_engine.domain.enums import ControllerStatus, ProofStatus, RoutingStatus
from nl_engine.domain.models import DecompositionORM, LeanJobORM, LeanResultORM, LemmaORM, ProblemORM, TheoremORM, WorkerJobORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import DecompositionCandidateRepository, DecompositionRepository, LeanResultRepository, LemmaRepository, ProblemRepository, TheoremRepository, WorkerJobRepository


def _store(tmp_path) -> FileStore:
    return FileStore(str(tmp_path / "data"))


def _problem_and_root(store: FileStore) -> tuple[ProblemORM, TheoremORM]:
    problem = ProblemORM(
        problem_id="prob_unified",
        title="Unified Lean problem",
        status="running",
        input_mode="both",
        root_theorem_id="thm_root",
        lean_image_tag="mock-image",
        config=ProblemConfig().model_dump(mode="json"),
        nl_only_mode=False,
        verification_level="formal",
    )
    theorem = TheoremORM(
        theorem_id="thm_root",
        problem_id=problem.problem_id,
        statement_nl="For all n, n = n",
        statement_semantic_sketch={
            "variables": [],
            "quantifier_order": [],
            "domain_restrictions": [],
            "witness_dependencies": [],
            "normalized_claim": "For all n, n = n",
        },
    )
    ProblemRepository(store).create(problem)
    TheoremRepository(store).create(theorem)
    return problem, theorem


def _candidate() -> Agent2Candidate:
    sketch = SemanticSketch(
        variables=[],
        quantifier_order=[],
        domain_restrictions=[],
        witness_dependencies=[],
        normalized_claim="For all n, n = n",
    )
    return Agent2Candidate(
        candidate_index=0,
        strategy_summary="single direct lemma",
        shared_context=[],
        lemmas=[
            Agent2Lemma(
                local_id="L1",
                statement_nl="For all n, n = n",
                semantic_sketch=sketch,
                role_in_assembly="direct",
                formalization_cost_estimate=0.2,
                self_check_true=True,
                self_check_notes="deterministic",
            )
        ],
        assembly_plan=Agent2AssemblyPlan(
            steps=[
                {
                    "step_id": "A1",
                    "uses_lemmas": ["L1"],
                    "uses_prior_steps": [],
                    "derives": "For all n, n = n",
                    "is_trivial": True,
                }
            ],
            proof_skeleton_nl="Apply L1.",
            final_step_yields_exact_root=True,
        ),
        formalization_cost_estimate_total=0.2,
        drift_self_check={"all_lemmas_consistent_with_root_sketch": True},
    )


def test_materialized_accepted_decomposition_immediately_submits_prepare_track(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    orch = Orchestrator(store, execution_id="exec_prepare")
    orch.proof_graphs.validate_decomposition_context_purity = lambda *_args, **_kwargs: []
    orch.proof_graphs.validate_decomposition_reduction = lambda *_args, **_kwargs: []

    submitted: list[str] = []
    orch._submit_prepare_track_if_v2 = lambda problem_arg, dec, cfg: submitted.append(dec.decomposition_id) or True

    payload = Agent2Input(
        theorem_nl=theorem.statement_nl,
        root_semantic_sketch=theorem.statement_semantic_sketch,
        num_candidates=1,
    )
    generated, accepted = orch._materialize_decomposition_candidates(
        problem=problem,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        request_tag="req_prepare",
        theorem_nl=theorem.statement_nl,
        theorem_semantic_sketch=theorem.statement_semantic_sketch,
        parent_depth=0,
        payload=payload,
        decompose_job_id="wrk_root_gen",
        candidates=[_candidate()],
        max_candidates=1,
        cfg=ProblemConfig(),
    )

    assert generated is True
    assert accepted == 1
    assert len(submitted) == 1


def test_lemma_candidates_stay_lightweight_until_promotion(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    parent = LemmaORM(
        lemma_id="lem_parent",
        problem_id=problem.problem_id,
        parent_id=theorem.theorem_id,
        parent_kind="theorem",
        depth=1,
        statement_nl="Parent lemma",
        statement_semantic_sketch=theorem.statement_semantic_sketch,
        proof_status=ProofStatus.PROOF_FLAWED.value,
        routing_status=RoutingStatus.DECOMPOSE_FURTHER.value,
    )
    LemmaRepository(store).create(parent)

    orch = Orchestrator(store, execution_id="exec_prepare_lemma")
    orch.proof_graphs.validate_decomposition_context_purity = lambda *_args, **_kwargs: []
    orch.proof_graphs.validate_decomposition_reduction = lambda *_args, **_kwargs: []

    submitted: list[str] = []
    orch._submit_prepare_track_if_v2 = lambda problem_arg, dec, cfg: submitted.append(dec.decomposition_id) or True

    cfg = ProblemConfig.model_validate({"decomposition": {"lemma_decomposition_candidates_n": 2}})
    payload = Agent2Input(
        theorem_nl=parent.statement_nl,
        root_semantic_sketch=parent.statement_semantic_sketch,
        num_candidates=2,
    )
    generated, accepted = orch._materialize_decomposition_candidates(
        problem=problem,
        node_id=parent.lemma_id,
        node_kind="lemma",
        request_tag="req_prepare_lemma",
        theorem_nl=parent.statement_nl,
        theorem_semantic_sketch=parent.statement_semantic_sketch,
        parent_depth=parent.depth,
        payload=payload,
        decompose_job_id="wrk_lemma_gen",
        candidates=[_candidate().model_copy(deep=True), _candidate().model_copy(deep=True)],
        max_candidates=2,
        cfg=cfg,
    )

    assert generated is True
    assert accepted == 2
    assert submitted == []
    assert DecompositionRepository(store).list_by_node(problem.problem_id, parent.lemma_id) == []

    candidate_rows = DecompositionCandidateRepository(store).list_by_node(problem.problem_id, parent.lemma_id)
    assert len(candidate_rows) == 2
    assert all(row.llm_vetting_status == "accepted" for row in candidate_rows)
    assert all(row.promoted_decomposition_id is None for row in candidate_rows)

    assert orch._select_active_decomposition_for_node(problem, parent.lemma_id, "lemma", cfg) is True

    promoted_rows = DecompositionCandidateRepository(store).list_by_node(problem.problem_id, parent.lemma_id)
    promoted = [row for row in promoted_rows if row.promoted_decomposition_id]
    assert len(promoted) == 1
    assert len(submitted) == 1
    dec_rows = DecompositionRepository(store).list_by_node(problem.problem_id, parent.lemma_id)
    assert len(dec_rows) == 1


def test_agent3_acceptance_is_not_overridden_by_controller_context_purity(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    orch = Orchestrator(store, execution_id="exec_agent3_authority")
    submitted: list[str] = []
    orch._submit_prepare_track_if_v2 = lambda problem_arg, dec, cfg: submitted.append(dec.decomposition_id) or True
    orch.workers.run_decomposition_vetting = lambda *_args, **_kwargs: Agent3Output(
        status="completed",
        decision="accepted",
        summary="Definitions are clean and the decomposition is acceptable.",
        lemma_findings=[
            {
                "local_id": "L1",
                "statement_status": "plausible",
                "evidence": "Directly acceptable.",
                "counterexample": None,
            }
        ],
        assembly_check={"verdict": "valid", "final_step_matches_root": True, "hidden_steps_found": [], "details": "Valid."},
        coverage_check={"missing_coverage": [], "redundant_lemmas": [], "disguised_difficulty": []},
        drift_assessment={
            "drift_detected": False,
            "drift_severity": "none",
            "assembly_conclusion_matches_root": True,
            "per_lemma_drift": [],
        },
        formalization_risk="medium",
        fixes_required=[],
        fatal_reason=None,
        dependency_risk_assessment={},
        equivalence_findings=[],
        context_purity_findings=[],
        accepted_bottleneck=False,
    )

    candidate = _candidate().model_copy(deep=True)
    candidate.shared_context = [
        {
            "kind": "definition",
            "label": "valley_set",
            "content": "For every j, j is in Val(s) iff s_{j-1} = -1 and s_j = +1.",
        }
    ]
    payload = Agent2Input(
        theorem_nl=theorem.statement_nl,
        root_semantic_sketch=theorem.statement_semantic_sketch,
        num_candidates=1,
    )

    generated, accepted = orch._materialize_decomposition_candidates(
        problem=problem,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        request_tag="req_agent3_authority",
        theorem_nl=theorem.statement_nl,
        theorem_semantic_sketch=theorem.statement_semantic_sketch,
        parent_depth=0,
        payload=payload,
        decompose_job_id="wrk_root_gen_authority",
        candidates=[candidate],
        max_candidates=1,
        cfg=ProblemConfig(),
    )

    assert generated is True
    assert accepted == 1
    rows = DecompositionRepository(store).list_by_node(problem.problem_id, theorem.theorem_id)
    assert len(rows) == 1
    assert rows[0].llm_vetting_status == "accepted"
    assert rows[0].controller_status == ControllerStatus.PENDING.value
    assert rows[0].failure_origin is None
    assert submitted == [rows[0].decomposition_id]


def test_root_semantic_sketch_runs_through_worker_state(tmp_path) -> None:
    store = _store(tmp_path)
    problem = ProblemORM(
        problem_id="prob_sketch",
        title="Sketch problem",
        status="running",
        input_mode="both",
        root_theorem_id="thm_sketch",
        lean_image_tag="mock-image",
        config=ProblemConfig().model_dump(mode="json"),
        nl_only_mode=False,
        verification_level="formal",
    )
    theorem = TheoremORM(
        theorem_id="thm_sketch",
        problem_id=problem.problem_id,
        statement_nl="Every n equals itself",
        statement_semantic_sketch={},
    )
    ProblemRepository(store).create(problem)
    TheoremRepository(store).create(theorem)
    orch = Orchestrator(store, execution_id="exec_sketch")

    assert orch._ensure_root_semantic_sketch_job(problem, theorem) is True
    job_row = WorkerJobRepository(store).get("wrk_thm_sketch_semantic_sketch_0")
    assert job_row is not None
    assert job_row.worker_kind == "root_semantic_sketch"

    job_row.status = "completed"
    job_row.result_payload = {
        "status": "completed",
        "statement_nl_received": theorem.statement_nl,
        "semantic_sketch": {
            "variables": [],
            "quantifier_order": [],
            "domain_restrictions": [],
            "witness_dependencies": [],
            "normalized_claim": theorem.statement_nl,
        },
        "implicit_assumptions_surfaced": [],
        "ambiguities": [],
    }
    WorkerJobRepository(store).save(job_row)

    assert orch._harvest_root_semantic_sketch_job(problem, theorem) is True
    refreshed = TheoremRepository(store).get(theorem.theorem_id)
    assert refreshed is not None
    assert refreshed.statement_semantic_sketch.get("normalized_claim") == theorem.statement_nl


def test_prepare_track_retries_lean_issue_and_fails_proof_issue(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    orch = Orchestrator(store, execution_id="exec_route")
    cfg = ProblemConfig()

    dec = DecompositionORM(
        decomposition_id="dec_prepare",
        problem_id=problem.problem_id,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        llm_vetting_status="accepted",
        controller_status=ControllerStatus.PENDING.value,
        lean_v2_prepare_status="queued",
    )
    orch.decompositions.create(dec)

    retried: list[str] = []
    orch._submit_prepare_track_if_v2 = lambda problem_arg, dec_arg, cfg_arg: retried.append(dec_arg.decomposition_id) or True

    lean_issue_job = LeanJobORM(
        job_id="lean_job_dec_prepare_prepare_track_1",
        problem_id=problem.problem_id,
        target_id=dec.decomposition_id,
        target_kind="assembly",
        mode="prepare_track",
        operation="prepare_track",
        status="fatal",
        attempt_index=1,
    )
    orch._route_terminal_lean_result(
        problem,
        lean_issue_job,
        {
            "issue_kind": "lean_issue",
            "error_class": "type_mismatch",
            "error_message": "retry me",
        },
        cfg,
        LeanResultORM(result_id="res_prepare_1", job_id=lean_issue_job.job_id, status="fatal"),
    )

    refreshed = orch.decompositions.get(dec.decomposition_id)
    assert refreshed is not None
    assert refreshed.lean_v2_prepare_status == "pending"
    assert retried == ["dec_prepare"]

    proof_issue_job = LeanJobORM(
        job_id="lean_job_dec_prepare_prepare_track_2",
        problem_id=problem.problem_id,
        target_id=dec.decomposition_id,
        target_kind="assembly",
        mode="prepare_track",
        operation="prepare_track",
        status="fatal",
        attempt_index=2,
    )
    orch._route_terminal_lean_result(
        problem,
        proof_issue_job,
        {
            "issue_kind": "proof_issue",
            "error_class": "bad_statement_translation",
            "error_message": "root decomposition is flawed",
        },
        cfg,
        LeanResultORM(result_id="res_prepare_2", job_id=proof_issue_job.job_id, status="fatal"),
    )

    refreshed = orch.decompositions.get(dec.decomposition_id)
    assert refreshed is not None
    assert refreshed.controller_status == ControllerStatus.PENDING.value
    assert refreshed.lean_v2_prepare_status == "lean_blocked"
    assert refreshed.failure_origin == "lean_prepare:proof_issue"


def test_poll_one_lean_job_normalizes_string_diagnostics_and_routes_retry(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    orch = Orchestrator(store, execution_id="exec_poll_retry")
    cfg = ProblemConfig()

    dec = DecompositionORM(
        decomposition_id="dec_poll",
        problem_id=problem.problem_id,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        llm_vetting_status="accepted",
        controller_status=ControllerStatus.PENDING.value,
        lean_v2_prepare_status="queued",
    )
    orch.decompositions.create(dec)
    orch.lean_jobs.create_if_absent(
        LeanJobORM(
            job_id="lean_job_dec_poll_prepare_track_1",
            problem_id=problem.problem_id,
            target_id=dec.decomposition_id,
            target_kind="assembly",
            mode="prepare_track",
            operation="prepare_track",
            status="running",
            attempt_index=1,
        )
    )

    class _TerminalLeanClient:
        def get_job(self, job_id: str) -> dict[str, object]:
            return {
                "job_id": job_id,
                "status": "repairable",
                "result": {
                    "issue_kind": "lean_issue",
                    "error_class": "llm_runtime_failure",
                    "error_message": "Claude stalled before first token",
                    "diagnostics": ["first attempt stalled", "second attempt stalled"],
                },
            }

    retried: list[str] = []
    orch.lean = _TerminalLeanClient()
    orch.artifacts.save_json = lambda key, payload: key
    orch._submit_prepare_track_if_v2 = lambda problem_arg, dec_arg, cfg_arg: retried.append(dec_arg.decomposition_id) or True

    changed = orch._poll_one_lean_job(problem, cfg)

    assert changed is True
    refreshed_dec = DecompositionRepository(store).get(dec.decomposition_id)
    assert refreshed_dec is not None
    assert refreshed_dec.lean_v2_prepare_status == "pending"
    assert retried == [dec.decomposition_id]

    result = LeanResultRepository(store).get_for_job("lean_job_dec_poll_prepare_track_1")
    assert result is not None
    assert result.status == "repairable"
    assert result.diagnostics == [
        {"message": "first attempt stalled"},
        {"message": "second attempt stalled"},
    ]


def test_false_lemma_suspected_routes_back_to_nl_retry_solver(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    cfg = ProblemConfig()
    orch = Orchestrator(store, execution_id="exec_false_lemma")

    dec = DecompositionORM(
        decomposition_id="dec_root",
        problem_id=problem.problem_id,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        llm_vetting_status="accepted",
        controller_status=ControllerStatus.ACTIVE.value,
        lemma_ids=["lem_false"],
        lean_v2_prepare_status="success",
        lean_v2_track_id="track_root",
        lean_v2_lemma_handles={"lem_false": "handle_false"},
        lean_run_dir="/tmp/track_root",
    )
    lemma = LemmaORM(
        lemma_id="lem_false",
        problem_id=problem.problem_id,
        parent_id=theorem.theorem_id,
        parent_kind="theorem",
        statement_nl="Some hard lemma",
        statement_semantic_sketch=theorem.statement_semantic_sketch,
        latest_nl_proof="Accepted NL proof.",
        proof_status=ProofStatus.PROOF_VETTED.value,
        routing_status=RoutingStatus.SEND_TO_LEAN.value,
    )
    DecompositionRepository(store).create(dec)
    LemmaRepository(store).create(lemma)

    lean_job = LeanJobORM(
        job_id="lean_job_false",
        problem_id=problem.problem_id,
        target_id=lemma.lemma_id,
        target_kind="lemma",
        mode="formalize_lemma",
        operation="formalize_lemma_from_nl",
        status="fatal",
        attempt_index=1,
    )
    orch._route_terminal_lean_result(
        problem,
        lean_job,
        {
            "issue_kind": "proof_issue",
            "error_class": "false_lemma_suspected",
            "error_message": "countermodel likely",
        },
        cfg,
        LeanResultORM(result_id="lean_res_false", job_id=lean_job.job_id, status="fatal"),
    )

    refreshed = LemmaRepository(store).get(lemma.lemma_id)
    assert refreshed is not None
    assert refreshed.routing_status == RoutingStatus.RETRY_SOLVER.value
    assert refreshed.next_action == "retry_solver"
    split_jobs = WorkerJobRepository(store).list_by_problem(problem.problem_id, worker_kind="proof_split_generation")
    assert split_jobs == []


def test_dependency_graph_invalid_does_not_split_existing_proof(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    cfg = ProblemConfig.model_validate(
        {
            **ProblemConfig().model_dump(),
            "mode": {
                "nl_only_mode": False,
                "lean_mode": True,
                "lean": {
                    "auto_split_sublemmas": True,
                },
            },
        }
    )
    orch = Orchestrator(store, execution_id="exec_dependency_graph_invalid")

    dec = DecompositionORM(
        decomposition_id="dec_root_dependency_graph",
        problem_id=problem.problem_id,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        llm_vetting_status="accepted",
        controller_status=ControllerStatus.ACTIVE.value,
        lemma_ids=["lem_dependency_graph"],
        lean_v2_prepare_status="success",
        lean_v2_track_id="track_dependency_graph",
        lean_v2_lemma_handles={"lem_dependency_graph": "handle_dependency_graph"},
        lean_run_dir="/tmp/track_dependency_graph",
    )
    lemma = LemmaORM(
        lemma_id="lem_dependency_graph",
        problem_id=problem.problem_id,
        parent_id=theorem.theorem_id,
        parent_kind="theorem",
        statement_nl="A lemma with a deterministic setup failure",
        statement_semantic_sketch=theorem.statement_semantic_sketch,
        latest_nl_proof="Accepted NL proof.",
        proof_status=ProofStatus.PROOF_VETTED.value,
        routing_status=RoutingStatus.SEND_TO_LEAN.value,
    )
    DecompositionRepository(store).create(dec)
    LemmaRepository(store).create(lemma)

    lean_job = LeanJobORM(
        job_id="lean_job_dependency_graph",
        problem_id=problem.problem_id,
        target_id=lemma.lemma_id,
        target_kind="lemma",
        mode="formalize_lemma",
        operation="formalize_lemma_from_nl",
        status="fatal",
        attempt_index=1,
    )
    orch._route_terminal_lean_result(
        problem,
        lean_job,
        {
            "issue_kind": "lean_issue",
            "error_class": "dependency_graph_invalid",
            "error_message": "phase04 dependency graph has a cycle",
        },
        cfg,
        LeanResultORM(result_id="lean_res_dependency_graph", job_id=lean_job.job_id, status="fatal"),
    )

    refreshed = LemmaRepository(store).get(lemma.lemma_id)
    assert refreshed is not None
    assert refreshed.routing_status == RoutingStatus.DECOMPOSE_FURTHER.value
    assert refreshed.next_action == "decompose_further"
    split_jobs = WorkerJobRepository(store).list_by_problem(problem.problem_id, worker_kind="proof_split_generation")
    assert split_jobs == []


def test_split_existing_proof_materializes_proof_carrying_child_decomposition(tmp_path) -> None:
    store = _store(tmp_path)
    problem, theorem = _problem_and_root(store)
    cfg = ProblemConfig.model_validate(
        {
            **ProblemConfig().model_dump(),
            "mode": {
                "nl_only_mode": False,
                "lean_mode": True,
                "lean": {
                    "auto_split_sublemmas": True,
                },
            },
        }
    )
    problem.config = cfg.model_dump(mode="json")
    ProblemRepository(store).save(problem)
    orch = Orchestrator(store, execution_id="exec_split")
    orch.proof_graphs.validate_decomposition_context_purity = lambda *_args, **_kwargs: []
    orch.proof_graphs.validate_decomposition_reduction = lambda *_args, **_kwargs: []

    submitted_prepare: list[str] = []
    orch._submit_prepare_track_if_v2 = lambda problem_arg, dec_arg, cfg_arg: submitted_prepare.append(dec_arg.decomposition_id) or True

    active_dec = DecompositionORM(
        decomposition_id="dec_root_split",
        problem_id=problem.problem_id,
        node_id=theorem.theorem_id,
        node_kind="theorem",
        llm_vetting_status="accepted",
        controller_status=ControllerStatus.ACTIVE.value,
        lemma_ids=["lem_parent"],
        lean_v2_prepare_status="success",
        lean_v2_track_id="track_parent",
        lean_v2_lemma_handles={"lem_parent": "handle_parent"},
        lean_run_dir="/tmp/track_parent",
    )
    lemma = LemmaORM(
        lemma_id="lem_parent",
        problem_id=problem.problem_id,
        parent_id=theorem.theorem_id,
        parent_kind="theorem",
        depth=1,
        statement_nl="Parent lemma",
        statement_semantic_sketch=theorem.statement_semantic_sketch,
        latest_nl_proof="We already have a complete NL proof of the parent lemma.",
        proof_status=ProofStatus.PROOF_VETTED.value,
        routing_status=RoutingStatus.SEND_TO_LEAN.value,
        role_in_parent="bottleneck lemma",
    )
    DecompositionRepository(store).create(active_dec)
    LemmaRepository(store).create(lemma)
    problem.active_decomposition_id = active_dec.decomposition_id
    ProblemRepository(store).save(problem)

    lean_job = LeanJobORM(
        job_id="lean_job_split",
        problem_id=problem.problem_id,
        target_id=lemma.lemma_id,
        target_kind="lemma",
        mode="formalize_lemma",
        operation="formalize_lemma_from_nl",
        status="fatal",
        attempt_index=3,
    )
    orch._route_terminal_lean_result(
        problem,
        lean_job,
        {
            "issue_kind": "proof_issue",
            "error_class": "major_proof_gap",
            "error_message": "proof should be split into intermediate claims",
        },
        cfg,
        LeanResultORM(result_id="lean_res_split", job_id=lean_job.job_id, status="fatal"),
    )

    lemma_after_route = LemmaRepository(store).get(lemma.lemma_id)
    assert lemma_after_route is not None
    assert lemma_after_route.next_action == "wait_on_split_existing_proof"

    split_job = WorkerJobRepository(store).list_by_problem(problem.problem_id, worker_kind="proof_split_generation")[-1]
    split_job.status = "completed"
    split_job.result_payload = Agent7Output(
        status="completed",
        decision="split",
        strategy_summary="Split the parent proof into two intermediate lemmas.",
        shared_context=[],
        context_items=[],
        lemmas=[
            Agent2Lemma(
                local_id="S1",
                statement_nl="Child lemma one",
                semantic_sketch=SemanticSketch(
                    variables=[],
                    quantifier_order=[],
                    domain_restrictions=[],
                    witness_dependencies=[],
                    normalized_claim="Child lemma one",
                ),
                role_in_assembly="first child",
                lemma_relation_to_parent="bottleneck",
                strictly_easier_reason="Smaller bridge claim.",
                bottleneck_reason="lean_failure",
                formalization_cost_estimate=0.2,
                self_check_true=True,
                self_check_notes="Looks valid.",
                proof_nl="Proof of child lemma one.",
            ),
            Agent2Lemma(
                local_id="S2",
                statement_nl="Child lemma two",
                semantic_sketch=SemanticSketch(
                    variables=[],
                    quantifier_order=[],
                    domain_restrictions=[],
                    witness_dependencies=[],
                    normalized_claim="Child lemma two",
                ),
                role_in_assembly="second child",
                lemma_relation_to_parent="bottleneck",
                strictly_easier_reason="Second bridge claim.",
                bottleneck_reason="lean_failure",
                formalization_cost_estimate=0.2,
                self_check_true=True,
                self_check_notes="Looks valid.",
                proof_nl="Proof of child lemma two.",
            ),
        ],
        assembly_plan=Agent2AssemblyPlan(
            steps=[
                {
                    "step_id": "combine",
                    "uses_lemmas": ["S1", "S2"],
                    "is_trivial": True,
                }
            ],
            proof_skeleton_nl="Apply S1 and S2.",
            final_step_yields_exact_root=True,
        ),
        parent_reassembly_explanation="The two child lemmas compose into the parent lemma.",
        confidence=0.91,
        summary="The parent proof splits cleanly into two smaller obligations.",
    ).model_dump(mode="json")
    WorkerJobRepository(store).save(split_job)

    assert orch._process_decomposition(problem, theorem, active_dec, cfg) is True
    lemma_waiting_for_vetter = LemmaRepository(store).get(lemma.lemma_id)
    assert lemma_waiting_for_vetter is not None
    assert lemma_waiting_for_vetter.next_action == "wait_on_split_existing_proof_vetter"

    vet_job = WorkerJobRepository(store).list_by_problem(problem.problem_id, worker_kind="proof_split_vetting")[-1]
    vet_job.status = "completed"
    vet_job.result_payload = Agent8Output(
        status="completed",
        decision="approved",
        confidence=0.94,
        summary="Approved as a Lean-failure-driven proof-preserving decomposition.",
        parent_reassembly_valid=True,
        child_findings=[],
        bundle_findings=[],
    ).model_dump(mode="json")
    WorkerJobRepository(store).save(vet_job)

    assert orch._process_decomposition(problem, theorem, active_dec, cfg) is True

    child_decompositions = DecompositionRepository(store).list_by_node(problem.problem_id, lemma.lemma_id)
    accepted_children = [row for row in child_decompositions if row.llm_vetting_status == "accepted"]
    assert len(accepted_children) == 1
    child_decomp = accepted_children[0]
    assert child_decomp.decomposition_origin == "lean_failure_subproof_decomposition"
    assert child_decomp.decomposition_origin_job_id == vet_job.worker_job_id
    assert child_decomp.decomposition_origin_reason == "Approved as a Lean-failure-driven proof-preserving decomposition."

    child_lemma_rows = [LemmaRepository(store).get(lemma_id) for lemma_id in child_decomp.lemma_ids]
    child_lemma_rows = [row for row in child_lemma_rows if row is not None]
    assert len(child_lemma_rows) == 2
    assert all(row.latest_nl_proof for row in child_lemma_rows)
    assert all(row.proof_status == ProofStatus.PROOF_VETTED.value for row in child_lemma_rows)
    assert all(row.routing_status == RoutingStatus.READY_FOR_LEAN.value for row in child_lemma_rows)
    assert submitted_prepare == [child_decomp.decomposition_id]
