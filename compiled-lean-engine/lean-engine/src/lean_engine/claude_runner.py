from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_io import RunPaths, write_json, write_text
from .config import RuntimeConfig, resolve_model_id

_log = logging.getLogger(__name__)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill a process and its entire process group (started with start_new_session=True)."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


@dataclass(frozen=True)
class FileUpdateEvent:
    file_path: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {
            "file_path": self.file_path,
            "content": self.content,
        }


@dataclass(frozen=True)
class ClaudeRunTrace:
    result_text: str
    assistant_text_chunks: tuple[str, ...]
    file_updates: tuple[FileUpdateEvent, ...]
    target_file_latest_update: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_text": self.result_text,
            "assistant_text_chunks": list(self.assistant_text_chunks),
            "file_updates": [item.to_dict() for item in self.file_updates],
            "target_file_latest_update": self.target_file_latest_update,
        }

    def latest_update_for_target(self, target_path: str | Path | None) -> str | None:
        if target_path is None:
            return None
        target_key = _path_key(target_path)
        for item in reversed(self.file_updates):
            if _path_key(item.file_path) == target_key:
                return item.content
        return None

    def with_target_file(self, target_path: str | Path | None) -> ClaudeRunTrace:
        latest = self.latest_update_for_target(target_path)
        return ClaudeRunTrace(
            result_text=self.result_text,
            assistant_text_chunks=self.assistant_text_chunks,
            file_updates=self.file_updates,
            target_file_latest_update=latest,
        )


@dataclass(frozen=True)
class ClaudeRunResult:
    phase_name: str
    model: str
    command: tuple[str, ...]
    cwd: Path
    returncode: int
    timed_out: bool
    duration_seconds: float
    prompt_path: Path
    raw_output_path: Path
    summary_path: Path
    result_event: dict[str, Any] | None
    trace: ClaudeRunTrace | None = None
    stall_killed: bool = False
    timeout_reason: str | None = None
    activity_state_last: str = "unknown"
    event_counts: dict[str, int] | None = None
    provider_limit_detected: bool = False
    provider_timeout_detected: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_name": self.phase_name,
            "model": self.model,
            "command": list(self.command),
            "cwd": str(self.cwd),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "stall_killed": self.stall_killed,
            "timeout_reason": self.timeout_reason,
            "activity_state_last": self.activity_state_last,
            "event_counts": dict(self.event_counts or {}),
            "provider_limit_detected": self.provider_limit_detected,
            "provider_timeout_detected": self.provider_timeout_detected,
            "duration_seconds": self.duration_seconds,
            "prompt_path": str(self.prompt_path),
            "raw_output_path": str(self.raw_output_path),
            "summary_path": str(self.summary_path),
            "result_event": self.result_event,
            "trace": self.trace.to_dict() if self.trace is not None else None,
            "ok": self.ok,
        }


class ClaudeRunner:
    def __init__(self, runtime_config: RuntimeConfig):
        self._runtime_config = runtime_config

    def run_prompt(
        self,
        *,
        run_paths: RunPaths,
        prompt: str,
        phase_name: str,
        model: str | None = None,
        timeout_seconds: int | None = None,
        tools: str | None = None,
        allowed_tools: str | None = None,
        permission_mode: str | None = None,
        extra_env: dict[str, str] | None = None,
        timestamp: str | None = None,
        max_turns: int | None = None,
        idle_timeout_seconds: int | None = None,
        tool_wait_timeout_seconds: int | None = None,
        init_timeout_seconds: int | None = None,
    ) -> ClaudeRunResult:
        effective_model = resolve_model_id(model or self._runtime_config.claude.model)
        configured_timeout = (
            self._runtime_config.claude.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        effective_timeout: int | float | None = None if configured_timeout <= 0 else configured_timeout

        if timestamp is None:
            timestamp = _utc_timestamp()

        prompt_path = run_paths.prompts_dir / f"{phase_name}_{timestamp}.txt"
        raw_output_path = run_paths.claude_raw_dir / f"{phase_name}_{timestamp}.jsonl"
        summary_path = run_paths.summaries_dir / f"{phase_name}_{timestamp}_claude_result.json"

        write_text(prompt_path, prompt if prompt.endswith("\n") else prompt + "\n")

        command_parts: list[str] = [
            self._runtime_config.claude.command,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--model",
            effective_model,
        ]
        if permission_mode:
            command_parts.extend(["--permission-mode", permission_mode])
        if tools is not None:
            command_parts.extend(["--tools", tools])
        if allowed_tools is not None:
            command_parts.extend(["--allowedTools", allowed_tools])
        if max_turns is not None and max_turns > 0:
            command_parts.extend(["--max-turns", str(max_turns)])
        # Pass prompt via stdin, not as positional arg — the Claude CLI
        # misparsess the positional prompt when --allowedTools is present.
        command = tuple(command_parts)

        env = os.environ.copy()
        env.setdefault("MCP_LOG_DIR", str(run_paths.run_root / ".mcp_logs"))
        env.setdefault("MCP_LOG_NAME", self._runtime_config.mcp.log_name)
        env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", str(self._runtime_config.claude.max_output_tokens))
        if extra_env:
            env.update(extra_env)

        cwd = run_paths.workspace_dir
        cwd.mkdir(parents=True, exist_ok=True)
        start = time.time()
        timed_out = False
        stall_killed = False
        timeout_reason: str | None = None
        activity_state_last = "init_only"
        idle_timeout = (
            self._runtime_config.claude.stall_timeout_seconds
            if idle_timeout_seconds is None
            else idle_timeout_seconds
        )
        tool_wait_timeout = (
            self._runtime_config.claude.tool_wait_timeout_seconds
            if tool_wait_timeout_seconds is None
            else tool_wait_timeout_seconds
        )
        init_timeout = (
            self._runtime_config.claude.init_timeout_seconds
            if init_timeout_seconds is None
            else init_timeout_seconds
        )

        with raw_output_path.open("w", encoding="utf-8") as output_file:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(cwd),
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"failed to launch Claude CLI command '{command[0]}': {exc}") from exc

            # Feed prompt via stdin and close to signal EOF.
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # Process may have exited early

            last_output_size = 0
            last_activity = time.time()
            waiting_on_tool = False
            seen_non_init_event = False
            poll_interval = 10  # seconds

            while True:
                try:
                    proc.wait(timeout=poll_interval)
                    break  # Process finished normally
                except subprocess.TimeoutExpired:
                    pass

                # Check for output activity (stall detection).
                try:
                    current_size = raw_output_path.stat().st_size
                except OSError:
                    current_size = last_output_size

                if current_size > last_output_size:
                    activity = _scan_new_activity(raw_output_path, last_output_size)
                    waiting_on_tool = activity.waiting_on_tool
                    if activity.saw_non_init_event:
                        seen_non_init_event = True
                    if waiting_on_tool:
                        activity_state_last = "tool_wait"
                    elif activity.saw_assistant_thinking_or_text:
                        activity_state_last = "thinking_silent"
                    elif activity.saw_any_event:
                        activity_state_last = "active_output"
                    last_output_size = current_size
                    last_activity = time.time()
                else:
                    idle_elapsed = time.time() - last_activity
                    if not seen_non_init_event and init_timeout > 0 and idle_elapsed > init_timeout:
                        _log.warning(
                            "Claude subprocess init silence timeout (%ds). Killing pid %d.",
                            init_timeout, proc.pid,
                        )
                        timed_out = True
                        stall_killed = True
                        timeout_reason = "init_silence_timeout"
                        activity_state_last = "init_only"
                        _kill_process_group(proc)
                        proc.wait()
                        break
                    if waiting_on_tool and tool_wait_timeout > 0 and idle_elapsed > tool_wait_timeout:
                        _log.warning(
                            "Claude subprocess tool wait timeout (%ds). Killing pid %d.",
                            tool_wait_timeout, proc.pid,
                        )
                        timed_out = True
                        stall_killed = True
                        timeout_reason = "tool_wait_timeout"
                        activity_state_last = "tool_wait"
                        _kill_process_group(proc)
                        proc.wait()
                        break
                    if idle_timeout > 0 and not waiting_on_tool and idle_elapsed > idle_timeout:
                        _log.warning(
                            "Claude subprocess idle timeout (%ds). Killing pid %d.",
                            idle_timeout, proc.pid,
                        )
                        timed_out = True
                        stall_killed = True
                        timeout_reason = "idle_timeout"
                        activity_state_last = "thinking_silent" if seen_non_init_event else "init_only"
                        _kill_process_group(proc)
                        proc.wait()
                        break

                # Respect hard timeout if set.
                if effective_timeout is not None and (time.time() - start) > effective_timeout:
                    _log.warning("Claude subprocess hard timeout (%ds). Killing pid %d.", effective_timeout, proc.pid)
                    timed_out = True
                    timeout_reason = "hard_timeout"
                    activity_state_last = "tool_wait" if waiting_on_tool else ("thinking_silent" if seen_non_init_event else "init_only")
                    _kill_process_group(proc)
                    proc.wait()
                    break

        elapsed = time.time() - start
        result_event, trace = _extract_result_event_and_trace(raw_output_path)
        event_summary = _summarize_raw_events(raw_output_path, result_event=result_event)
        if timeout_reason is None and timed_out:
            timeout_reason = "hard_timeout"
        if not timed_out and event_summary.saw_result:
            activity_state_last = "done"

        result = ClaudeRunResult(
            phase_name=phase_name,
            model=effective_model,
            command=command,
            cwd=cwd,
            returncode=proc.returncode,
            timed_out=timed_out,
            duration_seconds=elapsed,
            prompt_path=prompt_path,
            raw_output_path=raw_output_path,
            summary_path=summary_path,
            result_event=result_event,
            trace=trace,
            stall_killed=stall_killed,
            timeout_reason=timeout_reason,
            activity_state_last=activity_state_last,
            event_counts=event_summary.event_counts,
            provider_limit_detected=event_summary.provider_limit_detected,
            provider_timeout_detected=event_summary.provider_timeout_detected,
        )
        write_json(summary_path, result.to_dict())
        return result


def extract_run_trace(raw_output_path: Path) -> ClaudeRunTrace:
    _, trace = _extract_result_event_and_trace(raw_output_path)
    return trace


def _extract_result_event(raw_output_path: Path) -> dict[str, Any] | None:
    result_event, _ = _extract_result_event_and_trace(raw_output_path)
    return result_event


def _last_event_is_tool_use(raw_output_path: Path, previous_size: int) -> bool:
    """Check if the latest output in the JSONL file is a tool_use event.

    When Claude invokes an MCP tool, it writes a tool_use event and then waits
    for the result with no further stdout. We detect this to avoid false stall kills.
    """
    try:
        with open(raw_output_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, previous_size))
            last_line = ""
            for line in f:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
            if not last_line:
                return False
            event = json.loads(last_line)
            if event.get("type") == "assistant":
                msg = event.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    if isinstance(content, list) and content:
                        return content[-1].get("type") == "tool_use"
        return False
    except (OSError, json.JSONDecodeError, KeyError):
        return False


@dataclass(frozen=True)
class _ActivityScan:
    waiting_on_tool: bool
    saw_non_init_event: bool
    saw_assistant_thinking_or_text: bool
    saw_any_event: bool


def _scan_new_activity(raw_output_path: Path, previous_size: int) -> _ActivityScan:
    waiting_on_tool = False
    saw_non_init = False
    saw_thinking_or_text = False
    saw_any_event = False
    try:
        with raw_output_path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, previous_size))
            last_payload: dict[str, Any] | None = None
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                saw_any_event = True
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                last_payload = payload
                payload_type = payload.get("type")
                if payload_type != "system":
                    saw_non_init = True
                if payload_type == "assistant":
                    message = payload.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, list):
                            for item in content:
                                if not isinstance(item, dict):
                                    continue
                                item_type = item.get("type")
                                if item_type in {"thinking", "text"}:
                                    saw_thinking_or_text = True
            if isinstance(last_payload, dict) and last_payload.get("type") == "assistant":
                message = last_payload.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, list) and content:
                        last_item = content[-1]
                        if isinstance(last_item, dict):
                            waiting_on_tool = last_item.get("type") == "tool_use"
    except OSError:
        pass
    return _ActivityScan(
        waiting_on_tool=waiting_on_tool,
        saw_non_init_event=saw_non_init,
        saw_assistant_thinking_or_text=saw_thinking_or_text,
        saw_any_event=saw_any_event,
    )


@dataclass(frozen=True)
class _RawEventSummary:
    event_counts: dict[str, int]
    provider_limit_detected: bool
    provider_timeout_detected: bool
    saw_result: bool


def _summarize_raw_events(raw_output_path: Path, *, result_event: dict[str, Any] | None) -> _RawEventSummary:
    counts = {
        "system": 0,
        "assistant": 0,
        "user": 0,
        "result": 0,
        "rate_limit_event": 0,
    }
    provider_limit = False
    provider_timeout = False
    try:
        for line in raw_output_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload_type = str(payload.get("type", ""))
            if payload_type in counts:
                counts[payload_type] += 1
            if payload_type == "rate_limit_event":
                info = payload.get("rate_limit_info")
                if isinstance(info, dict):
                    status = str(info.get("status", "")).lower()
                    overage_status = str(info.get("overageStatus", "")).lower()
                    if status.startswith("rejected") or overage_status.startswith("rejected"):
                        provider_limit = True
    except OSError:
        pass

    result_text = ""
    if isinstance(result_event, dict):
        candidate = result_event.get("result")
        if isinstance(candidate, str):
            result_text = candidate.lower()
    if "you've hit your limit" in result_text or "resets 7am" in result_text:
        provider_limit = True
    if "request timed out" in result_text:
        provider_timeout = True

    return _RawEventSummary(
        event_counts=counts,
        provider_limit_detected=provider_limit,
        provider_timeout_detected=provider_timeout,
        saw_result=counts["result"] > 0,
    )


def _extract_result_event_and_trace(raw_output_path: Path) -> tuple[dict[str, Any] | None, ClaudeRunTrace]:
    last_result: dict[str, Any] | None = None
    result_text = ""
    assistant_text_chunks: list[str] = []
    file_updates: list[FileUpdateEvent] = []

    for line in raw_output_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get("type")
        if payload_type == "result":
            last_result = payload
            candidate = payload.get("result")
            if isinstance(candidate, str):
                result_text = candidate
            continue

        if payload_type == "assistant":
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "text":
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    assistant_text_chunks.append(text)
            continue

        if payload_type == "user":
            update = _extract_file_update(payload)
            if update is not None:
                file_updates.append(update)

    trace = ClaudeRunTrace(
        result_text=result_text,
        assistant_text_chunks=tuple(assistant_text_chunks),
        file_updates=tuple(file_updates),
        target_file_latest_update=None,
    )
    return last_result, trace


def _extract_file_update(payload: dict[str, Any]) -> FileUpdateEvent | None:
    tool_use_result = payload.get("tool_use_result")
    if not isinstance(tool_use_result, dict):
        return None
    if tool_use_result.get("type") != "update":
        return None

    file_path = tool_use_result.get("filePath")
    content = tool_use_result.get("content")
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    if not isinstance(content, str):
        return None
    return FileUpdateEvent(file_path=file_path, content=content)


def extract_result_text(result: ClaudeRunResult) -> str:
    if result.trace is not None:
        if result.trace.result_text:
            return result.trace.result_text
        if result.trace.assistant_text_chunks:
            return "\n\n".join(result.trace.assistant_text_chunks).strip()
    if result.result_event and isinstance(result.result_event.get("result"), str):
        return str(result.result_event.get("result"))
    if not result.ok:
        return ""
    return _extract_assistant_text_from_stream_json(result.raw_output_path)


def _extract_assistant_text_from_stream_json(raw_output_path: Path) -> str:
    last_result_text: str | None = None
    assistant_text_chunks: list[str] = []

    for line in raw_output_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        payload_type = payload.get("type")
        if payload_type == "result" and isinstance(payload.get("result"), str):
            last_result_text = str(payload.get("result"))
            continue

        if payload_type != "assistant":
            continue

        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                assistant_text_chunks.append(text)

    if last_result_text is not None:
        return last_result_text
    if assistant_text_chunks:
        return "\n\n".join(assistant_text_chunks).strip()
    return ""


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _path_key(value: str | Path) -> str:
    path = Path(value)
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)


# ---------------------------------------------------------------------------
# Search discovery extraction — carry forward useful Mathlib search results
# between repair rounds so fresh Claude subprocesses don't re-search.
# ---------------------------------------------------------------------------

_SEARCH_TOOL_NAMES = frozenset({
    "mcp__lean-lsp__lean_loogle",
    "mcp__lean-lsp__lean_leandex",
    "mcp__lean-lsp__lean_leanfinder",
    "mcp__lean-lsp__lean_local_search",
    "mcp__lean-lsp__lean_state_search",
})


def extract_search_discoveries(jsonl_path: Path) -> list[dict[str, Any]]:
    """Extract search tool results from a Claude JSONL trace.

    Returns list of dicts with keys: tool, query, results.
    Each result is ``{"name": str, "type": str}``.
    Only includes searches that returned non-empty results.
    """
    tool_id_to_info: dict[str, dict[str, str]] = {}
    discoveries: list[dict[str, Any]] = []

    try:
        text = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        # Track tool_use events for search tools.
        if payload.get("type") == "assistant":
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            for item in message.get("content", []):
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = item.get("name", "")
                if name in _SEARCH_TOOL_NAMES:
                    tid = item.get("id", "")
                    query = item.get("input", {}).get("query", "")
                    tool_id_to_info[tid] = {"name": name, "query": query}

        # Match results to tracked search tool IDs.
        if payload.get("type") == "user":
            msg = payload.get("message")
            if not isinstance(msg, dict):
                continue
            content_list = msg.get("content", [])
            if not isinstance(content_list, list):
                continue
            for item in content_list:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                tid = item.get("tool_use_id", "")
                if tid not in tool_id_to_info:
                    continue

                tur = payload.get("tool_use_result", {})
                if not isinstance(tur, dict):
                    continue

                raw_result = None
                sc = tur.get("structuredContent")
                if isinstance(sc, dict):
                    raw_result = sc.get("result")
                if raw_result is None:
                    c = tur.get("content")
                    if isinstance(c, str):
                        try:
                            raw_result = json.loads(c).get("result")
                        except (json.JSONDecodeError, AttributeError):
                            pass

                if not raw_result or not isinstance(raw_result, list):
                    continue

                found_items: list[dict[str, str]] = []
                for r in raw_result[:5]:
                    if not isinstance(r, dict):
                        continue
                    found_name = (
                        r.get("name")
                        or r.get("full_name")
                        or r.get("primary_declaration")
                        or ""
                    )
                    type_sig = r.get("type") or r.get("display_statement_text") or ""
                    if not type_sig and r.get("formal_statement"):
                        stmt = r["formal_statement"]
                        for sep in (" := by", " := ", " where"):
                            idx = stmt.find(sep)
                            if idx >= 0:
                                type_sig = stmt[:idx]
                                break
                    if found_name:
                        found_items.append({"name": found_name.strip(), "type": type_sig.strip()})

                if found_items:
                    tool_info = tool_id_to_info[tid]
                    tool_short = tool_info["name"].replace("mcp__lean-lsp__lean_", "")
                    discoveries.append({
                        "tool": tool_short,
                        "query": tool_info["query"],
                        "results": found_items,
                    })

    return discoveries


def format_search_discoveries(discoveries: list[dict[str, Any]], max_entries: int = 15) -> str:
    """Format search discoveries as compact text for inclusion in prompts."""
    if not discoveries:
        return ""

    seen_names: set[str] = set()
    lines = ["Mathlib search results from previous round(s) — use these instead of re-searching:"]
    count = 0

    for d in discoveries:
        for r in d["results"]:
            name = r["name"]
            if name in seen_names:
                continue
            seen_names.add(name)
            type_sig = r.get("type", "")
            if len(type_sig) > 200:
                type_sig = type_sig[:200] + "..."
            lines.append(f"- {name}: {type_sig}" if type_sig else f"- {name}")
            count += 1
            if count >= max_entries:
                break
        if count >= max_entries:
            break

    return "\n".join(lines)
