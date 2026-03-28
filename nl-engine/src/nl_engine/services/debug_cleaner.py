from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nl_engine.artifacts.store import ArtifactStore
from nl_engine.persistence.db import FileStore


@dataclass
class CleanupResult:
    scope: str
    problem_id: str | None
    deleted_rows: dict[str, int]
    deleted_artifacts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "problem_id": self.problem_id,
            "deleted_rows": self.deleted_rows,
            "deleted_artifacts": self.deleted_artifacts,
        }


class DebugDataCleaner:
    """Destructive local cleanup operations for debug-only endpoints."""

    def __init__(self, store: FileStore, artifact_root: str) -> None:
        self.store = store
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.artifacts = ArtifactStore(str(self.artifact_root))

    def _count_data_files(self, problem_id: str) -> dict[str, int]:
        """Count data files by type for backward-compatible deleted_rows report."""
        self.store.sync_problem(problem_id)
        pd = self.store.root / problem_id
        if not pd.exists():
            return {}
        # Map subdirectories to old table names for compatibility
        mapping = {
            "decompositions": "decompositions",
            "assembly_plans": "assembly_plans",
            "lemmas": "lemmas",
            "vetter_reports": "vetter_reports",
            "lean_jobs": "lean_jobs",
            "lean_results": "lean_results",
            "execution": "problem_executions",
            "worker_jobs": "worker_jobs",
            "request_records": "request_records",
        }
        counts: dict[str, int] = {}
        for subdir, table_name in mapping.items():
            d = pd / subdir
            counts[table_name] = len(list(d.glob("*.json"))) if d.exists() else 0
        # Count singleton files
        for name, table_name in [
            ("problem.json", "problems"),
            ("theorem.json", "theorems"),
            ("trusted_context.json", "trusted_context"),
            ("failure_report.json", "failure_reports"),
        ]:
            if (pd / name).exists():
                counts[table_name] = 1
        counts["events"] = len(list((pd / "events").glob("*.json"))) if (pd / "events").exists() else 0
        counts["llm_usage_records"] = len(list((pd / "llm_usage").glob("*.json"))) if (pd / "llm_usage").exists() else 0

        # Count legacy JSONL files for backward compatibility.
        for name, table_name in [("events.jsonl", "events"), ("llm_usage.jsonl", "llm_usage_records")]:
            p = pd / name
            if p.exists():
                counts[table_name] = counts.get(table_name, 0) + sum(1 for line in p.read_text().splitlines() if line.strip())
        # Cost rollup
        counts["run_cost_rollups"] = 1 if (pd / "cost_rollup.json").exists() else 0
        return counts

    def _remove_tree(self, rel_dir: str) -> int:
        prefix = rel_dir.strip("/")
        if not prefix:
            return 0
        keys = self.artifacts.list_keys(prefix=prefix)
        for key in keys:
            self.artifacts.delete(key)
        path = (self.artifact_root / prefix).resolve()
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        return len(keys)

    def delete_problem(self, problem_id: str) -> CleanupResult:
        deleted_artifacts: dict[str, int] = {}
        deleted_artifacts["problems"] = self._remove_tree(f"problems/{problem_id}")

        # Remove worker job artifacts for this problem
        worker_deleted = 0
        for key in list(self.artifacts.list_keys(prefix="worker_jobs")):
            if problem_id not in key:
                continue
            self.artifacts.delete(key)
            worker_deleted += 1
        deleted_artifacts["worker_jobs"] = worker_deleted

        # Count data files by type before deletion
        deleted_rows = self._count_data_files(problem_id)

        # Remove data directory for this problem
        self.store.delete_problem_dir(problem_id)

        return CleanupResult(
            scope="problem",
            problem_id=problem_id,
            deleted_rows=deleted_rows,
            deleted_artifacts=deleted_artifacts,
        )

    def reset_all(self) -> CleanupResult:
        # Count data files before deletion
        deleted_rows: dict[str, int] = {}
        for pid in self.store.list_problem_ids():
            for table, count in self._count_data_files(pid).items():
                deleted_rows[table] = deleted_rows.get(table, 0) + count

        deleted_artifacts: dict[str, int] = {}
        deleted_artifacts["problems"] = self._remove_tree("problems")
        deleted_artifacts["worker_jobs"] = self._remove_tree("worker_jobs")

        # Reset the entire data store
        self.store.reset_all()

        return CleanupResult(
            scope="global",
            problem_id=None,
            deleted_rows=deleted_rows,
            deleted_artifacts=deleted_artifacts,
        )
