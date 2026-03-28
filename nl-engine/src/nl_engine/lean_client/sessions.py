from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nl_engine.domain.config import ProblemConfig
from nl_engine.domain.enums import ProblemStatus, RoutingStatus
from nl_engine.domain.models import ProblemORM
from nl_engine.lean_client.client import LeanClient
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import DecompositionRepository, LeanJobRepository, LemmaRepository, ProblemRepository
from nl_engine.services.ids import new_id
from nl_engine.settings import get_settings

log = logging.getLogger("nl_engine.lean_sessions")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _compiled_lean_root() -> Path:
    return _repo_root() / "compiled-lean-engine" / "lean-engine"


def _session_artifact_root() -> Path:
    return _compiled_lean_root() / ".artifacts" / "lean_engine"


def _problem_session_root(problem_id: str) -> Path:
    return _session_artifact_root() / "service_sessions" / problem_id


def _problem_session_store_root(problem_id: str) -> Path:
    return _problem_session_root(problem_id) / "state"


def _pid_is_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


class LeanSessionManager:
    def __init__(self, store: FileStore) -> None:
        self.store = store
        self.problems = ProblemRepository(store)
        self.lean_jobs = LeanJobRepository(store)
        self.decompositions = DecompositionRepository(store)
        self.lemmas = LemmaRepository(store)
        self.settings = get_settings()

    def client_for_problem(
        self,
        problem: ProblemORM,
        *,
        create_if_missing: bool = True,
    ) -> LeanClient:
        if self.settings.lean_engine_runtime_mode == "external":
            return LeanClient()
        resolved = self.ensure_problem_session(problem, create_if_missing=create_if_missing)
        base_url = str(resolved.lean_session_endpoint or self.settings.lean_engine_base_url).strip()
        return LeanClient(base_url=base_url, api_version="v2")

    def ensure_problem_session(
        self,
        problem: ProblemORM,
        *,
        create_if_missing: bool = True,
    ) -> ProblemORM:
        if self.settings.lean_engine_runtime_mode == "external":
            return problem

        cfg = ProblemConfig.model_validate(problem.config)
        endpoint = str(problem.lean_session_endpoint or "").strip()
        if endpoint:
            try:
                live = LeanClient(base_url=endpoint, api_version="v2").health_live(version="v2")
                live_max_workers = int(live.get("max_workers") or cfg.lean_engine.max_workers)
                if live_max_workers == int(cfg.lean_engine.max_workers):
                    problem.lean_session_status = str(live.get("status") or "running")
                    problem.lean_session_last_seen_at = _now_utc()
                    problem.lean_session_max_workers = live_max_workers
                    return problem
                log.info(
                    "Relaunching Lean session for %s to match max_workers=%s",
                    problem.problem_id,
                    cfg.lean_engine.max_workers,
                )
                self._terminate_pid(problem.lean_session_pid)
            except Exception:
                log.warning("Lean session %s for problem %s is not healthy; relaunching", endpoint, problem.problem_id)

        if not create_if_missing:
            return problem

        if problem.status in {ProblemStatus.PAUSED.value, ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
            return problem

        return self._launch_problem_session(problem, cfg)

    def session_snapshot(self, problem: ProblemORM) -> dict[str, Any] | None:
        endpoint = str(problem.lean_session_endpoint or "").strip()
        if not endpoint and not problem.lean_session_id:
            return None
        payload: dict[str, Any] = {
            "session_id": problem.lean_session_id,
            "endpoint": problem.lean_session_endpoint,
            "pid": problem.lean_session_pid,
            "store_root": problem.lean_session_store_root,
            "max_workers": problem.lean_session_max_workers,
            "status": problem.lean_session_status,
            "started_at": problem.lean_session_started_at,
            "last_seen_at": problem.lean_session_last_seen_at,
            "active_jobs": None,
            "queue_depth": None,
        }
        if endpoint:
            try:
                live = LeanClient(base_url=endpoint, api_version="v2").health_live(version="v2")
                payload["status"] = live.get("status", payload["status"])
                payload["active_jobs"] = live.get("active_jobs")
                payload["queue_depth"] = live.get("queue_depth")
                payload["max_workers"] = live.get("max_workers", payload["max_workers"])
                payload["last_seen_at"] = _now_utc()
            except Exception:
                pass
        return payload

    def recover_active_problem_sessions(self) -> None:
        if self.settings.lean_engine_runtime_mode == "external":
            return
        for problem_id in sorted(self.store.list_problem_ids()):
            problem = self.problems.get(problem_id)
            if problem is None:
                continue
            if problem.status in {ProblemStatus.PAUSED.value, ProblemStatus.SUCCEEDED.value, ProblemStatus.FAILED.value}:
                continue
            if problem.nl_only_mode:
                continue
            if not self._problem_needs_lean(problem_id):
                continue
            try:
                self.ensure_problem_session(problem, create_if_missing=True)
            except Exception:
                log.exception("Failed to recover Lean session for %s", problem_id)

    def cancel_problem_lean_work(
        self,
        problem_id: str,
        *,
        reason: str,
        terminate_session: bool,
        clear_metadata: bool,
    ) -> None:
        problem = self.problems.get(problem_id)
        if problem is None:
            return

        client = None
        endpoint = str(problem.lean_session_endpoint or "").strip()
        if endpoint:
            client = LeanClient(base_url=endpoint, api_version="v2")

        now = _now_utc()
        for job in self.lean_jobs.list_non_terminal(problem_id):
            if client is not None:
                try:
                    if job.remote_operation_id:
                        client.cancel_operation(job.remote_operation_id, version="v2")
                    else:
                        client.cancel_job(job.job_id, version="v2")
                except Exception:
                    pass
            job.status = "cancelled"
            job.completed_at = now
            job.controller_harvested_at = now
            job.last_error = reason
            self.lean_jobs.save(job)
            self._reset_target_after_cancel(job)

        store_root = Path(problem.lean_session_store_root) if problem.lean_session_store_root else None
        if store_root:
            self._cancel_session_store_jobs(store_root)

        if terminate_session:
            self._terminate_pid(problem.lean_session_pid)
        if clear_metadata:
            self._clear_session_metadata(problem, status="stopped")

    def _problem_needs_lean(self, problem_id: str) -> bool:
        if self.lean_jobs.list_non_terminal(problem_id):
            return True
        if any(lemma.routing_status == RoutingStatus.READY_FOR_LEAN.value for lemma in self.lemmas.list_by_problem(problem_id)):
            return True
        for dec in self.decompositions.list_by_problem(problem_id):
            if dec.lean_v2_prepare_status in {"pending", "queued", "running"}:
                return True
        return False

    def _launch_problem_session(self, problem: ProblemORM, cfg: ProblemConfig) -> ProblemORM:
        if _pid_is_alive(problem.lean_session_pid):
            self._terminate_pid(problem.lean_session_pid)

        session_root = _problem_session_root(problem.problem_id)
        store_root = _problem_session_store_root(problem.problem_id)
        session_root.mkdir(parents=True, exist_ok=True)
        store_root.mkdir(parents=True, exist_ok=True)

        port = _reserve_local_port()
        endpoint = f"http://127.0.0.1:{port}"
        compiled_root = _compiled_lean_root()
        env = os.environ.copy()
        pythonpath_entries = [str((compiled_root / "src").resolve())]
        existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
        if existing_pythonpath:
            pythonpath_entries.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

        command = [
            sys.executable,
            "-m",
            "lean_engine.cli",
            "service",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--store-root",
            str(store_root),
            "--max-workers",
            str(max(1, int(cfg.lean_engine.max_workers))),
            "--artifact-root",
            str(_session_artifact_root()),
        ]
        if cfg.lean_engine.model:
            command.extend(["--model", cfg.lean_engine.model])

        log_path = session_root / "service.log"
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(  # noqa: S603
                command,
                cwd=str(compiled_root),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        client = LeanClient(base_url=endpoint, api_version="v2")
        deadline = time.monotonic() + 20.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                live = client.health_live(version="v2")
                now = _now_utc()
                problem.lean_session_id = new_id("leansess")
                problem.lean_session_endpoint = endpoint
                problem.lean_session_pid = int(process.pid)
                problem.lean_session_store_root = str(store_root)
                problem.lean_session_max_workers = int(live.get("max_workers") or cfg.lean_engine.max_workers)
                problem.lean_session_status = str(live.get("status") or "running")
                problem.lean_session_started_at = now
                problem.lean_session_last_seen_at = now
                self.problems.save(problem)
                return problem
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)

        self._terminate_pid(process.pid)
        raise RuntimeError(
            f"failed to start Lean session for {problem.problem_id}: {last_error or 'service exited before health check'}"
        )

    def _reset_target_after_cancel(self, job) -> None:
        operation = str(job.operation or job.mode or "").strip()
        if operation == "prepare_track":
            dec = self.decompositions.get(job.target_id)
            if dec is None:
                return
            if not dec.lean_v2_track_id:
                dec.lean_v2_prepare_status = "pending"
            if dec.latest_prepare_track_job_id == job.job_id:
                dec.latest_prepare_track_job_id = None
            self.decompositions.save(dec)
            return
        if operation == "formalize_lemma_from_nl":
            lemma = self.lemmas.get(job.target_id)
            if lemma is None:
                return
            if lemma.last_submitted_lean_job_id == job.job_id:
                lemma.last_submitted_lean_job_id = None
                lemma.last_submitted_lean_proof_fingerprint = None
            if lemma.routing_status != RoutingStatus.DONE.value:
                lemma.routing_status = RoutingStatus.READY_FOR_LEAN.value
                lemma.next_action = "wait_for_decomposition_track"
            self.lemmas.save(lemma)

    def _cancel_session_store_jobs(self, store_root: Path) -> None:
        jobs_dir = store_root / "jobs"
        if not jobs_dir.exists():
            return
        now_iso = _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for job_file in sorted(jobs_dir.glob("*.json")):
            try:
                payload = json.loads(job_file.read_text())
            except Exception:
                continue
            if payload.get("status") not in {"queued", "running"}:
                continue
            payload["status"] = "cancelled"
            payload["updated_at"] = now_iso
            payload["completed_at"] = now_iso
            try:
                job_file.write_text(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True))
            except Exception:
                continue

    def _clear_session_metadata(self, problem: ProblemORM, *, status: str) -> None:
        problem.lean_session_status = status
        problem.lean_session_id = None
        problem.lean_session_endpoint = None
        problem.lean_session_pid = None
        problem.lean_session_last_seen_at = _now_utc()
        self.problems.save(problem)

    @staticmethod
    def _terminate_pid(pid: int | None, *, grace_seconds: float = 2.0) -> None:
        if not _pid_is_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                return
        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline:
            if not _pid_is_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                return
