from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .object_store import JsonObjectStore

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

    def _default_progress_snapshot(self) -> dict[str, Any]:
        payload = self.request.get("payload") if isinstance(self.request.get("payload"), dict) else {}
        attempt = payload.get("attempt_index", 1)
        round_index = payload.get("round_index", 1)
        try:
            attempt = int(attempt)
        except Exception:
            attempt = 1
        try:
            round_index = int(round_index)
        except Exception:
            round_index = 1
        phase = str(self.request.get("operation") or self.mode)
        return {
            "phase": phase,
            "round": round_index,
            "attempt": attempt,
            "last_error": None,
        }

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
            response = {
                "job_id": self.job_id,
                "status": self.status,
                "mode": self.mode,
                "elapsed_seconds": elapsed_seconds,
                "updated_at": self.updated_at,
                "created_at": self.created_at,
                "started_at": self.started_at,
            }
            response["progress_snapshot"] = self._default_progress_snapshot()
            return response

        payload = {
            "job_id": self.job_id,
            "status": self.status,
            "mode": self.mode,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.error_class:
            payload["error_class"] = self.error_class
        if isinstance(self.result, dict):
            if "progress_snapshot" in self.result:
                payload["progress_snapshot"] = self.result.get("progress_snapshot")
            if "issue_kind" in self.result:
                payload["issue_kind"] = self.result.get("issue_kind")
            if "confidence" in self.result:
                payload["confidence"] = self.result.get("confidence")
            if "fatality" in self.result:
                payload["fatality"] = self.result.get("fatality")
        return payload


class JobStore:
    def __init__(self, store_root: Path) -> None:
        self._store_root = store_root.expanduser().resolve()
        self._store_root.mkdir(parents=True, exist_ok=True)
        self._objects = JsonObjectStore(self._store_root)
        self._lock = threading.Lock()

    @property
    def store_root(self) -> Path:
        return self._store_root

    @property
    def storage_mode(self) -> str:
        return self._objects.mode

    def _job_key(self, job_id: str) -> str:
        return f"jobs/{job_id}.json"

    def create_or_get(self, *, job_id: str, mode: str, request: dict[str, Any]) -> tuple[JobRecord, bool]:
        with self._lock:
            existing = self.get(job_id)
            if existing is not None:
                return existing, False

            now = utc_now_iso()
            created = self._objects.create_json_if_absent(
                self._job_key(job_id),
                {
                    "job_id": job_id,
                    "mode": mode,
                    "status": "queued",
                    "request": request,
                    "result": None,
                    "error_class": None,
                    "message": None,
                    "created_at": now,
                    "updated_at": now,
                    "started_at": None,
                    "completed_at": None,
                },
            )
            if not created:
                existing = self.get(job_id)
                if existing is None:
                    raise RuntimeError(f"failed to load existing job `{job_id}`")
                return existing, False

            record = self.get(job_id)
            if record is None:
                raise RuntimeError(f"failed to load newly inserted job `{job_id}`")
            return record, True

    def get(self, job_id: str) -> JobRecord | None:
        payload = self._objects.read_json(self._job_key(job_id))
        if payload is None:
            return None
        return _payload_to_record(payload)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            row = self.get(job_id)
            if row is None or row.status != "queued":
                return
            now = utc_now_iso()
            payload = _record_to_payload(row)
            payload["status"] = "running"
            payload["started_at"] = now
            payload["updated_at"] = now
            self._objects.write_json(self._job_key(job_id), payload)

    def cancel_job(self, job_id: str) -> JobRecord | None:
        """Best-effort cancellation for queued/running jobs."""
        with self._lock:
            row = self.get(job_id)
            if row is None:
                return None
            if row.status not in ACTIVE_JOB_STATUSES:
                return row

            now = utc_now_iso()
            payload = _record_to_payload(row)
            payload["status"] = "cancelled"
            payload["updated_at"] = now
            payload["completed_at"] = now
            self._objects.write_json(self._job_key(job_id), payload)

        return self.get(job_id)

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
            row = self.get(job_id)
            if row is None or row.status == "cancelled":
                return
            now = utc_now_iso()
            payload = _record_to_payload(row)
            payload["status"] = status
            payload["result"] = result
            payload["error_class"] = error_class
            payload["message"] = message
            payload["updated_at"] = now
            payload["completed_at"] = now
            self._objects.write_json(self._job_key(job_id), payload)

    def active_job_count(self) -> int:
        return sum(1 for record in self._list_records() if record.status in ACTIVE_JOB_STATUSES)

    def queue_depth(self) -> int:
        return sum(1 for record in self._list_records() if record.status == "queued")

    def _list_records(self) -> list[JobRecord]:
        rows: list[JobRecord] = []
        for key in self._objects.list_keys(prefix="jobs"):
            payload = self._objects.read_json(key)
            if isinstance(payload, dict):
                rows.append(_payload_to_record(payload))
        rows.sort(key=lambda row: row.created_at)
        return rows

    def list_records(self) -> list[JobRecord]:
        return self._list_records()

    def requeue_for_recovery(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self.get(job_id)
            if row is None or row.status not in ACTIVE_JOB_STATUSES:
                return row
            now = utc_now_iso()
            payload = _record_to_payload(row)
            payload["status"] = "queued"
            payload["updated_at"] = now
            payload["completed_at"] = None
            self._objects.write_json(self._job_key(job_id), payload)
        return self.get(job_id)


def _payload_to_record(payload: dict[str, Any]) -> JobRecord:
    request = payload.get("request")
    if not isinstance(request, dict):
        request = {}
    result = payload.get("result")
    if result is not None and not isinstance(result, dict):
        result = {"value": result}
    return JobRecord(
        job_id=str(payload.get("job_id", "")),
        mode=str(payload.get("mode", "")),
        status=str(payload.get("status", "")),
        request=request,
        result=result,
        error_class=str(payload.get("error_class")) if payload.get("error_class") else None,
        message=str(payload.get("message")) if payload.get("message") else None,
        created_at=str(payload.get("created_at", utc_now_iso())),
        updated_at=str(payload.get("updated_at", utc_now_iso())),
        started_at=str(payload.get("started_at")) if payload.get("started_at") else None,
        completed_at=str(payload.get("completed_at")) if payload.get("completed_at") else None,
    )


def _record_to_payload(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "mode": record.mode,
        "status": record.status,
        "request": record.request,
        "result": record.result,
        "error_class": record.error_class,
        "message": record.message,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
    }
