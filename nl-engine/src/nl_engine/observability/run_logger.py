"""Unified run logger — writes a single chronological run_log.jsonl per problem.

Every agent call, routing decision, and state transition is captured as a JSONL
entry so that an observer (human or Claude) can read one file to understand
everything that happened in a run.

File location: .artifacts/problems/{problem_id}/run_log.jsonl
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

from nl_engine.artifacts.store import ArtifactStore


class RunLogger:
    """Append-only JSONL logger per problem run."""

    _lock = threading.Lock()

    def __init__(self) -> None:
        self.store = ArtifactStore()

    def _log_key(self, problem_id: str) -> str:
        return f"problems/{problem_id}/run_log.jsonl"

    def _append(self, problem_id: str, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, default=str) + "\n"
        with self._lock:
            self.store.append_text(self._log_key(problem_id), line)

    def agent_start(
        self,
        *,
        problem_id: str,
        agent_key: str,
        artifact_prefix: str,
        input_payload: dict[str, Any],
        model: str,
        reasoning_effort: str,
        target_id: str | None = None,
    ) -> None:
        self._append(problem_id, {
            "ts": datetime.now(UTC).isoformat(),
            "event": "agent_start",
            "agent": agent_key,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "target_id": target_id,
            "artifact_prefix": artifact_prefix,
            "input": input_payload,
        })

    def agent_success(
        self,
        *,
        problem_id: str,
        agent_key: str,
        artifact_prefix: str,
        output: dict[str, Any],
        duration_seconds: float,
        model: str,
        target_id: str | None = None,
    ) -> None:
        self._append(problem_id, {
            "ts": datetime.now(UTC).isoformat(),
            "event": "agent_success",
            "agent": agent_key,
            "model": model,
            "duration_s": round(duration_seconds, 2),
            "target_id": target_id,
            "artifact_prefix": artifact_prefix,
            "output": output,
        })

    def agent_error(
        self,
        *,
        problem_id: str,
        agent_key: str,
        artifact_prefix: str,
        error_class: str,
        error_message: str,
        duration_seconds: float,
        model: str,
        target_id: str | None = None,
    ) -> None:
        self._append(problem_id, {
            "ts": datetime.now(UTC).isoformat(),
            "event": "agent_error",
            "agent": agent_key,
            "model": model,
            "duration_s": round(duration_seconds, 2),
            "error_class": error_class,
            "error_message": error_message,
            "target_id": target_id,
            "artifact_prefix": artifact_prefix,
        })

    def routing_decision(
        self,
        *,
        problem_id: str,
        stage: str,
        target_id: str | None = None,
        decision: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._append(problem_id, {
            "ts": datetime.now(UTC).isoformat(),
            "event": "routing",
            "stage": stage,
            "target_id": target_id,
            "decision": decision,
            "details": details or {},
        })

    def state_change(
        self,
        *,
        problem_id: str,
        stage: str,
        old_status: str | None,
        new_status: str | None,
        target_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._append(problem_id, {
            "ts": datetime.now(UTC).isoformat(),
            "event": "state_change",
            "stage": stage,
            "old_status": old_status,
            "new_status": new_status,
            "target_id": target_id,
            "reason": reason,
        })


_run_logger: RunLogger | None = None
_init_lock = threading.Lock()


def get_run_logger() -> RunLogger:
    global _run_logger
    if _run_logger is None:
        with _init_lock:
            if _run_logger is None:
                _run_logger = RunLogger()
    return _run_logger
