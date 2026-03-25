from __future__ import annotations

from datetime import UTC, datetime
from os import getenv
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

app = FastAPI(title="Orthosolver Mock Lean Engine", version="0.2.0")


class JobEnvelope(BaseModel):
    job_id: str
    problem_id: str
    target_id: str
    target_kind: str
    mode: str
    lean_image_tag: str
    callback_url: str | None = None
    payload: dict[str, Any]


JOBS: dict[str, dict[str, Any]] = {}


def now_utc() -> datetime:
    return datetime.now(UTC)


def default_delay_seconds() -> int:
    return int(getenv("MOCK_LEAN_DEFAULT_DELAY_SECONDS", "2"))


@app.post("/v1/jobs", status_code=202)
def submit_job(job: JobEnvelope, response: Response, x_mock_behavior: str | None = Header(default=None)) -> dict[str, Any]:
    existing = JOBS.get(job.job_id)
    if existing:
        response.status_code = 409
        return {
            "job_id": existing["job_id"],
            "status": existing["status"],
            "created_at": existing["created_at"],
            "estimated_duration_seconds": existing["delay_seconds"],
        }

    delay = default_delay_seconds()
    record = {
        "job_id": job.job_id,
        "problem_id": job.problem_id,
        "target_id": job.target_id,
        "target_kind": job.target_kind,
        "mode": job.mode,
        "status": "queued",
        "payload": job.payload,
        "created_at_dt": now_utc(),
        "created_at": now_utc().isoformat(),
        "delay_seconds": delay,
        "mock_behavior": x_mock_behavior,
        "cancelled": False,
    }
    JOBS[job.job_id] = record

    return {
        "job_id": job.job_id,
        "status": "queued",
        "created_at": record["created_at"],
        "estimated_duration_seconds": delay,
    }


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    if job["cancelled"]:
        return {"job_id": job_id, "status": "cancelled", "mode": job["mode"], "created_at": job["created_at"]}

    elapsed = (now_utc() - job["created_at_dt"]).total_seconds()
    if elapsed < job["delay_seconds"]:
        return {
            "job_id": job_id,
            "status": "running",
            "mode": job["mode"],
            "repair_rounds_used": 0,
            "elapsed_seconds": int(elapsed),
            "updated_at": now_utc().isoformat(),
        }

    behavior = job.get("mock_behavior") or ""

    if behavior.startswith("fatal/"):
        error_class = behavior.split("/", 1)[1]
        return {
            "job_id": job_id,
            "status": "fatal",
            "mode": job["mode"],
            "result": {
                "decl_name": None,
                "lean_code": None,
                "compiler_ok": False,
                "repair_rounds_used": 0,
                "pinned_statement_signatures": None,
                "error_class": error_class,
                "error_scope": "proof",
                "error_message": "forced by mock behavior",
                "diagnostics": [],
                "math_gap_description": "forced",
                "recommended_next_step": "decompose_current",
                "routing_confidence": 0.99,
            },
            "created_at": job["created_at"],
            "completed_at": now_utc().isoformat(),
        }

    if behavior.startswith("repairable/"):
        error_class = behavior.split("/", 1)[1]
        return {
            "job_id": job_id,
            "status": "repairable",
            "mode": job["mode"],
            "result": {
                "decl_name": None,
                "lean_code": None,
                "compiler_ok": False,
                "repair_rounds_used": 1,
                "pinned_statement_signatures": None,
                "error_class": error_class,
                "error_scope": "proof",
                "error_message": "repairable forced by mock behavior",
                "diagnostics": [],
                "math_gap_description": None,
                "recommended_next_step": "retry_lean_only",
                "routing_confidence": 0.9,
            },
            "created_at": job["created_at"],
            "completed_at": now_utc().isoformat(),
        }

    if job["mode"] == "check_statement_plausibility":
        verdict = "plausible"
        if behavior.startswith("plausibility/"):
            verdict = behavior.split("/", 1)[1]
        return {
            "job_id": job_id,
            "status": "success",
            "mode": job["mode"],
            "result": {
                "verdict": verdict,
                "evidence": "mock default",
                "tactic_used": "none",
                "confidence": 0.9,
            },
            "created_at": job["created_at"],
            "completed_at": now_utc().isoformat(),
        }

    pinned_signatures = None
    if job["mode"] == "check_assembly":
        pinned_signatures = {}
        for lemma in job["payload"].get("lemmas", []):
            local_id = lemma.get("local_id", "L1")
            pinned_signatures[local_id] = f"theorem {local_id} : True"

    return {
        "job_id": job_id,
        "status": "success",
        "mode": job["mode"],
        "result": {
            "decl_name": f"decl_{job_id}",
            "lean_code": "theorem dummy : True := by trivial",
            "compiler_ok": True,
            "repair_rounds_used": 0,
            "pinned_statement_signatures": pinned_signatures,
            "error_class": None,
            "error_scope": None,
            "error_message": None,
            "diagnostics": [],
            "math_gap_description": None,
            "recommended_next_step": "accept",
            "routing_confidence": 0.95,
        },
        "created_at": job["created_at"],
        "completed_at": now_utc().isoformat(),
    }


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    JOBS[job_id]["cancelled"] = True
    JOBS[job_id]["status"] = "cancelled"
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/v1/health")
def health() -> dict[str, Any]:
    active_jobs = sum(1 for job in JOBS.values() if not job["cancelled"])
    queue_depth = sum(1 for job in JOBS.values() if job["status"] == "queued")
    return {"image_tag": "mock", "active_jobs": active_jobs, "max_concurrent_jobs": 100, "queue_depth": queue_depth}
