from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    JsonDict,
    NormalizationProvenance,
    NormalizedAssemblyPlan,
    NormalizedAssemblyStep,
    NormalizedDecomposition,
    NormalizedLemma,
    NormalizedProblemBundle,
    NormalizedRootTheorem,
    SourceSpan,
)
from .result_types import FatalError, FatalResult, NormalizationError, NormalizationResult, OkResult

ArtifactInput = Path | str | Mapping[str, Any]


@dataclass(frozen=True)
class _IngestedSource:
    input_kind: str
    source_name: str | None
    source_path: str | None
    text: str | None
    source_text_sha256: str | None
    payload_object: JsonDict | None


@dataclass(frozen=True)
class _Candidate:
    payload: JsonDict
    index: int
    score: int
    diagnostics: list[str]
    span: SourceSpan | None


def normalize_problem_artifact(source: ArtifactInput, *, source_name: str | None = None) -> NormalizedProblemBundle:
    ingested = _ingest_source(source, source_name=source_name)
    candidates, extraction_method, pre_diagnostics = _collect_candidates(ingested)
    if not candidates:
        diagnostics = [*pre_diagnostics, "no parseable top-level JSON object found"]
        raise NormalizationError(
            FatalError(
                error_class="malformed_input_artifact",
                message="No valid problem bundle could be normalized from the provided artifact.",
                diagnostics=diagnostics,
            )
        )

    evaluated = [
        _Candidate(
            payload=candidate.payload,
            index=candidate.index,
            score=_structural_score(candidate.payload),
            diagnostics=_validate_candidate(candidate.payload),
            span=candidate.span,
        )
        for candidate in candidates
    ]

    valid_candidates = [candidate for candidate in evaluated if not candidate.diagnostics]
    if not valid_candidates:
        diagnostics = [*pre_diagnostics]
        for candidate in evaluated:
            joined = "; ".join(candidate.diagnostics) if candidate.diagnostics else "unknown validation failure"
            diagnostics.append(f"candidate {candidate.index + 1}: {joined}")
        raise NormalizationError(
            FatalError(
                error_class="malformed_input_artifact",
                message="No valid problem bundle could be normalized from the provided artifact.",
                diagnostics=diagnostics,
            )
        )

    selected = max(valid_candidates, key=lambda candidate: (candidate.score, candidate.index))

    provenance = NormalizationProvenance(
        input_kind=ingested.input_kind,
        extraction_method=extraction_method,
        source_path=ingested.source_path,
        source_name=ingested.source_name,
        source_text_sha256=ingested.source_text_sha256,
        candidate_count=len(candidates),
        selected_candidate_index=selected.index,
        selected_source_span=selected.span,
        structural_score=selected.score,
        original_payload=copy.deepcopy(selected.payload),
    )

    return _build_normalized_bundle(selected.payload, provenance)


def normalize_problem_artifact_result(
    source: ArtifactInput,
    *,
    source_name: str | None = None,
) -> NormalizationResult[NormalizedProblemBundle]:
    try:
        bundle = normalize_problem_artifact(source, source_name=source_name)
    except NormalizationError as exc:
        return FatalResult(error=exc.error)
    return OkResult(data=bundle)


def write_normalized_artifact(
    bundle: NormalizedProblemBundle,
    *,
    artifact_root: Path = Path(".artifacts/normalize"),
    output_path: Path | None = None,
) -> Path:
    if output_path is None:
        output_path = artifact_root / _sanitize_component(bundle.problem_id) / "normalized_problem.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(bundle.to_dict(), indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


@dataclass(frozen=True)
class _RawCandidate:
    payload: JsonDict
    index: int
    span: SourceSpan | None


def _ingest_source(source: ArtifactInput, *, source_name: str | None) -> _IngestedSource:
    if isinstance(source, Mapping):
        if not isinstance(source, dict):
            source = dict(source)
        return _IngestedSource(
            input_kind="object",
            source_name=source_name,
            source_path=None,
            text=None,
            source_text_sha256=None,
            payload_object=copy.deepcopy(source),
        )

    if isinstance(source, Path):
        path = source.expanduser().resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise NormalizationError(
                FatalError(
                    error_class="malformed_input_artifact",
                    message="Failed to read artifact from path input.",
                    diagnostics=[f"path: {path}", f"{exc.__class__.__name__}: {exc}"],
                )
            ) from exc
        return _IngestedSource(
            input_kind="path",
            source_name=source_name,
            source_path=str(path),
            text=text,
            source_text_sha256=_sha256(text),
            payload_object=None,
        )

    if isinstance(source, str):
        path = Path(source).expanduser()
        try:
            if len(source) <= 1024 and path.exists() and path.is_file():
                resolved = path.resolve()
                text = resolved.read_text(encoding="utf-8")
                return _IngestedSource(
                    input_kind="path",
                    source_name=source_name,
                    source_path=str(resolved),
                    text=text,
                    source_text_sha256=_sha256(text),
                    payload_object=None,
                )
        except OSError as exc:
            raise NormalizationError(
                FatalError(
                    error_class="malformed_input_artifact",
                    message="Failed to read artifact from path input.",
                    diagnostics=[f"path: {path}", f"{exc.__class__.__name__}: {exc}"],
                )
            ) from exc

        return _IngestedSource(
            input_kind="text",
            source_name=source_name,
            source_path=None,
            text=source,
            source_text_sha256=_sha256(source),
            payload_object=None,
        )

    raise NormalizationError(
        FatalError(
            error_class="malformed_input_artifact",
            message="Unsupported artifact input type.",
            diagnostics=[f"received input type: {type(source)!r}"],
        )
    )


def _collect_candidates(source: _IngestedSource) -> tuple[list[_RawCandidate], str, list[str]]:
    if source.payload_object is not None:
        if not isinstance(source.payload_object, dict):
            return [], "direct_object", ["object input must be a JSON object"]
        return [_RawCandidate(payload=source.payload_object, index=0, span=None)], "direct_object", []

    if source.text is None:
        return [], "unknown", ["no text or object input available"]

    strict_diagnostics: list[str] = []
    try:
        parsed = json.loads(source.text)
    except json.JSONDecodeError as exc:
        strict_diagnostics.append(f"strict JSON parse failed: {exc.msg} at line {exc.lineno}, column {exc.colno}")
    else:
        if isinstance(parsed, dict):
            span = SourceSpan(
                start_offset=0,
                end_offset=len(source.text),
                start_line=1,
                end_line=source.text.count("\n") + 1,
            )
            return [_RawCandidate(payload=parsed, index=0, span=span)], "strict_json", []
        strict_diagnostics.append("strict JSON parse succeeded but top-level value is not an object")

    extracted: list[_RawCandidate] = []
    for index, span in enumerate(_extract_object_spans(source.text)):
        chunk = source.text[span.start_offset : span.end_offset]
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            extracted.append(_RawCandidate(payload=parsed, index=index, span=span))

    if not extracted:
        return [], "candidate_scan", strict_diagnostics
    return extracted, "candidate_scan", strict_diagnostics


def _extract_object_spans(text: str) -> list[SourceSpan]:
    spans: list[SourceSpan] = []
    depth = 0
    in_string = False
    escaped = False
    start_offset: int | None = None

    for offset, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                start_offset = offset
            depth += 1
            continue

        if char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start_offset is not None:
                end_offset = offset + 1
                spans.append(
                    SourceSpan(
                        start_offset=start_offset,
                        end_offset=end_offset,
                        start_line=_line_number(text, start_offset),
                        end_line=_line_number(text, end_offset),
                    )
                )
                start_offset = None

    return spans


def _line_number(text: str, offset: int) -> int:
    clamped = min(max(offset, 0), len(text))
    return text.count("\n", 0, clamped) + 1


def _validate_candidate(payload: JsonDict) -> list[str]:
    diagnostics: list[str] = []

    root = payload.get("root_theorem")
    if not isinstance(root, dict):
        diagnostics.append("missing root_theorem object")
    else:
        if not _is_non_empty_string(root.get("statement_nl")):
            diagnostics.append("missing root_theorem.statement_nl")
        if root.get("semantic_sketch") is None:
            diagnostics.append("missing root_theorem.semantic_sketch")

    decomposition = payload.get("selected_decomposition")
    if not isinstance(decomposition, dict):
        diagnostics.append("missing selected_decomposition object")
    else:
        assembly_plan = decomposition.get("assembly_plan")
        if not isinstance(assembly_plan, dict):
            diagnostics.append("missing selected_decomposition.assembly_plan")
        elif "steps" in assembly_plan and not isinstance(assembly_plan.get("steps"), list):
            diagnostics.append("selected_decomposition.assembly_plan.steps must be a list")

    lemmas = payload.get("lemmas")
    if not isinstance(lemmas, list):
        diagnostics.append("missing lemmas list")
        return diagnostics

    if not lemmas:
        diagnostics.append("lemma list is empty")
        return diagnostics

    seen_lemma_ids: set[str] = set()
    for idx, lemma in enumerate(lemmas):
        path = f"lemmas[{idx}]"
        if not isinstance(lemma, dict):
            diagnostics.append(f"{path} is not an object")
            continue

        lemma_id = _optional_string(lemma.get("lemma_id"))
        if lemma_id is None:
            diagnostics.append(f"{path}.lemma_id is required")
        else:
            if lemma_id in seen_lemma_ids:
                diagnostics.append(f"duplicate lemma_id: {lemma_id}")
            seen_lemma_ids.add(lemma_id)

        if not _is_non_empty_string(lemma.get("statement_nl")):
            diagnostics.append(f"{path}.statement_nl is required")
        if lemma.get("semantic_sketch") is None:
            diagnostics.append(f"{path}.semantic_sketch is required")
        if not _is_non_empty_string(lemma.get("proof_nl")):
            diagnostics.append(f"{path}.proof_nl is required")

    return diagnostics


def _structural_score(payload: JsonDict) -> int:
    score = 0

    root = payload.get("root_theorem")
    if isinstance(root, dict):
        score += 20
        if _is_non_empty_string(root.get("statement_nl")):
            score += 10
        if root.get("semantic_sketch") is not None:
            score += 10

    decomposition = payload.get("selected_decomposition")
    if isinstance(decomposition, dict):
        score += 20
        assembly_plan = decomposition.get("assembly_plan")
        if isinstance(assembly_plan, dict):
            score += 12
            steps = assembly_plan.get("steps")
            if isinstance(steps, list):
                score += min(len(steps), 20)

    lemmas = payload.get("lemmas")
    if isinstance(lemmas, list):
        score += 20
        score += min(len(lemmas), 50)

    return score


def _build_normalized_bundle(payload: JsonDict, provenance: NormalizationProvenance) -> NormalizedProblemBundle:
    root_raw = _as_dict(payload.get("root_theorem"))
    decomposition_raw = _as_dict(payload.get("selected_decomposition"))
    assembly_plan_raw = _as_dict(decomposition_raw.get("assembly_plan"))
    lemmas_raw = payload.get("lemmas")
    assert isinstance(lemmas_raw, list)

    root = NormalizedRootTheorem(
        theorem_id=_optional_string(root_raw.get("theorem_id")),
        statement_nl=str(root_raw.get("statement_nl", "")).strip(),
        semantic_sketch=copy.deepcopy(root_raw.get("semantic_sketch")),
        extra=_extra_fields(root_raw, {"theorem_id", "statement_nl", "semantic_sketch"}),
    )

    steps: list[NormalizedAssemblyStep] = []
    for step_index, step_raw_value in enumerate(assembly_plan_raw.get("steps", [])):
        step_raw = _as_dict(step_raw_value)
        step_id = _optional_string(step_raw.get("step_id")) or f"step_{step_index + 1}"
        step = NormalizedAssemblyStep(
            step_id=step_id,
            uses_lemmas=_string_list(step_raw.get("uses_lemmas")),
            uses_prior_steps=_string_list(step_raw.get("uses_prior_steps")),
            derives=_optional_string(step_raw.get("derives")) or "",
            is_trivial=step_raw.get("is_trivial") if isinstance(step_raw.get("is_trivial"), bool) else None,
            trivial_justification=_optional_string(step_raw.get("trivial_justification")),
            extra=_extra_fields(
                step_raw,
                {
                    "step_id",
                    "uses_lemmas",
                    "uses_prior_steps",
                    "derives",
                    "is_trivial",
                    "trivial_justification",
                },
            ),
        )
        steps.append(step)

    assembly_plan = NormalizedAssemblyPlan(
        assembly_plan_id=_optional_string(assembly_plan_raw.get("assembly_plan_id")),
        steps=steps,
        proof_skeleton_nl=_optional_string(assembly_plan_raw.get("proof_skeleton_nl")),
        is_trivially_composable=(
            assembly_plan_raw.get("is_trivially_composable")
            if isinstance(assembly_plan_raw.get("is_trivially_composable"), bool)
            else None
        ),
        extra=_extra_fields(
            assembly_plan_raw,
            {"assembly_plan_id", "steps", "proof_skeleton_nl", "is_trivially_composable"},
        ),
    )

    decomposition = NormalizedDecomposition(
        decomposition_id=_optional_string(decomposition_raw.get("decomposition_id")),
        strategy_summary=_optional_string(decomposition_raw.get("strategy_summary")),
        controller_status=_optional_string(decomposition_raw.get("controller_status")),
        llm_vetting_status=_optional_string(decomposition_raw.get("llm_vetting_status")),
        assembly_plan=assembly_plan,
        extra=_extra_fields(
            decomposition_raw,
            {
                "decomposition_id",
                "strategy_summary",
                "controller_status",
                "llm_vetting_status",
                "assembly_plan",
            },
        ),
    )

    lemmas: list[NormalizedLemma] = []
    for lemma_raw_value in lemmas_raw:
        lemma_raw = _as_dict(lemma_raw_value)
        lemma = NormalizedLemma(
            lemma_id=str(lemma_raw.get("lemma_id", "")).strip(),
            statement_nl=str(lemma_raw.get("statement_nl", "")).strip(),
            semantic_sketch=copy.deepcopy(lemma_raw.get("semantic_sketch")),
            proof_nl=str(lemma_raw.get("proof_nl", "")).strip(),
            proof_status=_optional_string(lemma_raw.get("proof_status")),
            routing_status=_optional_string(lemma_raw.get("routing_status")),
            role_in_parent=_optional_string(lemma_raw.get("role_in_parent")),
            depends_on=[
                str(item).strip()
                for item in lemma_raw.get("depends_on", [])
                if str(item).strip()
            ] if isinstance(lemma_raw.get("depends_on"), list) else [],
            layer_index=(
                int(lemma_raw.get("layer_index"))
                if isinstance(lemma_raw.get("layer_index"), int)
                else None
            ),
            extra=_extra_fields(
                lemma_raw,
                {
                    "lemma_id",
                    "statement_nl",
                    "semantic_sketch",
                    "proof_nl",
                    "proof_status",
                    "routing_status",
                    "role_in_parent",
                    "depends_on",
                    "layer_index",
                },
            ),
        )
        lemmas.append(lemma)

    all_lemma_ids = [lemma.lemma_id for lemma in lemmas]
    lemma_map = {lemma.lemma_id: lemma for lemma in lemmas}
    assembly_lemma_ids = _ordered_unique([lemma_id for step in steps for lemma_id in step.uses_lemmas])
    topo_lemma_ids = _derive_topological_lemma_order(steps=steps, all_lemma_ids=all_lemma_ids)

    top_level_known_fields = {
        "problem_id",
        "title",
        "verification_level",
        "root_theorem",
        "selected_decomposition",
        "lemmas",
        "all_visible_lemmas_nl_accepted",
    }

    return NormalizedProblemBundle(
        problem_id=_optional_string(payload.get("problem_id")) or "unknown_problem_id",
        title=_optional_string(payload.get("title")) or "",
        verification_level=_optional_string(payload.get("verification_level")) or "",
        root_theorem=root,
        selected_decomposition=decomposition,
        lemmas=lemmas,
        all_visible_lemmas_nl_accepted=(
            payload.get("all_visible_lemmas_nl_accepted")
            if isinstance(payload.get("all_visible_lemmas_nl_accepted"), bool)
            else None
        ),
        lemma_count=len(lemmas),
        assembly_step_count=len(steps),
        all_lemma_ids=all_lemma_ids,
        lemma_map=lemma_map,
        assembly_lemma_ids=assembly_lemma_ids,
        topologically_sorted_lemma_ids=topo_lemma_ids,
        provenance=provenance,
        extra=_extra_fields(payload, top_level_known_fields),
    )


def _derive_topological_lemma_order(
    *,
    steps: list[NormalizedAssemblyStep],
    all_lemma_ids: list[str],
) -> list[str] | None:
    if not steps or not all_lemma_ids:
        return None

    step_to_index: dict[str, int] = {}
    for index, step in enumerate(steps):
        if not step.step_id or step.step_id in step_to_index:
            return None
        step_to_index[step.step_id] = index

    adjacency: dict[str, list[str]] = {step.step_id: [] for step in steps}
    indegree: dict[str, int] = {step.step_id: 0 for step in steps}

    for step in steps:
        for dependency in step.uses_prior_steps:
            if dependency not in adjacency:
                return None
            adjacency[dependency].append(step.step_id)
            indegree[step.step_id] += 1

    ready = sorted(
        [step_id for step_id, degree in indegree.items() if degree == 0],
        key=lambda step_id: step_to_index[step_id],
    )
    ordered_steps: list[str] = []

    while ready:
        current = ready.pop(0)
        ordered_steps.append(current)
        for child in adjacency[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda step_id: step_to_index[step_id])

    if len(ordered_steps) != len(steps):
        return None

    known_lemma_ids = set(all_lemma_ids)
    seen: set[str] = set()
    ordered_lemmas: list[str] = []
    for step_id in ordered_steps:
        step = steps[step_to_index[step_id]]
        for lemma_id in step.uses_lemmas:
            if lemma_id in known_lemma_ids and lemma_id not in seen:
                seen.add(lemma_id)
                ordered_lemmas.append(lemma_id)

    if len(ordered_lemmas) != len(all_lemma_ids):
        return None
    return ordered_lemmas


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _as_dict(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return value
    return {}


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _extra_fields(raw: JsonDict, known_fields: set[str]) -> JsonDict:
    return {key: copy.deepcopy(value) for key, value in raw.items() if key not in known_fields}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sanitize_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._")
    return cleaned or "unknown_problem"
