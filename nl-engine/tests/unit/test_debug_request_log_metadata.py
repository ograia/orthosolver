from __future__ import annotations

import json
from pathlib import Path

from nl_engine.api.debug import _collect_request_log_entries
from nl_engine.artifacts.browser import ArtifactBrowser
from nl_engine.domain.models import ProblemORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import ProblemRepository, RequestRecordRepository


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _store(tmp_path: Path) -> FileStore:
    store = FileStore(str(tmp_path / "data"))
    ProblemRepository(store).create(
        ProblemORM(
            problem_id="p",
            title="t",
            lean_image_tag="img",
            config={},
            status="created",
            input_mode="nl_only",
            nl_only_mode=True,
            verification_level="nl_only",
        )
    )
    return store


def test_request_log_uses_request_record_llm_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    browser = ArtifactBrowser(str(tmp_path))
    artifact_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write_json(tmp_path / artifact_key, {"theorem_nl": "For all n, n = n"})

    RequestRecordRepository(store).upsert(
        "reqrec_agent2_p_1",
        problem_id="p",
        execution_id="exec_1",
        worker_job_id="wrk_1",
        source="agent2",
        target_id="p",
        status="completed",
        summary="theorem",
        llm_model="gpt-5.4-pro",
        llm_reasoning_effort="high",
        llm_text_verbosity="medium",
        llm_timeout_seconds=3600,
        request_artifact_key=artifact_key,
    )

    entries = _collect_request_log_entries(store, browser, "p", source_filter="agent2", limit=20)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.llm_model == "gpt-5.4-pro"
    assert entry.llm_reasoning_effort == "high"
    assert entry.llm_text_verbosity == "medium"
    assert entry.llm_timeout_seconds == 3600


def test_request_log_falls_back_to_request_state_llm_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    browser = ArtifactBrowser(str(tmp_path))
    artifact_key = "problems/p/decomposer/attempt_1/agent2_input.json"
    _write_json(tmp_path / artifact_key, {"theorem_nl": "For all n, n = n"})
    _write_json(
        tmp_path / "problems/p/decomposer/attempt_1/agent2_request_state_attempt_1.json",
        {
            "status": "completed",
            "model": "gpt-5.4",
            "reasoning_effort": "low",
            "text_verbosity": "medium",
            "timeout_seconds": 600,
            "worker_job_id": "wrk_state",
        },
    )

    RequestRecordRepository(store).upsert(
        "reqrec_agent2_p_2",
        problem_id="p",
        execution_id="exec_2",
        worker_job_id="wrk_state",
        source="agent2",
        target_id="p",
        status="completed",
        summary="theorem",
        request_artifact_key=artifact_key,
    )

    entries = _collect_request_log_entries(store, browser, "p", source_filter="agent2", limit=20)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.llm_model == "gpt-5.4"
    assert entry.llm_reasoning_effort == "low"
    assert entry.llm_text_verbosity == "medium"
    assert entry.llm_timeout_seconds == 600
