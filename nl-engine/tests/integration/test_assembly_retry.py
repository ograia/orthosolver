from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from nl_engine.api.main import app
from nl_engine.controller import orchestrator as orchestrator_module


class BadStatementThenSuccessLeanClient:
    jobs: dict[str, dict] = {}

    def submit_job(self, body, request_id: str, mock_behavior=None):
        record = self.jobs.get(body["job_id"])
        if record is None:
            record = {"body": body, "created_at": datetime.now(UTC).isoformat()}
            self.jobs[body["job_id"]] = record
        return {
            "job_id": body["job_id"],
            "status": "queued",
            "created_at": record["created_at"],
            "estimated_duration_seconds": 0,
        }

    def submit_operation(self, operation: str, body, request_id: str, version: str = "v2", mock_behavior=None):
        operation_id = body.get("operation_id") or body.get("job_id")
        envelope = dict(body)
        envelope["job_id"] = operation_id
        envelope["mode"] = operation
        response = self.submit_job(envelope, request_id=request_id, mock_behavior=mock_behavior)
        response["operation_id"] = operation_id
        response["operation"] = operation
        return response

    def get_job(self, job_id: str):
        record = self.jobs[job_id]
        mode = record["body"]["mode"]
        now = datetime.now(UTC).isoformat()

        if mode == "check_assembly":
            attempt_index = int(job_id.rsplit("_", 1)[-1])
            if attempt_index == 1:
                return {
                    "job_id": job_id,
                    "status": "fatal",
                    "mode": mode,
                    "result": {
                        "decl_name": None,
                        "lean_code": None,
                        "compiler_ok": False,
                        "repair_rounds_used": 0,
                        "pinned_statement_signatures": None,
                        "error_class": "bad_statement_translation",
                        "error_scope": "statement",
                        "error_message": "forced first assembly translation error",
                        "diagnostics": [],
                        "math_gap_description": None,
                        "recommended_next_step": "retry_lean_only",
                        "routing_confidence": 0.9,
                    },
                    "created_at": record["created_at"],
                    "completed_at": now,
                }
            pinned = {
                lemma.get("local_id", "L1"): f"theorem {lemma.get('local_id', 'L1')} : True"
                for lemma in record["body"]["payload"].get("lemmas", [])
            }
            return {
                "job_id": job_id,
                "status": "success",
                "mode": mode,
                "result": {
                    "decl_name": f"decl_{job_id}",
                    "lean_code": "theorem dummy : True := by trivial",
                    "compiler_ok": True,
                    "repair_rounds_used": 1,
                    "pinned_statement_signatures": pinned,
                    "error_class": None,
                    "error_scope": None,
                    "error_message": None,
                    "diagnostics": [],
                    "math_gap_description": None,
                    "recommended_next_step": "accept",
                    "routing_confidence": 0.95,
                },
                "created_at": record["created_at"],
                "completed_at": now,
            }

        if mode == "check_statement_plausibility":
            return {
                "job_id": job_id,
                "status": "success",
                "mode": mode,
                "result": {
                    "verdict": "plausible",
                    "evidence": "mock",
                    "tactic_used": "none",
                    "confidence": 0.9,
                },
                "created_at": record["created_at"],
                "completed_at": now,
            }

        return {
            "job_id": job_id,
            "status": "success",
            "mode": mode,
            "result": {
                "decl_name": f"decl_{job_id}",
                "lean_code": "theorem dummy : True := by trivial",
                "compiler_ok": True,
                "repair_rounds_used": 0,
                "pinned_statement_signatures": None,
                "error_class": None,
                "error_scope": None,
                "error_message": None,
                "diagnostics": [],
                "math_gap_description": None,
                "recommended_next_step": "accept",
                "routing_confidence": 0.95,
            },
            "created_at": record["created_at"],
            "completed_at": now,
        }

    def get_operation(self, operation_id: str, version: str = "v2"):
        payload = self.get_job(operation_id)
        payload.setdefault("operation_id", operation_id)
        payload.setdefault("operation", payload.get("mode"))
        return payload

    def cancel_job(self, job_id: str):
        return {"job_id": job_id, "status": "cancelled"}

    def health(self):
        return {"image_tag": "mock", "active_jobs": 0, "max_concurrent_jobs": 100, "queue_depth": 0}


def test_bad_statement_translation_retries_once_then_succeeds(monkeypatch) -> None:
    BadStatementThenSuccessLeanClient.jobs.clear()
    monkeypatch.setattr(orchestrator_module, "LeanClient", BadStatementThenSuccessLeanClient)

    client = TestClient(app)
    create = client.post(
        "/v1/problems",
        json={
            "title": "Assembly retry theorem",
            "statement_nl": "For all n, n = n",
            "config": {"mode": {"nl_only_mode": False}},
        },
    )
    assert create.status_code == 200
    problem_id = create.json()["problem_id"]

    terminal = None
    for _ in range(140):
        run = client.post(f"/v1/problems/{problem_id}/run")
        assert run.status_code == 200
        terminal = run.json()["status"]
        if terminal in {"succeeded", "failed"}:
            break

    assert terminal == "succeeded"

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    assert events.status_code == 200
    rows = events.json()["events"]
    stages = [row["stage"] for row in rows]
    assert "decomposition.assembly_retry" in stages
    assert any(
        row["stage"] == "lean.job_submitted"
        and row["reason"] == "check_assembly"
        and row["worker_job_id"]
        and row["worker_job_id"].endswith("_2")
        for row in rows
    )
