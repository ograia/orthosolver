#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nl_engine.persistence.db import get_file_store
from nl_engine.persistence.repositories import RunCostRollupRepository


@dataclass
class EvalCase:
    case_id: str
    title: str
    statement_nl: str
    mode: str
    expected_terminal_status: str
    expected_verification_level: str
    expected_failure_reason: str | None
    routing_invariants: list[str]
    config_overrides: dict[str, Any]


def _load_cases(path: Path) -> list[EvalCase]:
    body = json.loads(path.read_text())
    cases = []
    for item in body["cases"]:
        cases.append(
            EvalCase(
                case_id=item["case_id"],
                title=item["title"],
                statement_nl=item["statement_nl"],
                mode=item["mode"],
                expected_terminal_status=item["expected_terminal_status"],
                expected_verification_level=item["expected_verification_level"],
                expected_failure_reason=item.get("expected_failure_reason"),
                routing_invariants=item.get("routing_invariants", []),
                config_overrides=item.get("config_overrides", {}),
            )
        )
    return cases


def _case_config(case: EvalCase) -> dict[str, Any]:
    base = {"mode": {"nl_only_mode": case.mode == "nl_only"}}
    if case.config_overrides:
        base.update(case.config_overrides)
    return base


def _fetch_problem_cost(database_url: str | None, problem_id: str) -> float | None:
    try:
        store = get_file_store()
        rows = RunCostRollupRepository(store).list_by_problem(problem_id)
        return float(sum(row.total_estimated_cost_usd for row in rows))
    except Exception:
        return None


def run_case(client: httpx.Client, case: EvalCase, tick_limit: int, poll_sleep: float, database_url: str | None) -> dict[str, Any]:
    create = client.post(
        "/v1/problems",
        json={
            "title": case.title,
            "statement_nl": case.statement_nl,
            "config": _case_config(case),
        },
    )
    create.raise_for_status()
    problem_id = create.json()["problem_id"]

    # Brief pause to allow background semantic sketch thread to complete.
    time.sleep(max(0.2, poll_sleep))

    started = time.perf_counter()
    ticks = 0
    terminal_status = "created"
    for _ in range(tick_limit):
        run = client.post(f"/v1/problems/{problem_id}/run")
        run.raise_for_status()
        ticks += 1
        terminal_status = run.json()["status"]
        if terminal_status in {"succeeded", "failed"}:
            break
        time.sleep(poll_sleep)
    elapsed = time.perf_counter() - started

    problem = client.get(f"/v1/problems/{problem_id}")
    problem.raise_for_status()
    problem_body = problem.json()["problem"]
    verification_level = problem_body["verification_level"]

    events = client.get(f"/v1/problems/{problem_id}/events?limit=1000")
    events.raise_for_status()
    event_rows = events.json()["events"]
    event_stages = [row["stage"] for row in event_rows]

    failure_reason = None
    if terminal_status == "failed":
        fail = client.get(f"/v1/problems/{problem_id}/failure-report")
        if fail.status_code == 200:
            failure_reason = fail.json()["failure_report"]["failure_reason"]

    missing_invariants = [stage for stage in case.routing_invariants if stage not in event_stages]
    passed = (
        terminal_status == case.expected_terminal_status
        and verification_level == case.expected_verification_level
        and (case.expected_failure_reason is None or failure_reason == case.expected_failure_reason)
        and not missing_invariants
    )

    decomposition_churn = sum(
        1 for stage in event_stages if stage in {"decomposition.generated", "decomposition.invalidated", "decomposition.promoted"}
    )
    estimated_cost_usd = _fetch_problem_cost(database_url, problem_id)

    return {
        "case_id": case.case_id,
        "problem_id": problem_id,
        "passed": passed,
        "ticks": ticks,
        "elapsed_seconds": round(elapsed, 4),
        "terminal_status": terminal_status,
        "verification_level": verification_level,
        "failure_reason": failure_reason,
        "missing_invariants": missing_invariants,
        "decomposition_churn": decomposition_churn,
        "estimated_cost_usd": estimated_cost_usd,
    }


def _build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    passed = [r for r in results if r["passed"]]
    solved = [r for r in results if r["terminal_status"] == "succeeded"]
    solved_with_cost = [r for r in solved if r["estimated_cost_usd"] is not None]
    return {
        "total_cases": len(results),
        "passed_cases": len(passed),
        "pass_rate": (len(passed) / len(results)) if results else 0.0,
        "avg_ticks": (sum(r["ticks"] for r in results) / len(results)) if results else 0.0,
        "avg_elapsed_seconds": (sum(r["elapsed_seconds"] for r in results) / len(results)) if results else 0.0,
        "avg_decomposition_churn": (sum(r["decomposition_churn"] for r in results) / len(results)) if results else 0.0,
        "avg_cost_per_solved_theorem_usd": (
            sum(r["estimated_cost_usd"] for r in solved_with_cost) / len(solved_with_cost)
            if solved_with_cost
            else None
        ),
    }


def _to_markdown(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Orthos Evaluator Report",
        "",
        f"- Total cases: {summary['total_cases']}",
        f"- Passed cases: {summary['passed_cases']}",
        f"- Pass rate: {summary['pass_rate']:.2%}",
        f"- Avg ticks: {summary['avg_ticks']:.2f}",
        f"- Avg elapsed seconds: {summary['avg_elapsed_seconds']:.2f}",
        f"- Avg decomposition churn: {summary['avg_decomposition_churn']:.2f}",
        f"- Avg cost per solved theorem (USD): {summary['avg_cost_per_solved_theorem_usd']}",
        "",
        "## Case Results",
        "",
        "| case_id | passed | status | verification | ticks | elapsed_s | churn | cost_usd |",
        "|---|---:|---|---|---:|---:|---:|---:|",
    ]
    for row in results:
        lines.append(
            f"| {row['case_id']} | {row['passed']} | {row['terminal_status']} | {row['verification_level']} | {row['ticks']} | {row['elapsed_seconds']} | {row['decomposition_churn']} | {row['estimated_cost_usd']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Orthos evaluator cases")
    parser.add_argument("--api-base-url", default="http://localhost:8000", help="Orchestrator API base URL")
    parser.add_argument("--cases", required=True, help="Path to evaluator case JSON")
    parser.add_argument("--out-json", default="eval_results.json")
    parser.add_argument("--out-md", default="eval_results.md")
    parser.add_argument("--tick-limit", type=int, default=200)
    parser.add_argument("--poll-sleep", type=float, default=0.1)
    args = parser.parse_args()

    cases = _load_cases(Path(args.cases))
    database_url = os.getenv("DATABASE_URL")

    with httpx.Client(base_url=args.api_base_url, timeout=60) as client:
        results = [run_case(client, case, args.tick_limit, args.poll_sleep, database_url) for case in cases]

    summary = _build_summary(results)
    out = {"summary": summary, "results": results}
    Path(args.out_json).write_text(json.dumps(out, indent=2, sort_keys=True))
    Path(args.out_md).write_text(_to_markdown(results, summary))


if __name__ == "__main__":
    main()
