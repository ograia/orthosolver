from __future__ import annotations

import signal
import threading
import time
from datetime import UTC, datetime

from nl_engine.execution.runtime import ExecutionDriver, StageWorkerRuntime
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import ProblemExecutionRepository
from nl_engine.settings import get_settings


def main() -> None:
    settings = get_settings()
    stop = threading.Event()

    def _handle_signal(signum, frame) -> None:  # pragma: no cover - signal driven
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    stage_runtime = StageWorkerRuntime()
    execution_driver = ExecutionDriver()
    worker_id = "standalone-worker"
    poll_sleep = max(0.05, float(settings.worker_poll_interval_seconds))

    while not stop.is_set():
        did_work = False
        did_work |= stage_runtime.process_next(worker_id)

        store = get_file_store()
        repo = ProblemExecutionRepository(store)
        execution = repo.claim_next(worker_id, datetime.now(UTC), settings.worker_lease_seconds)
        if execution is not None:
            execution_id = execution.execution_id
            execution_driver.advance_until_blocked(execution_id)
            did_work = True

        if not did_work:
            time.sleep(poll_sleep)


if __name__ == "__main__":
    main()
