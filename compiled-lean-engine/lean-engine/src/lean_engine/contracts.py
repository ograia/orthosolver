from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JsonValue = Any
JsonDict = dict[str, JsonValue]


@dataclass(frozen=True)
class SourceSpan:
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int

    def to_dict(self) -> JsonDict:
        return {
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class NormalizationProvenance:
    input_kind: str
    extraction_method: str
    source_path: str | None
    source_name: str | None
    source_text_sha256: str | None
    candidate_count: int
    selected_candidate_index: int
    selected_source_span: SourceSpan | None
    structural_score: int
    original_payload: JsonDict

    def to_dict(self) -> JsonDict:
        return {
            "input_kind": self.input_kind,
            "extraction_method": self.extraction_method,
            "source_path": self.source_path,
            "source_name": self.source_name,
            "source_text_sha256": self.source_text_sha256,
            "candidate_count": self.candidate_count,
            "selected_candidate_index": self.selected_candidate_index,
            "selected_source_span": self.selected_source_span.to_dict() if self.selected_source_span else None,
            "structural_score": self.structural_score,
            "original_payload": self.original_payload,
        }


@dataclass(frozen=True)
class NormalizedRootTheorem:
    statement_nl: str
    semantic_sketch: JsonValue
    theorem_id: str | None = None
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = dict(self.extra)
        data.update(
            {
                "theorem_id": self.theorem_id,
                "statement_nl": self.statement_nl,
                "semantic_sketch": self.semantic_sketch,
            }
        )
        return data


@dataclass(frozen=True)
class NormalizedAssemblyStep:
    step_id: str
    uses_lemmas: list[str]
    uses_prior_steps: list[str]
    derives: str
    is_trivial: bool | None
    trivial_justification: str | None
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = dict(self.extra)
        data.update(
            {
                "step_id": self.step_id,
                "uses_lemmas": list(self.uses_lemmas),
                "uses_prior_steps": list(self.uses_prior_steps),
                "derives": self.derives,
                "is_trivial": self.is_trivial,
                "trivial_justification": self.trivial_justification,
            }
        )
        return data


@dataclass(frozen=True)
class NormalizedAssemblyPlan:
    steps: list[NormalizedAssemblyStep]
    assembly_plan_id: str | None = None
    proof_skeleton_nl: str | None = None
    is_trivially_composable: bool | None = None
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = dict(self.extra)
        data.update(
            {
                "assembly_plan_id": self.assembly_plan_id,
                "steps": [step.to_dict() for step in self.steps],
                "proof_skeleton_nl": self.proof_skeleton_nl,
                "is_trivially_composable": self.is_trivially_composable,
            }
        )
        return data


@dataclass(frozen=True)
class NormalizedDecomposition:
    assembly_plan: NormalizedAssemblyPlan
    decomposition_id: str | None = None
    strategy_summary: str | None = None
    controller_status: str | None = None
    llm_vetting_status: str | None = None
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = dict(self.extra)
        data.update(
            {
                "decomposition_id": self.decomposition_id,
                "strategy_summary": self.strategy_summary,
                "controller_status": self.controller_status,
                "llm_vetting_status": self.llm_vetting_status,
                "assembly_plan": self.assembly_plan.to_dict(),
            }
        )
        return data


@dataclass(frozen=True)
class NormalizedLemma:
    lemma_id: str
    statement_nl: str
    semantic_sketch: JsonValue
    proof_nl: str
    proof_status: str | None = None
    routing_status: str | None = None
    role_in_parent: str | None = None
    depends_on: list[str] = field(default_factory=list)
    layer_index: int | None = None
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = dict(self.extra)
        data.update(
            {
                "lemma_id": self.lemma_id,
                "statement_nl": self.statement_nl,
                "semantic_sketch": self.semantic_sketch,
                "proof_nl": self.proof_nl,
                "proof_status": self.proof_status,
                "routing_status": self.routing_status,
                "role_in_parent": self.role_in_parent,
                "depends_on": list(self.depends_on),
                "layer_index": self.layer_index,
            }
        )
        return data


@dataclass(frozen=True)
class NormalizedProblemBundle:
    problem_id: str
    title: str
    verification_level: str
    root_theorem: NormalizedRootTheorem
    selected_decomposition: NormalizedDecomposition
    lemmas: list[NormalizedLemma]
    all_visible_lemmas_nl_accepted: bool | None
    lemma_count: int
    assembly_step_count: int
    all_lemma_ids: list[str]
    lemma_map: dict[str, NormalizedLemma]
    assembly_lemma_ids: list[str]
    topologically_sorted_lemma_ids: list[str] | None
    provenance: NormalizationProvenance
    extra: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = dict(self.extra)
        data.update(
            {
                "problem_id": self.problem_id,
                "title": self.title,
                "verification_level": self.verification_level,
                "root_theorem": self.root_theorem.to_dict(),
                "selected_decomposition": self.selected_decomposition.to_dict(),
                "lemmas": [lemma.to_dict() for lemma in self.lemmas],
                "all_visible_lemmas_nl_accepted": self.all_visible_lemmas_nl_accepted,
                "lemma_count": self.lemma_count,
                "assembly_step_count": self.assembly_step_count,
                "all_lemma_ids": list(self.all_lemma_ids),
                "lemma_map": {lemma_id: lemma.to_dict() for lemma_id, lemma in self.lemma_map.items()},
                "assembly_lemma_ids": list(self.assembly_lemma_ids),
                "topologically_sorted_lemma_ids": (
                    list(self.topologically_sorted_lemma_ids) if self.topologically_sorted_lemma_ids else None
                ),
                "provenance": self.provenance.to_dict(),
            }
        )
        return data
