from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.domain.contracts import Agent1Output, SemanticSketch


def test_create_and_run_problem_nl_only_to_terminal() -> None:
    client = TestClient(app)
    payload = {
        "title": "Simple theorem",
        "statement_nl": "For all n, n = n",
        "config": {"mode": {"nl_only_mode": True}},
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    status = "created"
    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        status = run.json()["status"]
        if status in {"succeeded", "failed"}:
            break

    assert status == "succeeded"

    getp = client.get(f"/v1/problems/{problem_id}")
    assert getp.status_code == 200
    body = getp.json()
    assert body["problem"]["verification_level"] == "nl_only"
    assert body["problem"]["status"] == "succeeded"


def test_create_and_run_problem_with_simplified_config_surface() -> None:
    client = TestClient(app)
    payload = {
        "title": "Simplified config theorem",
        "statement_nl": "For all n, n = n",
        "config": {
            "mode": {"nl_only_mode": True},
            "decomposition": {
                "parallel_root_decompositions_n": 2,
                "parallel_root_take_k": 1,
                "max_consecutive_fatal_rejections_per_node": 2,
            },
            "lemma_solving": {
                "max_solver_retries_per_lemma": 4,
                "max_total_lemma_nodes": 64,
            },
            "llm": {"agent1": {"timeout_seconds": 600}, "agent5": {"timeout_seconds": 600}},
        },
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = "created"
    for _ in range(30):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded"


def test_progress_events_and_sse_resume() -> None:
    client = TestClient(app)
    payload = {
        "title": "Monitor theorem",
        "statement_nl": "For all x, x = x",
        "config": {"mode": {"nl_only_mode": True}},
    }
    create = client.post("/v1/problems", json=payload)
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    for _ in range(20):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        if run.json()["status"] in {"succeeded", "failed"}:
            break

    progress = client.get(f"/v1/problems/{problem_id}/progress")
    assert progress.status_code == 200
    assert "lemma_counts" in progress.json()
    assert "lean_job_counts" in progress.json()
    assert "per_lemma_lean_status" in progress.json()
    assert "lean_v2_track_id" in progress.json()
    assert "active_decomposition_status" in progress.json()
    assert "standby_decomposition_status" in progress.json()
    assert "latest_blocking_reason" in progress.json()

    lean_jobs = client.get(f"/v1/problems/{problem_id}/lean-jobs")
    assert lean_jobs.status_code == 200
    lean_jobs_body = lean_jobs.json()
    assert lean_jobs_body["problem_id"] == problem_id
    assert isinstance(lean_jobs_body["jobs"], list)

    events = client.get(f"/v1/problems/{problem_id}/events")
    assert events.status_code == 200
    rows = events.json()["events"]
    assert len(rows) > 0

    cursor = rows[-2]["event_id"] if len(rows) >= 2 else rows[-1]["event_id"]

    with client.stream(
        "GET",
        f"/v1/problems/{problem_id}/events/stream",
        headers={"Last-Event-ID": str(cursor)},
    ) as stream:
        text = ""
        for chunk in stream.iter_text():
            text += chunk
            if "event: terminal" in text:
                break

    assert "event: terminal" in text


def test_run_is_compatibility_alias_for_durable_start() -> None:
    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={"title": "Compatibility run", "statement_nl": "For all n, n = n", "config": {"mode": {"nl_only_mode": True}}},
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    run = client.post(f"/v1/problems/{problem_id}/run")
    assert run.status_code == 200
    body = run.json()
    assert body["execution_id"]

    execution = client.get(f"/v1/problems/{problem_id}/execution")
    assert execution.status_code == 200
    assert execution.json()["execution"]["execution_id"] == body["execution_id"]


def test_create_problem_applies_per_agent_llm_overrides(monkeypatch) -> None:
    from nl_engine.api import main as api_main

    captured: dict[str, object] = {}

    class _CaptureAgentService:
        def __init__(self, *args, **kwargs) -> None:
            captured["init_kwargs"] = dict(kwargs)
            captured["set_llm_overrides"] = None

        def set_llm_overrides(self, llm_overrides) -> None:
            captured["set_llm_overrides"] = llm_overrides

        def semantic_sketch(self, statement_nl: str, artifact_prefix: str) -> Agent1Output:
            return Agent1Output(
                status="completed",
                statement_nl_received=statement_nl,
                semantic_sketch=SemanticSketch(
                    variables=[],
                    quantifier_order=[],
                    domain_restrictions=[],
                    witness_dependencies=[],
                    normalized_claim=statement_nl,
                ),
                implicit_assumptions_surfaced=[],
                ambiguities=[],
            )

    monkeypatch.setattr(api_main, "AgentService", _CaptureAgentService)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "LLM override create",
            "statement_nl": "For all n, n = n",
            "config": {
                "mode": {"nl_only_mode": True},
                "llm": {
                    "agent1": {"model": "gpt-5-mini", "thinking_level": "high"},
                    "agent2": {"model": "gpt-5.4", "thinking_level": "low"},
                },
            },
        },
    )
    assert create.status_code == 200
    init_kwargs = captured.get("init_kwargs")
    assert isinstance(init_kwargs, dict)
    assert init_kwargs["llm_overrides"]["agent1"]["model"] == "gpt-5-mini"
    assert init_kwargs["llm_overrides"]["agent1"]["thinking_level"] == "high"
    # llm_overrides passed directly in constructor; set_llm_overrides no longer called separately
    assert init_kwargs["db_session"] is not None
