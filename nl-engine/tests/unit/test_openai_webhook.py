from __future__ import annotations

from fastapi.testclient import TestClient

from nl_engine.api import main as api_main
from nl_engine.api.main import app
from nl_engine.domain.models import ProblemORM
from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import ProblemRepository, RequestRecordRepository


class _WebhookFinalizeAgent:
    def __init__(self, *args, db_session=None, **kwargs) -> None:
        self.db_session = db_session

    def set_runtime_context(self, **kwargs) -> None:  # noqa: ANN003
        return None

    def finalize_background_request_record(self, record):
        RequestRecordRepository(self.db_session).upsert(
            record.request_record_id,
            problem_id=record.problem_id,
            execution_id=record.execution_id,
            worker_job_id=record.worker_job_id,
            source=record.source,
            target_id=record.target_id,
            status="completed",
            response_id=record.provider_response_id or record.response_id,
            provider_response_id=record.provider_response_id or record.response_id,
            provider_status="completed",
            summary=record.summary,
            llm_model=record.llm_model,
            llm_reasoning_effort=record.llm_reasoning_effort,
            llm_text_verbosity=record.llm_text_verbosity,
            llm_timeout_seconds=record.llm_timeout_seconds,
            request_artifact_key=record.request_artifact_key,
            response_artifact_key="problems/p/decomposer/attempt_1/agent2_parsed_output_attempt_1.json",
        )
        return {"status": "completed", "candidates": []}


def test_openai_webhook_completes_pending_request(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "AgentService", _WebhookFinalizeAgent)
    store = get_file_store()
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="p",
            title="webhook",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )
    RequestRecordRepository(store).upsert(
        "reqrec_agent2_webhook",
        problem_id="p",
        execution_id=None,
        worker_job_id=None,
        source="agent2",
        target_id="p",
        status="provider_pending",
        response_id="resp_webhook_123",
        provider_response_id="resp_webhook_123",
        provider_status="queued",
        summary="theorem",
        llm_model="gpt-5.4-pro",
        llm_reasoning_effort="xhigh",
        llm_text_verbosity="medium",
        llm_timeout_seconds=3600,
        request_artifact_key="problems/p/decomposer/attempt_1/agent2_input.json",
    )

    client = TestClient(app)
    response = client.post(
        "/v1/openai/webhook",
        json={"type": "response.completed", "data": {"id": "resp_webhook_123"}},
    )
    assert response.status_code == 200

    updated = RequestRecordRepository(store).find_by_response_id("resp_webhook_123")
    assert updated is not None
    assert updated.status == "completed"
    assert updated.provider_status == "completed"
