from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nl_engine.api.debug import _attach_llm_usage_to_request_entries, _request_completion_status
from nl_engine.artifacts.browser import ArtifactBrowser
from nl_engine.domain.contracts import DebugRequestLogEntry
from nl_engine.domain.models import LlmUsageRecordORM


def _write(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_api_run_missing_response_with_downstream_artifacts_is_unknown(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    request_key = "problems/p/api/run_requests/run_req_1.request.json"
    _write(tmp_path / request_key)
    _write(tmp_path / "problems/p/decomposer/attempt_1/agent2_input.json")

    status, detail = _request_completion_status(
        browser=browser,
        source="api_run",
        artifact_key=request_key,
        problem_id="p",
        key_set={request_key},
        problem_running=False,
    )

    assert status == "unknown"
    assert detail and "missing" in detail


def test_api_run_in_progress_response_is_pending_while_running(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    request_key = "problems/p/api/run_requests/run_req_1.request.json"
    response_key = "problems/p/api/run_requests/run_req_1.response.json"
    _write(tmp_path / request_key)
    _write(tmp_path / response_key, '{"ok": null, "status": "in_progress"}')

    status, detail = _request_completion_status(
        browser=browser,
        source="api_run",
        artifact_key=request_key,
        problem_id="p",
        key_set={request_key, response_key},
        problem_running=True,
    )

    assert status == "pending"
    assert detail == "compatibility run request still waiting on execution"


def test_agent_completion_uses_local_folder_artifacts(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(tmp_path / "problems/p/decomposer/attempt_1/agent2_parsed_output_attempt_1.json")

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "completed"
    assert detail == "agent output artifact present"


def test_agent_completion_prefers_success_after_retry_error_artifact(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(tmp_path / "problems/p/decomposer/attempt_1/agent2_request_error_attempt_1.json")
    _write(tmp_path / "problems/p/decomposer/attempt_1/agent2_parsed_output_attempt_2.json")

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "completed"
    assert detail == "agent output artifact present"


def test_agent_validation_error_is_reported(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(tmp_path / "problems/p/decomposer/attempt_1/agent2_validation_error.json")

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "failed"
    assert detail == "agent output failed schema validation"


def test_root_agent_started_state_without_outputs_is_pending_when_not_running(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        '{"status": "started"}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "pending"
    assert detail == "agent request in progress"


def test_root_agent_started_state_wins_over_earlier_request_error(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(tmp_path / "problems/p/decomposer/attempt_1/agent2_request_error_attempt_1.json")
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_3.json",
        '{"status": "started", "timeout_seconds": 600}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "pending"
    assert detail == "agent request in progress (timeout=600s)"


def test_provider_pending_state_is_reported_as_recovery_in_progress(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        (
            '{"status": "provider_pending", "provider_status": "unreachable", '
            '"last_retrieve_error": "APIConnectionError: Connection error."}'
        ),
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "pending"
    assert detail == (
        "local retrieval failed, recovery in progress (provider_status=unreachable): "
        "APIConnectionError: Connection error."
    )


def test_root_agent_started_state_uses_worker_failure_result(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        '{"status": "started", "worker_job_id": "wrk_agent2_1", "timeout_seconds": 600}',
    )
    _write(
        tmp_path / "worker_jobs/wrk_agent2_1/result.json",
        '{"status": "failed", "error": {"error_class": "infrastructure", "message": "usage write failed"}}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "failed"
    assert detail == "worker failed (infrastructure): usage write failed"


def test_non_root_agent_started_state_is_interrupted_when_not_running(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/lemmas/lem1/solver_attempt_1/agent4_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/lemmas/lem1/solver_attempt_1/agent4_request_state_attempt_1.json",
        '{"status": "started"}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent4",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "failed"
    assert detail == "agent request interrupted"


def test_root_agent_deferred_state_is_reported_as_deferred_failure(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        (
            '{"status": "deferred", "timeout_seconds": 60, '
            '"message": "root generation still running after the parallel join timeout"}'
        ),
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "pending"
    assert detail == (
        "agent request continuing after join timeout (timeout=60s): "
        "root generation still running after the parallel join timeout"
    )


def test_lean_completion_uses_response_artifact_and_terminal_status(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    request_key = "problems/p/lean_jobs/lean_job_1/request.json"
    response_key = "problems/p/lean_jobs/lean_job_1/response.json"
    _write(tmp_path / request_key)
    _write(
        tmp_path / response_key,
        '{"status":"repairable","result":{"issue_kind":"lean_issue","error_class":"llm_runtime_failure"}}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="lean",
        artifact_key=request_key,
        problem_id="p",
        key_set={request_key, response_key},
        problem_running=False,
    )

    assert status == "failed"
    assert detail == "lean result repairable (lean_issue, llm_runtime_failure)"


def test_root_agent_queued_state_is_pending(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        '{"status": "queued", "timeout_seconds": 60}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "pending"
    assert detail == "agent request queued (timeout=60s)"


def test_root_agent_deferred_state_uses_worker_completion_result(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        '{"status": "deferred", "worker_job_id": "wrk_agent2_2", "timeout_seconds": 60}',
    )
    _write(
        tmp_path / "worker_jobs/wrk_agent2_2/result.json",
        '{"status": "completed", "output": {"status": "completed", "candidates": []}}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "completed"
    assert detail == "worker result artifact present (timeout=60s)"


def test_agent_completion_detail_includes_effective_timeout_seconds(tmp_path: Path) -> None:
    browser = ArtifactBrowser(str(tmp_path))
    input_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write(tmp_path / input_key)
    _write(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        '{"status": "completed", "timeout_seconds": 600}',
    )

    status, detail = _request_completion_status(
        browser=browser,
        source="agent2",
        artifact_key=input_key,
        problem_id="p",
        key_set={input_key},
        problem_running=False,
    )

    assert status == "completed"
    assert detail == "agent request completed (timeout=600s)"


def test_attach_llm_usage_to_request_entries_assigns_by_stage_and_time() -> None:
    base = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    entries = [
        DebugRequestLogEntry(
            entry_id="e1",
            timestamp=base,
            source="agent2",
            target_id="p",
            artifact_key="problems/p/decomposer/attempt_1/agent2_input.json",
            summary="agent2",
            completion_status="completed",
            completion_detail=None,
            size_bytes=10,
        ),
        DebugRequestLogEntry(
            entry_id="e2",
            timestamp=base + timedelta(seconds=20),
            source="agent2",
            target_id="p",
            artifact_key="problems/p/decomposer/attempt_2/agent2_input.json",
            summary="agent2",
            completion_status="completed",
            completion_detail=None,
            size_bytes=10,
        ),
    ]
    usage_rows = [
        LlmUsageRecordORM(
            usage_id="u1",
            problem_id="p",
            lemma_id=None,
            worker_job_id=None,
            stage="agent2",
            provider="openai",
            model="gpt-5.4",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.001,
            raw_usage={"input_tokens": 100, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 10}},
            created_at=base + timedelta(seconds=5),
        ),
        LlmUsageRecordORM(
            usage_id="u2",
            problem_id="p",
            lemma_id=None,
            worker_job_id=None,
            stage="agent2",
            provider="openai",
            model="gpt-5.4",
            input_tokens=200,
            output_tokens=75,
            estimated_cost_usd=0.002,
            raw_usage={"input_tokens": 200, "output_tokens": 75, "input_tokens_details": {"cached_tokens": 20}},
            created_at=base + timedelta(seconds=25),
        ),
    ]

    _attach_llm_usage_to_request_entries(entries, usage_rows)

    assert entries[0].llm_call_count == 1
    assert entries[0].llm_input_tokens == 100
    assert entries[0].llm_cached_input_tokens == 10
    assert entries[0].llm_output_tokens == 50
    assert entries[0].llm_total_tokens == 150
    assert entries[0].llm_models == ["gpt-5.4"]

    assert entries[1].llm_call_count == 1
    assert entries[1].llm_input_tokens == 200
    assert entries[1].llm_cached_input_tokens == 20
    assert entries[1].llm_output_tokens == 75
    assert entries[1].llm_total_tokens == 275


def test_attach_llm_usage_prefers_worker_job_id_match() -> None:
    base = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    entries = [
        DebugRequestLogEntry(
            entry_id="e1",
            timestamp=base,
            source="agent2",
            target_id="p",
            worker_job_id="wrk_track_1",
            artifact_key="problems/p/decomposer/attempt_1/agent2_input.json",
            summary="agent2 track 1",
            completion_status="completed",
            completion_detail=None,
            size_bytes=10,
        ),
        DebugRequestLogEntry(
            entry_id="e2",
            timestamp=base,
            source="agent2",
            target_id="p",
            worker_job_id="wrk_track_2",
            artifact_key="problems/p/decomposer/attempt_2/agent2_input.json",
            summary="agent2 track 2",
            completion_status="failed",
            completion_detail=None,
            size_bytes=10,
        ),
    ]
    usage_rows = [
        LlmUsageRecordORM(
            usage_id="u1",
            problem_id="p",
            lemma_id=None,
            worker_job_id="wrk_track_1",
            stage="agent2",
            provider="openai",
            model="gpt-5.4",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.001,
            raw_usage={"input_tokens": 100, "output_tokens": 50},
            created_at=base + timedelta(minutes=10),
        )
    ]

    _attach_llm_usage_to_request_entries(entries, usage_rows)

    assert entries[0].llm_call_count == 1
    assert entries[0].llm_total_tokens == 150
    assert entries[1].llm_call_count == 0
