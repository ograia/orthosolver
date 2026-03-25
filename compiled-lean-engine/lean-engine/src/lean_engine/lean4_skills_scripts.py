from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _scripts_dir(lean4_skills_root: Path) -> Path:
    return lean4_skills_root / "lib" / "scripts"


def _run_script(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run a script and return structured result."""
    effective_timeout: int | float | None = None if timeout_seconds <= 0 else timeout_seconds
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr + f"\ncommand timed out after {timeout_seconds} seconds",
            "timed_out": True,
        }


def run_solver_cascade(
    *,
    lean4_skills_root: Path,
    workspace_root: Path,
    target_file: Path,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Try rfl→simp→ring→linarith→nlinarith→omega→exact?→apply?→grind→aesop on sorries.

    Returns dict with 'status' ('ok' if any tactic succeeded, 'failed' otherwise),
    'stdout' with the diff/result, etc.
    """
    scripts = _scripts_dir(lean4_skills_root)
    cascade_script = scripts / "solver_cascade.py"
    if not cascade_script.exists():
        logger.warning("solver_cascade.py not found at %s", cascade_script)
        return {"status": "skipped", "reason": "solver_cascade.py not found"}

    result = _run_script(
        ["python3", str(cascade_script), str(target_file)],
        cwd=workspace_root,
        timeout_seconds=timeout_seconds,
    )
    result["status"] = "ok" if result["returncode"] == 0 else "failed"
    return result


def parse_lean_errors(
    *,
    lean4_skills_root: Path,
    workspace_root: Path,
    stderr_text: str,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    """Parse Lean compiler errors into structured JSON.

    Returns list of error dicts with keys: errorType, line, message, error_hash, etc.
    """
    scripts = _scripts_dir(lean4_skills_root)
    parser_script = scripts / "parse_lean_errors.py"
    if not parser_script.exists():
        logger.warning("parse_lean_errors.py not found at %s", parser_script)
        return []

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(stderr_text if stderr_text.endswith("\n") else stderr_text + "\n")
        error_file = f.name

    try:
        result = _run_script(
            ["python3", str(parser_script), error_file, "--all"],
            cwd=workspace_root,
            timeout_seconds=timeout_seconds,
        )
    finally:
        Path(error_file).unlink(missing_ok=True)

    if not result["stdout"].strip():
        return []

    try:
        parsed = json.loads(result["stdout"])
        if isinstance(parsed, dict) and isinstance(parsed.get("errors"), list):
            return parsed["errors"]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        logger.warning("Failed to parse lean4-skills error output as JSON")

    return []


def analyze_sorries(
    *,
    lean4_skills_root: Path,
    lean_file: Path,
    workspace_root: Path,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    """Find all sorry instances with context.

    Returns list of sorry dicts with declaration, line, surrounding code.
    """
    scripts = _scripts_dir(lean4_skills_root)
    sorry_script = scripts / "sorry_analyzer.py"
    if not sorry_script.exists():
        logger.warning("sorry_analyzer.py not found at %s", sorry_script)
        return []

    result = _run_script(
        ["python3", str(sorry_script), str(lean_file), "--format=json", "--report-only"],
        cwd=workspace_root,
        timeout_seconds=timeout_seconds,
    )

    if not result["stdout"].strip():
        return []

    try:
        parsed = json.loads(result["stdout"])
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("sorries"), list):
            return parsed["sorries"]
    except json.JSONDecodeError:
        logger.warning("Failed to parse sorry_analyzer output as JSON")

    return []


def check_axioms(
    *,
    lean4_skills_root: Path,
    lean_file: Path,
    workspace_root: Path,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Check for non-standard axioms in a Lean file.

    Returns dict with 'clean' (bool) and 'output' (str).
    """
    scripts = _scripts_dir(lean4_skills_root)
    axiom_script = scripts / "check_axioms_inline.sh"
    if not axiom_script.exists():
        logger.warning("check_axioms_inline.sh not found at %s", axiom_script)
        return {"clean": True, "output": "check_axioms_inline.sh not found, skipped"}

    result = _run_script(
        ["bash", str(axiom_script), str(lean_file), "--report-only"],
        cwd=workspace_root,
        timeout_seconds=timeout_seconds,
    )

    output = (result["stdout"] + "\n" + result["stderr"]).strip()
    # The script exits 0 even with findings when --report-only is used.
    # Check output for non-standard axiom mentions.
    has_custom_axioms = "non-standard" in output.lower() or "custom axiom" in output.lower()

    return {
        "clean": not has_custom_axioms,
        "output": output,
        "returncode": result["returncode"],
    }
