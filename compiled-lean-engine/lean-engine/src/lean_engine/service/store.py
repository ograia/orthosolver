from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_JOB_STATUSES = ("queued", "running")
TERMINAL_JOB_STATUSES = ("success", "repairable", "fatal", "cancelled")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    mode: str
    status: str
    request: dict[str, Any]
    result: dict[str, Any] | None
    error_class: str | None
    message: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None

    def to_submit_response(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "mode": self.mode,
            "created_at": self.created_at,
            "estimated_duration_seconds": 120,
        }

    def to_poll_response(self) -> dict[str, Any]:
        if self.status in ACTIVE_JOB_STATUSES:
            started = self.started_at or self.created_at
            elapsed_seconds = max(
                int((datetime.now(timezone.utc) - parse_utc_iso(started)).total_seconds()),
                0,
            )
            return {
                "job_id": self.job_id,
                "status": self.status,
                "mode": self.mode,
                "elapsed_seconds": elapsed_seconds,
                "updated_at": self.updated_at,
                "created_at": self.created_at,
            }

        payload = {
            "job_id": self.job_id,
            "status": self.status,
            "mode": self.mode,
            "result": self.result,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }
        if self.error_class:
            payload["error_class"] = self.error_class
        return payload


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path.expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def create_or_get(self, *, job_id: str, mode: str, request: dict[str, Any]) -> tuple[JobRecord, bool]:
        with self._lock:
            existing = self.get(job_id)
            if existing is not None:
                return existing, False

            now = utc_now_iso()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id,
                        mode,
                        status,
                        request_json,
                        result_json,
                        error_class,
                        message,
                        created_at,
                        updated_at,
                        started_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        mode,
                        "queued",
                        json.dumps(request, ensure_ascii=True, sort_keys=True),
                        None,
                        None,
                        None,
                        now,
                        now,
                        None,
                        None,
                    ),
                )

            created = self.get(job_id)
            if created is None:
                raise RuntimeError(f"failed to load newly inserted job `{job_id}`")
            return created, True

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    job_id,
                    mode,
                    status,
                    request_json,
                    result_json,
                    error_class,
                    message,
                    created_at,
                    updated_at,
                    started_at,
                    completed_at
                FROM jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            now = utc_now_iso()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, started_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    ("running", now, now, job_id),
                )

    def mark_terminal(
        self,
        *,
        job_id: str,
        status: str,
        result: dict[str, Any],
        error_class: str | None,
        message: str | None,
    ) -> None:
        with self._lock:
            now = utc_now_iso()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET
                        status = ?,
                        result_json = ?,
                        error_class = ?,
                        message = ?,
                        updated_at = ?,
                        completed_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        status,
                        json.dumps(result, ensure_ascii=True, sort_keys=True),
                        error_class,
                        message,
                        now,
                        now,
                        job_id,
                    ),
                )

    def active_job_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE status IN (?, ?)",
                ACTIVE_JOB_STATUSES,
            ).fetchone()
        return int(row["c"]) if row is not None else 0

    def queue_depth(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE status = ?",
                ("queued",),
            ).fetchone()
        return int(row["c"]) if row is not None else 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_class TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    request_raw = row["request_json"]
    result_raw = row["result_json"]

    request = json.loads(request_raw) if isinstance(request_raw, str) and request_raw else {}
    if not isinstance(request, dict):
        request = {}

    result: dict[str, Any] | None
    if isinstance(result_raw, str) and result_raw:
        parsed_result = json.loads(result_raw)
        result = parsed_result if isinstance(parsed_result, dict) else {"value": parsed_result}
    else:
        result = None

    return JobRecord(
        job_id=str(row["job_id"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        request=request,
        result=result,
        error_class=str(row["error_class"]) if row["error_class"] else None,
        message=str(row["message"]) if row["message"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=str(row["started_at"]) if row["started_at"] else None,
        completed_at=str(row["completed_at"]) if row["completed_at"] else None,
    )
