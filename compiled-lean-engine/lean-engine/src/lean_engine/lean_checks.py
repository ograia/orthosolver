from __future__ import annotations

import io
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .artifact_io import RunPaths, sanitize_component, write_text


def _ensure_lean_on_path() -> None:
    """Add elan bin dir to PATH if lake is not already findable."""
    if shutil.which("lake") is not None:
        return
    elan_bin = Path.home() / ".elan" / "bin"
    if elan_bin.exists():
        os.environ["PATH"] = str(elan_bin) + os.pathsep + os.environ.get("PATH", "")


_ensure_lean_on_path()


@dataclass(frozen=True)
class LeanCommandResult:
    check_name: str
    command: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "command": list(self.command),
            "cwd": str(self.cwd),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": self.duration_seconds,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class LeanWorkspacePreparationResult:
    status: str
    checks: tuple[LeanCommandResult, ...]
    error_class: str | None = None
    message: str | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [item.to_dict() for item in self.checks],
            "error_class": self.error_class,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
        }


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def check_lean_file(
    workspace_root: Path,
    relative_file: str,
    *,
    timeout_seconds: int | None = 2700,
    runner: SubprocessRunner = subprocess.run,
    lake_jobs: int = 0,
) -> LeanCommandResult:
    command = ("lake", "env", "lean", relative_file)
    result = _run_command(
        check_name=f"file_{sanitize_component(relative_file)}",
        workspace_root=workspace_root,
        command=command,
        timeout_seconds=timeout_seconds,
        runner=runner,
        lake_jobs=lake_jobs,
    )
    return result


def check_lean_project(
    workspace_root: Path,
    *,
    timeout_seconds: int | None = 2700,
    runner: SubprocessRunner = subprocess.run,
    log_dir: Path | None = None,
    lake_jobs: int = 0,
) -> LeanCommandResult:
    return _run_command(
        check_name="project_build",
        workspace_root=workspace_root,
        command=("lake", "build"),
        timeout_seconds=timeout_seconds,
        runner=runner,
        log_dir=log_dir,
        lake_jobs=lake_jobs,
    )


def check_lean_module(
    workspace_root: Path,
    module_name: str,
    *,
    timeout_seconds: int | None = 2700,
    runner: SubprocessRunner = subprocess.run,
    lake_jobs: int = 0,
) -> LeanCommandResult:
    return _run_command(
        check_name=f"module_{sanitize_component(module_name)}",
        workspace_root=workspace_root,
        command=("lake", "build", module_name),
        timeout_seconds=timeout_seconds,
        runner=runner,
        lake_jobs=lake_jobs,
    )


def rebuild_module_olean(
    workspace_root: Path,
    relative_file: str,
    module_name: str,
    *,
    timeout_seconds: int | None = 2700,
    runner: SubprocessRunner = subprocess.run,
) -> LeanCommandResult:
    """Rebuild a single module's olean/ilean without Lake's dependency tracker.

    Uses ``lake env lean <file> -o <olean> -i <ilean>`` which runs the Lean
    compiler directly with the correct LEAN_PATH but does NOT trigger Lake's
    full dependency rebuild.  This avoids the pathological case where Lake
    decides to recompile Mathlib from source due to timestamp inconsistencies
    in a hardlink-cached ``.lake/`` directory.
    """
    # Derive output paths from module name.
    # e.g. module "Orthos.Statements" -> .lake/build/lib/lean/Orthos/Statements.olean
    parts = module_name.replace(".", "/")
    olean_path = f".lake/build/lib/lean/{parts}.olean"
    ilean_path = f".lake/build/lib/lean/{parts}.ilean"

    # Ensure parent directory exists.
    olean_dir = workspace_root / Path(olean_path).parent
    olean_dir.mkdir(parents=True, exist_ok=True)

    command = ("lake", "env", "lean", relative_file, "-o", olean_path, "-i", ilean_path)
    return _run_command(
        check_name=f"rebuild_olean_{sanitize_component(module_name)}",
        workspace_root=workspace_root,
        command=command,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )


def run_phase02_checks(
    workspace_root: Path,
    *,
    timeout_seconds: int | None = 2700,
    runner: SubprocessRunner = subprocess.run,
    lake_jobs: int = 0,
) -> list[LeanCommandResult]:
    checks = [
        check_lean_file(workspace_root, "Orthos/Statements.lean", timeout_seconds=timeout_seconds, runner=runner, lake_jobs=lake_jobs),
        check_lean_file(workspace_root, "Orthos/Lemmas.lean", timeout_seconds=timeout_seconds, runner=runner, lake_jobs=lake_jobs),
        check_lean_file(workspace_root, "Orthos/Root.lean", timeout_seconds=timeout_seconds, runner=runner, lake_jobs=lake_jobs),
        check_lean_project(workspace_root, timeout_seconds=timeout_seconds, runner=runner, lake_jobs=lake_jobs),
    ]
    return checks


def prepare_lean_workspace(
    workspace_root: Path,
    *,
    timeout_seconds: int | None = 180,
    runner: SubprocessRunner = subprocess.run,
    log_dir: Path | None = None,
    skip_cache_get: bool = False,
    lake_jobs: int = 0,
    require_cache: bool = False,
) -> LeanWorkspacePreparationResult:
    checks: list[LeanCommandResult] = []

    if skip_cache_get:
        print("[lean_engine] skipping lake cache get (workspace cache hit)", file=sys.stderr, flush=True)
    else:
        # Fetch pre-built Mathlib oleans from Reservoir cache before building.
        cache_check = _run_command(
            check_name="cache_get",
            workspace_root=workspace_root,
            command=("lake", "cache", "get"),
            timeout_seconds=timeout_seconds,
            runner=runner,
            log_dir=log_dir,
        )
        checks.append(cache_check)
        if not cache_check.ok:
            print(
                f"[lean_engine] lake cache get failed (exit {cache_check.returncode}), "
                "falling back to full build",
                file=sys.stderr, flush=True,
            )

    # Check for Mathlib oleans before starting lake build.
    # If no oleans exist, lake will compile all ~7700 Mathlib modules from source (~60 min).
    # Mathlib oleans live under .lake/packages/mathlib/.lake/build/lib/lean/
    mathlib_olean_sentinel = workspace_root / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "lib" / "lean" / "Mathlib.olean"
    if not mathlib_olean_sentinel.exists():
        if require_cache:
            msg = (
                "FATAL: Mathlib oleans not found (.lake/build/lib/Mathlib.olean missing). "
                "A full Mathlib rebuild would take ~60 minutes and saturate all CPU cores. "
                "Seed the workspace cache first (see CLAUDE.md) or remove --require-cache."
            )
            print(f"[lean_engine] {msg}", file=sys.stderr, flush=True)
            return LeanWorkspacePreparationResult(
                status="fatal",
                checks=tuple(checks),
                error_class="cache_miss_requires_full_rebuild",
                message=msg,
                diagnostics=(msg,),
            )
        else:
            print(
                "[lean_engine] WARNING: Mathlib oleans not found — lake build will compile "
                "~7700 modules from source (~60 min, high CPU). Consider seeding the cache first.",
                file=sys.stderr, flush=True,
            )

    project_check = check_lean_project(workspace_root, timeout_seconds=timeout_seconds, runner=runner, log_dir=log_dir, lake_jobs=lake_jobs)
    checks.append(project_check)
    if not project_check.ok:
        diagnostics = summarize_lean_command_result(project_check)
        return LeanWorkspacePreparationResult(
            status="fatal",
            checks=tuple(checks),
            error_class=classify_preparation_failure(diagnostics, check_name=project_check.check_name),
            message="Lean workspace preparation failed before theorem formalization.",
            diagnostics=tuple(diagnostics),
        )

    return LeanWorkspacePreparationResult(
        status="ok",
        checks=tuple(checks),
    )


def summarize_lean_command_result(result: LeanCommandResult, *, limit: int = 10) -> list[str]:
    diagnostics: list[str] = []
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    diagnostics.extend(stderr_lines[:limit])
    if len(diagnostics) < limit:
        diagnostics.extend(stdout_lines[: limit - len(diagnostics)])
    if not diagnostics:
        diagnostics.append("Lean command failed with no emitted diagnostics.")
    return diagnostics


def classify_preparation_failure(diagnostics: list[str], *, check_name: str) -> str:
    text = "\n".join(diagnostics).lower()
    if "timed out after" in text:
        return "workspace_preparation_timeout"
    if (
        "toolchain not updated; multiple toolchain candidates" in text
        or "does not match the toolchain version" in text
        or "unknown attribute `[implicit_reducible]`" in text
        or "unknown option `backward.isdefeq.respecttransparency`" in text
    ):
        return "lean_toolchain_mismatch"
    if (
        "reservoir lookup failed" in text
        or "could not materialize package" in text
        or "failed to download" in text
        or "network is unreachable" in text
        or "curl:" in text
    ):
        return "environment_dependency_missing"
    if (
        "unknown module prefix 'mathlib'" in text
        or "no directory 'mathlib'" in text
        or "mathlib.olean" in text
    ):
        return "workspace_unprepared"
    if "unknown package" in text or "unknown import" in text:
        return "workspace_unprepared"
    if check_name == "project_build":
        return "lean_project_not_buildable"
    return "workspace_unprepared"


def write_diagnostics(results: list[LeanCommandResult], run_paths: RunPaths) -> list[Path]:
    written: list[Path] = []
    for result in results:
        path = run_paths.diagnostics_dir / f"{result.check_name}.txt"
        content = _format_result(result)
        write_text(path, content)
        written.append(path)
    return written


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill a process and its entire process group (if started with start_new_session=True)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        # Fallback: process group may not exist (e.g. already exited)
        try:
            proc.kill()
        except OSError:
            pass


_lake_jobs_logged: set[int] = set()


def _lake_env(lake_jobs: int) -> dict[str, str] | None:
    """Return an env dict with LEAN_NUM_THREADS set if lake_jobs > 0, else None (inherit)."""
    if lake_jobs <= 0:
        return None
    env = os.environ.copy()
    env["LEAN_NUM_THREADS"] = str(lake_jobs)
    if lake_jobs not in _lake_jobs_logged:
        _lake_jobs_logged.add(lake_jobs)
        print(f"[lean_engine] lake parallelism capped to {lake_jobs} threads (LEAN_NUM_THREADS)", file=sys.stderr, flush=True)
    return env


def _run_command(
    *,
    check_name: str,
    workspace_root: Path,
    command: tuple[str, ...],
    timeout_seconds: int | None,
    runner: SubprocessRunner,
    log_dir: Path | None = None,
    lake_jobs: int = 0,
) -> LeanCommandResult:
    cwd = workspace_root.expanduser().resolve()
    effective_timeout = _effective_subprocess_timeout(timeout_seconds)
    start = time.time()

    # Build env with optional LEAN_NUM_THREADS cap for lake parallelism.
    env = _lake_env(lake_jobs)

    # If a log_dir is provided and we're using the real subprocess.run,
    # stream output to a live log file so the user can monitor with tail -f.
    if log_dir is not None and runner is subprocess.run:
        return _run_command_streaming(
            check_name=check_name,
            cwd=cwd,
            command=command,
            timeout=effective_timeout,
            timeout_seconds_raw=timeout_seconds,
            log_dir=log_dir,
            start=start,
            env=env,
        )

    # Use start_new_session so child processes get their own process group.
    # This allows clean cleanup via os.killpg if the pipeline is killed.
    # Only pass to real subprocess.run — mock runners may not accept it.
    extra_kwargs: dict[str, Any] = {}
    if runner is subprocess.run:
        extra_kwargs["start_new_session"] = True
    try:
        completed = runner(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
            env=env,
            **extra_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - start
        timeout_label = (
            f"{int(timeout_seconds)} seconds"
            if isinstance(timeout_seconds, int) and timeout_seconds > 0
            else "configured limit"
        )
        timeout_stderr = _coerce_timeout_stream(exc.stderr) + f"\ncommand timed out after {timeout_label}\n"
        return LeanCommandResult(
            check_name=check_name,
            command=command,
            cwd=cwd,
            returncode=124,
            stdout=_coerce_timeout_stream(exc.stdout),
            stderr=timeout_stderr.strip(),
            duration_seconds=elapsed,
        )
    elapsed = time.time() - start
    return LeanCommandResult(
        check_name=check_name,
        command=command,
        cwd=cwd,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=elapsed,
    )


def _stream_reader(stream: io.TextIOWrapper, buf: list[str], log_file: io.TextIOWrapper, lock: threading.Lock) -> None:
    """Read lines from a subprocess stream, appending to buf and writing to log_file in real time."""
    for line in stream:
        buf.append(line)
        with lock:
            log_file.write(line)
            log_file.flush()


def _run_command_streaming(
    *,
    check_name: str,
    cwd: Path,
    command: tuple[str, ...],
    timeout: int | float | None,
    timeout_seconds_raw: int | None,
    log_dir: Path,
    start: float,
    env: dict[str, str] | None = None,
) -> LeanCommandResult:
    """Run a command while streaming stdout/stderr to a live log file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{check_name}.log"
    status_path = log_dir / f"{check_name}.status.json"

    import json
    # Write initial status
    status_path.write_text(json.dumps({
        "check_name": check_name,
        "command": list(command),
        "cwd": str(cwd),
        "status": "running",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "log_file": str(log_path),
    }, indent=2) + "\n")

    with open(log_path, "w", encoding="utf-8") as log_file:
        header = (
            f"=== {check_name} ===\n"
            f"command: {' '.join(command)}\n"
            f"cwd: {cwd}\n"
            f"started: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"monitor with: tail -f {log_path}\n"
            f"{'=' * 60}\n\n"
        )
        log_file.write(header)
        log_file.flush()
        # Also print the monitor hint to stderr so the user sees it in the terminal
        print(f"[lean_engine] {check_name}: streaming log -> {log_path}", file=sys.stderr, flush=True)
        print(f"[lean_engine] {check_name}: monitor with: tail -f {log_path}", file=sys.stderr, flush=True)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        lock = threading.Lock()

        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        stdout_thread = threading.Thread(
            target=_stream_reader,
            args=(proc.stdout, stdout_lines, log_file, lock),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_stream_reader,
            args=(proc.stderr, stderr_lines, log_file, lock),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            elapsed = time.time() - start
            timeout_label = (
                f"{int(timeout_seconds_raw)} seconds"
                if isinstance(timeout_seconds_raw, int) and timeout_seconds_raw > 0
                else "configured limit"
            )
            with lock:
                log_file.write(f"\n\n=== TIMED OUT after {timeout_label} ===\n")
                log_file.flush()
            _write_final_status(status_path, check_name, command, cwd, "timeout", elapsed)
            return LeanCommandResult(
                check_name=check_name,
                command=command,
                cwd=cwd,
                returncode=124,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines) + f"\ncommand timed out after {timeout_label}\n",
                duration_seconds=elapsed,
            )

        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        elapsed = time.time() - start

        status_label = "ok" if proc.returncode == 0 else "failed"
        with lock:
            log_file.write(f"\n\n=== FINISHED: {status_label} (exit {proc.returncode}) in {elapsed:.1f}s ===\n")
            log_file.flush()
        _write_final_status(status_path, check_name, command, cwd, status_label, elapsed, proc.returncode)
        print(
            f"[lean_engine] {check_name}: {status_label} (exit {proc.returncode}, {elapsed:.1f}s)",
            file=sys.stderr, flush=True,
        )

    return LeanCommandResult(
        check_name=check_name,
        command=command,
        cwd=cwd,
        returncode=proc.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        duration_seconds=elapsed,
    )


def _write_final_status(
    status_path: Path,
    check_name: str,
    command: tuple[str, ...],
    cwd: Path,
    status: str,
    elapsed: float,
    returncode: int | None = None,
) -> None:
    import json
    status_path.write_text(json.dumps({
        "check_name": check_name,
        "command": list(command),
        "cwd": str(cwd),
        "status": status,
        "returncode": returncode,
        "elapsed_seconds": round(elapsed, 2),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, indent=2) + "\n")


def _format_result(result: LeanCommandResult) -> str:
    lines = [
        f"check_name: {result.check_name}",
        f"cwd: {result.cwd}",
        f"command: {' '.join(result.command)}",
        f"returncode: {result.returncode}",
        f"duration_seconds: {result.duration_seconds:.3f}",
        "",
        "stdout:",
        result.stdout.rstrip(),
        "",
        "stderr:",
        result.stderr.rstrip(),
        "",
    ]
    return "\n".join(lines)


def _coerce_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _effective_subprocess_timeout(timeout_seconds: int | None) -> int | float | None:
    if timeout_seconds is None:
        return None
    if timeout_seconds <= 0:
        return None
    return timeout_seconds
