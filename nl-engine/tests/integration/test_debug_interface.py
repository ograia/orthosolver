from __future__ import annotations

from pathlib import Path
import threading
import time
from urllib.parse import quote

from fastapi.testclient import TestClient

from mock_lean.main import JOBS
from nl_engine.api.main import app
from nl_engine.api.run_state import acquire_problem_run, release_problem_run
from nl_engine.controller import orchestrator as orchestrator_module
from nl_engine.settings import get_settings
from tests.integration.test_api_standard_mode import MockLeanClientProxy


def _run_to_terminal(client: TestClient, problem_id: str, max_ticks: int = 160) -> str:
    status = "created"
    for _ in range(max_ticks):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        status = run.json()["status"]
        if status in {"succeeded", "failed"}:
            break
    return status


def test_debug_problem_template_list_and_limit() -> None:
    client = TestClient(app)

    template = client.get("/v1/debug/problem-create-template")
    assert template.status_code == 200
    t = template.json()["template"]
    assert "title" in t
    assert "statement_nl" in t
    assert t["config"]["mode"]["nl_only_mode"] is False
    assert "budget" in t["config"]
    assert t["config"]["llm"]["agent1"]["timeout_seconds"] == 600
    assert "parallel_root_decompositions_n" in t["config"]["decomposition"]
    assert "parallel_root_take_k" in t["config"]["decomposition"]
    assert t["config"]["decomposition"]["lemma_decomposition_candidates_n"] == 1
    assert "max_consecutive_fatal_rejections_per_lemma" in t["config"]["lemma_solving"]
    assert "max_minor_rejections_per_lemma" in t["config"]["lemma_solving"]
    assert "max_total_lemma_nodes" in t["config"]["lemma_solving"]
    assert "max_solver_attempts_per_lemma_total" in t["config"]["lemma_solving"]
    assert "max_consecutive_infrastructure_failures_per_lemma" in t["config"]["lemma_solving"]
    assert "max_solver_series_wall_clock_seconds_per_lemma" in t["config"]["lemma_solving"]
    assert "lean_engine" in t["config"]
    assert "model" in t["config"]["lean_engine"]
    assert t["config"]["lean_engine"]["no_lean4_refs"] is False
    assert t["config"]["lean_engine"]["lean_job_timeout_seconds"] == 300
    assert t["config"]["lean_engine"]["assemble_root_timeout_seconds"] == 300
    assert t["config"]["lean_engine"]["claude_activity_timeout_seconds"] == 0
    assert t["config"]["lean_engine"]["claude_init_timeout_seconds"] == 0
    assert "max_parallel_lean_jobs_per_problem" not in t["config"]["lean_engine"]
    assert "effective_max_parallel_lean_jobs" not in t["config"]["lean_engine"]
    assert "assembly_check_timeout_seconds" not in t["config"]["lean_engine"]
    assert "plausibility_check_timeout_seconds" not in t["config"]["lean_engine"]
    assert "claude_stall_timeout_seconds" not in t["config"]["lean_engine"]
    assert "claude_tool_wait_timeout_seconds" not in t["config"]["lean_engine"]
    assert "lean_mode" not in t["config"]["mode"]
    assert t["config"]["final_check"]["fail_problem_on_fatal"] is False
    assert "routing" not in t["config"]
    assert "global" not in t["config"]
    assert "ops" not in t["config"]

    create_a = client.post(
        "/v1/problems",
        json={"title": "Debug list A", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    create_b = client.post(
        "/v1/problems",
        json={"title": "Debug list B", "statement_nl": "For all x, x = x", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create_a.status_code == 200
    assert create_b.status_code == 200

    limited = client.get("/v1/debug/problems?limit=1")
    assert limited.status_code == 200
    assert len(limited.json()["problems"]) == 1

    listed = client.get("/v1/debug/problems?limit=20")
    assert listed.status_code == 200
    ids = [row["problem_id"] for row in listed.json()["problems"]]
    assert create_a.json()["problem_id"] in ids
    assert create_b.json()["problem_id"] in ids


def test_debug_problem_input_json_endpoint() -> None:
    client = TestClient(app)
    payload = {
        "title": "Debug input payload",
        "statement_nl": "For all n, n = n",
        "statement_lean": "theorem t : True := by trivial",
        "imports": ["Mathlib", "Mathlib.Data.Nat.Basic"],
        "lean_image_tag": "Orthosolver-lean-4.18.0-mathlib-v4.18.0",
        "initial_trusted_context": [{"decl_name": "my_decl", "lean_code": "theorem my_decl : True := by trivial"}],
        "config": {"mode": {"nl_only_mode": True}},
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    response = client.get(f"/v1/debug/problems/{problem_id}/input-json")
    assert response.status_code == 200
    body = response.json()
    assert body["problem_id"] == problem_id
    assert body["artifact_key"] == f"problems/{problem_id}/api/problem_create_request.json"
    assert body["input_json"]["title"] == payload["title"]
    assert body["input_json"]["statement_nl"] == payload["statement_nl"]
    assert body["input_json"]["statement_lean"] == payload["statement_lean"]
    assert body["input_json"]["imports"] == payload["imports"]
    assert body["input_json"]["initial_trusted_context"] == payload["initial_trusted_context"]
    assert body["input_json"]["config"]["mode"]["nl_only_mode"] is True


def test_debug_snapshot_contains_graph_and_lookup_maps() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Debug snapshot", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = _run_to_terminal(client, problem_id)
    assert terminal == "succeeded"

    snapshot = client.get(f"/v1/debug/problems/{problem_id}/snapshot?events_limit=50")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["problem_id"] == problem_id
    assert body["problem"]["nl_only_mode"] is True
    assert body["problem"]["status"] == "succeeded"
    assert "cost_summary" in body["problem"]
    assert "budget_guardrails" in body["problem"]
    assert body["root_tree"]["kind"] == "theorem"
    assert len(body["events"]) <= 50
    assert len(body["lemmas"]) > 0
    assert "node_graph" in body
    assert len(body["node_graph"]["nodes"]) >= 1
    assert isinstance(body["root_track_decomposition_ids"], list)
    assert isinstance(body["visible_lemma_ids"], list)
    assert isinstance(body["hidden_candidate_lemma_ids"], list)
    assert isinstance(body["lemma_owner_decomposition"], dict)
    assert body["nl_only_final_output"] is not None
    assert body["running_final_proof"] is not None
    assert body["final_proof"] is not None
    assert isinstance(body["lemma_by_id"], dict)
    assert isinstance(body["decomposition_by_id"], dict)
    assert isinstance(body["decomposition_candidate_by_id"], dict)
    assert isinstance(body["logical_decomposition_by_id"], dict)
    assert isinstance(body["lean_job_by_id"], dict)
    visible_lemma_ids = set(body["visible_lemma_ids"])
    owner_map = body["lemma_owner_decomposition"]
    graph_edges = body["node_graph"]["edges"]
    owned_visible_lemma_ids = [
        lemma_id
        for lemma_id, owner_id in owner_map.items()
        if owner_id and lemma_id in visible_lemma_ids
    ]
    assert owned_visible_lemma_ids
    for lemma_id in owned_visible_lemma_ids:
        owner_id = owner_map[lemma_id]
        assert any(edge["from"] == owner_id and edge["to"] == lemma_id for edge in graph_edges)
    lemma_row = body["lemmas"][0]
    assert "truth_status" in lemma_row
    assert "counterexample_status" in lemma_row
    assert "proof_attempts" in lemma_row
    assert "decomposition_round_count" in lemma_row
    assert "materialized_candidate_count" in lemma_row
    assert "promoted_decomposition_count" in lemma_row
    assert "next_action" in lemma_row
    assert "last_terminal_worker_result" in lemma_row
    assert "last_transition_reason" in lemma_row
    assert "last_activity_at" in lemma_row
    assert "decomposition_candidates" in lemma_row
    assert isinstance(body["decomposition_candidates"], list)
    if body["logical_decompositions"]:
        logical_row = body["logical_decompositions"][0]
        assert "logical_decomposition_id" in logical_row
        assert "current_revision_id" in logical_row
        assert "revision_count" in logical_row


def test_debug_request_log_and_artifact_content_standard_mode(monkeypatch) -> None:
    JOBS.clear()
    monkeypatch.setenv("MOCK_LEAN_DEFAULT_DELAY_SECONDS", "0")
    monkeypatch.setattr(orchestrator_module, "LeanClient", MockLeanClientProxy)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Debug artifacts", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": False}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = _run_to_terminal(client, problem_id)
    assert terminal == "succeeded"

    reqlog = client.get(f"/v1/debug/problems/{problem_id}/request-log?limit=1000")
    assert reqlog.status_code == 200
    entries = reqlog.json()["entries"]
    assert any(row["source"] == "api_create" for row in entries)
    assert any(row["source"] == "api_run" for row in entries)
    assert any(row["source"] == "lean" for row in entries)

    reqlog_lean = client.get(f"/v1/debug/problems/{problem_id}/request-log?source=lean&limit=1000")
    assert reqlog_lean.status_code == 200
    assert all(row["source"] == "lean" for row in reqlog_lean.json()["entries"])

    reqlog_runs = client.get(f"/v1/debug/problems/{problem_id}/request-log?source=api_run&limit=1000")
    assert reqlog_runs.status_code == 200
    assert len(reqlog_runs.json()["entries"]) >= 1
    assert all(row["source"] == "api_run" for row in reqlog_runs.json()["entries"])
    assert all(row["completion_status"] == "completed" for row in reqlog_runs.json()["entries"])
    sample_entry = reqlog_runs.json()["entries"][0]
    assert "llm_input_tokens" in sample_entry
    assert "llm_output_tokens" in sample_entry
    assert "llm_estimated_cost_usd" in sample_entry
    assert "response_id" in sample_entry
    assert "provider_terminal_status" in sample_entry
    assert "llm_model" in sample_entry
    assert "llm_reasoning_effort" in sample_entry

    usage = client.get(f"/v1/debug/problems/{problem_id}/llm-usage")
    assert usage.status_code == 200
    usage_body = usage.json()
    assert usage_body["problem_id"] == problem_id
    assert set(usage_body["pricing_by_model_usd_per_1m"].keys()) == {
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    }
    assert "totals" in usage_body
    assert "by_stage" in usage_body

    artifacts = client.get(f"/v1/debug/problems/{problem_id}/artifacts?include_worker_jobs=true&limit=2000")
    assert artifacts.status_code == 200
    items = artifacts.json()["artifacts"]
    keys = [row["artifact_key"] for row in items]
    assert any(key.startswith(f"problems/{problem_id}/lean_jobs/") for key in keys)
    assert any(key.startswith("worker_jobs/") for key in keys)

    json_key = next((key for key in keys if key.endswith(".json")), None)
    assert json_key is not None
    encoded_json_key = "/".join([quote(part, safe="") for part in json_key.split("/")])
    artifact_content = client.get(f"/v1/debug/artifacts/{encoded_json_key}")
    assert artifact_content.status_code == 200
    content_body = artifact_content.json()
    assert content_body["artifact_key"] == json_key
    assert content_body["format"] in {"json", "text"}

    settings = get_settings()
    text_key = f"debug_test/{problem_id}.txt"
    text_path = Path(settings.artifact_store_dir) / text_key
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("debug-text-artifact")

    text_content = client.get(f"/v1/debug/artifacts/{text_key}")
    assert text_content.status_code == 200
    assert text_content.json()["format"] == "text"

    traversal = client.get("/v1/debug/artifacts/%2E%2E%2FREADME.md")
    assert traversal.status_code == 400
    assert traversal.json()["error"]["code"] == "invalid_artifact_key"


def test_create_only_then_run_records_api_run_artifacts() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Run artifact checks", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    initial = client.get(f"/v1/problems/{problem_id}")
    assert initial.status_code == 200
    assert initial.json()["problem"]["status"] == "created"

    reqlog_before = client.get(f"/v1/debug/problems/{problem_id}/request-log?limit=1000")
    assert reqlog_before.status_code == 200
    entries_before = reqlog_before.json()["entries"]
    assert any(row["source"] == "api_create" for row in entries_before)
    assert not any(row["source"] == "api_run" for row in entries_before)

    api_create_idx = next(idx for idx, row in enumerate(entries_before) if row["source"] == "api_create")
    agent1_indices = [idx for idx, row in enumerate(entries_before) if row["source"] == "agent1"]
    if agent1_indices:
        assert api_create_idx <= agent1_indices[0]

    run = client.post(f"/v1/problems/{problem_id}/run", headers={"X-Debug-Run-Trigger": "manual"})
    assert run.status_code == 200

    reqlog_runs = client.get(f"/v1/debug/problems/{problem_id}/request-log?source=api_run&limit=1000")
    assert reqlog_runs.status_code == 200
    run_entries = reqlog_runs.json()["entries"]
    assert len(run_entries) >= 1
    assert any("trigger=manual" in row["summary"] for row in run_entries)
    assert all(row["completion_status"] == "completed" for row in run_entries)

    artifacts = client.get(f"/v1/debug/problems/{problem_id}/artifacts?include_worker_jobs=true&limit=2000")
    assert artifacts.status_code == 200
    keys = [row["artifact_key"] for row in artifacts.json()["artifacts"]]
    assert any(key.startswith(f"problems/{problem_id}/api/run_requests/") and key.endswith(".request.json") for key in keys)
    assert any(key.startswith(f"problems/{problem_id}/api/run_requests/") and key.endswith(".response.json") for key in keys)


def test_reconcile_incomplete_runs_marks_orphaned_tick_failed() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Reconcile orphan", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    settings = get_settings()
    request_key = Path(settings.artifact_store_dir) / f"problems/{problem_id}/api/run_requests/run_req_orphan.request.json"
    request_key.parent.mkdir(parents=True, exist_ok=True)
    request_key.write_text(
        '{"problem_id": "%s", "run_request_id": "run_req_orphan", "trigger": "submit_auto"}' % problem_id
    )

    reconcile = client.post(f"/v1/debug/problems/{problem_id}/reconcile-incomplete-runs")
    assert reconcile.status_code == 200
    body = reconcile.json()
    assert body["problem_id"] == problem_id
    assert body["reconciled_run_requests"] >= 1

    response_key = Path(settings.artifact_store_dir) / f"problems/{problem_id}/api/run_requests/run_req_orphan.response.json"
    assert response_key.exists()
    payload = response_key.read_text()
    assert "run_interrupted" in payload

    reqlog = client.get(f"/v1/debug/problems/{problem_id}/request-log?source=api_run&limit=100")
    assert reqlog.status_code == 200
    entries = reqlog.json()["entries"]
    assert entries
    assert any(entry["completion_status"] == "failed" for entry in entries)
    assert any("run_interrupted" in (entry.get("completion_detail") or "") for entry in entries)


def test_debug_problem_delete_and_reset_local() -> None:
    client = TestClient(app)
    create_a = client.post(
        "/v1/problems",
        json={"title": "Delete A", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    create_b = client.post(
        "/v1/problems",
        json={"title": "Delete B", "statement_nl": "For all x, x = x", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create_a.status_code == 200
    assert create_b.status_code == 200
    problem_a = create_a.json()["problem_id"]
    problem_b = create_b.json()["problem_id"]

    # Simulate in-flight work: hold the run lock, then release it from a
    # background thread.  The delete endpoint should cancel, wait for work
    # to drain, and then succeed.
    held = acquire_problem_run(problem_a)
    assert held is True

    def _release_after_delay() -> None:
        time.sleep(0.15)
        release_problem_run(problem_a)

    t = threading.Thread(target=_release_after_delay, daemon=True)
    t.start()

    deleted = client.delete(f"/v1/debug/problems/{problem_a}")
    t.join(timeout=5)
    assert deleted.status_code == 200
    payload = deleted.json()
    assert payload["scope"] == "problem"
    assert payload["problem_id"] == problem_a
    assert "problem_executions" in payload["deleted_rows"]
    assert "request_records" in payload["deleted_rows"]
    assert "problems" in payload["deleted_artifacts"]

    still_exists = client.get(f"/v1/problems/{problem_b}")
    assert still_exists.status_code == 200

    reset = client.post("/v1/debug/reset-local")
    assert reset.status_code == 200
    reset_body = reset.json()
    assert reset_body["scope"] == "global"
    assert reset_body["problem_id"] is None
    assert "problem_executions" in reset_body["deleted_rows"]
    assert "request_records" in reset_body["deleted_rows"]

    listed = client.get("/v1/debug/problems?limit=50")
    assert listed.status_code == 200
    assert listed.json()["problems"] == []


def test_debug_execution_list_and_delete_block_on_active_durable_execution(monkeypatch) -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Active execution", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    original_run_once = orchestrator_module.Orchestrator.run_once

    def slow_run_once(self, pid: str):
        time.sleep(0.25)
        return original_run_once(self, pid)

    monkeypatch.setattr(orchestrator_module.Orchestrator, "run_once", slow_run_once)

    start = client.post(f"/v1/problems/{problem_id}/start")
    assert start.status_code == 200
    execution_id = start.json()["execution"]["execution_id"]
    assert execution_id

    executions = client.get(f"/v1/debug/problems/{problem_id}/executions")
    assert executions.status_code == 200
    rows = executions.json()["executions"]
    assert any(row["execution_id"] == execution_id for row in rows)
    assert any(row["status"] in {"queued", "running", "waiting"} for row in rows)

    # Delete should cancel the active execution, wait for work to drain,
    # then delete the problem successfully.
    deleted = client.delete(f"/v1/debug/problems/{problem_id}")
    assert deleted.status_code == 200
    assert deleted.json()["scope"] == "problem"
    assert deleted.json()["problem_id"] == problem_id

    gone = client.get(f"/v1/problems/{problem_id}")
    assert gone.status_code == 404


def test_debug_reset_local_waits_for_running_problem_and_then_cleans() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Reset waiting", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    held = acquire_problem_run(problem_id)
    assert held is True

    def _release_later() -> None:
        time.sleep(0.1)
        release_problem_run(problem_id)

    releaser = threading.Thread(target=_release_later, daemon=True)
    releaser.start()
    reset = client.post("/v1/debug/reset-local")
    releaser.join(timeout=1)

    assert reset.status_code == 200
    body = reset.json()
    assert body["scope"] == "global"
    assert body["problem_id"] is None

    listed = client.get("/v1/debug/problems?limit=50")
    assert listed.status_code == 200
    assert listed.json()["problems"] == []


def test_debug_ui_static_routes_smoke() -> None:
    client = TestClient(app)
    index = client.get("/debug")
    assert index.status_code == 200
    assert "Orthosolver Debug Console" in index.text
    assert "submit-request" in index.text
    assert "api_run" not in index.text
    assert "agent2" in index.text
    assert "agent6" in index.text
    assert "Open Problem" in index.text
    assert "continue-problem" in index.text
    assert "pause-problem" in index.text
    assert "resume-infra" not in index.text
    assert "badge-auto" in index.text
    assert "badge-refresh" in index.text
    assert 'id="run-once"' not in index.text
    assert 'id="auto-run"' not in index.text
    assert "form-agent1-timeout" in index.text
    assert "form-config-fields" in index.text
    assert "overview-final-nl-output" in index.text
    assert "overview-problem-input-json" in index.text
    assert "overview-problem-input-meta" in index.text
    assert "reload-request-log" not in index.text
    assert "reload-artifacts" not in index.text
    assert "reload-events" not in index.text
    assert "refresh-raw" not in index.text

    script = client.get("/debug/static/app.js")
    assert script.status_code == 200
    assert "submitRequestFromEditor" in script.text
    assert "X-Debug-Run-Trigger" in script.text
    assert "startLiveRefresh" in script.text
    assert "reconcileIncompleteRuns" in script.text
    assert "continueCurrentProblem" in script.text
    assert "pauseCurrentProblem" in script.text
    assert "fetchProblemInput" in script.text
    assert "syncAutoRunWithProblemStatus" in script.text
    assert 'status === "failed" || status === "succeeded" || status === "paused"' in script.text
    assert "resumeAfterInfrastructureFailure" not in script.text
    assert "renderConfigFields" in script.text

    styles = client.get("/debug/static/styles.css")
    assert styles.status_code == 200
    assert "--bg" in styles.text
