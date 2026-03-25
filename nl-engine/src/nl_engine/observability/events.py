from __future__ import annotations

from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import EventRepository
from nl_engine.observability.run_logger import get_run_logger


class EventLogger:
    def __init__(self, store: FileStore):
        self.repo = EventRepository(store)

    def transition(
        self,
        problem_id: str,
        stage: str,
        old_status: str | None,
        new_status: str | None,
        *,
        target_node_id: str | None = None,
        worker_job_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.repo.append(
            problem_id,
            stage,
            old_status,
            new_status,
            target_node_id=target_node_id,
            worker_job_id=worker_job_id,
            reason=reason,
        )
        try:
            get_run_logger().state_change(
                problem_id=problem_id,
                stage=stage,
                old_status=old_status,
                new_status=new_status,
                target_id=target_node_id,
                reason=reason,
            )
        except Exception:
            pass
