from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module


def _create_problem(client: TestClient) -> str:
    create = client.post(
        "/v1/problems",
        json={
            "title": "Local reliability theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": True}},
        },
    )
    assert create.status_code == 200
    return create.json()["problem_id"]


def test_run_reuses_existing_execution_instead_of_409(monkeypatch) -> None:
    with TestClient(app) as client:
        problem_id = _create_problem(client)

    original_ensure_running = orchestrator_module.Orchestrator.run_once

    def slow_run_once(self, pid: str):
        time.sleep(0.25)
        return original_ensure_running(self, pid)

    monkeypatch.setattr(orchestrator_module.Orchestrator, "run_once", slow_run_once)

    responses = []

    def first_request():
        with TestClient(app) as c:
            responses.append(c.post(f"/v1/problems/{problem_id}/run"))

    t = threading.Thread(target=first_request)
    t.start()
    time.sleep(0.05)
    with TestClient(app) as c:
        second = c.post(f"/v1/problems/{problem_id}/run")
    t.join()

    assert len(responses) == 1
    assert responses[0].status_code == 200
    assert second.status_code == 200
    assert responses[0].json()["execution_id"] == second.json()["execution_id"]


def test_db_locked_maps_to_structured_503(monkeypatch) -> None:
    """Legacy lock-behavior coverage retained only as a placeholder.

    The file-backed runtime no longer produces relational-lock behavior.
    """
    pytest.skip("Legacy lock behavior test is not applicable with the file-based store")

    # No-op path kept to satisfy static analysis in this skipped test.
    assert monkeypatch is not None


def test_sse_and_run_interleaving_no_deadlock() -> None:
    with TestClient(app) as client:
        problem_id = _create_problem(client)

    stream_chunks: list[str] = []
    done = threading.Event()

    def consume_stream() -> None:
        with TestClient(app) as client:
            with client.stream("GET", f"/v1/problems/{problem_id}/events/stream") as stream:
                for chunk in stream.iter_text():
                    stream_chunks.append(chunk)
                    if "event: terminal" in "".join(stream_chunks):
                        done.set()
                        break

    t = threading.Thread(target=consume_stream)
    t.start()

    terminal = None
    with TestClient(app) as client:
        for _ in range(40):
            run = client.post(f"/v1/problems/{problem_id}/run")
            assert run.status_code == 200
            terminal = run.json()["status"]
            if terminal in {"succeeded", "failed"}:
                break
            time.sleep(0.05)

    t.join(timeout=8)
    assert terminal == "succeeded"
    assert done.is_set()
    assert "event: terminal" in "".join(stream_chunks)
