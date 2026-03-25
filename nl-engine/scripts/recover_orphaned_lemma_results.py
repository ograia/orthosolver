"""
One-off recovery script for prob_20260316200002_e457403f.

Reads completed solver/vetter artifacts from .artifacts/worker_jobs/ and
patches the corresponding lemma JSON files in data/ that were never updated
due to Bug 1 (batch results discarded on single failure).

Also creates VetterReport records that were never persisted.

Usage:
    python scripts/recover_orphaned_lemma_results.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROBLEM_ID = "prob_20260316200002_e457403f"
DATA_DIR = Path("data") / PROBLEM_ID
ARTIFACTS_DIR = Path(".artifacts/worker_jobs")

# The 3 lemmas with completed solve + vet artifacts
RECOVERABLE = [
    "lem_20260316204001_b410c175",
    "lem_20260316204033_8a672840",
    "lem_20260316204056_24f32f47",
]

NL_ONLY_MODE = True
dry_run = "--dry-run" in sys.argv


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def save_json(path: Path, data: dict) -> None:
    if dry_run:
        print(f"  [dry-run] would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))


def recover_lemma(lemma_id: str) -> None:
    print(f"\n--- Recovering {lemma_id} ---")

    # Load current lemma state
    lemma_path = DATA_DIR / "lemmas" / f"{lemma_id}.json"
    lemma = load_json(lemma_path)
    print(f"  Current: proof_status={lemma['proof_status']}, routing_status={lemma['routing_status']}, "
          f"solver_attempt_count={lemma['solver_attempt_count']}")

    if lemma["routing_status"] == "done":
        print("  Already done, skipping.")
        return

    # Load solver artifact
    solve_result_path = ARTIFACTS_DIR / f"wrk_{lemma_id}_solve_1" / "result.json"
    if not solve_result_path.exists():
        print(f"  No solver artifact at {solve_result_path}, skipping.")
        return
    solve_result = load_json(solve_result_path)
    if solve_result["status"] != "completed":
        print(f"  Solver status={solve_result['status']}, skipping.")
        return
    solver_output = solve_result["output"]
    if solver_output["status"] != "proved":
        print(f"  Solver output status={solver_output['status']}, skipping.")
        return

    # Load vetter artifact
    vet_result_path = ARTIFACTS_DIR / f"wrk_{lemma_id}_vet_1" / "result.json"
    if not vet_result_path.exists():
        print(f"  No vetter artifact at {vet_result_path}, skipping.")
        return
    vet_result = load_json(vet_result_path)
    if vet_result["status"] != "completed":
        print(f"  Vetter status={vet_result['status']}, skipping.")
        return
    vet_output = vet_result["output"]

    # Create vetter report
    now = datetime.now(UTC).isoformat()
    report_id = f"rep_recovered_{lemma_id}"
    report = {
        "report_id": report_id,
        "problem_id": PROBLEM_ID,
        "target_id": lemma_id,
        "target_kind": "lemma",
        "statement_status": vet_output.get("statement_status", "plausible"),
        "proof_status": vet_output.get("proof_status", "complete"),
        "drift_assessment": vet_output.get("drift_assessment", {}),
        "recommended_action": vet_output.get("recommended_action", "send_to_lean"),
        "reason": vet_output.get("reason", ""),
        "confidence": vet_output.get("confidence", 0.0),
        "feedback_for_solver": vet_output.get("feedback_for_solver"),
        "detailed_findings": vet_output.get("detailed_findings", []),
        "created_at": now,
        "updated_at": now,
    }
    report_path = DATA_DIR / "vetter_reports" / f"{report_id}.json"
    print(f"  Creating vetter report: {report_id}")
    save_json(report_path, report)

    # Patch lemma
    lemma["solver_attempt_count"] = max(lemma["solver_attempt_count"], 1)
    lemma["latest_nl_proof"] = solver_output.get("proof_nl")
    lemma["latest_vetter_report_id"] = report_id
    lemma["statement_status"] = vet_output.get("statement_status", "plausible")

    if NL_ONLY_MODE:
        lemma["proof_status"] = "nl_accepted"
        lemma["routing_status"] = "done"
    else:
        lemma["proof_status"] = "proof_vetted"
        lemma["routing_status"] = "send_to_lean"

    lemma["updated_at"] = now

    print(f"  Patching lemma: proof_status={lemma['proof_status']}, "
          f"routing_status={lemma['routing_status']}, "
          f"solver_attempt_count={lemma['solver_attempt_count']}, "
          f"proof_len={len(lemma.get('latest_nl_proof') or '')}")
    save_json(lemma_path, lemma)


def main() -> None:
    if dry_run:
        print("=== DRY RUN MODE ===\n")

    # Verify data dir exists
    if not DATA_DIR.exists():
        print(f"ERROR: {DATA_DIR} does not exist")
        sys.exit(1)

    # Ensure vetter_reports dir exists
    vetter_dir = DATA_DIR / "vetter_reports"
    if not dry_run:
        vetter_dir.mkdir(parents=True, exist_ok=True)

    for lemma_id in RECOVERABLE:
        recover_lemma(lemma_id)

    # Summary
    print("\n--- Summary ---")
    for lemma_id in RECOVERABLE:
        lemma = load_json(DATA_DIR / "lemmas" / f"{lemma_id}.json")
        print(f"  {lemma_id}: proof_status={lemma['proof_status']}, routing_status={lemma['routing_status']}")

    remaining = []
    dec = load_json(DATA_DIR / "decompositions" / "dec_20260316204001_965476c3.json")
    for lid in dec["lemma_ids"]:
        lem = load_json(DATA_DIR / "lemmas" / f"{lid}.json")
        if lem["routing_status"] != "done":
            remaining.append(lid)
    print(f"\n  Active decomposition has {len(dec['lemma_ids'])} lemmas, {len(dec['lemma_ids']) - len(remaining)} done, {len(remaining)} remaining")
    if remaining:
        print(f"  Remaining: {remaining}")


if __name__ == "__main__":
    main()
