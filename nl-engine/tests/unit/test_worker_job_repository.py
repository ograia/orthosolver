from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta

from nl_engine.domain.models import ProblemORM, WorkerJobORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import ProblemRepository, WorkerJobRepository


def _store() -> FileStore:
    td = tempfile.mkdtemp()
    return FileStore(td)


def test_worker_claim_and_complete_flow() -> None:
    store = _store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="prob_1",
            title="t",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )

    repo = WorkerJobRepository(store)
    repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_1",
            problem_id="prob_1",
            worker_kind="lemma_solver",
            status="queued",
            payload={},
        )
    )
    claimed = repo.claim_next("lemma_solver", "worker-a", datetime.now(UTC), lease_seconds=60)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.lease_owner == "worker-a"

    completed = repo.complete("job_1", {"ok": True})
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result_payload == {"ok": True}


def test_worker_requeue_expired_lease() -> None:
    store = _store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="prob_2",
            title="t",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )

    repo = WorkerJobRepository(store)
    repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_expired",
            problem_id="prob_2",
            worker_kind="lemma_solver",
            status="running",
            payload={},
            lease_owner="worker-a",
            lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            attempt_count=1,
            max_attempts=3,
        )
    )

    changed = repo.requeue_expired("lemma_solver", datetime.now(UTC))
    assert changed == 1
    row = repo.get("job_expired")
    assert row is not None
    assert row.status == "queued"
    assert row.lease_owner is None


def test_worker_requeue_expired_last_attempt_becomes_failed() -> None:
    store = _store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="prob_2b",
            title="t",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )

    repo = WorkerJobRepository(store)
    repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_expired_terminal",
            problem_id="prob_2b",
            worker_kind="lemma_solver",
            status="running",
            payload={},
            lease_owner="worker-a",
            lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            attempt_count=3,
            max_attempts=3,
            error_payload={"error_class": "infrastructure_transient", "message": "timed out"},
        )
    )

    changed = repo.requeue_expired("lemma_solver", datetime.now(UTC))
    assert changed == 1
    row = repo.get("job_expired_terminal")
    assert row is not None
    assert row.status == "failed"
    assert row.lease_owner is None
    assert row.lease_expires_at is None


def test_worker_consume_terminal_is_exactly_once() -> None:
    store = _store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="prob_3",
            title="t",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )

    repo = WorkerJobRepository(store)
    repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_terminal",
            problem_id="prob_3",
            worker_kind="lemma_solver",
            status="completed",
            payload={"attempt_number": 1},
            result_payload={"ok": True},
        )
    )

    first_row, first_consumed = repo.consume_terminal("job_terminal", execution_id="exec_a")
    assert first_row is not None
    assert first_consumed is True
    assert first_row.controller_consumed_at is not None
    assert first_row.controller_consumed_by_execution_id == "exec_a"

    second_row, second_consumed = repo.consume_terminal("job_terminal", execution_id="exec_b")
    assert second_row is not None
    assert second_consumed is False
    assert second_row.controller_consumed_at is not None
    assert second_row.controller_consumed_by_execution_id == "exec_a"


def test_superseded_running_job_stays_superseded_on_late_complete() -> None:
    store = _store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="prob_sup",
            title="t",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )

    repo = WorkerJobRepository(store)
    repo.enqueue_if_absent(
        WorkerJobORM(
            worker_job_id="job_sup",
            problem_id="prob_sup",
            worker_kind="lemma_solver",
            status="running",
            continuation_generation=1,
            payload={"attempt_number": 1},
            attempt_number=1,
        )
    )
    updated = repo.supersede_older_inflight(
        "prob_sup",
        min_generation=2,
        superseded_by_execution_id="exec_new",
        reason="fresh_continue",
    )
    assert len(updated) == 1

    row = repo.complete("job_sup", {"ok": True})
    assert row is not None
    assert row.status == "superseded"
    assert row.result_payload == {"ok": True}
