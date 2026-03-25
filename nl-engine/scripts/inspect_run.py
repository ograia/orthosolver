#!/usr/bin/env python3
"""CLI tool to inspect problem runs from the terminal.

Usage:
    python scripts/inspect_run.py                          # list all runs
    python scripts/inspect_run.py <problem_id>             # show run summary + timeline
    python scripts/inspect_run.py <problem_id> --full      # include full agent I/O
    python scripts/inspect_run.py <problem_id> --agent 2   # show only agent2 calls
    python scripts/inspect_run.py <problem_id> --errors    # show only errors
    python scripts/inspect_run.py <problem_id> --tail      # follow the log in real-time
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ARTIFACTS_DIR = os.environ.get("ARTIFACT_STORE_DIR", ".artifacts")
DATA_DIR = os.environ.get("DATA_DIR", "data")


def _trunc(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[:n] + "..."


def _ts_short(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso[:19]


def list_runs() -> None:
    artifacts_root = Path(ARTIFACTS_DIR) / "problems"
    data_root = Path(DATA_DIR)

    problem_ids: set[str] = set()
    if artifacts_root.exists():
        for d in sorted(artifacts_root.iterdir()):
            if d.is_dir() and d.name.startswith("prob_"):
                problem_ids.add(d.name)
    if data_root.exists():
        for d in sorted(data_root.iterdir()):
            if d.is_dir() and d.name.startswith("prob_"):
                problem_ids.add(d.name)

    if not problem_ids:
        print("No runs found.")
        return

    print(f"{'PROBLEM ID':<45} {'STATUS':<12} {'TITLE':<30} {'LOG ENTRIES'}")
    print("-" * 100)

    for pid in sorted(problem_ids):
        status = "?"
        title = ""
        # Try data dir for structured state
        problem_json = data_root / pid / "problem.json"
        if problem_json.exists():
            try:
                p = json.loads(problem_json.read_text())
                status = p.get("status", "?")
                title = p.get("title", "")
            except Exception:
                pass

        # Count run_log entries
        log_file = artifacts_root / pid / "run_log.jsonl"
        log_count = 0
        if log_file.exists():
            log_count = sum(1 for _ in open(log_file))

        print(f"{pid:<45} {status:<12} {title:<30} {log_count}")


def _load_run_log(problem_id: str) -> list[dict]:
    log_file = Path(ARTIFACTS_DIR) / "problems" / problem_id / "run_log.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in open(log_file):
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _format_entry(entry: dict, *, full: bool = False) -> str:
    ts = _ts_short(entry.get("ts", ""))
    event = entry.get("event", "?")

    if event == "agent_start":
        agent = entry.get("agent", "?")
        model = entry.get("model", "?")
        target = entry.get("target_id", "")
        effort = entry.get("reasoning_effort", "")
        line = f"  {ts}  START  {agent:<8} model={model} effort={effort}"
        if target:
            line += f"  target={target}"
        if full:
            inp = entry.get("input", {})
            line += f"\n           INPUT: {json.dumps(inp, indent=2, default=str)}"
        return line

    if event == "agent_success":
        agent = entry.get("agent", "?")
        dur = entry.get("duration_s", "?")
        target = entry.get("target_id", "")
        line = f"  {ts}  OK     {agent:<8} {dur}s"
        if target:
            line += f"  target={target}"
        if full:
            out = entry.get("output", {})
            line += f"\n           OUTPUT: {json.dumps(out, indent=2, default=str)}"
        else:
            # Show key fields from output
            out = entry.get("output", {})
            highlights = {}
            for key in ("status", "decision", "confidence", "normalized_claim",
                        "lemma_count", "proof_summary", "overall_verdict"):
                if key in out:
                    highlights[key] = out[key]
            if highlights:
                line += f"  {highlights}"
        return line

    if event == "agent_error":
        agent = entry.get("agent", "?")
        dur = entry.get("duration_s", "?")
        err_class = entry.get("error_class", "?")
        err_msg = entry.get("error_message", "")
        return f"  {ts}  ERROR  {agent:<8} {dur}s  [{err_class}] {_trunc(err_msg, 80)}"

    if event == "state_change":
        stage = entry.get("stage", "?")
        old = entry.get("old_status") or "-"
        new = entry.get("new_status") or "-"
        target = entry.get("target_id", "")
        reason = entry.get("reason", "")
        line = f"  {ts}  STATE  {stage:<45} {old} -> {new}"
        if target:
            line += f"  [{target}]"
        if reason:
            line += f"  ({reason})"
        return line

    if event == "routing":
        stage = entry.get("stage", "?")
        decision = entry.get("decision", "?")
        target = entry.get("target_id", "")
        details = entry.get("details", {})
        line = f"  {ts}  ROUTE  {stage:<30} decision={decision}"
        if target:
            line += f"  [{target}]"
        if details:
            line += f"  {details}"
        return line

    # Unknown event
    return f"  {ts}  {event:<7} {json.dumps(entry, default=str)}"


def show_run(
    problem_id: str,
    *,
    full: bool = False,
    agent_filter: str | None = None,
    errors_only: bool = False,
) -> None:
    # Show problem info from data dir
    problem_json = Path(DATA_DIR) / problem_id / "problem.json"
    if problem_json.exists():
        try:
            p = json.loads(problem_json.read_text())
            print(f"Problem:  {problem_id}")
            print(f"Title:    {p.get('title', '?')}")
            print(f"Status:   {p.get('status', '?')}")
            print(f"Created:  {p.get('created_at', '?')}")
            print(f"NL-only:  {p.get('nl_only_mode', '?')}")
            print()
        except Exception:
            pass

    entries = _load_run_log(problem_id)
    if not entries:
        print(f"No run_log.jsonl found for {problem_id}")
        print(f"  (looked in {Path(ARTIFACTS_DIR) / 'problems' / problem_id / 'run_log.jsonl'})")
        # Fall back to checking events.jsonl
        events_file = Path(DATA_DIR) / problem_id / "events.jsonl"
        if events_file.exists():
            print(f"\nBut found events.jsonl ({sum(1 for _ in open(events_file))} entries):")
            for line in open(events_file):
                line = line.strip()
                if line:
                    try:
                        e = json.loads(line)
                        ts = _ts_short(e.get("created_at", ""))
                        stage = e.get("stage", "?")
                        old = e.get("old_status") or "-"
                        new = e.get("new_status") or "-"
                        print(f"  {ts}  {stage:<45} {old} -> {new}")
                    except Exception:
                        pass
        return

    # Apply filters
    filtered = entries
    if agent_filter:
        agent_key = f"agent{agent_filter}" if agent_filter.isdigit() else agent_filter
        filtered = [e for e in filtered if e.get("agent") == agent_key]
    if errors_only:
        filtered = [e for e in filtered if e.get("event") in ("agent_error",)]

    # Stats
    agent_calls = [e for e in entries if e.get("event") == "agent_start"]
    agent_ok = [e for e in entries if e.get("event") == "agent_success"]
    agent_err = [e for e in entries if e.get("event") == "agent_error"]
    print(f"Timeline ({len(entries)} entries | {len(agent_calls)} agent calls | "
          f"{len(agent_ok)} success | {len(agent_err)} errors):")
    print("-" * 100)

    for entry in filtered:
        print(_format_entry(entry, full=full))

    print()


def tail_run(problem_id: str) -> None:
    log_file = Path(ARTIFACTS_DIR) / "problems" / problem_id / "run_log.jsonl"
    print(f"Tailing {log_file} (Ctrl+C to stop)...")
    print()

    seen = 0
    try:
        while True:
            if log_file.exists():
                with open(log_file) as f:
                    lines = f.readlines()
                for line in lines[seen:]:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            print(_format_entry(entry))
                        except json.JSONDecodeError:
                            pass
                seen = len(lines)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect NL Engine problem runs")
    parser.add_argument("problem_id", nargs="?", help="Problem ID to inspect")
    parser.add_argument("--full", action="store_true", help="Show full agent I/O")
    parser.add_argument("--agent", type=str, help="Filter by agent (e.g. 2 or agent2)")
    parser.add_argument("--errors", action="store_true", help="Show only errors")
    parser.add_argument("--tail", action="store_true", help="Follow log in real-time")
    parser.add_argument("--artifacts-dir", default=None, help="Override ARTIFACT_STORE_DIR")
    parser.add_argument("--data-dir", default=None, help="Override DATA_DIR")
    args = parser.parse_args()

    global ARTIFACTS_DIR, DATA_DIR
    if args.artifacts_dir:
        ARTIFACTS_DIR = args.artifacts_dir
    if args.data_dir:
        DATA_DIR = args.data_dir

    if not args.problem_id:
        list_runs()
        return

    if args.tail:
        tail_run(args.problem_id)
        return

    show_run(
        args.problem_id,
        full=args.full,
        agent_filter=args.agent,
        errors_only=args.errors,
    )


if __name__ == "__main__":
    main()
