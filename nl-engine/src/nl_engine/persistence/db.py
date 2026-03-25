"""File-based JSON persistence store, replacing SQLAlchemy/SQLite."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from nl_engine.settings import get_settings

log = logging.getLogger("nl_engine.persistence")


class FileStore:
    """Thread-safe, file-based JSON persistence store.

    Data layout::

        {data_dir}/
            problems_index.json
            {problem_id}/
                problem.json
                theorem.json
                execution/{id}.json
                decompositions/{id}.json
                assembly_plans/{id}.json
                lemmas/{id}.json
                vetter_reports/{id}.json
                lean_jobs/{id}.json
                lean_results/{id}.json
                trusted_context.json
                failure_report.json
                worker_jobs/{id}.json
                events.jsonl
                request_log.jsonl
                llm_usage.jsonl
                cost_rollup.json
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self.root = Path(data_dir or get_settings().data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.Lock()
        self._event_counters: dict[str, int] = {}

    # ── Locking ──────────────────────────────────────────────

    def lock_for(self, problem_id: str) -> threading.RLock:
        with self._global_lock:
            if problem_id not in self._locks:
                self._locks[problem_id] = threading.RLock()
            return self._locks[problem_id]

    # ── Path helpers ─────────────────────────────────────────

    def problem_dir(self, problem_id: str) -> Path:
        d = self.root / problem_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Atomic I/O ───────────────────────────────────────────

    def atomic_write(self, path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
            os.replace(str(tmp), str(path))
        except OSError:
            log.exception("atomic_write failed for %s", path)
            raise

    def read_json(self, path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read JSON from %s: %s", path, exc)
            return None

    def append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(data, sort_keys=True, default=str) + "\n"
            with open(path, "a") as f:
                f.write(line)
        except OSError:
            log.exception("append_jsonl failed for %s", path)
            raise

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        result: list[dict[str, Any]] = []
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return result

    def glob_read(self, directory: Path, pattern: str = "*.json") -> list[dict[str, Any]]:
        if not directory.exists():
            return []
        results: list[dict[str, Any]] = []
        for fp in sorted(directory.glob(pattern)):
            data = self.read_json(fp)
            if isinstance(data, dict):
                results.append(data)
        return results

    # ── Event ID assignment ──────────────────────────────────

    def next_event_id(self, problem_id: str) -> int:
        # Must be called while holding the per-problem lock.
        if problem_id not in self._event_counters:
            events_path = self.problem_dir(problem_id) / "events.jsonl"
            self._event_counters[problem_id] = len(self.read_jsonl(events_path))
        self._event_counters[problem_id] += 1
        return self._event_counters[problem_id]

    # ── Problem index ────────────────────────────────────────

    def _index_path(self) -> Path:
        return self.root / "problems_index.json"

    def update_index(self, problem_id: str, title: str, status: str, created_at: str) -> None:
        with self._global_lock:
            index = self.read_json(self._index_path()) or {}
            index[problem_id] = {"title": title, "status": status, "created_at": created_at}
            self.atomic_write(self._index_path(), index)

    def update_index_status(self, problem_id: str, status: str) -> None:
        with self._global_lock:
            index = self.read_json(self._index_path()) or {}
            if problem_id in index:
                index[problem_id]["status"] = status
                self.atomic_write(self._index_path(), index)

    def remove_from_index(self, problem_id: str) -> None:
        with self._global_lock:
            index = self.read_json(self._index_path()) or {}
            index.pop(problem_id, None)
            self.atomic_write(self._index_path(), index)

    def read_index(self) -> dict[str, Any]:
        return self.read_json(self._index_path()) or {}

    # ── Problem listing ──────────────────────────────────────

    def list_problem_ids(self) -> list[str]:
        index = self.read_index()
        return list(index.keys())

    # ── Cleanup ──────────────────────────────────────────────

    def delete_problem_dir(self, problem_id: str) -> None:
        import shutil
        d = self.root / problem_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        self.remove_from_index(problem_id)
        with self._global_lock:
            self._locks.pop(problem_id, None)
            self._event_counters.pop(problem_id, None)

    def reset_all(self) -> None:
        import shutil
        for child in self.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        with self._global_lock:
            self._locks.clear()
            self._event_counters.clear()

    # ── Compatibility shims ──────────────────────────────────

    def commit(self) -> None:
        """No-op. Writes are immediately durable in the file store."""

    def close(self) -> None:
        """No-op. File store has no session lifecycle."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ── Singleton ────────────────────────────────────────────────

_STORE: FileStore | None = None
_STORE_LOCK = threading.Lock()


def get_file_store() -> FileStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = FileStore()
        return _STORE


def reset_file_store(store: FileStore | None = None) -> None:
    """Replace the global singleton (for testing)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store


# Backward compatibility aliases
SessionLocal = get_file_store
engine = None
