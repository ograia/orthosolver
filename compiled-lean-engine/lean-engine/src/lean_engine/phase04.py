from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import logging

from .artifact_io import RunPaths, sanitize_component, write_json, write_text
from .claude_runner import ClaudeRunner
from .config import RuntimeConfig, current_code_origin, load_runtime_config
from .contracts import NormalizedProblemBundle
from .lean_checks import rebuild_module_olean
from .lean4_skills_refs import get_all_proving_refs, get_compact_proving_refs
from .lemma_phase import (
    AssumptionCapsule,
    LemmaFormalizationResult,
    PinnedLemmaSignature,
    TrustedContextEntry,
    classify_lemma_failure,
    derive_trusted_entries_from_lemmas_file,
    lemma_id_to_path_token,
    load_pinned_lemma_signatures,
    merge_declaration_into_lemmas_file,
    run_lemma_formalization,
    write_trusted_manifest,
)
from .workspace import clone_workspace, snapshot_workspace, write_project_mcp_config

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Phase04RunResult:
    status: str
    problem_id: str
    run_root: Path
    pinned_signatures_path: Path
    trusted_manifest_path: Path
    phase_summary_path: Path
    lemma_order: tuple[str, ...]
    lemma_results: tuple[LemmaFormalizationResult, ...]
    code_origin: dict[str, Any] | None = None
    workspace_revision: str | None = None
    merge_revision: int | None = None
    verifier_revision: int | None = None
    dependency_graph: dict[str, list[str]] | None = None
    dependency_closure: dict[str, list[str]] | None = None
    layer_order: list[list[str]] | None = None
    error_class: str | None = None
    message: str | None = None
    failed_lemma_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problem_id": self.problem_id,
            "run_root": str(self.run_root),
            "pinned_signatures_path": str(self.pinned_signatures_path),
            "trusted_manifest_path": str(self.trusted_manifest_path),
            "phase_summary_path": str(self.phase_summary_path),
            "lemma_order": list(self.lemma_order),
            "lemma_results": [item.to_dict() for item in self.lemma_results],
            "code_origin": self.code_origin,
            "workspace_revision": self.workspace_revision,
            "merge_revision": self.merge_revision,
            "verifier_revision": self.verifier_revision,
            "dependency_graph": self.dependency_graph,
            "dependency_closure": self.dependency_closure,
            "layer_order": self.layer_order,
            "error_class": self.error_class,
            "message": self.message,
            "failed_lemma_id": self.failed_lemma_id,
        }


def run_phase04(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    bundle: NormalizedProblemBundle,
    pinned_signatures_path: Path,
    max_attempts_per_lemma: int,
    timeout_seconds: int,
    lean_check_timeout_seconds: int | None = None,
    model: str | None = None,
    target_lemma_id: str | None = None,
    target_lemma_ids: set[str] | None = None,
    mock_candidates: dict[str, list[str]] | None = None,
    parallel_lemmas: bool = False,
    lemma_workers: int = 1,
    runner: SubprocessRunner = subprocess.run,
) -> Phase04RunResult:
    if lemma_workers <= 0:
        raise ValueError("lemma_workers must be > 0")
    code_origin = current_code_origin()
    effective_lean_check_timeout = timeout_seconds if lean_check_timeout_seconds is None else lean_check_timeout_seconds
    lean4_skills_refs = get_all_proving_refs(runtime_config.integrations.lean4_skills_root)
    lean4_skills_refs_compact = get_compact_proving_refs(runtime_config.integrations.lean4_skills_root)

    full_lemma_order = _deterministic_lemma_order(bundle)
    if target_lemma_ids is not None:
        # Multi-target mode: process only specified lemmas
        lemma_order = tuple(lid for lid in full_lemma_order if lid in target_lemma_ids)
    elif target_lemma_id is not None:
        normalized_target = target_lemma_id.strip()
        if not normalized_target:
            raise ValueError("target_lemma_id must be non-empty when provided")
        if normalized_target not in bundle.lemma_map:
            raise ValueError(f"target_lemma_id not found in normalized bundle: {normalized_target}")
        lemma_order = (normalized_target,)
    else:
        lemma_order = tuple(full_lemma_order)
    phase_summary_path = _phase04_summary_path(
        run_paths=run_paths,
        lemma_order=lemma_order,
        target_lemma_id=target_lemma_id,
        target_lemma_ids=target_lemma_ids,
    )
    try:
        dependency_graph, dependency_closure, lemma_layer_index = build_dependency_graph(
            bundle=bundle,
            lemma_order=lemma_order,
        )
    except ValueError as exc:
        result = Phase04RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=run_paths.run_root / "trusted_context_manifest.json",
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            lemma_results=(),
            code_origin=current_code_origin(),
            workspace_revision=run_paths.run_name,
            error_class="dependency_graph_invalid",
            message=str(exc),
        )
        write_json(result.phase_summary_path, result.to_dict())
        return result
    trusted_manifest_path = run_paths.run_root / "trusted_context_manifest.json"
    manifest_problem_id = sanitize_component(bundle.problem_id)
    # Cap parallel workers at 4 to prevent CPU/RAM saturation (Phase 3).
    _MAX_LEMMA_WORKERS = 4
    effective_lemma_workers = lemma_workers if parallel_lemmas else 1
    if parallel_lemmas and effective_lemma_workers <= 1:
        effective_lemma_workers = max(1, len(lemma_order))
    effective_lemma_workers = min(effective_lemma_workers, _MAX_LEMMA_WORKERS)

    if run_paths.problem_id != manifest_problem_id:
        result = Phase04RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            lemma_results=(),
            code_origin=code_origin,
            error_class="problem_binding_mismatch",
            message=(
                "run directory problem binding mismatch: "
                f"run expects `{run_paths.problem_id}`, normalized bundle resolved to `{manifest_problem_id}`"
            ),
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    try:
        pinned_map = load_pinned_lemma_signatures(
            pinned_signatures_path,
            expected_problem_id=bundle.problem_id,
            expected_run_name=run_paths.run_name,
        )
    except ValueError as exc:
        result = Phase04RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            lemma_results=(),
            code_origin=code_origin,
            error_class="pinned_signature_binding_mismatch",
            message=str(exc),
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    # --- Workspace preparation: prevent axiom/theorem name conflicts ---
    # Remove AssemblyCheck.lean BEFORE Phase 04 — it declares axiom stubs that
    # conflict with theorem declarations in scratch files (same lean_lib).
    _assembly_check = run_paths.workspace_dir / "Orthos" / "AssemblyCheck.lean"
    if _assembly_check.exists():
        _assembly_check.unlink()
        _log.info("Removed Orthos/AssemblyCheck.lean to prevent lean_lib name collisions.")

    # Collect the set of pinned declaration names (lemma + root) that will be
    # re-declared as theorems.  Only these are neutralized in Statements.lean;
    # helper axioms (e.g. ``axiom f_count``) and all defs are preserved so that
    # downstream definitions that depend on them continue to compile.
    pinned_decl_names: set[str] = {sig.decl_name for sig in pinned_map.values()}
    _pinned_data = json.loads(pinned_signatures_path.read_text("utf-8"))
    _root_decl = _pinned_data.get("root", {}).get("decl_name", "")
    if _root_decl:
        pinned_decl_names.add(_root_decl)

    _neutralize_statements_for_proving(run_paths.workspace_dir, pinned_decl_names)
    _reset_lemmas_file(run_paths.workspace_dir)
    _validate_workspace_integrity(run_paths.workspace_dir)

    # Purge stale build artifacts so ``rebuild_module_olean`` compiles from the
    # neutralized source rather than serving a stale olean with old axiom decls.
    for _module_path in ("Orthos/Statements", "Orthos/Lemmas"):
        deleted = _purge_module_build_artifacts(run_paths.workspace_dir, _module_path)
        if deleted:
            _log.info("Purged %d stale %s build artifacts.", len(deleted), _module_path)

    # Rebuild oleans directly (bypass Lake's dependency tracker).  Using
    # ``lake build`` here would trigger Lake to recheck all Mathlib dependencies
    # and, with hardlinked-cache timestamps, it often decides to rebuild Mathlib
    # from source (~60 min).  ``rebuild_module_olean`` runs ``lake env lean``
    # which compiles only the target file using the existing LEAN_PATH.
    _log.info("Rebuilding Orthos.Statements olean after neutralization...")
    olean_rebuild = rebuild_module_olean(
        run_paths.workspace_dir,
        "Orthos/Statements.lean",
        "Orthos.Statements",
        timeout_seconds=effective_lean_check_timeout,
        runner=runner,
    )
    if not olean_rebuild.ok:
        _log.error("Orthos.Statements olean rebuild failed (exit %d): %s", olean_rebuild.returncode, olean_rebuild.stderr[:500])
        result = Phase04RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            lemma_results=(),
            code_origin=code_origin,
            error_class="stale_olean",
            message=(
                "Failed to rebuild Orthos.Statements olean after neutralization. "
                f"Exit code: {olean_rebuild.returncode}. "
                "All downstream lemma proofs would fail with 'already declared' errors."
            ),
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    # Rebuild Lemmas olean (imports Statements, so must come after).
    _log.info("Rebuilding Orthos.Lemmas olean after reset...")
    _lemmas_rebuild = rebuild_module_olean(
        run_paths.workspace_dir,
        "Orthos/Lemmas.lean",
        "Orthos.Lemmas",
        timeout_seconds=effective_lean_check_timeout,
        runner=runner,
    )
    if not _lemmas_rebuild.ok:
        _log.warning(
            "Orthos.Lemmas olean rebuild failed (exit %d): %s — "
            "scratch files may fail on first import but MCP will handle it.",
            _lemmas_rebuild.returncode,
            _lemmas_rebuild.stderr[:300],
        )

    if not _validate_olean_freshness(run_paths.workspace_dir):
        _log.error("Olean freshness check failed after rebuild.")
        result = Phase04RunResult(
            status="fatal",
            problem_id=bundle.problem_id,
            run_root=run_paths.run_root,
            pinned_signatures_path=pinned_signatures_path,
            trusted_manifest_path=trusted_manifest_path,
            phase_summary_path=phase_summary_path,
            lemma_order=lemma_order,
            lemma_results=(),
            code_origin=code_origin,
            error_class="stale_olean",
            message=(
                "Orthos.Statements olean is stale after rebuild — lake build "
                "did not recompile the module. All downstream lemma proofs "
                "would fail with 'already declared' errors."
            ),
        )
        write_json(phase_summary_path, result.to_dict())
        return result

    pinned_by_decl_name = {sig.decl_name: sig for sig in pinned_map.values()}
    trusted_entries = derive_trusted_entries_from_lemmas_file(
        run_paths.workspace_dir / "Orthos" / "Lemmas.lean",
        pinned_by_decl_name=pinned_by_decl_name,
    )
    write_trusted_manifest(trusted_manifest_path, manifest_problem_id, trusted_entries)

    # Validate all lemma/pinned pairs up-front.
    for lemma_id in lemma_order:
        lemma = bundle.lemma_map.get(lemma_id)
        pinned = pinned_map.get(lemma_id)
        if lemma is None or pinned is None:
            result = _fatal_missing_lemma_result(
                run_paths=run_paths,
                bundle=bundle,
                pinned_signatures_path=pinned_signatures_path,
                trusted_manifest_path=trusted_manifest_path,
                phase_summary_path=phase_summary_path,
                lemma_order=lemma_order,
                lemma_results=[],
                lemma_id=lemma_id,
            )
            write_json(result.phase_summary_path, result.to_dict())
            return result

    lemma_results: list[LemmaFormalizationResult] = []
    execution_groups = (
        _lemma_execution_groups(
            bundle=bundle,
            lemma_order=lemma_order,
            lemma_workers=effective_lemma_workers,
        )
        if effective_lemma_workers > 1 and len(lemma_order) > 1
        else tuple((lemma_id,) for lemma_id in lemma_order)
    )
    _log.info(
        "Phase 04 proving %d lemma(s) across %d DAG layer(s) with up to %d worker(s).",
        len(lemma_order),
        len(execution_groups),
        effective_lemma_workers,
    )

    nproc = os.cpu_count() or 8
    worker_count = max(1, effective_lemma_workers)
    parallel_lake_jobs = max(2, nproc // worker_count)
    parallel_runtime_config = replace(runtime_config, lean=replace(runtime_config.lean, lake_jobs=parallel_lake_jobs))

    all_layer_results: dict[str, LemmaFormalizationResult] = {}
    merge_revision = len(trusted_entries)
    for layer_idx, layer_ids in enumerate(execution_groups, start=1):
        layer_results, merge_revision = _run_parallel_lemma_group(
            run_paths=run_paths,
            runtime_config=parallel_runtime_config,
            lemma_ids=layer_ids,
            bundle=bundle,
            pinned_map=pinned_map,
            pinned_by_decl_name=pinned_by_decl_name,
            pinned_decl_names=pinned_decl_names,
            trusted_manifest_path=trusted_manifest_path,
            manifest_problem_id=manifest_problem_id,
            max_attempts_per_lemma=max_attempts_per_lemma,
            timeout_seconds=timeout_seconds,
            lean_check_timeout_seconds=effective_lean_check_timeout,
            model=model,
            mock_candidates=mock_candidates or {},
            runner=runner,
            lemma_workers=worker_count if len(layer_ids) > 1 else 1,
            layer_index=layer_idx,
            starting_merge_revision=merge_revision,
            lean4_skills_refs=lean4_skills_refs,
            lean4_skills_refs_compact=lean4_skills_refs_compact,
            dependency_graph=dependency_graph,
            dependency_closure=dependency_closure,
            lemma_layer_index=lemma_layer_index,
        )
        all_layer_results.update(layer_results)
        layer_failed = next((layer_results[lid] for lid in layer_ids if layer_results[lid].status != "succeeded"), None)
        if layer_failed is not None:
            lemma_results = [all_layer_results[lid] for lid in lemma_order if lid in all_layer_results]
            result = Phase04RunResult(
                status="fatal",
                problem_id=bundle.problem_id,
                run_root=run_paths.run_root,
                pinned_signatures_path=pinned_signatures_path,
                trusted_manifest_path=trusted_manifest_path,
                phase_summary_path=phase_summary_path,
                lemma_order=lemma_order,
                lemma_results=tuple(lemma_results),
                code_origin=code_origin,
                workspace_revision=run_paths.run_name,
                merge_revision=merge_revision,
                verifier_revision=merge_revision,
                dependency_graph={key: list(value) for key, value in dependency_graph.items()},
                dependency_closure={key: list(value) for key, value in dependency_closure.items()},
                layer_order=[list(group) for group in execution_groups],
                error_class=layer_failed.error_class,
                message=layer_failed.message or "lemma formalization failed",
                failed_lemma_id=layer_failed.lemma_id,
            )
            write_json(phase_summary_path, result.to_dict())
            return result

    lemma_results = [all_layer_results[lid] for lid in lemma_order if lid in all_layer_results]

    _cleanup_scratch_files(run_paths.workspace_dir)

    result = Phase04RunResult(
        status="ok",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        pinned_signatures_path=pinned_signatures_path,
        trusted_manifest_path=trusted_manifest_path,
        phase_summary_path=phase_summary_path,
        lemma_order=lemma_order,
        lemma_results=tuple(lemma_results),
        code_origin=code_origin,
        workspace_revision=run_paths.run_name,
        merge_revision=merge_revision,
        verifier_revision=merge_revision,
        dependency_graph={key: list(value) for key, value in dependency_graph.items()},
        dependency_closure={key: list(value) for key, value in dependency_closure.items()},
        layer_order=[list(group) for group in execution_groups],
    )
    write_json(phase_summary_path, result.to_dict())
    return result


def _lemma_execution_groups(
    *,
    bundle: NormalizedProblemBundle,
    lemma_order: tuple[str, ...],
    lemma_workers: int,
) -> tuple[tuple[str, ...], ...]:
    if lemma_workers <= 1 or len(lemma_order) <= 1:
        return tuple((lemma_id,) for lemma_id in lemma_order)

    steps = list(bundle.selected_decomposition.assembly_plan.steps)
    if not steps:
        return tuple((lemma_id,) for lemma_id in lemma_order)

    step_index: dict[str, int] = {}
    for index, step in enumerate(steps):
        if not step.step_id or step.step_id in step_index:
            return tuple((lemma_id,) for lemma_id in lemma_order)
        step_index[step.step_id] = index

    adjacency: dict[str, list[str]] = {step.step_id: [] for step in steps}
    indegree: dict[str, int] = {step.step_id: 0 for step in steps}
    parents: dict[str, list[str]] = {step.step_id: [] for step in steps}
    for step in steps:
        for dependency in step.uses_prior_steps:
            if dependency not in adjacency:
                return tuple((lemma_id,) for lemma_id in lemma_order)
            adjacency[dependency].append(step.step_id)
            parents[step.step_id].append(dependency)
            indegree[step.step_id] += 1

    ready = sorted((step_id for step_id, degree in indegree.items() if degree == 0), key=lambda item: step_index[item])
    step_levels: dict[str, int] = {}
    ordered_steps: list[str] = []
    while ready:
        step_id = ready.pop(0)
        parent_levels = [step_levels[parent] for parent in parents[step_id]]
        step_levels[step_id] = (max(parent_levels) + 1) if parent_levels else 0
        ordered_steps.append(step_id)
        for child in adjacency[step_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda item: step_index[item])

    if len(ordered_steps) != len(steps):
        return tuple((lemma_id,) for lemma_id in lemma_order)

    step_by_id = {step.step_id: step for step in steps}
    lemma_set = set(lemma_order)
    lemma_levels: dict[str, int] = {}
    for step_id in ordered_steps:
        level = step_levels[step_id]
        step = step_by_id[step_id]
        for lemma_id in step.uses_lemmas:
            if lemma_id in lemma_set and lemma_id not in lemma_levels:
                lemma_levels[lemma_id] = level

    fallback_level = max(step_levels.values(), default=-1) + 1
    ordered_levels: list[tuple[str, int]] = []
    for lemma_id in lemma_order:
        level = lemma_levels.get(lemma_id)
        if level is None:
            level = fallback_level
            # All unreferenced lemmas share the same level → one parallel group
        ordered_levels.append((lemma_id, level))

    groups: list[tuple[str, ...]] = []
    current_group: list[str] = []
    current_level: int | None = None
    for lemma_id, level in ordered_levels:
        if current_group and current_level is not None and level != current_level:
            groups.append(tuple(current_group))
            current_group = [lemma_id]
            current_level = level
            continue
        if not current_group:
            current_level = level
        current_group.append(lemma_id)
    if current_group:
        groups.append(tuple(current_group))

    return tuple(groups)


def _run_parallel_lemma_group(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    lemma_ids: tuple[str, ...],
    bundle: NormalizedProblemBundle,
    pinned_map: dict[str, PinnedLemmaSignature],
    pinned_by_decl_name: dict[str, PinnedLemmaSignature],
    pinned_decl_names: set[str],
    trusted_manifest_path: Path,
    manifest_problem_id: str,
    max_attempts_per_lemma: int,
    timeout_seconds: int,
    lean_check_timeout_seconds: int | None,
    model: str | None,
    mock_candidates: dict[str, list[str]],
    runner: SubprocessRunner,
    lemma_workers: int = 1,
    layer_index: int = 1,
    starting_merge_revision: int = 0,
    lean4_skills_refs: str = "",
    lean4_skills_refs_compact: str = "",
    dependency_graph: dict[str, tuple[str, ...]] | None = None,
    dependency_closure: dict[str, tuple[str, ...]] | None = None,
    lemma_layer_index: dict[str, int] | None = None,
) -> tuple[dict[str, LemmaFormalizationResult], int]:
    worker_count = max(1, min(lemma_workers, len(lemma_ids)))
    batch_results: dict[str, LemmaFormalizationResult] = {}
    worker_run_paths: dict[str, RunPaths] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {}
        for lemma_id in lemma_ids:
            lemma = bundle.lemma_map[lemma_id]
            pinned = pinned_map[lemma_id]
            trusted_snapshot = derive_trusted_entries_from_lemmas_file(
                run_paths.workspace_dir / "Orthos" / "Lemmas.lean",
                pinned_by_decl_name=pinned_by_decl_name,
            )
            resolved_ids = {entry.lemma_id for entry in trusted_snapshot}
            declared_dependencies = tuple((dependency_graph or {}).get(lemma_id, ()))
            closure_dependencies = tuple((dependency_closure or {}).get(lemma_id, declared_dependencies))
            unresolved_dependencies = tuple(dep for dep in declared_dependencies if dep not in resolved_ids)
            statements_by_lemma = {
                dep: pinned_map[dep].signature
                for dep in closure_dependencies
                if dep in pinned_map
            }
            assumption_capsule = AssumptionCapsule(
                lemma_id=lemma_id,
                direct_predecessors=declared_dependencies,
                transitive_predecessors=closure_dependencies,
                resolved_predecessors=tuple(dep for dep in declared_dependencies if dep in resolved_ids),
                unresolved_predecessors=unresolved_dependencies,
                statements_by_lemma=statements_by_lemma,
                dependency_source_revision=f"{run_paths.run_name}:layer_{layer_index:02d}",
                authoritative_workspace_revision=run_paths.run_name,
                worker_workspace_revision=f"{run_paths.run_name}:worker:{lemma_id}",
            )
            worker_paths = _create_worker_run_paths(
                authoritative_run_paths=run_paths,
                runtime_config=runtime_config,
                lemma_id=lemma_id,
                layer_index=layer_index,
            )
            worker_run_paths[lemma_id] = worker_paths
            worker_manifest_path = worker_paths.run_root / "workers" / f"layer_{layer_index:02d}" / lemma_id_to_path_token(lemma_id) / "trusted_context_manifest.json"
            write_trusted_manifest(worker_manifest_path, manifest_problem_id, trusted_snapshot)
            future = executor.submit(
                run_lemma_formalization,
                run_paths=worker_paths,
                runtime_config=runtime_config,
                lemma=lemma,
                pinned=pinned,
                trusted_entries=trusted_snapshot,
                assumption_capsule=assumption_capsule,
                manifest_path=worker_manifest_path,
                manifest_problem_id=manifest_problem_id,
                max_attempts=max_attempts_per_lemma,
                timeout_seconds=timeout_seconds,
                lean_check_timeout_seconds=lean_check_timeout_seconds,
                model=model,
                pinned_decl_names=pinned_decl_names,
                runner=runner,
                mock_candidates=mock_candidates.get(lemma_id),
                commit_on_success=False,
                lean4_skills_refs=lean4_skills_refs,
                lean4_skills_refs_compact=lean4_skills_refs_compact,
            )
            futures[future] = lemma_id

        for future in as_completed(futures):
            lemma_id = futures[future]
            batch_results[lemma_id] = future.result()

    committed_results: dict[str, LemmaFormalizationResult] = {}
    seen_failure = False
    merge_revision = starting_merge_revision
    code_origin = current_code_origin()
    for lemma_id in lemma_ids:
        lemma_result = replace(
            batch_results[lemma_id],
            layer_index=layer_index,
            worker_workspace=worker_run_paths[lemma_id].workspace_dir,
            code_origin=code_origin,
        )
        if seen_failure:
            committed_results[lemma_id] = lemma_result
            continue
        if lemma_result.status != "proved_under_assumptions":
            committed_results[lemma_id] = lemma_result
            seen_failure = True
            continue
        unresolved_predecessors = [
            dependency
            for dependency in (dependency_graph or {}).get(lemma_id, ())
            if dependency not in {
                result.lemma_id
                for result in committed_results.values()
                if result.status == "succeeded"
            } and dependency not in {
                entry.lemma_id
                for entry in derive_trusted_entries_from_lemmas_file(
                    run_paths.workspace_dir / "Orthos" / "Lemmas.lean",
                    pinned_by_decl_name=pinned_by_decl_name,
                )
            }
        ]
        if unresolved_predecessors:
            blocked = replace(
                lemma_result,
                status="blocked_on_predecessors",
                terminal=False,
                message=(
                    "lemma verified provisionally but cannot merge until predecessors are merged: "
                    + ", ".join(unresolved_predecessors)
                ),
            )
            write_json(blocked.result_path, blocked.to_dict())
            committed_results[lemma_id] = blocked
            seen_failure = True
            continue
        committed = _commit_parallel_lemma_result(
            run_paths=run_paths,
            runtime_config=runtime_config,
            lemma_result=lemma_result,
            pinned=pinned_map[lemma_id],
            pinned_by_decl_name=pinned_by_decl_name,
            trusted_manifest_path=trusted_manifest_path,
            manifest_problem_id=manifest_problem_id,
            timeout_seconds=timeout_seconds,
            layer_index=layer_index,
            next_merge_revision=merge_revision + 1,
            runner=runner,
        )
        committed_results[lemma_id] = committed
        if committed.status != "succeeded":
            seen_failure = True
            continue
        merge_revision += 1
    return committed_results, merge_revision


def _create_worker_run_paths(
    *,
    authoritative_run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    lemma_id: str,
    layer_index: int,
) -> RunPaths:
    lemma_token = lemma_id_to_path_token(lemma_id)
    worker_root = authoritative_run_paths.run_root / "workers" / f"layer_{layer_index:02d}" / lemma_token
    worker_workspace = worker_root / "workspace"
    clone_workspace(
        authoritative_run_paths.workspace_dir,
        worker_workspace,
        mode=runtime_config.phase04.worker_snapshot_mode,
    )
    prompts_dir = worker_root / "prompts"
    claude_raw_dir = worker_root / "claude_raw"
    diagnostics_dir = worker_root / "diagnostics"
    summaries_dir = worker_root / "summaries"
    for directory in (prompts_dir, claude_raw_dir, diagnostics_dir, summaries_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if runtime_config.mcp.enabled:
        write_project_mcp_config(
            worker_workspace,
            runtime_config,
            mcp_log_dir=worker_root / ".mcp_logs",
        )
    runtime_config_path = worker_root / "runtime_config.json"
    write_json(runtime_config_path, runtime_config.to_dict())
    workspace_snapshot_path = worker_root / "workspace_snapshot_pre_phase.json"
    write_json(workspace_snapshot_path, snapshot_workspace(worker_workspace))
    return replace(
        authoritative_run_paths,
        workspace_dir=worker_workspace,
        prompts_dir=prompts_dir,
        claude_raw_dir=claude_raw_dir,
        diagnostics_dir=diagnostics_dir,
        summaries_dir=summaries_dir,
        runtime_config_path=runtime_config_path,
        workspace_snapshot_path=workspace_snapshot_path,
    )


def _create_merge_validation_workspace(
    *,
    authoritative_run_paths: RunPaths,
    lemma_id: str,
    layer_index: int,
    runtime_config: RuntimeConfig | None,
) -> Path:
    validation_root = authoritative_run_paths.run_root / "merge_validation" / f"layer_{layer_index:02d}" / lemma_id_to_path_token(lemma_id)
    validation_workspace = validation_root / "workspace"
    if validation_root.exists():
        shutil.rmtree(validation_root, ignore_errors=True)
    if runtime_config is None:
        runtime_config = load_runtime_config(authoritative_run_paths.runtime_config_path)
    clone_workspace(
        authoritative_run_paths.workspace_dir,
        validation_workspace,
        mode=runtime_config.phase04.worker_snapshot_mode,
    )
    return validation_workspace


def _commit_parallel_lemma_result(
    *,
    run_paths: RunPaths,
    runtime_config: RuntimeConfig,
    lemma_result: LemmaFormalizationResult,
    pinned: PinnedLemmaSignature,
    pinned_by_decl_name: dict[str, PinnedLemmaSignature],
    trusted_manifest_path: Path,
    manifest_problem_id: str,
    timeout_seconds: int,
    layer_index: int,
    next_merge_revision: int,
    runner: SubprocessRunner,
) -> LemmaFormalizationResult:
    if lemma_result.verified_candidate_path is None or not lemma_result.verified_candidate_path.exists():
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text="Parallel commit failed: missing provisional verified declaration artifact.",
        )

    declaration = lemma_result.verified_candidate_path.read_text(encoding="utf-8")
    if re.search(r"(?m)^\\s*(?:private\\s+|protected\\s+)?axiom\\b", declaration):
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text="Parallel commit failed: merged output must not contain dependency axioms.",
        )
    validation_workspace = _create_merge_validation_workspace(
        authoritative_run_paths=run_paths,
        lemma_id=lemma_result.lemma_id,
        layer_index=layer_index,
        runtime_config=runtime_config,
    )
    validation_lemmas_path = validation_workspace / "Orthos" / "Lemmas.lean"
    validation_original_text = validation_lemmas_path.read_text(encoding="utf-8")
    try:
        validation_merged_text = merge_declaration_into_lemmas_file(
            original_text=validation_original_text,
            declaration_block=declaration,
        )
    except ValueError as exc:
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text=f"Parallel commit failed: {exc}",
        )
    write_text(validation_lemmas_path, validation_merged_text)
    if _contains_disallowed_proof_tokens(validation_merged_text):
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text="policy_violation: disallowed token `sorry` or `admit` in merged Lemmas.lean.",
        )
    validation_check = rebuild_module_olean(
        validation_workspace,
        "Orthos/Lemmas.lean",
        "Orthos.Lemmas",
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if not validation_check.ok:
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text=validation_check.stderr or "authoritative merge replay failed",
        )
    validation_warnings = _validate_workspace_integrity(validation_workspace)
    if any("Duplicate declarations" in warning or "Unresolved assumption stubs" in warning for warning in validation_warnings):
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text="authoritative merge replay failed workspace integrity checks",
        )

    lemmas_file_path = run_paths.workspace_dir / "Orthos" / "Lemmas.lean"
    original_text = lemmas_file_path.read_text(encoding="utf-8")
    try:
        merged_text = merge_declaration_into_lemmas_file(
            original_text=original_text,
            declaration_block=declaration,
        )
    except ValueError as exc:
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text=f"Parallel commit failed: {exc}",
        )

    write_text(lemmas_file_path, merged_text)
    if _contains_disallowed_proof_tokens(merged_text):
        write_text(lemmas_file_path, original_text)
        diagnostics_text = "policy_violation: disallowed token `sorry` or `admit` in merged Lemmas.lean."
        return _replace_with_commit_failure(lemma_result, diagnostics_text=diagnostics_text)
    authoritative_check = rebuild_module_olean(
        run_paths.workspace_dir,
        "Orthos/Lemmas.lean",
        "Orthos.Lemmas",
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if not authoritative_check.ok:
        write_text(lemmas_file_path, original_text)
        rebuild_module_olean(
            run_paths.workspace_dir,
            "Orthos/Lemmas.lean",
            "Orthos.Lemmas",
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text=authoritative_check.stderr or "authoritative merge check failed",
        )
    authoritative_warnings = _validate_workspace_integrity(run_paths.workspace_dir)
    if any("Duplicate declarations" in warning or "Unresolved assumption stubs" in warning for warning in authoritative_warnings):
        write_text(lemmas_file_path, original_text)
        rebuild_module_olean(
            run_paths.workspace_dir,
            "Orthos/Lemmas.lean",
            "Orthos.Lemmas",
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        return _replace_with_commit_failure(
            lemma_result,
            diagnostics_text="authoritative merge created invalid workspace state",
        )
    trusted_entries = derive_trusted_entries_from_lemmas_file(
        lemmas_file_path,
        pinned_by_decl_name=pinned_by_decl_name,
    )
    write_trusted_manifest(trusted_manifest_path, manifest_problem_id, trusted_entries)
    merged_authoritative_path = lemma_result.lemma_artifact_dir / "merged_authoritative.lean"
    write_text(merged_authoritative_path, declaration.strip() + "\n")
    committed = replace(
        lemma_result,
        status="succeeded",
        terminal=True,
        merged_authoritative_path=merged_authoritative_path,
        resolved_dependencies=lemma_result.declared_dependencies,
        merge_revision=next_merge_revision,
        verifier_revision=next_merge_revision,
        merge_replay_result={
            "status": "passed",
            "workspace": str(validation_workspace),
            "merge_revision": next_merge_revision,
            "validation_check": validation_check.to_dict(),
            "authoritative_check": authoritative_check.to_dict(),
        },
    )
    write_json(committed.result_path, committed.to_dict())
    return committed


def _replace_with_commit_failure(
    lemma_result: LemmaFormalizationResult,
    *,
    diagnostics_text: str,
) -> LemmaFormalizationResult:
    error_class = classify_lemma_failure(diagnostics_text)
    replaced = replace(
        lemma_result,
        status="failed",
        terminal=True,
        error_class=error_class,
        message="Lemma candidate could not be committed to shared trusted context.",
        diagnostics=tuple(_diagnostic_lines(diagnostics_text)),
        merged_authoritative_path=None,
        merge_replay_result={"status": "failed", "diagnostics": _diagnostic_lines(diagnostics_text)},
    )
    write_json(replaced.result_path, replaced.to_dict())
    return replaced


def _contains_disallowed_proof_tokens(text: str) -> bool:
    return bool(re.search(r"\b(sorry|admit)\b", text))


def _diagnostic_lines(text: str, *, limit: int = 8) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ["no diagnostics emitted"]
    return lines[:limit]


def load_mock_candidates_dir(path: Path) -> dict[str, list[str]]:
    root = path.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"mock candidates directory does not exist: {root}")

    result: dict[str, list[str]] = {}
    for lemma_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name):
        lemma_id = lemma_dir.name
        round_files = sorted(
            (item for item in lemma_dir.iterdir() if item.is_file() and item.suffix == ".lean"),
            key=lambda item: item.name,
        )
        ordered: list[tuple[int, str]] = []
        for file_path in round_files:
            round_index = _extract_round_index(file_path.name)
            if round_index is None:
                continue
            ordered.append((round_index, file_path.read_text(encoding="utf-8")))
        if not ordered:
            continue
        ordered.sort(key=lambda item: item[0])
        result[lemma_id] = [text for _, text in ordered]
    return result


def _deterministic_lemma_order(bundle: NormalizedProblemBundle) -> list[str]:
    if bundle.topologically_sorted_lemma_ids:
        return list(bundle.topologically_sorted_lemma_ids)
    return [lemma.lemma_id for lemma in bundle.lemmas]


def _phase04_summary_path(
    *,
    run_paths: RunPaths,
    lemma_order: tuple[str, ...],
    target_lemma_id: str | None,
    target_lemma_ids: set[str] | None,
) -> Path:
    if target_lemma_id is None and target_lemma_ids is None:
        return run_paths.summaries_dir / "phase04_summary.json"
    if len(lemma_order) == 1:
        return run_paths.summaries_dir / f"phase04_{lemma_id_to_path_token(lemma_order[0])}.json"
    digest_input = ",".join(sorted(lemma_order))
    digest = hashlib.sha1(digest_input.encode("utf-8")).hexdigest()[:10]
    return run_paths.summaries_dir / f"phase04_subset_{digest}.json"


def build_dependency_graph(
    *,
    bundle: NormalizedProblemBundle,
    lemma_order: tuple[str, ...],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]], dict[str, int]]:
    lemma_set = set(lemma_order)
    graph: dict[str, set[str]] = {lemma_id: set() for lemma_id in lemma_order}
    layer_index: dict[str, int] = {lemma_id: 0 for lemma_id in lemma_order}

    for lemma_id in lemma_order:
        lemma = bundle.lemma_map.get(lemma_id)
        if lemma is None:
            continue
        graph[lemma_id].update(dep for dep in lemma.depends_on if dep in lemma_set)
        if lemma.layer_index is not None:
            layer_index[lemma_id] = lemma.layer_index

    steps = list(bundle.selected_decomposition.assembly_plan.steps)
    step_by_id = {step.step_id: step for step in steps if step.step_id}
    step_levels: dict[str, int] = {}
    for step in steps:
        if not step.step_id:
            continue
        parents = [parent for parent in step.uses_prior_steps if parent in step_by_id]
        parent_levels = [step_levels.get(parent, 0) for parent in parents]
        step_levels[step.step_id] = (max(parent_levels) + 1) if parent_levels else 0
        predecessor_lemmas: set[str] = set()
        for parent in parents:
            predecessor_lemmas.update(
                lemma_id for lemma_id in step_by_id[parent].uses_lemmas if lemma_id in lemma_set
            )
        for lemma_id in step.uses_lemmas:
            if lemma_id not in lemma_set:
                continue
            graph[lemma_id].update(dep for dep in predecessor_lemmas if dep != lemma_id)
            layer_index[lemma_id] = max(layer_index.get(lemma_id, 0), step_levels[step.step_id])

    unknown_dependencies = {
        lemma_id: sorted(dep for dep in deps if dep not in bundle.lemma_map)
        for lemma_id, deps in graph.items()
        if any(dep not in bundle.lemma_map for dep in deps)
    }
    if unknown_dependencies:
        raise ValueError(f"phase04 dependency graph references unknown lemmas: {unknown_dependencies}")

    closure: dict[str, tuple[str, ...]] = {}
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(lemma_id: str) -> tuple[str, ...]:
        if lemma_id in closure:
            return closure[lemma_id]
        if lemma_id in visiting:
            raise ValueError(f"phase04 dependency graph has a cycle involving `{lemma_id}`")
        visiting.add(lemma_id)
        expanded: list[str] = []
        seen: set[str] = set()
        for dep in sorted(graph[lemma_id], key=lambda item: lemma_order.index(item) if item in lemma_order else item):
            if dep not in seen:
                expanded.append(dep)
                seen.add(dep)
            for transitive in _visit(dep):
                if transitive not in seen:
                    expanded.append(transitive)
                    seen.add(transitive)
        visiting.remove(lemma_id)
        visited.add(lemma_id)
        closure[lemma_id] = tuple(expanded)
        return closure[lemma_id]

    for lemma_id in lemma_order:
        _visit(lemma_id)

    return (
        {lemma_id: tuple(sorted(graph[lemma_id], key=lambda item: lemma_order.index(item))) for lemma_id in lemma_order},
        closure,
        layer_index,
    )


def _extract_round_index(filename: str) -> int | None:
    match = re.search(r"round[_-]?(\d+)", filename)
    if match is None:
        return None
    return int(match.group(1))


_DECL_START_RE = re.compile(
    r"^\s*(?:noncomputable\s+)?(?:private\s+|protected\s+)?(axiom|theorem|lemma|def)\b"
)
_DECL_NEUTRALIZE_RE = re.compile(
    r"^\s*(?:noncomputable\s+)?(?:private\s+|protected\s+)?(axiom|theorem|lemma)\b"
)
_DECL_NAME_RE = re.compile(
    r"^\s*(?:noncomputable\s+)?(?:private\s+|protected\s+)?(?:axiom|theorem|lemma|def)\s+(\S+)"
)


def _neutralize_statements_for_proving(workspace_dir: Path, pinned_decl_names: set[str]) -> None:
    """Comment out only *pinned* axiom/theorem/lemma blocks in Statements.lean.

    Only declarations whose names appear in ``pinned_decl_names`` (the lemma
    and root declarations that will be re-declared as theorems in scratch files
    / Lemmas.lean) are neutralized.  Helper axioms (e.g. ``axiom f_count``)
    and all defs are preserved so that downstream declarations that depend on
    them continue to compile.

    A declaration block starts with a keyword line and continues through all
    non-blank lines until a blank line or the start of another top-level
    declaration (``def``, ``axiom``, ``theorem``, ``lemma``).  Comment lines
    and doc comments embedded within the declaration body are also neutralized
    to avoid leaving dangling expressions after the commented-out signature.

    Doc comments (``/-- ... -/``) that immediately precede a neutralized
    declaration are neutralized together with it.  In Lean 4, a doc comment
    **must** be followed by a declaration — leaving it orphaned is a parse error.
    Doc comments preceding preserved declarations are kept as-is.
    """
    statements_path = workspace_dir / "Orthos" / "Statements.lean"
    if not statements_path.exists():
        return
    original = statements_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    neutralized: list[str] = []
    inside_decl = False
    # Buffer for doc-comment lines that precede a declaration.  We hold them
    # until we see what declaration follows: pinned axiom → neutralize, else → keep.
    doc_comment_buffer: list[str] = []
    inside_doc_comment = False  # True while accumulating a multi-line /-- ... -/

    for line in lines:
        stripped = line.strip()

        # --- Doc-comment buffering ---
        # A doc comment starts with /-- and ends with -/ (possibly same line).
        if not inside_decl and not inside_doc_comment and stripped.startswith("/--"):
            inside_doc_comment = True
            doc_comment_buffer.append(line)
            if "-/" in stripped[3:]:  # closed on same line
                inside_doc_comment = False
            continue
        if inside_doc_comment:
            doc_comment_buffer.append(line)
            if "-/" in stripped:
                inside_doc_comment = False
            continue

        # --- Start of a new declaration block ---
        if _DECL_START_RE.match(line):
            name_match = _DECL_NAME_RE.match(line)
            decl_name = name_match.group(1) if name_match else ""
            should_neutralize = (
                _DECL_NEUTRALIZE_RE.match(line) is not None
                and decl_name in pinned_decl_names
            )
            if should_neutralize:
                # Pinned axiom/theorem/lemma — neutralize doc comment buffer + declaration
                for buf_line in doc_comment_buffer:
                    neutralized.append(f"-- [pinned] {buf_line}")
                doc_comment_buffer.clear()
                inside_decl = True
                neutralized.append(f"-- [pinned] {line}")
                continue
            else:
                # def, or non-pinned axiom — flush doc comment buffer as-is, preserve
                neutralized.extend(doc_comment_buffer)
                doc_comment_buffer.clear()
                inside_decl = False
                neutralized.append(line)
                continue

        # --- Inside a neutralized declaration ---
        if inside_decl:
            if stripped:
                neutralized.append(f"-- [pinned] {line}")
                continue
            # Blank line ends the declaration block.
            inside_decl = False

        # If we reach a non-declaration line with a pending doc comment buffer
        # (e.g. section comments, blank lines, etc.), flush it as-is.
        if doc_comment_buffer:
            neutralized.extend(doc_comment_buffer)
            doc_comment_buffer.clear()

        neutralized.append(line)

    # Flush any remaining buffered doc comments at end of file.
    if doc_comment_buffer:
        neutralized.extend(doc_comment_buffer)

    statements_path.write_text("\n".join(neutralized) + "\n", encoding="utf-8")
    _log.info(
        "Neutralized %d pinned declarations in Statements.lean (helper axioms/defs preserved).",
        len(pinned_decl_names),
    )


def _reset_lemmas_file(workspace_dir: Path) -> None:
    """Reset Lemmas.lean to its clean template state.

    Phase 03 runs Claude with MCP workspace access, which may modify Lemmas.lean
    (e.g., adding ``import Orthos.Statements``). This causes axiom/theorem name
    conflicts in Phase 04. Resetting to the template state ensures a clean start.

    Imports ``Orthos.Statements`` so that definitions (``f``, ``G``, ``b``, etc.)
    are available to proven lemma declarations and scratch files.
    """
    lemmas_path = workspace_dir / "Orthos" / "Lemmas.lean"
    lemmas_path.write_text(
        "import Mathlib\n"
        "import Orthos.Statements\n\n"
        "-- Proven lemma declarations accumulate below.\n\n"
        "-- END LEMMAS\n",
        encoding="utf-8",
    )
    _log.info("Reset Orthos/Lemmas.lean to clean template state (imports Statements).")


def _rebuild_workspace_after_reset(
    workspace_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Rebuild .olean files after resetting Lemmas.lean and neutralizing Statements.lean.

    Without this, ``lake env lean`` on scratch files that ``import Orthos.Lemmas``
    will fail because the .olean is stale or missing.
    """
    run = runner or subprocess.run
    result = run(
        ["lake", "build"],
        cwd=str(workspace_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        _log.warning(
            "Workspace rebuild after reset exited %d: %s",
            result.returncode,
            (result.stderr or result.stdout or "")[:500],
        )
    else:
        _log.info("Rebuilt workspace .olean files after Phase 04 reset.")


def _validate_workspace_integrity(workspace_dir: Path) -> list[str]:
    """Validate and repair workspace files after Phase 03.

    Returns a list of diagnostic warnings for any repairs performed.
    """
    warnings: list[str] = []

    orthos_root = workspace_dir / "Orthos.lean"
    if orthos_root.exists():
        content = orthos_root.read_text(encoding="utf-8")
        if "import Orthos.Statements" in content:
            repaired = content.replace("import Orthos.Statements\n", "")
            orthos_root.write_text(repaired, encoding="utf-8")
            warnings.append("Removed unexpected 'import Orthos.Statements' from Orthos.lean")

    root_lean = workspace_dir / "Orthos" / "Root.lean"
    if root_lean.exists():
        content = root_lean.read_text(encoding="utf-8")
        if "import Orthos.Statements" in content:
            repaired = content.replace("import Orthos.Statements\n", "")
            root_lean.write_text(repaired, encoding="utf-8")
            warnings.append("Removed unexpected 'import Orthos.Statements' from Root.lean")

    lemmas_path = workspace_dir / "Orthos" / "Lemmas.lean"
    if lemmas_path.exists():
        content = lemmas_path.read_text(encoding="utf-8")
        decl_names: list[str] = []
        for match in re.finditer(
            r"(?m)^\\s*(?:private\\s+|protected\\s+)?(?:noncomputable\\s+)?(?:theorem|lemma|axiom|def|abbrev)\\s+([A-Za-z0-9_'.]+)\\b",
            content,
        ):
            decl_names.append(match.group(1))
        duplicates = sorted({name for name in decl_names if decl_names.count(name) > 1})
        if duplicates:
            warnings.append(f"Duplicate declarations detected in Orthos/Lemmas.lean: {', '.join(duplicates)}")
        if re.search(r"(?m)^\\s*(?:private\\s+|protected\\s+)?axiom\\b", content):
            warnings.append("Unresolved assumption stubs detected in Orthos/Lemmas.lean")

    for warning in warnings:
        _log.warning("Workspace integrity: %s", warning)
    return warnings


def _purge_module_build_artifacts(workspace_dir: Path, module_path: str) -> list[str]:
    """Delete all build artifacts for a module to force Lake to rebuild from source.

    Args:
        workspace_dir: Root of the Lean workspace.
        module_path: Slash-separated module path, e.g. ``"Orthos/Statements"``.

    Returns:
        List of deleted file paths (for logging).
    """
    lake_build = workspace_dir / ".lake" / "build"
    suffixes = [
        f"lib/lean/{module_path}.olean",
        f"lib/lean/{module_path}.ilean",
        f"lib/lean/{module_path}.olean.hash",
        f"lib/lean/{module_path}.ilean.hash",
        f"lib/lean/{module_path}.trace",
        f"ir/{module_path}.c",
        f"ir/{module_path}.c.hash",
        f"ir/{module_path}.setup.json",
    ]
    deleted: list[str] = []
    for suffix in suffixes:
        artifact = lake_build / suffix
        if artifact.exists():
            artifact.unlink()
            deleted.append(str(artifact))
    return deleted


def _validate_olean_freshness(workspace_dir: Path) -> bool:
    """Check that the Orthos.Statements olean is at least as recent as the source."""
    source = workspace_dir / "Orthos" / "Statements.lean"
    olean = workspace_dir / ".lake" / "build" / "lib" / "lean" / "Orthos" / "Statements.olean"
    if not source.exists():
        return True
    if not olean.exists():
        return False
    return olean.stat().st_mtime >= source.stat().st_mtime


def _cleanup_scratch_files(workspace_dir: Path) -> int:
    """Remove scratch files so they don't interfere with lake build."""
    count = 0
    for scratch in workspace_dir.glob("Orthos/Scratch_*.lean"):
        scratch.unlink()
        count += 1
    if count:
        _log.info("Cleaned up %d scratch file(s) from workspace.", count)
    return count


def _fatal_missing_lemma_result(
    *,
    run_paths: RunPaths,
    bundle: NormalizedProblemBundle,
    pinned_signatures_path: Path,
    trusted_manifest_path: Path,
    phase_summary_path: Path,
    lemma_order: tuple[str, ...],
    lemma_results: list[LemmaFormalizationResult],
    lemma_id: str,
) -> Phase04RunResult:
    return Phase04RunResult(
        status="fatal",
        problem_id=bundle.problem_id,
        run_root=run_paths.run_root,
        pinned_signatures_path=pinned_signatures_path,
        trusted_manifest_path=trusted_manifest_path,
        phase_summary_path=phase_summary_path,
        lemma_order=lemma_order,
        lemma_results=tuple(lemma_results),
        error_class="malformed_input_artifact",
        message=f"phase04 input mismatch: missing lemma or pinned signature for `{lemma_id}`",
        failed_lemma_id=lemma_id,
    )
