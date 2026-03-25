from __future__ import annotations

import json
from pathlib import Path

import pytest

import nl_engine.services.agents as agents_module
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import RequestRecordRepository
from nl_engine.services.agents import AgentExecutionError, AgentService
from nl_engine.settings import get_settings


class _FakeResponsesOk:
    def create(self, **kwargs):  # noqa: ANN003
        class _Resp:
            output_text = '{"status":"completed","candidates":[]}'
            usage = {"input_tokens": 10, "output_tokens": 5}

        return _Resp()


class _APIConnectionError(RuntimeError):
    pass


class _FakeResponsesFail:
    def create(self, **kwargs):  # noqa: ANN003
        raise _APIConnectionError("network down")


class _FakeResponsesFlaky:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self.calls == 1:
            raise _APIConnectionError("temporary network issue")

        class _Resp:
            output_text = '{"status":"completed","candidates":[]}'
            usage = {"input_tokens": 10, "output_tokens": 5}

        return _Resp()


class _FakeResponsesInvalidJsonThenOk:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1

        class _Resp:
            usage = {"input_tokens": 10, "output_tokens": 5}

        if self.calls == 1:
            _Resp.output_text = "not-json"
        else:
            _Resp.output_text = '{"status":"completed","candidates":[]}'
        return _Resp()


class _FakeResponsesInvalidBackslashJson:
    def create(self, **kwargs):  # noqa: ANN003
        class _Resp:
            output_text = (
                '{"status":"completed","semantic_sketch":{"variables":[],"quantifier_order":["x in Z_{\\ge 0}"],'
                '"domain_restrictions":[],"witness_dependencies":[],"normalized_claim":"x in Z_{\\ge 0}"}}'
            )
            usage = {"input_tokens": 10, "output_tokens": 5}

        return _Resp()


class _FakeResponsesBackgroundTerminalFail:
    def create(self, **kwargs):  # noqa: ANN003
        class _Resp:
            id = "resp_bg_fail_123"
            status = "queued"

        return _Resp()

    def retrieve(self, response_id):  # noqa: ANN001
        class _Resp:
            id = response_id
            status = "failed"

        return _Resp()

    def cancel(self, response_id):  # noqa: ANN001
        return None


def _service_with_fake_client(tmp_path: Path, *, failing: bool) -> AgentService:
    get_settings.cache_clear()
    service = AgentService()
    service.openai_package_available = True
    service.settings.openai_api_key = "test-key"
    service.client = type(
        "_FakeClient",
        (),
        {"responses": _FakeResponsesFail() if failing else _FakeResponsesOk()},
    )()
    return service


def _service_with_flaky_client(tmp_path: Path) -> AgentService:
    get_settings.cache_clear()
    service = AgentService()
    service.openai_package_available = True
    service.settings.openai_api_key = "test-key"
    service.client = type("_FakeClient", (), {"responses": _FakeResponsesFlaky()})()
    return service


def _service_with_invalid_json_retry_client(tmp_path: Path) -> AgentService:
    get_settings.cache_clear()
    service = AgentService()
    service.openai_package_available = True
    service.settings.openai_api_key = "test-key"
    service.client = type("_FakeClient", (), {"responses": _FakeResponsesInvalidJsonThenOk()})()
    return service


def _service_with_invalid_backslash_json_client(tmp_path: Path) -> AgentService:
    get_settings.cache_clear()
    service = AgentService()
    service.openai_package_available = True
    service.settings.openai_api_key = "test-key"
    service.client = type("_FakeClient", (), {"responses": _FakeResponsesInvalidBackslashJson()})()
    return service


def test_request_state_artifact_completed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    service = _service_with_fake_client(tmp_path, failing=False)

    parsed = service._run_json_agent(
        agent_key="agent2",
        model="gpt-5.4",
        reasoning_effort="none",
        text_verbosity="medium",
        timeout_seconds=321,
        payload={"theorem_nl": "For all n, n = n"},
        artifact_prefix="problems/p/decomposer/attempt_1",
    )
    assert parsed["status"] == "completed"

    state_path = tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed"
    assert state["timeout_seconds"] == 321
    assert state["text_verbosity"] == "medium"

    get_settings.cache_clear()


def test_request_state_artifact_failed_on_request_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    service = _service_with_fake_client(tmp_path, failing=True)

    with pytest.raises(AgentExecutionError):
        service._run_json_agent(
            agent_key="agent2",
            model="gpt-5.4",
            reasoning_effort="none",
            text_verbosity="low",
            timeout_seconds=654,
            payload={"theorem_nl": "For all n, n = n"},
            artifact_prefix="problems/p/decomposer/attempt_1",
        )

    state_path = tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["status"] == "failed"
    assert state["error_class"] == "infrastructure_transient"
    assert state["timeout_seconds"] == 654
    assert state["text_verbosity"] == "low"

    second_attempt_path = tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_2.json"
    assert not second_attempt_path.exists()

    get_settings.cache_clear()


def test_request_state_artifact_retries_invalid_json_then_completes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    service = _service_with_invalid_json_retry_client(tmp_path)

    parsed = service._run_json_agent(
        agent_key="agent2",
        model="gpt-5.4",
        reasoning_effort="none",
        text_verbosity="low",
        timeout_seconds=222,
        payload={"theorem_nl": "For all n, n = n"},
        artifact_prefix="problems/p/decomposer/attempt_1",
    )
    assert parsed["status"] == "completed"

    first_state = json.loads(
        (tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json").read_text()
    )
    second_state = json.loads(
        (tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_2.json").read_text()
    )
    assert first_state["status"] == "failed"
    assert first_state["error_class"] == "invalid_agent_output"
    assert second_state["status"] == "completed"

    get_settings.cache_clear()


def test_request_state_artifact_repairs_invalid_backslashes_in_json_strings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    service = _service_with_invalid_backslash_json_client(tmp_path)

    parsed = service._run_json_agent(
        agent_key="agent1",
        model="gpt-5.4-nano",
        reasoning_effort="low",
        text_verbosity="low",
        timeout_seconds=222,
        payload={"statement_nl": "x"},
        artifact_prefix="problems/p/inputs",
    )
    assert parsed["status"] == "completed"
    assert parsed["semantic_sketch"]["normalized_claim"] == "x in Z_{\\ge 0}"

    state = json.loads((tmp_path / "problems/p/inputs/agent1_request_state_attempt_1.json").read_text())
    assert state["status"] == "completed"

    get_settings.cache_clear()


def test_request_state_artifact_failed_on_post_request_processing_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    service = _service_with_fake_client(tmp_path, failing=False)

    def _raise_usage_error(**kwargs):  # noqa: ANN003
        raise RuntimeError("usage write failed")

    monkeypatch.setattr(agents_module, "record_llm_usage", _raise_usage_error)

    with pytest.raises(AgentExecutionError) as exc_info:
        service._run_json_agent(
            agent_key="agent2",
            model="gpt-5.4",
            reasoning_effort="none",
            text_verbosity="low",
            timeout_seconds=123,
            payload={"theorem_nl": "For all n, n = n"},
            artifact_prefix="problems/p/decomposer/attempt_1",
        )
    assert exc_info.value.error_class == "infrastructure"

    state_path = tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["status"] == "failed"
    assert state["error_class"] == "infrastructure"

    request_error = tmp_path / "problems/p/decomposer/attempt_1/agent2_request_error_attempt_1.json"
    assert request_error.exists()
    request_error_payload = json.loads(request_error.read_text())
    assert request_error_payload["phase"] == "post_request_processing"
    assert request_error_payload["error_class"] == "infrastructure"

    get_settings.cache_clear()


def test_background_terminal_failure_is_retryable_and_persists_provider_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    get_settings.cache_clear()
    service = AgentService()
    service.openai_package_available = True
    service.settings.openai_api_key = "test-key"
    service.client = type("_FakeClient", (), {"responses": _FakeResponsesBackgroundTerminalFail()})()
    service._BACKGROUND_POLL_INTERVAL = 0
    db = FileStore(str(tmp_path / "data"))
    service.db_session = db
    service.runtime_execution_id = "exec_test"
    service.runtime_worker_job_id = "wrk_test"

    with pytest.raises(AgentExecutionError) as exc_info:
        service._run_json_agent(
            agent_key="agent4",
            model="gpt-5.4",
            reasoning_effort="none",
            text_verbosity="low",
            timeout_seconds=60,
            payload={"lemma_id": "lem_1", "statement_nl": "x = x"},
            artifact_prefix="problems/prob_bg/lemmas/lem_1/solver_attempt_2",
        )
    assert exc_info.value.error_class == "infrastructure_transient"

    state = json.loads(
        (tmp_path / "problems/prob_bg/lemmas/lem_1/solver_attempt_2/agent4_request_state_attempt_1.json").read_text()
    )
    assert state["status"] == "failed"
    assert state["error_class"] == "infrastructure_transient"
    assert state["response_id"] == "resp_bg_fail_123"

    error_payload = json.loads(
        (tmp_path / "problems/prob_bg/lemmas/lem_1/solver_attempt_2/agent4_request_error_attempt_1.json").read_text()
    )
    assert error_payload["response_id"] == "resp_bg_fail_123"
    assert error_payload["provider_terminal_status"] == "failed"
    assert isinstance(error_payload.get("provider_error_excerpt"), str)

    request_rows = RequestRecordRepository(db).list_by_problem("prob_bg", source="agent4", limit=20)
    assert request_rows
    latest = request_rows[-1]
    assert latest.status == "failed"
    assert latest.response_id == "resp_bg_fail_123"
    assert latest.error_class == "infrastructure_transient"
    assert latest.llm_model == "gpt-5.4"
    assert latest.llm_reasoning_effort == "none"
    assert latest.llm_text_verbosity == "low"
    assert latest.llm_timeout_seconds == 60

    get_settings.cache_clear()


def test_request_record_persists_resolved_llm_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path))
    get_settings.cache_clear()
    db = FileStore(str(tmp_path / "data"))
    service = AgentService(db_session=db)
    service.openai_package_available = True
    service.settings.openai_api_key = "test-key"
    service.client = type("_FakeClient", (), {"responses": _FakeResponsesOk()})()
    service.runtime_execution_id = "exec_cfg"
    service.runtime_worker_job_id = "wrk_cfg"

    parsed = service._run_json_agent(
        agent_key="agent2",
        model="gpt-5.4-pro",
        reasoning_effort="xhigh",
        text_verbosity="medium",
        timeout_seconds=3600,
        payload={"theorem_nl": "For all n, n = n"},
        artifact_prefix="problems/prob_cfg/decomposer/attempt_1",
    )
    assert parsed["status"] == "completed"

    request_rows = RequestRecordRepository(db).list_by_problem("prob_cfg", source="agent2", limit=20)
    assert request_rows
    latest = request_rows[-1]
    assert latest.llm_model == "gpt-5.4-pro"
    assert latest.llm_reasoning_effort == "xhigh"
    assert latest.llm_text_verbosity == "medium"
    assert latest.llm_timeout_seconds == 3600

    get_settings.cache_clear()
