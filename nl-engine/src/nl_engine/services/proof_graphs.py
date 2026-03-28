from __future__ import annotations

from collections import defaultdict
import hashlib
import re
from typing import Any

from nl_engine.domain.contracts import DependencyCitation, DependencyManifestItem
from nl_engine.domain.models import (
    DecompositionORM,
    LemmaORM,
    ProblemORM,
    ProofDependencyCheckORM,
    ProofGraphEdgeORM,
    ProofGraphNodeORM,
    ProofGraphORM,
    TheoremORM,
)
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import (
    AssemblyPlanRepository,
    DecompositionRepository,
    LemmaRepository,
    ProofDependencyCheckRepository,
    ProofGraphEdgeRepository,
    ProofGraphNodeRepository,
    ProofGraphRepository,
    TheoremRepository,
    TrustedContextRepository,
)
from nl_engine.services.ids import new_id


class ProofGraphService:
    def __init__(self, store: FileStore) -> None:
        self.store = store
        self.graphs = ProofGraphRepository(store)
        self.nodes = ProofGraphNodeRepository(store)
        self.edges = ProofGraphEdgeRepository(store)
        self.checks = ProofDependencyCheckRepository(store)
        self.theorems = TheoremRepository(store)
        self.lemmas = LemmaRepository(store)
        self.decompositions = DecompositionRepository(store)
        self.assembly_plans = AssemblyPlanRepository(store)
        self.trusted_context = TrustedContextRepository(store)

    @staticmethod
    def normalized_claim_hash(statement_nl: str, semantic_sketch: dict[str, Any] | None = None) -> str:
        sketch = semantic_sketch or {}
        normalized = str(sketch.get("normalized_claim") or "").strip().lower()
        base = normalized or " ".join(str(statement_nl or "").lower().split())
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_words(text: str) -> set[str]:
        return {word for word in re.findall(r"[a-z0-9_]+", text.lower()) if len(word) >= 3}

    @classmethod
    def lexical_overlap(cls, left: str, right: str) -> float:
        left_words = cls._canonical_words(left)
        right_words = cls._canonical_words(right)
        if not left_words or not right_words:
            return 0.0
        return len(left_words & right_words) / max(len(left_words), len(right_words))

    @staticmethod
    def _same_target_family(left: str, right: str) -> bool:
        families = [
            {"inject", "injection", "injective"},
            {"monotone", "monotonicity", "increasing", "increase"},
            {"positive", "positivity", "nonnegative", "nonnegativity"},
            {"hilbert", "ehrhart", "series", "generating"},
            {"count", "cardinality", "cardinal", "size"},
        ]
        left_words = set(re.findall(r"[a-z0-9_]+", left.lower()))
        right_words = set(re.findall(r"[a-z0-9_]+", right.lower()))
        for family in families:
            if left_words & family and right_words & family:
                return True
        return False

    def ensure_graph_for_decomposition(self, problem: ProblemORM, decomp: DecompositionORM) -> ProofGraphORM | None:
        if decomp.proof_graph_id:
            return self.graphs.get(decomp.proof_graph_id)
        if decomp.node_kind == "theorem":
            proof_graph_id = new_id("pgraph")
            graph = ProofGraphORM(
                proof_graph_id=proof_graph_id,
                problem_id=problem.problem_id,
                root_theorem_id=problem.root_theorem_id or decomp.node_id,
                root_decomposition_id=decomp.decomposition_id,
                graph_status="building",
                verification_status=problem.dependency_verification_level or "legacy",
                dag_version="v1",
            )
            self.graphs.create(graph)
            decomp.proof_graph_id = proof_graph_id
            problem.active_proof_graph_id = proof_graph_id
            return graph
        parent_lemma = self.lemmas.get(decomp.node_id)
        if parent_lemma is None or not parent_lemma.proof_graph_id:
            return None
        graph = self.graphs.get(parent_lemma.proof_graph_id)
        decomp.proof_graph_id = parent_lemma.proof_graph_id
        return graph

    def ensure_claim_node(
        self,
        *,
        proof_graph_id: str,
        owner_kind: str,
        owner_id: str,
        statement_nl: str,
        semantic_sketch: dict[str, Any],
        node_status: str,
        metadata: dict[str, Any] | None = None,
    ) -> ProofGraphNodeORM:
        existing = self.nodes.find_by_owner(proof_graph_id, owner_kind=owner_kind, owner_id=owner_id, node_kind="claim")
        if existing is not None:
            existing.statement_nl = statement_nl
            existing.semantic_sketch_json = semantic_sketch
            existing.normalized_claim_hash = self.normalized_claim_hash(statement_nl, semantic_sketch)
            existing.node_status = node_status
            existing.metadata = metadata or existing.metadata
            return self.nodes.save(existing)
        row = ProofGraphNodeORM(
            graph_node_id=new_id("pnode"),
            proof_graph_id=proof_graph_id,
            node_kind="claim",
            owner_kind=owner_kind,
            owner_id=owner_id,
            statement_nl=statement_nl,
            semantic_sketch_json=semantic_sketch,
            normalized_claim_hash=self.normalized_claim_hash(statement_nl, semantic_sketch),
            node_status=node_status,
            metadata=metadata or {},
        )
        return self.nodes.create(row)

    def _definition_node(self, proof_graph_id: str, owner_id: str, item: dict[str, Any]) -> ProofGraphNodeORM:
        label = str(item.get("label") or item.get("name") or "definition")
        content = str(item.get("content") or item.get("value") or "")
        row = ProofGraphNodeORM(
            graph_node_id=new_id("pnode"),
            proof_graph_id=proof_graph_id,
            node_kind="definition",
            owner_kind="decomposition",
            owner_id=owner_id,
            statement_nl=content,
            semantic_sketch_json={},
            normalized_claim_hash=None,
            node_status="available",
            metadata={"label": label, "kind": item.get("kind", "definition")},
        )
        return self.nodes.create(row)

    def _ensure_definition_node(self, proof_graph_id: str, owner_id: str, item: dict[str, Any]) -> ProofGraphNodeORM:
        label = str(item.get("label") or item.get("name") or "definition")
        kind = str(item.get("kind") or "definition")
        content = str(item.get("content") or item.get("value") or "")
        for row in self.nodes.list_by_graph(proof_graph_id):
            if row.node_kind != "definition":
                continue
            if row.owner_kind != "decomposition" or row.owner_id != owner_id:
                continue
            if row.statement_nl != content:
                continue
            if str(row.metadata.get("label") or "") != label:
                continue
            if str(row.metadata.get("kind") or "") != kind:
                continue
            return row
        return self._definition_node(proof_graph_id, owner_id, item)

    def _ensure_trusted_context_node(self, proof_graph_id: str, row: Any) -> ProofGraphNodeORM:
        owner_id = str(row.id)
        existing = self.nodes.find_by_owner(
            proof_graph_id,
            owner_kind="trusted_context",
            owner_id=owner_id,
            node_kind="trusted_decl",
        )
        metadata = {
            "decl_name": row.decl_name,
            "context_scope": row.context_scope,
            "proof_graph_id": row.proof_graph_id,
        }
        if existing is not None:
            existing.statement_nl = row.lean_code
            existing.metadata = metadata
            existing.node_status = "trusted"
            return self.nodes.save(existing)
        return self.nodes.create(
            ProofGraphNodeORM(
                graph_node_id=new_id("pnode"),
                proof_graph_id=proof_graph_id,
                node_kind="trusted_decl",
                owner_kind="trusted_context",
                owner_id=owner_id,
                statement_nl=row.lean_code,
                semantic_sketch_json={},
                normalized_claim_hash=None,
                node_status="trusted",
                metadata=metadata,
            )
        )

    def _ensure_root_semantic_anchor_node(self, proof_graph_id: str, root: TheoremORM) -> ProofGraphNodeORM:
        existing = self.nodes.find_by_owner(
            proof_graph_id,
            owner_kind="theorem",
            owner_id=root.theorem_id,
            node_kind="semantic_anchor",
        )
        normalized_claim = str(root.statement_semantic_sketch.get("normalized_claim") or "")
        if existing is not None:
            existing.statement_nl = normalized_claim
            existing.semantic_sketch_json = root.statement_semantic_sketch
            existing.metadata = {"label": "root_semantic_sketch"}
            existing.node_status = "available"
            return self.nodes.save(existing)
        return self.nodes.create(
            ProofGraphNodeORM(
                graph_node_id=new_id("pnode"),
                proof_graph_id=proof_graph_id,
                node_kind="semantic_anchor",
                owner_kind="theorem",
                owner_id=root.theorem_id,
                statement_nl=normalized_claim,
                semantic_sketch_json=root.statement_semantic_sketch,
                normalized_claim_hash=self.normalized_claim_hash(root.statement_nl, root.statement_semantic_sketch),
                node_status="available",
                metadata={"label": "root_semantic_sketch"},
            )
        )

    def record_trusted_decl_node(
        self,
        *,
        proof_graph_id: str,
        decl_name: str,
        lean_code: str,
        owner_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> ProofGraphNodeORM:
        existing = self.nodes.find_by_owner(proof_graph_id, owner_kind="trusted_context", owner_id=owner_id, node_kind="trusted_decl")
        if existing is not None:
            existing.statement_nl = lean_code
            existing.metadata = metadata or existing.metadata
            return self.nodes.save(existing)
        return self.nodes.create(
            ProofGraphNodeORM(
                graph_node_id=new_id("pnode"),
                proof_graph_id=proof_graph_id,
                node_kind="trusted_decl",
                owner_kind="trusted_context",
                owner_id=owner_id,
                statement_nl=lean_code,
                semantic_sketch_json={},
                normalized_claim_hash=None,
                node_status="trusted",
                metadata={"decl_name": decl_name, **(metadata or {})},
            )
        )

    def annotate_node_metadata(
        self,
        *,
        proof_graph_id: str,
        node_id: str,
        metadata: dict[str, Any],
        node_status: str | None = None,
    ) -> ProofGraphNodeORM | None:
        row = self.nodes.get(node_id)
        if row is None or row.proof_graph_id != proof_graph_id:
            return None
        row.metadata = {**row.metadata, **metadata}
        if node_status is not None:
            row.node_status = node_status
        return self.nodes.save(row)

    def bootstrap_decomposition_graph(
        self,
        *,
        problem: ProblemORM,
        theorem: TheoremORM | None,
        decomp: DecompositionORM,
        lemmas: list[LemmaORM],
    ) -> tuple[ProofGraphORM | None, list[dict[str, Any]], list[dict[str, Any]]]:
        graph = self.ensure_graph_for_decomposition(problem, decomp)
        if graph is None or not decomp.proof_graph_id:
            return None, [], []
        parent_statement = ""
        parent_sketch: dict[str, Any] = {}
        owner_kind = "theorem" if decomp.node_kind == "theorem" else "lemma"
        if decomp.node_kind == "theorem" and theorem is not None:
            parent_statement = theorem.statement_nl
            parent_sketch = theorem.statement_semantic_sketch
        elif decomp.node_kind == "lemma":
            parent_lemma = self.lemmas.get(decomp.node_id)
            if parent_lemma is not None:
                parent_statement = parent_lemma.statement_nl
                parent_sketch = parent_lemma.statement_semantic_sketch
        parent_claim = self.ensure_claim_node(
            proof_graph_id=decomp.proof_graph_id,
            owner_kind=owner_kind,
            owner_id=decomp.node_id,
            statement_nl=parent_statement,
            semantic_sketch=parent_sketch,
            node_status="active",
            metadata={"node_kind": decomp.node_kind},
        )
        context_nodes: list[ProofGraphNodeORM] = []
        for item in self.normalize_definition_context(decomp.shared_context):
            context_nodes.append(self._definition_node(decomp.proof_graph_id, decomp.decomposition_id, item))
        for lemma in lemmas:
            claim = self.ensure_claim_node(
                proof_graph_id=decomp.proof_graph_id,
                owner_kind="lemma",
                owner_id=lemma.lemma_id,
                statement_nl=lemma.statement_nl,
                semantic_sketch=lemma.statement_semantic_sketch,
                node_status=lemma.proof_status,
                metadata={"parent_id": lemma.parent_id},
            )
            lemma.claim_node_id = claim.graph_node_id
            self.edges.create(
                ProofGraphEdgeORM(
                    edge_id=new_id("pedge"),
                    proof_graph_id=decomp.proof_graph_id,
                    from_node_id=parent_claim.graph_node_id,
                    to_node_id=claim.graph_node_id,
                    edge_kind="decomposes_to",
                    dependency_scope="allowed",
                )
            )
        plan = self.assembly_plans.get(decomp.assembly_plan_id) if decomp.assembly_plan_id else None
        if plan is not None:
            for step in plan.steps:
                step_node = self.nodes.create(
                    ProofGraphNodeORM(
                        graph_node_id=new_id("pnode"),
                        proof_graph_id=decomp.proof_graph_id,
                        node_kind="assembly_step",
                        owner_kind="decomposition",
                        owner_id=decomp.decomposition_id,
                        statement_nl=str(step.get("derives") or ""),
                        semantic_sketch_json={},
                        normalized_claim_hash=None,
                        node_status="accepted",
                        metadata={"step_id": step.get("step_id"), "uses_lemmas": list(step.get("uses_lemmas", []))},
                    )
                )
                self.edges.create(
                    ProofGraphEdgeORM(
                        edge_id=new_id("pedge"),
                        proof_graph_id=decomp.proof_graph_id,
                        from_node_id=parent_claim.graph_node_id,
                        to_node_id=step_node.graph_node_id,
                        edge_kind="assembly_uses",
                        dependency_scope="allowed",
                    )
                )
        reduction = self.validate_decomposition_reduction(parent_statement, parent_sketch, lemmas)
        return graph, [], reduction

    def normalize_definition_context(self, raw_context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_context):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "definition").strip().lower()
            if kind not in {"definition", "notation"}:
                continue
            label = str(item.get("label") or item.get("name") or f"context_{idx + 1}")
            content = str(item.get("content") or item.get("value") or "").strip()
            normalized.append({"kind": kind, "label": label, "content": content})
        return normalized

    @staticmethod
    def _definition_context_key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("kind") or "").strip().lower(),
            str(item.get("label") or item.get("name") or "").strip(),
            str(item.get("content") or item.get("value") or "").strip(),
        )

    def _transitive_definition_context(self, problem_id: str, lemma: LemmaORM, *, depth_cap: int = 64) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        current_parent_id = lemma.parent_id
        current_parent_kind = lemma.parent_kind
        depth = 0

        while depth < depth_cap and current_parent_id:
            if current_parent_kind == "decomposition":
                decomp = self.decompositions.get(current_parent_id)
                if decomp is None or decomp.problem_id != problem_id:
                    break
                for item in self.normalize_definition_context(decomp.shared_context):
                    key = self._definition_context_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(
                        {
                            "kind": item["kind"],
                            "label": item["label"],
                            "content": item["content"],
                            "source_decomposition_id": decomp.decomposition_id,
                        }
                    )
                current_parent_id = decomp.node_id
                current_parent_kind = decomp.node_kind
                depth += 1
                continue

            if current_parent_kind == "lemma":
                parent_lemma = self.lemmas.get(current_parent_id)
                if parent_lemma is None or parent_lemma.problem_id != problem_id:
                    break
                current_parent_id = parent_lemma.parent_id
                current_parent_kind = parent_lemma.parent_kind
                depth += 1
                continue

            break

        return collected

    def validate_decomposition_context_purity(self, raw_context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_context):
            if not isinstance(item, dict):
                findings.append({"index": idx, "severity": "fatal", "reason": "context item must be an object"})
                continue
            kind = str(item.get("kind") or "").strip().lower()
            content = str(item.get("content") or item.get("value") or "")
            if kind not in {"definition", "notation"}:
                findings.append({"index": idx, "severity": "fatal", "reason": f"context kind '{kind or 'unknown'}' is not allowed"})
                continue
            if re.search(r"\b(for all|for every|there exists|injective|surjective|if and only if|iff|implies)\b", content, flags=re.I):
                findings.append({"index": idx, "severity": "fatal", "reason": "context item appears propositional rather than definitional"})
        return findings

    def validate_decomposition_reduction(
        self,
        parent_statement: str,
        parent_sketch: dict[str, Any],
        lemmas: list[LemmaORM],
    ) -> list[dict[str, Any]]:
        parent_norm = str(parent_sketch.get("normalized_claim") or parent_statement)
        findings: list[dict[str, Any]] = []
        for lemma in lemmas:
            lemma_norm = str(lemma.statement_semantic_sketch.get("normalized_claim") or lemma.statement_nl)
            overlap = self.lexical_overlap(parent_norm, lemma_norm)
            same_hash = self.normalized_claim_hash(parent_statement, parent_sketch) == self.normalized_claim_hash(
                lemma.statement_nl,
                lemma.statement_semantic_sketch,
            )
            same_family = self._same_target_family(parent_norm, lemma_norm)
            if same_hash or (same_family and overlap >= 0.65):
                findings.append(
                    {
                        "lemma_id": lemma.lemma_id,
                        "severity": "fatal" if same_hash else "warning",
                        "equivalence_risk": "high" if same_hash else "medium",
                        "overlap": overlap,
                        "reason": "child lemma appears equivalent-strength or trivially reformulates the parent",
                    }
                )
        return findings

    def build_dependency_manifest(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        root: TheoremORM,
    ) -> tuple[list[DependencyManifestItem], list[dict[str, Any]], list[dict[str, Any]], str | None]:
        proof_graph_id = lemma.proof_graph_id or problem.active_proof_graph_id
        if not proof_graph_id:
            return [], [], [], None
        allowed: list[DependencyManifestItem] = []
        definition_context: list[dict[str, Any]] = []
        context_items = self._transitive_definition_context(problem.problem_id, lemma)
        if context_items:
            definition_context = [
                {
                    "kind": item["kind"],
                    "label": item["label"],
                    "content": item["content"],
                }
                for item in context_items
            ]
            for item in context_items:
                def_node = self._ensure_definition_node(
                    proof_graph_id,
                    str(item["source_decomposition_id"]),
                    item,
                )
                allowed.append(
                    DependencyManifestItem(
                        item_id=def_node.graph_node_id,
                        item_kind=item["kind"],
                        label=item["label"],
                        source="definition_context",
                        content=item["content"],
                    )
                )
        for row in self.trusted_context.list_for_graph(problem.problem_id, proof_graph_id=proof_graph_id):
            trusted_node = self._ensure_trusted_context_node(proof_graph_id, row)
            allowed.append(
                DependencyManifestItem(
                    item_id=trusted_node.graph_node_id,
                    item_kind="trusted_decl",
                    label=row.decl_name,
                    source=row.context_scope,
                    content=row.lean_code,
                    metadata={"proof_graph_id": row.proof_graph_id},
                )
            )
        anchor_node = self._ensure_root_semantic_anchor_node(proof_graph_id, root)
        allowed.append(
            DependencyManifestItem(
                item_id=anchor_node.graph_node_id,
                item_kind="semantic_anchor",
                label="root_semantic_sketch",
                source="root_semantic_sketch",
                content=str(root.statement_semantic_sketch.get("normalized_claim") or ""),
            )
        )
        forbidden_claims = self._forbidden_claims(problem, lemma, root)
        proof_attempt_node = self.nodes.create(
            ProofGraphNodeORM(
                graph_node_id=new_id("pnode"),
                proof_graph_id=proof_graph_id,
                node_kind="proof_attempt",
                owner_kind="lemma",
                owner_id=lemma.lemma_id,
                statement_nl=lemma.statement_nl,
                semantic_sketch_json={"attempt": int(lemma.solver_attempt_count) + 1},
                normalized_claim_hash=None,
                node_status="queued",
                metadata={
                    "allowed_dependency_manifest": [item.model_dump(mode="json") for item in allowed],
                    "forbidden_claims": forbidden_claims,
                },
            )
        )
        if lemma.claim_node_id:
            self.edges.create(
                ProofGraphEdgeORM(
                    edge_id=new_id("pedge"),
                    proof_graph_id=proof_graph_id,
                    from_node_id=proof_attempt_node.graph_node_id,
                    to_node_id=lemma.claim_node_id,
                    edge_kind="proof_attempt_targets",
                    dependency_scope="allowed",
                )
            )
        return allowed, definition_context, forbidden_claims, proof_attempt_node.graph_node_id

    def _forbidden_claims(self, problem: ProblemORM, lemma: LemmaORM, root: TheoremORM) -> list[dict[str, Any]]:
        forbidden: list[dict[str, Any]] = []
        proof_graph_id = lemma.proof_graph_id or problem.active_proof_graph_id
        root_claim = None
        if proof_graph_id:
            root_claim = self.nodes.find_by_owner(
                proof_graph_id,
                owner_kind="theorem",
                owner_id=root.theorem_id,
                node_kind="claim",
            )
            if root_claim is None:
                root_claim = self.ensure_claim_node(
                    proof_graph_id=proof_graph_id,
                    owner_kind="theorem",
                    owner_id=root.theorem_id,
                    statement_nl=root.statement_nl,
                    semantic_sketch=root.statement_semantic_sketch,
                    node_status="active",
                    metadata={"node_kind": "theorem"},
                )
        forbidden.append(
            {
                "item_id": root_claim.graph_node_id if root_claim is not None else root.theorem_id,
                "label": "root_theorem_claim",
                "statement_nl": root.statement_nl,
                "reason": "root theorem proposition is not citable solver context",
            }
        )
        for ancestor in self._ancestor_lemmas(problem.problem_id, lemma):
            claim_node = None
            if proof_graph_id:
                claim_node = self.nodes.find_by_owner(
                    proof_graph_id,
                    owner_kind="lemma",
                    owner_id=ancestor.lemma_id,
                    node_kind="claim",
                )
                if claim_node is None:
                    claim_node = self.ensure_claim_node(
                        proof_graph_id=proof_graph_id,
                        owner_kind="lemma",
                        owner_id=ancestor.lemma_id,
                        statement_nl=ancestor.statement_nl,
                        semantic_sketch=ancestor.statement_semantic_sketch,
                        node_status=ancestor.proof_status,
                        metadata={"parent_id": ancestor.parent_id},
                    )
            forbidden.append(
                {
                    "item_id": claim_node.graph_node_id if claim_node is not None else ancestor.lemma_id,
                    "label": ancestor.lemma_id,
                    "statement_nl": ancestor.statement_nl,
                    "reason": "ancestor unresolved claim is forbidden",
                }
            )
        if lemma.parent_kind == "decomposition":
            parent_decomp = self.decompositions.get(lemma.parent_id)
            if parent_decomp is not None:
                for sibling_id in parent_decomp.lemma_ids:
                    if sibling_id == lemma.lemma_id:
                        continue
                    sibling = self.lemmas.get(sibling_id)
                    if sibling is None:
                        continue
                    claim_node = None
                    if proof_graph_id:
                        claim_node = self.nodes.find_by_owner(
                            proof_graph_id,
                            owner_kind="lemma",
                            owner_id=sibling.lemma_id,
                            node_kind="claim",
                        )
                        if claim_node is None:
                            claim_node = self.ensure_claim_node(
                                proof_graph_id=proof_graph_id,
                                owner_kind="lemma",
                                owner_id=sibling.lemma_id,
                                statement_nl=sibling.statement_nl,
                                semantic_sketch=sibling.statement_semantic_sketch,
                                node_status=sibling.proof_status,
                                metadata={"parent_id": sibling.parent_id},
                            )
                    forbidden.append(
                        {
                            "item_id": claim_node.graph_node_id if claim_node is not None else sibling.lemma_id,
                            "label": sibling.lemma_id,
                            "statement_nl": sibling.statement_nl,
                            "reason": "sibling unresolved claim is forbidden",
                        }
                    )
        return forbidden

    def _ancestor_lemmas(self, problem_id: str, lemma: LemmaORM) -> list[LemmaORM]:
        rows: list[LemmaORM] = []
        current = lemma
        seen: set[str] = set()
        while current.parent_kind == "lemma" and current.parent_id and current.parent_id not in seen:
            seen.add(current.parent_id)
            parent = self.lemmas.get(current.parent_id)
            if parent is None or parent.problem_id != problem_id:
                break
            rows.append(parent)
            current = parent
        return rows

    def record_dependency_check(
        self,
        *,
        proof_graph_id: str,
        target_node_id: str,
        artifact_kind: str,
        artifact_id: str,
        check_status: str,
        violations: list[dict[str, Any]],
    ) -> ProofDependencyCheckORM:
        row = ProofDependencyCheckORM(
            dependency_check_id=new_id("depchk"),
            proof_graph_id=proof_graph_id,
            target_node_id=target_node_id,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            check_status=check_status,
            violations_json=violations,
        )
        return self.checks.create(row)

    def validate_solver_output(
        self,
        *,
        problem: ProblemORM,
        lemma: LemmaORM,
        proof_attempt_node_id: str | None,
        allowed_manifest: list[DependencyManifestItem],
        forbidden_claims: list[dict[str, Any]],
        output_proof_nl: str | None,
        citations: list[DependencyCitation],
        used_forbidden_claim: bool,
        artifact_id: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        proof_graph_id = lemma.proof_graph_id or problem.active_proof_graph_id
        if not proof_graph_id:
            return "legacy_unknown", []
        violations: list[dict[str, Any]] = []
        allowed_ids = {item.item_id for item in allowed_manifest}
        forbidden_ids = {str(item.get("item_id") or "") for item in forbidden_claims}
        proof_text = str(output_proof_nl or "")
        normalized_citations: list[DependencyCitation] = []
        for citation in citations:
            if isinstance(citation, DependencyCitation):
                normalized_citations.append(citation)
            elif isinstance(citation, dict):
                normalized_citations.append(DependencyCitation.model_validate(citation))
        for citation in normalized_citations:
            if citation.item_id in forbidden_ids:
                violations.append(
                    {
                        "type": "forbidden_citation",
                        "item_id": citation.item_id,
                        "label": citation.label,
                        "reason": "citation targets a forbidden unresolved claim",
                    }
                )
            elif citation.item_id not in allowed_ids:
                violations.append(
                    {
                        "type": "undeclared_citation",
                        "item_id": citation.item_id,
                        "label": citation.label,
                        "reason": "citation not present in the allowed dependency manifest",
                    }
                )
                continue
            target_node = self.nodes.get(citation.item_id)
            if target_node is None or target_node.proof_graph_id != proof_graph_id:
                violations.append(
                    {
                        "type": "dangling_citation_target",
                        "item_id": citation.item_id,
                        "label": citation.label,
                        "reason": "citation target is not a node in this proof graph",
                    }
                )
        lowered = proof_text.lower()
        for forbidden in forbidden_claims:
            label = str(forbidden.get("label") or "")
            if label and label.lower() in lowered:
                violations.append(
                    {
                        "type": "forbidden_reference_text",
                        "item_id": forbidden.get("item_id"),
                        "label": label,
                        "reason": "proof text mentions a forbidden claim label",
                    }
                )
        if used_forbidden_claim:
            violations.append(
                {
                    "type": "solver_self_reported_forbidden_use",
                    "reason": "solver self-check reported forbidden claim use",
                }
            )
        if proof_text and re.search(r"\btrusted lemma\b|\bshared context\b", proof_text, flags=re.I) and not citations:
            violations.append(
                {
                    "type": "missing_citations",
                    "reason": "proof references external trusted context without structured citations",
                }
            )
        status = "passed" if not violations else "retryable_violation"
        if proof_attempt_node_id is not None:
            for citation in normalized_citations:
                if citation.item_id not in forbidden_ids and citation.item_id not in allowed_ids:
                    continue
                target_node = self.nodes.get(citation.item_id)
                if target_node is None or target_node.proof_graph_id != proof_graph_id:
                    continue
                scope = "allowed"
                edge_kind = "proof_depends_on_definition"
                if citation.citation_kind == "trusted_decl":
                    edge_kind = "proof_depends_on_trusted_decl"
                elif citation.citation_kind == "claim":
                    edge_kind = "proof_depends_on_claim"
                    scope = "forbidden" if citation.item_id in forbidden_ids else "allowed"
                self.edges.create(
                    ProofGraphEdgeORM(
                        edge_id=new_id("pedge"),
                        proof_graph_id=proof_graph_id,
                        from_node_id=proof_attempt_node_id,
                        to_node_id=citation.item_id,
                        edge_kind=edge_kind,
                        dependency_scope=scope,
                        metadata={"label": citation.label, "detail": citation.detail},
                    )
                )
                if edge_kind == "proof_depends_on_claim" and lemma.claim_node_id:
                    self.edges.create(
                        ProofGraphEdgeORM(
                            edge_id=new_id("pedge"),
                            proof_graph_id=proof_graph_id,
                            from_node_id=lemma.claim_node_id,
                            to_node_id=citation.item_id,
                            edge_kind="proof_depends_on_claim",
                            dependency_scope=scope,
                            metadata={"via": "proof_attempt_projection", "label": citation.label},
                        )
                    )
        self.record_dependency_check(
            proof_graph_id=proof_graph_id,
            target_node_id=lemma.claim_node_id or lemma.lemma_id,
            artifact_kind="solver",
            artifact_id=artifact_id,
            check_status=status,
            violations=violations,
        )
        return status, violations

    def validate_vetter_consistency(
        self,
        *,
        proof_graph_id: str,
        target_node_id: str,
        artifact_id: str,
        dependency_assessment: dict[str, Any] | None,
        forbidden_findings: list[dict[str, Any]] | None,
        undeclared_findings: list[dict[str, Any]] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        violations = list(forbidden_findings or []) + list(undeclared_findings or [])
        if dependency_assessment and dependency_assessment.get("status") == "fatal":
            violations.append({"type": "vetter_dependency_status", "reason": "vetter marked dependency violation as fatal"})
        status = "passed" if not violations else "retryable_violation"
        self.record_dependency_check(
            proof_graph_id=proof_graph_id,
            target_node_id=target_node_id,
            artifact_kind="vetter",
            artifact_id=artifact_id,
            check_status=status,
            violations=violations,
        )
        return status, violations

    def can_promote_trusted_context(self, lemma: LemmaORM) -> bool:
        if not lemma.proof_graph_id or not lemma.claim_node_id:
            return False
        latest = self.checks.latest_for_target(lemma.proof_graph_id, target_node_id=lemma.claim_node_id)
        if latest is None or latest.check_status != "passed":
            return False
        edges = self.edges.list_by_graph(lemma.proof_graph_id)
        for edge in edges:
            if edge.edge_kind == "proof_depends_on_claim" and edge.dependency_scope != "allowed":
                return False
        return True

    def final_graph_integrity(self, proof_graph_id: str, *, artifact_id: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        nodes = self.nodes.list_by_graph(proof_graph_id)
        edges = self.edges.list_by_graph(proof_graph_id)
        nodes_by_id = {row.graph_node_id: row for row in nodes}
        claim_nodes = {row.graph_node_id: row for row in nodes if row.node_kind == "claim"}
        claim_by_owner = {(row.owner_kind, row.owner_id): row.graph_node_id for row in claim_nodes.values()}
        proof_attempt_nodes = {row.graph_node_id: row for row in nodes if row.node_kind == "proof_attempt"}
        adjacency: dict[str, set[str]] = defaultdict(set)
        violations: list[dict[str, Any]] = []
        for edge in edges:
            if edge.edge_kind == "proof_depends_on_claim":
                if edge.from_node_id not in nodes_by_id or edge.to_node_id not in nodes_by_id:
                    violations.append(
                        {
                            "type": "dangling_dependency_edge",
                            "edge_id": edge.edge_id,
                            "from_node_id": edge.from_node_id,
                            "to_node_id": edge.to_node_id,
                        }
                    )
                    continue
                if edge.dependency_scope != "allowed":
                    violations.append(
                        {
                            "type": "forbidden_claim_dependency",
                            "from_node_id": edge.from_node_id,
                            "to_node_id": edge.to_node_id,
                        }
                    )
                if edge.from_node_id in claim_nodes and edge.to_node_id in claim_nodes:
                    adjacency[edge.from_node_id].add(edge.to_node_id)
                elif edge.from_node_id in proof_attempt_nodes and edge.to_node_id in claim_nodes:
                    attempt = proof_attempt_nodes[edge.from_node_id]
                    owner_claim_id = claim_by_owner.get(("lemma", attempt.owner_id)) or claim_by_owner.get(
                        (attempt.owner_kind, attempt.owner_id)
                    )
                    if owner_claim_id:
                        adjacency[owner_claim_id].add(edge.to_node_id)
        cycle = self._find_cycle(adjacency)
        if cycle:
            violations.append({"type": "claim_dependency_cycle", "cycle": cycle})
        cross_track = [edge for edge in edges if edge.metadata.get("proof_graph_id") not in {None, proof_graph_id}]
        for edge in cross_track:
            violations.append(
                {
                    "type": "cross_track_contamination",
                    "edge_id": edge.edge_id,
                    "to_node_id": edge.to_node_id,
                }
            )
        status = "passed" if not violations else "fatal_violation"
        summary = {
            "proof_graph_id": proof_graph_id,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "claim_node_count": len(claim_nodes),
            "cycle_found": bool(cycle),
        }
        self.record_dependency_check(
            proof_graph_id=proof_graph_id,
            target_node_id=proof_graph_id,
            artifact_kind="final_check",
            artifact_id=artifact_id,
            check_status=status,
            violations=violations,
        )
        return status, violations, summary

    @staticmethod
    def _find_cycle(adjacency: dict[str, set[str]]) -> list[str]:
        visited: set[str] = set()
        stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> list[str]:
            visited.add(node)
            stack.add(node)
            path.append(node)
            for nxt in adjacency.get(node, set()):
                if nxt not in visited:
                    found = dfs(nxt)
                    if found:
                        return found
                elif nxt in stack:
                    idx = path.index(nxt)
                    return path[idx:] + [nxt]
            stack.remove(node)
            path.pop()
            return []

        for node in adjacency:
            if node not in visited:
                found = dfs(node)
                if found:
                    return found
        return []
