from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUN_DIR_PATTERN = re.compile(r"^run_(\d+)$")


@dataclass(frozen=True)
class RunPaths:
    problem_id: str
    problem_dir: Path
    run_name: str
    run_number: int
    run_root: Path
    normalized_problem_path: Path
    workspace_dir: Path
    prompts_dir: Path
    claude_raw_dir: Path
    diagnostics_dir: Path
    summaries_dir: Path
    runtime_config_path: Path
    workspace_snapshot_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "problem_dir": str(self.problem_dir),
            "run_name": self.run_name,
            "run_number": self.run_number,
            "run_root": str(self.run_root),
            "normalized_problem_path": str(self.normalized_problem_path),
            "workspace_dir": str(self.workspace_dir),
            "prompts_dir": str(self.prompts_dir),
            "claude_raw_dir": str(self.claude_raw_dir),
            "diagnostics_dir": str(self.diagnostics_dir),
            "summaries_dir": str(self.summaries_dir),
            "runtime_config_path": str(self.runtime_config_path),
            "workspace_snapshot_path": str(self.workspace_snapshot_path),
        }


def create_run_paths(
    problem_id: str,
    *,
    artifacts_root: Path = Path(".artifacts/lean_engine"),
    source_label: str | None = None,
) -> RunPaths:
    sanitized_problem_id = sanitize_component(problem_id)
    if source_label:
        sanitized_label = sanitize_component(source_label)
        dir_name = f"{sanitized_label}__{sanitized_problem_id}"
    else:
        dir_name = sanitized_problem_id
    root = artifacts_root.expanduser().resolve()
    problem_dir = root / dir_name
    problem_dir.mkdir(parents=True, exist_ok=True)

    next_run_number = _next_run_number(problem_dir)
    run_name = f"run_{next_run_number:03d}"
    run_root = problem_dir / run_name
    run_root.mkdir(parents=True, exist_ok=False)

    workspace_dir = run_root / "workspace"
    prompts_dir = run_root / "prompts"
    claude_raw_dir = run_root / "claude_raw"
    diagnostics_dir = run_root / "diagnostics"
    summaries_dir = run_root / "summaries"

    for directory in (workspace_dir, prompts_dir, claude_raw_dir, diagnostics_dir, summaries_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return RunPaths(
        problem_id=sanitized_problem_id,
        problem_dir=problem_dir,
        run_name=run_name,
        run_number=next_run_number,
        run_root=run_root,
        normalized_problem_path=run_root / "normalized_problem.json",
        workspace_dir=workspace_dir,
        prompts_dir=prompts_dir,
        claude_raw_dir=claude_raw_dir,
        diagnostics_dir=diagnostics_dir,
        summaries_dir=summaries_dir,
        runtime_config_path=run_root / "runtime_config.json",
        workspace_snapshot_path=run_root / "workspace_snapshot_pre_phase.json",
    )


def load_run_paths(run_root: Path) -> RunPaths:
    resolved = run_root.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"run directory does not exist: {resolved}")

    run_name = resolved.name
    run_match = RUN_DIR_PATTERN.match(run_name)
    if run_match is None:
        raise ValueError(f"run directory must be named run_xxx: {resolved}")
    run_number = int(run_match.group(1))

    problem_dir = resolved.parent
    return RunPaths(
        problem_id=problem_dir.name,
        problem_dir=problem_dir,
        run_name=run_name,
        run_number=run_number,
        run_root=resolved,
        normalized_problem_path=resolved / "normalized_problem.json",
        workspace_dir=resolved / "workspace",
        prompts_dir=resolved / "prompts",
        claude_raw_dir=resolved / "claude_raw",
        diagnostics_dir=resolved / "diagnostics",
        summaries_dir=resolved / "summaries",
        runtime_config_path=resolved / "runtime_config.json",
        workspace_snapshot_path=resolved / "workspace_snapshot_pre_phase.json",
    )


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def workspace_file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sanitize_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._")
    return cleaned or "unknown_problem"


def _next_run_number(problem_dir: Path) -> int:
    max_run = 0
    for child in sorted(problem_dir.iterdir(), key=lambda item: item.name):
        match = RUN_DIR_PATTERN.match(child.name)
        if match is None:
            continue
        max_run = max(max_run, int(match.group(1)))
    return max_run + 1
