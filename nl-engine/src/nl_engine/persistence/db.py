"""Object-backed JSON persistence store."""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from nl_engine.settings import get_settings
from nl_engine.storage import ObjectStore

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
                events/{id}.json
                request_log.jsonl
                llm_usage/{id}.json
                cost_rollup.json
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self.objects = ObjectStore(data_dir or get_settings().data_dir, purpose="state")
        self.root = self.objects.root
        self._locks: dict[str, "_ScopedProblemLock"] = {}
        self._global_lock = threading.Lock()
        self._event_counters: dict[str, int] = {}

    # ── Locking ──────────────────────────────────────────────

    def lock_for(self, problem_id: str) -> "_ScopedProblemLock":
        with self._global_lock:
            if problem_id not in self._locks:
                self._locks[problem_id] = _ScopedProblemLock(self, problem_id)
            return self._locks[problem_id]

    # ── Path helpers ─────────────────────────────────────────

    def problem_dir(self, problem_id: str) -> Path:
        d = self.root / problem_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_key(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def sync_problem(self, problem_id: str) -> None:
        if self.objects.mode == "gcs":
            self.objects.sync_prefix(f"{problem_id}/")

    # ── Atomic I/O ───────────────────────────────────────────

    def atomic_write(self, path: Path, data: Any) -> None:
        try:
            self.objects.write_json(self.path_key(path), data)
        except OSError:
            log.exception("atomic_write failed for %s", path)
            raise

    def read_json(self, path: Path) -> Any | None:
        try:
            return self.objects.read_json(self.path_key(path))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read JSON from %s: %s", path, exc)
            return None

    def append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(data, sort_keys=True, default=str) + "\n"
            with open(path, "a") as f:
                f.write(line)
            key = self.path_key(path)
            if self.objects.mode == "gcs":
                self.objects.write_text(key, path.read_text(encoding="utf-8"), content_type="application/x-ndjson")
        except OSError:
            log.exception("append_jsonl failed for %s", path)
            raise

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if self.objects.mode == "gcs":
            self.objects.sync_key(self.path_key(path))
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
        if self.objects.mode == "gcs":
            prefix = self.path_key(directory) if directory.exists() or directory.parent.exists() else str(directory.relative_to(self.root)).replace("\\", "/")
            self.objects.sync_prefix(prefix)
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
            events_dir = self.problem_dir(problem_id) / "events"
            legacy_events_path = self.problem_dir(problem_id) / "events.jsonl"
            current_events = len(self.glob_read(events_dir))
            legacy_events = len(self.read_jsonl(legacy_events_path))
            self._event_counters[problem_id] = current_events + legacy_events
        self._event_counters[problem_id] += 1
        return self._event_counters[problem_id]

    # ── Problem index ────────────────────────────────────────

    def _index_path(self) -> Path:
        return self.root / "problems_index.json"

    def update_index(self, problem_id: str, title: str, status: str, created_at: str) -> None:
        with self.lock_for("__index__"):
            with self._global_lock:
                index = self.read_json(self._index_path()) or {}
                index[problem_id] = {"title": title, "status": status, "created_at": created_at}
                self.atomic_write(self._index_path(), index)

    def update_index_status(self, problem_id: str, status: str) -> None:
        with self.lock_for("__index__"):
            with self._global_lock:
                index = self.read_json(self._index_path()) or {}
                if problem_id in index:
                    index[problem_id]["status"] = status
                    self.atomic_write(self._index_path(), index)

    def remove_from_index(self, problem_id: str) -> None:
        with self.lock_for("__index__"):
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
        self.objects.clear_prefix(problem_id)
        d = self.root / problem_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        self.remove_from_index(problem_id)
        with self._global_lock:
            self._locks.pop(problem_id, None)
            self._event_counters.pop(problem_id, None)

    def reset_all(self) -> None:
        self.objects.clear_all()
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


class _ScopedProblemLock(AbstractContextManager[None]):
    def __init__(self, store: FileStore, problem_id: str) -> None:
        self._store = store
        self._problem_id = problem_id
        self._local_lock = threading.RLock()
        self._state = threading.local()

    def __enter__(self) -> None:
        self._local_lock.acquire()
        depth = int(getattr(self._state, "depth", 0))
        if depth == 0 and self._store.objects.mode == "gcs":
            owner = f"{os.getpid()}:{threading.get_ident()}:{self._problem_id}"
            acquired = self._store.objects.acquire_lease(
                f"_leases/{self._problem_id}.json",
                owner=owner,
                ttl_seconds=10.0,
                wait_timeout_seconds=10.0,
            )
            if not acquired:
                self._local_lock.release()
                raise TimeoutError(f"timed out acquiring distributed lease for {self._problem_id}")
            self._state.owner = owner
        self._state.depth = depth + 1
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        depth = max(int(getattr(self._state, "depth", 1)) - 1, 0)
        self._state.depth = depth
        if depth == 0 and self._store.objects.mode == "gcs":
            owner = str(getattr(self._state, "owner", "") or "")
            if owner:
                self._store.objects.release_lease(f"_leases/{self._problem_id}.json", owner=owner)
            self._state.owner = None
        self._local_lock.release()
        return None
