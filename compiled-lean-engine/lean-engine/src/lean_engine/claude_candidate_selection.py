from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .claude_runner import ClaudeRunTrace


StageValidator = Callable[[str], tuple[bool, str | None]]
Normalizer = Callable[[str], str]


@dataclass(frozen=True)
class CandidateSelection:
    text: str
    source: str
    reasons: tuple[str, ...]
    lean_score: int

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "source": self.source,
            "reasons": list(self.reasons),
            "lean_score": self.lean_score,
        }


def select_lean_candidate(
    *,
    trace: ClaudeRunTrace | None,
    target_path: Path | None,
    baseline_text: str,
    normalize_text: Normalizer,
    stage_validator: StageValidator | None = None,
    prefer_workspace_fallback: bool = True,
) -> CandidateSelection:
    reasons: list[str] = []
    candidates: list[tuple[str, str]] = []
    resolved_trace = trace.with_target_file(target_path) if trace is not None else None

    # Workspace file on disk is the ground truth — it reflects ALL of Claude's
    # edits (both Write and Edit tools) and is what the MCP diagnostics checked.
    # Prioritize it over tool_update, which only captures Write events and can
    # be stale when Claude uses Edit after Write.
    workspace_fallback = _load_workspace_fallback_candidate(
        target_path=target_path,
        baseline_text=baseline_text,
        normalize_text=normalize_text,
    )
    if prefer_workspace_fallback and workspace_fallback:
        candidates.append(("workspace_fallback", workspace_fallback))

    if resolved_trace is not None and resolved_trace.target_file_latest_update is not None:
        candidates.append(("tool_update", resolved_trace.target_file_latest_update))
    if resolved_trace is not None and resolved_trace.result_text.strip():
        candidates.append(("result_text", resolved_trace.result_text))
    if resolved_trace is not None and resolved_trace.assistant_text_chunks:
        assistant_text = "\n\n".join(resolved_trace.assistant_text_chunks).strip()
        if assistant_text:
            candidates.append(("assistant_text", assistant_text))
    if not prefer_workspace_fallback and workspace_fallback:
        candidates.append(("workspace_fallback", workspace_fallback))

    # Raw workspace file as last resort — Claude may have written a valid proof
    # via MCP tool_update that the normalization pipeline failed to extract.
    if target_path is not None and target_path.exists():
        try:
            raw_workspace = target_path.read_text(encoding="utf-8")
            if raw_workspace.strip() and raw_workspace.strip() != baseline_text.strip():
                candidates.append(("workspace_raw", raw_workspace))
        except OSError:
            pass

    for source, raw in candidates:
        selected = _evaluate_candidate(
            source=source,
            raw_text=raw,
            normalize_text=normalize_text,
            stage_validator=stage_validator,
            reasons=reasons,
        )
        if selected is not None:
            return selected

    if not reasons:
        reasons.append("no candidate sources were available")
    return CandidateSelection(text="", source="none", reasons=tuple(reasons), lean_score=0)


def _evaluate_candidate(
    *,
    source: str,
    raw_text: str,
    normalize_text: Normalizer,
    stage_validator: StageValidator | None,
    reasons: list[str],
) -> CandidateSelection | None:
    if not raw_text.strip():
        reasons.append(f"{source}: rejected empty candidate")
        return None

    normalized = normalize_text(raw_text)
    if not normalized.strip():
        reasons.append(f"{source}: rejected after normalization (empty)")
        return None

    lean_score = _lean_likeness_score(normalized)
    if lean_score <= 0:
        reasons.append(f"{source}: rejected as non-lean-like (score={lean_score})")
        return None

    if stage_validator is not None:
        ok, details = stage_validator(normalized)
        if not ok:
            detail_text = f": {details}" if details else ""
            reasons.append(f"{source}: rejected by stage validator{detail_text}")
            return None

    reasons.append(f"{source}: selected (lean_score={lean_score})")
    return CandidateSelection(
        text=normalized,
        source=source,
        reasons=tuple(reasons),
        lean_score=lean_score,
    )


def _load_workspace_fallback_candidate(
    *,
    target_path: Path | None,
    baseline_text: str,
    normalize_text: Normalizer,
) -> str:
    if target_path is None or not target_path.exists():
        return ""
    try:
        current_text = target_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    baseline_norm = normalize_text(baseline_text).strip()
    current_norm = normalize_text(current_text).strip()
    if not current_norm:
        return ""
    if current_norm == baseline_norm:
        return ""
    return current_norm + "\n"


def _lean_likeness_score(text: str) -> int:
    # Shared implementation with statement_phase._lean_likeness_score.
    from .statement_phase import _lean_likeness_score as _score
    return _score(text)
