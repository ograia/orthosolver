from __future__ import annotations

import json
import re
import socket
from datetime import UTC, datetime
from pathlib import Path
import threading
import time
from typing import cast
from typing import Any

from nl_engine.persistence.db import get_file_store

try:
    import httpx
    from openai import OpenAI
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    httpx = None  # type: ignore[assignment]
    OpenAI = None  # type: ignore[assignment]

from nl_engine.api.run_state import is_problem_stop_requested
from nl_engine.artifacts.store import ArtifactStore
from nl_engine.domain.config import LlmConfig, ReasoningEffort, TextVerbosity
from nl_engine.domain.contracts import (
    Agent1Input,
    Agent1Output,
    Agent2Input,
    Agent2Output,
    Agent3Input,
    Agent3Output,
    Agent4Input,
    Agent4Output,
    Agent5Input,
    Agent5Output,
    Agent6Input,
    Agent6Output,
    Agent7Input,
    Agent7Output,
    Agent8Input,
    Agent8Output,
)
from nl_engine.persistence.repositories import RequestRecordRepository
from nl_engine.settings import get_settings
from nl_engine.observability.costs import record_llm_usage
from nl_engine.observability.run_logger import get_run_logger


class AgentExecutionError(RuntimeError):
    def __init__(
        self,
        *,
        agent_key: str,
        error_class: str,
        message: str,
        artifact_prefix: str,
        parse_error_artifact: str | None = None,
    ) -> None:
        super().__init__(message)
        self.agent_key = agent_key
        self.error_class = error_class
        self.message = message
        self.artifact_prefix = artifact_prefix
        self.parse_error_artifact = parse_error_artifact

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_key": self.agent_key,
            "error_class": self.error_class,
            "message": self.message,
            "artifact_prefix": self.artifact_prefix,
            "parse_error_artifact": self.parse_error_artifact,
        }


class BackgroundResponseFailed(RuntimeError):
    """OpenAI background response reached a non-completed terminal status.

    Treated as retryable since the failure is server-side.
    """


class AgentService:
    _SUPPORTED_MODELS = {"gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano"}
    _BACKGROUND_ELIGIBLE_MODELS = {"gpt-5.4", "gpt-5.4-pro"}
    _BACKGROUND_POLL_INTERVAL = 3  # seconds between polls
    _BACKGROUND_POLL_INTERVALS = (15, 30, 60)
    _REQUEST_MAX_ATTEMPTS = 2
    _VALID_JSON_ESCAPE_CHARS = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    _RETRYABLE_REQUEST_EXCEPTION_NAMES = {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
        "BackgroundResponseFailed",
        "APIError",
    }

    def __init__(
        self,
        db_session: Any = None,
        *,
        llm_overrides: LlmConfig | dict[str, Any] | None = None,
        usage_persistence_mode_override: str | None = None,
        usage_buffer: list[dict[str, Any]] | None = None,
        usage_buffer_lock: threading.Lock | None = None,
    ) -> None:
        self.settings = get_settings()
        self.store = ArtifactStore()
        self.openai_package_available = OpenAI is not None
        self.client = (
            OpenAI(
                api_key=self.settings.openai_api_key,
                timeout=float(self.settings.openai_timeout_seconds),
                max_retries=0,
                http_client=self._make_http_client(),
            )
            if (self.openai_package_available and self.settings.openai_api_key)
            else None
        )
        self.db_session = db_session
        self.llm_overrides = self._coerce_llm_overrides(llm_overrides)
        self.usage_persistence_mode_override = usage_persistence_mode_override
        self.usage_buffer = usage_buffer
        self.usage_buffer_lock = usage_buffer_lock
        self.runtime_worker_job_id: str | None = None
        self.runtime_execution_id: str | None = None
        self.runtime_worker_attempt_count: int | None = None

        repo_root = Path(__file__).resolve().parents[3]
        self.prompt_files = {
            "agent1": repo_root / "prompts" / "agent1_semantic_sketch" / "system.txt",
            "agent2": repo_root / "prompts" / "agent2_decomposer" / "system.txt",
            "agent3": repo_root / "prompts" / "agent3_decomposition_vetter" / "system.txt",
            "agent4": repo_root / "prompts" / "agent4_lemma_solver" / "system.txt",
            "agent5": repo_root / "prompts" / "agent5_lemma_vetter" / "system.txt",
            "agent6": repo_root / "prompts" / "agent6_final_checker" / "system.txt",
            "agent7": repo_root / "prompts" / "agent7_split_existing_proof" / "system.txt",
            "agent8": repo_root / "prompts" / "agent8_split_bundle_vetter" / "system.txt",
        }

    @classmethod
    def best_effort_cancel_response(cls, response_id: str) -> None:
        response_id = str(response_id or "").strip()
        if not response_id:
            return
        settings = get_settings()
        if OpenAI is None or not settings.openai_api_key:
            return
        client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=float(settings.openai_timeout_seconds),
            max_retries=0,
            http_client=cls._make_http_client(),
        )
        try:
            client.responses.cancel(response_id)
        except Exception:
            # Cancellation is best-effort and should never block continuation.
            return

    @staticmethod
    def _make_http_client() -> "httpx.Client | None":
        """Return an httpx client with TCP keepalive enabled.

        Long-running reasoning requests (xhigh thinking) can take 5-10+ minutes
        with no data flowing on the wire. NAT devices drop idle TCP connections after
        ~60-90s. SO_KEEPALIVE alone isn't enough — the OS won't send probes until
        TCP_KEEPIDLE seconds have passed (macOS default: 7200s). Setting TCP_KEEPIDLE
        to 30s ensures probes start before any typical NAT expiry.
        """
        if httpx is None:
            return None
        try:
            socket_options: list[tuple[int, int, int]] = [
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
            ]
            # Set idle time before first probe: 30s (well under typical 60-90s NAT timeout).
            # TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS (both set the idle time).
            for attr in ("TCP_KEEPIDLE", "TCP_KEEPALIVE"):
                val = getattr(socket, attr, None)
                if val is not None:
                    socket_options.append((socket.IPPROTO_TCP, val, 30))
                    break
            transport = httpx.HTTPTransport(socket_options=socket_options)
            return httpx.Client(
                transport=transport,
                timeout=httpx.Timeout(connect=30.0, read=None, write=60.0, pool=30.0),
            )
        except Exception:
            return None

    @staticmethod
    def _coerce_llm_overrides(llm_overrides: LlmConfig | dict[str, Any] | None) -> LlmConfig:
        try:
            return LlmConfig.model_validate(llm_overrides or {})
        except Exception:
            return LlmConfig()

    def set_llm_overrides(self, llm_overrides: LlmConfig | dict[str, Any] | None) -> None:
        self.llm_overrides = self._coerce_llm_overrides(llm_overrides)

    def set_runtime_context(
        self,
        *,
        worker_job_id: str | None = None,
        execution_id: str | None = None,
        worker_attempt_count: int | None = None,
    ) -> None:
        self.runtime_worker_job_id = worker_job_id
        self.runtime_execution_id = execution_id
        self.runtime_worker_attempt_count = (
            max(1, int(worker_attempt_count))
            if worker_attempt_count is not None
            else None
        )

    @staticmethod
    def _request_record_id(*, artifact_prefix: str, agent_key: str, attempt_no: int) -> str:
        safe_prefix = artifact_prefix.replace("/", "_")
        return f"reqrec_{agent_key}_{safe_prefix}_{attempt_no}"

    def _request_record_id_for_runtime(self, *, artifact_prefix: str, agent_key: str, attempt_no: int) -> str:
        record_id = self._request_record_id(
            artifact_prefix=artifact_prefix,
            agent_key=agent_key,
            attempt_no=attempt_no,
        )
        worker_attempt_count = self.runtime_worker_attempt_count
        if worker_attempt_count is not None and worker_attempt_count > 0:
            return f"{record_id}_w{worker_attempt_count}"
        return record_id

    def _attempt_scoped_artifact_key(
        self,
        *,
        artifact_prefix: str,
        filename: str,
        attempt_no: int,
    ) -> str | None:
        worker_attempt_count = self.runtime_worker_attempt_count
        if worker_attempt_count is None or worker_attempt_count <= 0:
            return None
        stem, dot, suffix = filename.partition(".")
        if not dot:
            scoped_filename = f"{filename}_worker_attempt_{worker_attempt_count}_request_attempt_{attempt_no}"
        else:
            scoped_filename = (
                f"{stem}_worker_attempt_{worker_attempt_count}_request_attempt_{attempt_no}.{suffix}"
            )
        return f"{artifact_prefix}/{scoped_filename}"

    @staticmethod
    def _request_summary(agent_key: str, payload: dict[str, Any]) -> str | None:
        if agent_key == "agent1":
            statement = str(payload.get("statement_nl") or "").strip()
            return statement[:240] if statement else None
        if agent_key == "agent2":
            theorem = str(payload.get("theorem_nl") or "").strip()
            return theorem[:240] if theorem else None
        if agent_key in {"agent7", "agent8"}:
            statement = str(payload.get("parent_statement_nl") or "").strip()
            return statement[:240] if statement else None
        lemma_id = str(payload.get("lemma_id") or "").strip()
        if lemma_id:
            return f"lemma_id={lemma_id}"
        return None

    def _update_request_record(
        self,
        *,
        request_record_id: str,
        problem_id: str | None,
        target_id: str | None,
        source: str,
        status: str,
        summary: str | None,
        request_artifact_key: str | None,
        response_artifact_key: str | None = None,
        response_id: str | None = None,
        provider_response_id: str | None = None,
        provider_status: str | None = None,
        provider_submitted_at: datetime | None = None,
        last_provider_contact_at: datetime | None = None,
        last_retrieve_error: str | None = None,
        retrieve_attempt_count: int | None = None,
        provider_submission_count: int | None = None,
        recovered_from_connection_error_count: int | None = None,
        error_class: str | None = None,
        llm_model: str | None = None,
        llm_reasoning_effort: str | None = None,
        llm_text_verbosity: str | None = None,
        llm_timeout_seconds: int | None = None,
    ) -> None:
        if not problem_id:
            return
        if self.db_session is not None:
            RequestRecordRepository(self.db_session).upsert(
                request_record_id,
                problem_id=problem_id,
                execution_id=self.runtime_execution_id,
                worker_job_id=self.runtime_worker_job_id,
                source=source,
                target_id=target_id,
                status=status,
                response_id=response_id,
                provider_response_id=provider_response_id,
                provider_status=provider_status,
                provider_submitted_at=provider_submitted_at,
                last_provider_contact_at=last_provider_contact_at,
                last_retrieve_error=last_retrieve_error,
                retrieve_attempt_count=retrieve_attempt_count,
                provider_submission_count=provider_submission_count,
                recovered_from_connection_error_count=recovered_from_connection_error_count,
                error_class=error_class,
                summary=summary,
                llm_model=llm_model,
                llm_reasoning_effort=llm_reasoning_effort,
                llm_text_verbosity=llm_text_verbosity,
                llm_timeout_seconds=llm_timeout_seconds,
                request_artifact_key=request_artifact_key,
                response_artifact_key=response_artifact_key,
            )
            return
        # Avoid creating dangling request records for create-time Agent1 calls
        # before a problem row exists.
        if not (self.runtime_execution_id or self.runtime_worker_job_id):
            return
        try:
            store = get_file_store()
            RequestRecordRepository(store).upsert(
                request_record_id,
                problem_id=problem_id,
                execution_id=self.runtime_execution_id,
                worker_job_id=self.runtime_worker_job_id,
                source=source,
                target_id=target_id,
                status=status,
                response_id=response_id,
                provider_response_id=provider_response_id,
                provider_status=provider_status,
                provider_submitted_at=provider_submitted_at,
                last_provider_contact_at=last_provider_contact_at,
                last_retrieve_error=last_retrieve_error,
                retrieve_attempt_count=retrieve_attempt_count,
                provider_submission_count=provider_submission_count,
                recovered_from_connection_error_count=recovered_from_connection_error_count,
                error_class=error_class,
                summary=summary,
                llm_model=llm_model,
                llm_reasoning_effort=llm_reasoning_effort,
                llm_text_verbosity=llm_text_verbosity,
                llm_timeout_seconds=llm_timeout_seconds,
                request_artifact_key=request_artifact_key,
                response_artifact_key=response_artifact_key,
            )
        except Exception:
            pass  # Non-critical observability write

    @classmethod
    def _normalize_model_name(cls, model: str | None) -> str | None:
        if not isinstance(model, str):
            return None
        value = model.strip().lower()
        if not value:
            return None
        if value in cls._SUPPORTED_MODELS:
            return value
        return None

    @staticmethod
    def _normalize_effort_for_model(model: str, effort: ReasoningEffort) -> ReasoningEffort:
        if model in ("gpt-5.4-mini", "gpt-5.4-nano") and effort == "xhigh":
            return "high"
        return effort

    def _resolve_agent_model_and_effort(
        self,
        *,
        agent_key: str,
        default_model: str,
        override_key: str | None = None,
    ) -> tuple[str, ReasoningEffort, TextVerbosity, int, int]:
        # If an override_key is provided (e.g. "agent2_first_root"),
        # check for that profile first; fall back to the standard agent_key.
        profile = None
        if override_key is not None:
            profile = getattr(self.llm_overrides, override_key, None)
        if profile is None:
            profile = getattr(self.llm_overrides, agent_key, None)
        model = self._normalize_model_name(default_model) or "gpt-5.4-nano"
        effort = self.settings.openai_reasoning_effort
        verbosity: TextVerbosity = self.settings.openai_text_verbosity
        timeout_seconds = int(max(0, self.settings.openai_timeout_seconds))
        max_attempts = self._REQUEST_MAX_ATTEMPTS
        if profile is not None:
            profile_model = self._normalize_model_name(profile.model)
            if profile_model is not None:
                model = profile_model
            if profile.thinking_level is not None:
                effort = profile.thinking_level
            if profile.verbosity is not None:
                verbosity = profile.verbosity
            if isinstance(profile.timeout_seconds, int):
                timeout_seconds = max(0, profile.timeout_seconds)
            if isinstance(profile.max_attempts, int):
                max_attempts = max(1, profile.max_attempts)
        effort = self._normalize_effort_for_model(model, effort)
        return model, effort, verbosity, timeout_seconds, max_attempts

    @classmethod
    def _parse_json_with_invalid_backslash_repair(cls, raw_text: str) -> Any:
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            repaired = cls._escape_invalid_backslashes_in_json_strings(raw_text)
            if repaired == raw_text:
                raise
            return json.loads(repaired)

    @classmethod
    def _escape_invalid_backslashes_in_json_strings(cls, raw_text: str) -> str:
        out: list[str] = []
        in_string = False
        escape_active = False
        i = 0
        n = len(raw_text)
        while i < n:
            ch = raw_text[i]
            if not in_string:
                out.append(ch)
                if ch == '"':
                    in_string = True
                i += 1
                continue

            if escape_active:
                out.append(ch)
                escape_active = False
                i += 1
                continue

            if ch == "\\":
                next_ch = raw_text[i + 1] if i + 1 < n else ""
                if next_ch in cls._VALID_JSON_ESCAPE_CHARS:
                    out.append(ch)
                    escape_active = True
                else:
                    out.append("\\\\")
                i += 1
                continue

            out.append(ch)
            if ch == '"':
                in_string = False
            i += 1
        return "".join(out)

    def _prompt(self, agent_key: str) -> str:
        return self.prompt_files[agent_key].read_text()

    @classmethod
    def _is_retryable_request_exception(cls, exc: Exception) -> bool:
        name = type(exc).__name__
        if name in cls._RETRYABLE_REQUEST_EXCEPTION_NAMES:
            return True

        lowered = name.lower()
        if any(token in lowered for token in ("connection", "timeout", "rate", "server")):
            return True

        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
            return True

        return False

    @staticmethod
    def _extract_background_terminal_context(message: str) -> tuple[str | None, str | None]:
        text = str(message or "")
        match = re.search(
            r"Background response\s+([^\s]+).*terminal status:\s*([A-Za-z0-9_-]+)",
            text,
        )
        if match is None:
            return None, None
        response_id = match.group(1).strip() or None
        terminal_status = match.group(2).strip().lower() or None
        return response_id, terminal_status

    def _background_poll_delay_seconds(self, retrieve_attempt_count: int) -> float:
        if self._BACKGROUND_POLL_INTERVAL <= 0:
            return 0.0
        if self._BACKGROUND_POLL_INTERVAL != 3:
            return float(self._BACKGROUND_POLL_INTERVAL)
        if retrieve_attempt_count >= 6:
            return float(self._BACKGROUND_POLL_INTERVALS[2])
        if retrieve_attempt_count >= 2:
            return float(self._BACKGROUND_POLL_INTERVALS[1])
        return float(self._BACKGROUND_POLL_INTERVALS[0])

    def _latest_request_state(self, *, artifact_prefix: str, agent_key: str, max_attempts: int) -> dict[str, Any] | None:
        for attempt_no in range(max(1, max_attempts), 0, -1):
            key = f"{artifact_prefix}/{agent_key}_request_state_attempt_{attempt_no}.json"
            if not self.store.exists(key):
                continue
            try:
                payload = self.store.load_json(key)
            except Exception:
                continue
            if isinstance(payload, dict):
                return dict(payload)
        return None

    def _recover_background_response_context(
        self,
        *,
        artifact_prefix: str,
        agent_key: str,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        state = self._latest_request_state(
            artifact_prefix=artifact_prefix,
            agent_key=agent_key,
            max_attempts=max_attempts,
        )
        if not isinstance(state, dict):
            return None
        response_id = str(state.get("response_id") or "").strip()
        if not response_id:
            return None
        status = str(state.get("status") or "").strip().lower()
        provider_status = str(state.get("provider_status") or "").strip().lower() or None
        last_retrieve_error = str(state.get("last_retrieve_error") or "").strip() or None
        if status not in {"provider_pending", "provider_running"} and not last_retrieve_error:
            return None
        return {
            "response_id": response_id,
            "status": status or "provider_pending",
            "provider_status": provider_status or "queued",
            "provider_submitted_at": state.get("provider_submitted_at"),
            "last_provider_contact_at": state.get("last_provider_contact_at"),
            "last_retrieve_error": last_retrieve_error,
            "retrieve_attempt_count": max(0, int(state.get("retrieve_attempt_count") or 0)),
            "provider_submission_count": max(1, int(state.get("provider_submission_count") or 1)),
            "recovered_from_connection_error_count": max(
                0,
                int(state.get("recovered_from_connection_error_count") or 0),
            ),
        }

    def _poll_background_response(
        self,
        response_id: str,
        *,
        timeout_seconds: int,
        agent_key: str,
        problem_id: str | None = None,
        retrieve_attempt_count: int = 0,
        on_status_update: Any | None = None,
        recovered_from_connection_error_count: int = 0,
    ) -> Any:
        """Poll a background response until it reaches a terminal state."""
        deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
        while True:
            time.sleep(self._background_poll_delay_seconds(retrieve_attempt_count))
            # Check if the problem has been stopped while we're polling
            if problem_id and is_problem_stop_requested(problem_id):
                try:
                    self.client.responses.cancel(response_id)
                except Exception:
                    pass
                raise TimeoutError(
                    f"Background response {response_id} for {agent_key} "
                    f"cancelled: problem {problem_id} stop requested"
                )
            retrieve_attempt_count += 1
            try:
                response = self.client.responses.retrieve(response_id)
            except Exception as exc:
                if not self._is_retryable_request_exception(exc):
                    raise
                if on_status_update is not None:
                    recovered_from_connection_error_count += 1
                    on_status_update(
                        status="provider_pending",
                        provider_status="unreachable",
                        last_provider_contact_at=datetime.now(UTC),
                        last_retrieve_error=f"{type(exc).__name__}: {exc}",
                        retrieve_attempt_count=retrieve_attempt_count,
                        recovered_from_connection_error_count=recovered_from_connection_error_count,
                    )
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Background response {response_id} for {agent_key} "
                        f"remains provider_pending after {timeout_seconds}s"
                    ) from exc
                continue
            status = getattr(response, "status", None)
            if on_status_update is not None:
                on_status_update(
                    status="provider_running" if status in ("queued", "in_progress") else "completed",
                    provider_status=str(status or "").strip().lower() or None,
                    last_provider_contact_at=datetime.now(UTC),
                    last_retrieve_error=None,
                    retrieve_attempt_count=retrieve_attempt_count,
                    recovered_from_connection_error_count=recovered_from_connection_error_count,
                )
            if status not in ("queued", "in_progress"):
                return response
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Background response {response_id} for {agent_key} "
                    f"did not complete within {timeout_seconds}s"
                )

    def _run_json_agent(
        self,
        *,
        agent_key: str,
        model: str,
        reasoning_effort: ReasoningEffort,
        text_verbosity: TextVerbosity,
        timeout_seconds: int,
        payload: dict[str, Any],
        artifact_prefix: str,
        max_attempts: int = 2,
        system_prompt_override: str | None = None,
        temperature: float = 0,
    ) -> dict[str, Any]:
        system_prompt = system_prompt_override or self._prompt(agent_key)
        self.store.save_text(f"{artifact_prefix}/{agent_key}_system_prompt.txt", system_prompt)
        input_key = f"{artifact_prefix}/{agent_key}_input.json"
        self.store.save_json(input_key, payload)

        problem_id_for_log, lemma_id_for_log = self._extract_problem_and_lemma_ids(artifact_prefix)
        _rl_target = lemma_id_for_log or problem_id_for_log
        _t0 = time.monotonic()
        if problem_id_for_log:
            try:
                get_run_logger().agent_start(
                    problem_id=problem_id_for_log,
                    agent_key=agent_key,
                    artifact_prefix=artifact_prefix,
                    input_payload=payload,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    target_id=_rl_target,
                )
            except Exception:
                pass

        def _log_agent_error(err: AgentExecutionError) -> None:
            if problem_id_for_log:
                try:
                    get_run_logger().agent_error(
                        problem_id=problem_id_for_log,
                        agent_key=agent_key,
                        artifact_prefix=artifact_prefix,
                        error_class=err.error_class,
                        error_message=err.message,
                        duration_seconds=time.monotonic() - _t0,
                        model=model,
                        target_id=_rl_target,
                    )
                except Exception:
                    pass

        if not self.openai_package_available:
            _err = AgentExecutionError(
                agent_key=agent_key,
                error_class="infrastructure",
                message="openai package is not installed in this Python environment",
                artifact_prefix=artifact_prefix,
            )
            _log_agent_error(_err)
            raise _err

        if not self.settings.openai_api_key:
            _err = AgentExecutionError(
                agent_key=agent_key,
                error_class="infrastructure",
                message="OPENAI_API_KEY is not configured",
                artifact_prefix=artifact_prefix,
            )
            _log_agent_error(_err)
            raise _err

        if not self.client:
            _err = AgentExecutionError(
                agent_key=agent_key,
                error_class="infrastructure",
                message="OpenAI client initialization failed",
                artifact_prefix=artifact_prefix,
            )
            _log_agent_error(_err)
            raise _err

        user_payload = json.dumps(payload, sort_keys=True)
        problem_id, lemma_id = self._extract_problem_and_lemma_ids(artifact_prefix)
        target_id = lemma_id
        if target_id is None and agent_key == "agent2" and problem_id:
            target_id = problem_id
        summary = self._request_summary(agent_key, payload)

        def _save_request_state(
            attempt_no: int,
            *,
            status: str,
            error_class: str | None = None,
            message: str | None = None,
            response_id: str | None = None,
            provider_status: str | None = None,
            provider_submitted_at: str | None = None,
            last_provider_contact_at: str | None = None,
            last_retrieve_error: str | None = None,
            retrieve_attempt_count: int | None = None,
            provider_submission_count: int | None = None,
            recovered_from_connection_error_count: int | None = None,
        ) -> None:
            row: dict[str, Any] = {
                "agent_key": agent_key,
                "attempt": attempt_no,
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
                "model": model,
                "reasoning_effort": reasoning_effort,
                "text_verbosity": text_verbosity,
                "timeout_seconds": timeout_seconds,
            }
            if self.runtime_worker_job_id:
                row["worker_job_id"] = self.runtime_worker_job_id
            if self.runtime_worker_attempt_count is not None:
                row["worker_attempt_count"] = self.runtime_worker_attempt_count
            if error_class:
                row["error_class"] = error_class
            if message:
                row["message"] = message
            if response_id:
                row["response_id"] = response_id
            if provider_status:
                row["provider_status"] = provider_status
            if provider_submitted_at:
                row["provider_submitted_at"] = provider_submitted_at
            if last_provider_contact_at:
                row["last_provider_contact_at"] = last_provider_contact_at
            if last_retrieve_error:
                row["last_retrieve_error"] = last_retrieve_error
            if retrieve_attempt_count is not None:
                row["retrieve_attempt_count"] = max(0, int(retrieve_attempt_count))
            if provider_submission_count is not None:
                row["provider_submission_count"] = max(0, int(provider_submission_count))
            if recovered_from_connection_error_count is not None:
                row["recovered_from_connection_error_count"] = max(
                    0,
                    int(recovered_from_connection_error_count),
                )
            self.store.save_json(f"{artifact_prefix}/{agent_key}_request_state_attempt_{attempt_no}.json", row)
            scoped_state_key = self._attempt_scoped_artifact_key(
                artifact_prefix=artifact_prefix,
                filename=f"{agent_key}_request_state_attempt_{attempt_no}.json",
                attempt_no=attempt_no,
            )
            if scoped_state_key is not None:
                self.store.save_json(scoped_state_key, row)
            self._update_request_record(
                request_record_id=self._request_record_id_for_runtime(
                    artifact_prefix=artifact_prefix,
                    agent_key=agent_key,
                    attempt_no=attempt_no,
                ),
                problem_id=problem_id,
                target_id=target_id,
                source=agent_key,
                status=status,
                summary=summary,
                request_artifact_key=input_key,
                response_id=response_id,
                provider_response_id=response_id,
                provider_status=provider_status,
                provider_submitted_at=(
                    datetime.fromisoformat(provider_submitted_at)
                    if provider_submitted_at
                    else None
                ),
                last_provider_contact_at=(
                    datetime.fromisoformat(last_provider_contact_at)
                    if last_provider_contact_at
                    else None
                ),
                last_retrieve_error=last_retrieve_error,
                retrieve_attempt_count=retrieve_attempt_count,
                provider_submission_count=provider_submission_count,
                recovered_from_connection_error_count=recovered_from_connection_error_count,
                error_class=error_class,
                llm_model=model,
                llm_reasoning_effort=reasoning_effort,
                llm_text_verbosity=text_verbosity,
                llm_timeout_seconds=timeout_seconds,
            )

        def _raise_if_problem_interrupted(attempt_no: int) -> None:
            if not problem_id or not is_problem_stop_requested(problem_id):
                return
            _save_request_state(
                attempt_no,
                status="interrupted",
                error_class="interrupted",
                message="request interrupted by local reset",
            )
            _int_err = AgentExecutionError(
                agent_key=agent_key,
                error_class="interrupted",
                message="request interrupted by local reset",
                artifact_prefix=artifact_prefix,
            )
            _log_agent_error(_int_err)
            raise _int_err

        recoverable_background = (
            model in self._BACKGROUND_ELIGIBLE_MODELS
            and self._recover_background_response_context(
                artifact_prefix=artifact_prefix,
                agent_key=agent_key,
                max_attempts=max_attempts,
            )
        )

        for attempt_no in range(1, max_attempts + 1):
            _raise_if_problem_interrupted(attempt_no)
            if recoverable_background and attempt_no == 1:
                recovered_response_id = str(recoverable_background.get("response_id") or "").strip() or None
                _save_request_state(
                    attempt_no,
                    status=str(recoverable_background.get("status") or "provider_pending"),
                    response_id=recovered_response_id,
                    provider_status=str(recoverable_background.get("provider_status") or "queued"),
                    provider_submitted_at=str(recoverable_background.get("provider_submitted_at") or ""),
                    last_provider_contact_at=str(recoverable_background.get("last_provider_contact_at") or ""),
                    last_retrieve_error=str(recoverable_background.get("last_retrieve_error") or ""),
                    retrieve_attempt_count=int(recoverable_background.get("retrieve_attempt_count") or 0),
                    provider_submission_count=int(recoverable_background.get("provider_submission_count") or 1),
                    recovered_from_connection_error_count=int(
                        recoverable_background.get("recovered_from_connection_error_count") or 0
                    ),
                )
            else:
                _save_request_state(attempt_no, status="started")
            background_response_id: str | None = None
            try:
                _req_kwargs = self._responses_create_kwargs(
                    model=model,
                    reasoning_effort=reasoning_effort,
                    text_verbosity=text_verbosity,
                    timeout_seconds=timeout_seconds,
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                    temperature=temperature,
                )

                def _persist_provider_runtime(
                    *,
                    status: str,
                    provider_status: str | None,
                    response_id: str | None,
                    provider_submitted_at: str | None,
                    last_provider_contact_at: datetime | None,
                    last_retrieve_error: str | None,
                    retrieve_attempt_count: int,
                    provider_submission_count: int,
                    recovered_from_connection_error_count: int,
                ) -> None:
                    _save_request_state(
                        attempt_no,
                        status=status,
                        response_id=response_id,
                        provider_status=provider_status,
                        provider_submitted_at=provider_submitted_at,
                        last_provider_contact_at=(
                            last_provider_contact_at.isoformat()
                            if last_provider_contact_at is not None
                            else None
                        ),
                        last_retrieve_error=last_retrieve_error,
                        retrieve_attempt_count=retrieve_attempt_count,
                        provider_submission_count=provider_submission_count,
                        recovered_from_connection_error_count=recovered_from_connection_error_count,
                    )

                if _req_kwargs.get("background"):
                    provider_submitted_at = datetime.now(UTC).isoformat()
                    retrieve_count = 0
                    provider_submission_count = 0
                    recovered_connection_errors = 0
                    if recoverable_background and attempt_no == 1:
                        background_response_id = str(recoverable_background.get("response_id") or "").strip() or None
                        provider_submitted_at = str(
                            recoverable_background.get("provider_submitted_at") or provider_submitted_at
                        )
                        retrieve_count = int(recoverable_background.get("retrieve_attempt_count") or 0)
                        provider_submission_count = int(
                            recoverable_background.get("provider_submission_count") or 1
                        )
                        recovered_connection_errors = int(
                            recoverable_background.get("recovered_from_connection_error_count") or 0
                        )
                        initial_provider_status = str(
                            recoverable_background.get("provider_status") or "queued"
                        ).strip().lower() or "queued"
                        _persist_provider_runtime(
                            status="provider_pending",
                            provider_status=initial_provider_status,
                            response_id=background_response_id,
                            provider_submitted_at=provider_submitted_at,
                            last_provider_contact_at=None,
                            last_retrieve_error=str(recoverable_background.get("last_retrieve_error") or "") or None,
                            retrieve_attempt_count=retrieve_count,
                            provider_submission_count=provider_submission_count,
                            recovered_from_connection_error_count=recovered_connection_errors,
                        )
                        response = self._poll_background_response(
                            background_response_id or "",
                            timeout_seconds=timeout_seconds,
                            agent_key=agent_key,
                            problem_id=problem_id_for_log,
                            retrieve_attempt_count=retrieve_count,
                            recovered_from_connection_error_count=recovered_connection_errors,
                            on_status_update=lambda **kwargs: _persist_provider_runtime(
                                status=str(kwargs.get("status") or "provider_pending"),
                                provider_status=cast(str | None, kwargs.get("provider_status")),
                                response_id=background_response_id,
                                provider_submitted_at=provider_submitted_at,
                                last_provider_contact_at=cast(datetime | None, kwargs.get("last_provider_contact_at")),
                                last_retrieve_error=cast(str | None, kwargs.get("last_retrieve_error")),
                                retrieve_attempt_count=int(kwargs.get("retrieve_attempt_count") or retrieve_count),
                                provider_submission_count=provider_submission_count,
                                recovered_from_connection_error_count=int(
                                    kwargs.get("recovered_from_connection_error_count")
                                    or recovered_connection_errors
                                ),
                            ),
                        )
                    else:
                        # Background mode: submit asynchronously, then poll
                        # until OpenAI finishes. Resilient to connection drops.
                        response = self.client.responses.create(**_req_kwargs)
                        background_response_id = str(getattr(response, "id", "") or "").strip() or None
                        provider_submission_count = 1
                        bg_status = str(getattr(response, "status", "") or "").strip().lower() or None
                        if background_response_id is not None:
                            _persist_provider_runtime(
                                status="provider_running" if bg_status in ("queued", "in_progress") else "started",
                                provider_status=bg_status,
                                response_id=background_response_id,
                                provider_submitted_at=provider_submitted_at,
                                last_provider_contact_at=datetime.now(UTC),
                                last_retrieve_error=None,
                                retrieve_attempt_count=0,
                                provider_submission_count=provider_submission_count,
                                recovered_from_connection_error_count=0,
                            )
                        if bg_status in ("queued", "in_progress"):
                            response = self._poll_background_response(
                                background_response_id or response.id,
                                timeout_seconds=timeout_seconds,
                                agent_key=agent_key,
                                problem_id=problem_id_for_log,
                                retrieve_attempt_count=0,
                                recovered_from_connection_error_count=0,
                                on_status_update=lambda **kwargs: _persist_provider_runtime(
                                    status=str(kwargs.get("status") or "provider_pending"),
                                    provider_status=cast(str | None, kwargs.get("provider_status")),
                                    response_id=background_response_id,
                                    provider_submitted_at=provider_submitted_at,
                                    last_provider_contact_at=cast(datetime | None, kwargs.get("last_provider_contact_at")),
                                    last_retrieve_error=cast(str | None, kwargs.get("last_retrieve_error")),
                                    retrieve_attempt_count=int(kwargs.get("retrieve_attempt_count") or 0),
                                    provider_submission_count=provider_submission_count,
                                    recovered_from_connection_error_count=int(
                                        kwargs.get("recovered_from_connection_error_count") or 0
                                    ),
                                ),
                            )
                    final_status = getattr(response, "status", None)
                    if final_status and final_status != "completed":
                        raise BackgroundResponseFailed(
                            f"Background response {response.id} for {agent_key} "
                            f"reached terminal status: {final_status}"
                        )
                else:
                    # Use streaming to keep the HTTP connection alive during
                    # long reasoning. Non-streaming requests get dropped by
                    # OpenAI's edge proxy after ~90s idle.
                    _stream_fn = getattr(self.client.responses, "stream", None)
                    if _stream_fn is not None:
                        with _stream_fn(**_req_kwargs) as _stream:
                            response = _stream.get_final_response()
                    else:
                        response = self.client.responses.create(**_req_kwargs)
            except Exception as exc:
                raw_message = str(exc)
                response_id_hint, provider_terminal_status = self._extract_background_terminal_context(raw_message)
                response_id_hint = response_id_hint or background_response_id
                retryable = self._is_retryable_request_exception(exc)
                if provider_terminal_status and provider_terminal_status != "completed":
                    retryable = True
                provider_pending = (
                    retryable
                    and bool(response_id_hint)
                    and provider_terminal_status in {None, "", "queued", "in_progress", "unreachable"}
                )

                interrupted_during_poll = (
                    "cancelled: problem" in raw_message.lower()
                    and "stop requested" in raw_message.lower()
                )
                if interrupted_during_poll:
                    error_class = "interrupted"
                else:
                    error_class = "infrastructure_transient" if retryable else "infrastructure"

                error_payload = {
                    "error_class": error_class,
                    "retryable": retryable,
                    "will_retry": False,
                    "attempt": attempt_no,
                    "max_attempts": max_attempts,
                    "exception_type": type(exc).__name__,
                    "message": raw_message,
                    "response_id": response_id_hint,
                    "provider_terminal_status": provider_terminal_status,
                    "provider_error_excerpt": raw_message[:500],
                }
                error_artifact = self.store.save_json(
                    f"{artifact_prefix}/{agent_key}_request_error_attempt_{attempt_no}.json",
                    error_payload,
                )
                scoped_error_key = self._attempt_scoped_artifact_key(
                    artifact_prefix=artifact_prefix,
                    filename=f"{agent_key}_request_error_attempt_{attempt_no}.json",
                    attempt_no=attempt_no,
                )
                if scoped_error_key is not None:
                    error_artifact = self.store.save_json(scoped_error_key, error_payload)
                _save_request_state(
                    attempt_no,
                    status="provider_pending" if provider_pending else "failed",
                    error_class=error_class,
                    message=f"{type(exc).__name__}: {exc}",
                    response_id=response_id_hint,
                    provider_status=provider_terminal_status or ("unreachable" if provider_pending else None),
                    last_retrieve_error=f"{type(exc).__name__}: {exc}" if provider_pending else None,
                )
                self._update_request_record(
                    request_record_id=self._request_record_id_for_runtime(
                        artifact_prefix=artifact_prefix,
                        agent_key=agent_key,
                        attempt_no=attempt_no,
                    ),
                    problem_id=problem_id,
                    target_id=target_id,
                    source=agent_key,
                    status="provider_pending" if provider_pending else "failed",
                    summary=summary,
                    request_artifact_key=input_key,
                    response_artifact_key=error_artifact,
                    response_id=response_id_hint,
                    provider_response_id=response_id_hint,
                    provider_status=provider_terminal_status or ("unreachable" if provider_pending else None),
                    last_retrieve_error=f"{type(exc).__name__}: {exc}" if provider_pending else None,
                    error_class=error_class,
                    llm_model=model,
                    llm_reasoning_effort=reasoning_effort,
                    llm_text_verbosity=text_verbosity,
                    llm_timeout_seconds=timeout_seconds,
                )
                _req_err = AgentExecutionError(
                    agent_key=agent_key,
                    error_class=error_class,
                    message=f"OpenAI request failed for {agent_key}: {type(exc).__name__}: {exc}",
                    artifact_prefix=artifact_prefix,
                    parse_error_artifact=error_artifact,
                )
                _log_agent_error(_req_err)
                raise _req_err from exc
            try:
                if not (problem_id and is_problem_stop_requested(problem_id)):
                    record_llm_usage(
                        problem_id=problem_id,
                        lemma_id=lemma_id,
                        worker_job_id=self.runtime_worker_job_id,
                        stage=agent_key,
                        provider="openai",
                        model=model,
                        response=response,
                        db_session=self.db_session,
                        usage_persistence_mode=self.usage_persistence_mode_override,
                        usage_buffer=self.usage_buffer,
                        usage_buffer_lock=self.usage_buffer_lock,
                    )
                _raise_if_problem_interrupted(attempt_no)
                raw_text = response.output_text
                raw_output_key = f"{artifact_prefix}/{agent_key}_raw_output_attempt_{attempt_no}.txt"
                self.store.save_text(raw_output_key, raw_text)
                scoped_raw_output_key = self._attempt_scoped_artifact_key(
                    artifact_prefix=artifact_prefix,
                    filename=f"{agent_key}_raw_output_attempt_{attempt_no}.txt",
                    attempt_no=attempt_no,
                )
                if scoped_raw_output_key is not None:
                    self.store.save_text(scoped_raw_output_key, raw_text)
                try:
                    parsed = self._parse_json_with_invalid_backslash_repair(raw_text)
                    parsed_output_key = f"{artifact_prefix}/{agent_key}_parsed_output_attempt_{attempt_no}.json"
                    self.store.save_json(parsed_output_key, parsed)
                    scoped_parsed_output_key = self._attempt_scoped_artifact_key(
                        artifact_prefix=artifact_prefix,
                        filename=f"{agent_key}_parsed_output_attempt_{attempt_no}.json",
                        attempt_no=attempt_no,
                    )
                    if scoped_parsed_output_key is not None:
                        self.store.save_json(scoped_parsed_output_key, parsed)
                    _save_request_state(
                        attempt_no,
                        status="completed",
                        response_id=str(getattr(response, "id", "") or ""),
                        provider_status=str(getattr(response, "status", "") or "completed"),
                    )
                    self._update_request_record(
                        request_record_id=self._request_record_id_for_runtime(
                            artifact_prefix=artifact_prefix,
                            agent_key=agent_key,
                            attempt_no=attempt_no,
                        ),
                        problem_id=problem_id,
                        target_id=target_id,
                        source=agent_key,
                        status="completed",
                        summary=summary,
                        request_artifact_key=input_key,
                        response_artifact_key=parsed_output_key,
                        response_id=str(getattr(response, "id", "") or ""),
                        provider_response_id=str(getattr(response, "id", "") or ""),
                        provider_status=str(getattr(response, "status", "") or "completed"),
                        last_provider_contact_at=datetime.now(UTC),
                        llm_model=model,
                        llm_reasoning_effort=reasoning_effort,
                        llm_text_verbosity=text_verbosity,
                        llm_timeout_seconds=timeout_seconds,
                    )
                    if problem_id_for_log:
                        try:
                            get_run_logger().agent_success(
                                problem_id=problem_id_for_log,
                                agent_key=agent_key,
                                artifact_prefix=artifact_prefix,
                                output=parsed,
                                duration_seconds=time.monotonic() - _t0,
                                model=model,
                                target_id=_rl_target,
                            )
                        except Exception:
                            pass
                    return parsed
                except json.JSONDecodeError:
                    if attempt_no >= max_attempts:
                        parse_error_artifact = self.store.save_json(
                            f"{artifact_prefix}/{agent_key}_parse_error.json",
                            {"error_class": "invalid_agent_output", "raw_output": raw_text},
                        )
                        scoped_parse_error_key = self._attempt_scoped_artifact_key(
                            artifact_prefix=artifact_prefix,
                            filename=f"{agent_key}_parse_error.json",
                            attempt_no=attempt_no,
                        )
                        if scoped_parse_error_key is not None:
                            parse_error_artifact = self.store.save_json(
                                scoped_parse_error_key,
                                {"error_class": "invalid_agent_output", "raw_output": raw_text},
                            )
                        _save_request_state(
                            attempt_no,
                            status="failed",
                            error_class="invalid_agent_output",
                            message=f"Agent output was not valid JSON after {max_attempts} attempts",
                        )
                        self._update_request_record(
                            request_record_id=self._request_record_id_for_runtime(
                                artifact_prefix=artifact_prefix,
                                agent_key=agent_key,
                                attempt_no=attempt_no,
                            ),
                            problem_id=problem_id,
                            target_id=target_id,
                            source=agent_key,
                            status="failed",
                            summary=summary,
                            request_artifact_key=input_key,
                            response_artifact_key=parse_error_artifact,
                            error_class="invalid_agent_output",
                            llm_model=model,
                            llm_reasoning_effort=reasoning_effort,
                            llm_text_verbosity=text_verbosity,
                            llm_timeout_seconds=timeout_seconds,
                        )
                        _parse_err = AgentExecutionError(
                            agent_key=agent_key,
                            error_class="invalid_agent_output",
                            message=f"Agent output was not valid JSON after {max_attempts} attempts",
                            artifact_prefix=artifact_prefix,
                            parse_error_artifact=parse_error_artifact,
                        )
                        _log_agent_error(_parse_err)
                        raise _parse_err
                    _save_request_state(
                        attempt_no,
                        status="failed",
                        error_class="invalid_agent_output",
                        message=f"Agent output was not valid JSON; retrying (attempt {attempt_no + 1}/{max_attempts})",
                    )
                    continue
            except AgentExecutionError:
                raise
            except Exception as exc:
                error_artifact = self.store.save_json(
                    f"{artifact_prefix}/{agent_key}_request_error_attempt_{attempt_no}.json",
                    {
                        "error_class": "infrastructure",
                        "retryable": False,
                        "will_retry": False,
                        "attempt": attempt_no,
                        "max_attempts": max_attempts,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "phase": "post_request_processing",
                    },
                )
                _save_request_state(
                    attempt_no,
                    status="failed",
                    error_class="infrastructure",
                    message=f"{type(exc).__name__}: {exc}",
                )
                _post_err = AgentExecutionError(
                    agent_key=agent_key,
                    error_class="infrastructure",
                    message=f"OpenAI post-request processing failed for {agent_key}: {type(exc).__name__}: {exc}",
                    artifact_prefix=artifact_prefix,
                    parse_error_artifact=error_artifact,
                )
                _log_agent_error(_post_err)
                raise _post_err from exc

        _fallback_err = AgentExecutionError(
            agent_key=agent_key,
            error_class="infrastructure_transient",
            message="Agent output unavailable",
            artifact_prefix=artifact_prefix,
        )
        _log_agent_error(_fallback_err)
        raise _fallback_err

    @staticmethod
    def _extract_problem_and_lemma_ids(artifact_prefix: str) -> tuple[str | None, str | None]:
        parts = artifact_prefix.split("/")
        problem_id = None
        lemma_id = None
        for idx, part in enumerate(parts):
            if part == "problems" and idx + 1 < len(parts):
                problem_id = parts[idx + 1]
            if part == "lemmas" and idx + 1 < len(parts):
                lemma_id = parts[idx + 1]
        return problem_id, lemma_id

    @staticmethod
    def _coerce_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            else:
                out.append(json.dumps(item, sort_keys=True))
        return out

    def _normalize_semantic_sketch(self, raw: Any) -> dict[str, Any]:
        sketch = raw if isinstance(raw, dict) else {}
        variables = sketch.get("variables", [])
        if not isinstance(variables, list):
            variables = []
        normalized_variables: list[dict[str, Any]] = []
        for item in variables:
            if isinstance(item, dict):
                normalized_variables.append(dict(item))
                continue
            if isinstance(item, str):
                normalized_variables.append({"name": item})
                continue
            normalized_variables.append({"value": item})
        return {
            "variables": normalized_variables,
            "quantifier_order": self._coerce_string_list(sketch.get("quantifier_order")),
            "domain_restrictions": self._coerce_string_list(sketch.get("domain_restrictions")),
            "witness_dependencies": self._coerce_string_list(sketch.get("witness_dependencies")),
            "normalized_claim": str(sketch.get("normalized_claim", "")),
        }

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {}

    def _normalize_shared_context(self, raw: Any) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            return []

        normalized: list[dict[str, str]] = []
        for idx, item in enumerate(raw):
            if isinstance(item, dict):
                label = str(item.get("label") or item.get("name") or f"context_{idx + 1}")
                content = str(item.get("content") or item.get("value") or "")
                kind = str(item.get("kind") or "definition")
            else:
                label = f"context_{idx + 1}"
                content = str(item)
                kind = "definition"
            normalized.append({"label": label, "content": content, "kind": kind})
        return normalized

    @staticmethod
    def _normalize_assembly_plan_steps(raw_steps: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_steps, list):
            return []

        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_steps):
            if isinstance(item, dict):
                normalized.append(dict(item))
                continue
            line = str(item).strip()
            if not line:
                continue
            normalized.append(
                {
                    "step_id": f"S{idx + 1}",
                    "uses_lemmas": [],
                    "uses_prior_steps": [],
                    "derives": line,
                    "is_trivial": True,
                    "trivial_justification": "normalized from textual assembly step",
                }
            )
        return normalized

    def _raise_validation_error(
        self,
        *,
        agent_key: str,
        artifact_prefix: str,
        payload: dict[str, Any],
        exc: Exception,
    ) -> None:
        artifact = self.store.save_json(
            f"{artifact_prefix}/{agent_key}_validation_error.json",
            {
                "error_class": "invalid_agent_output",
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "normalized_payload": payload,
            },
        )
        problem_id, lemma_id = self._extract_problem_and_lemma_ids(artifact_prefix)
        target_id = lemma_id or problem_id
        for attempt_no in range(2, 0, -1):
            key = f"{artifact_prefix}/{agent_key}_request_state_attempt_{attempt_no}.json"
            if not self.store.exists(key):
                continue
            existing_row: dict[str, Any] = {}
            try:
                loaded = self.store.load_json(key)
                if isinstance(loaded, dict):
                    existing_row = dict(loaded)
            except Exception:
                existing_row = {}
            self.store.save_json(
                key,
                {
                    **existing_row,
                    "agent_key": existing_row.get("agent_key", agent_key),
                    "attempt": int(existing_row.get("attempt", attempt_no)),
                    "status": "failed",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "error_class": "invalid_agent_output",
                    "message": str(exc),
                },
            )
            self._update_request_record(
                request_record_id=self._request_record_id_for_runtime(
                    artifact_prefix=artifact_prefix,
                    agent_key=agent_key,
                    attempt_no=attempt_no,
                ),
                problem_id=problem_id,
                target_id=target_id,
                source=agent_key,
                status="failed",
                summary=None,
                request_artifact_key=f"{artifact_prefix}/{agent_key}_input.json",
                response_artifact_key=artifact,
                error_class="invalid_agent_output",
            )
            break
        raise AgentExecutionError(
            agent_key=agent_key,
            error_class="invalid_agent_output",
            message=f"{agent_key} output failed schema validation: {exc}",
            artifact_prefix=artifact_prefix,
            parse_error_artifact=artifact,
        ) from exc

    def _normalize_agent2_assembly_plan(self, raw_plan: Any, *, theorem_nl: str) -> dict[str, Any]:
        if isinstance(raw_plan, dict):
            steps = self._normalize_assembly_plan_steps(raw_plan.get("steps"))
            proof_skeleton_nl = str(raw_plan.get("proof_skeleton_nl", ""))
            if not proof_skeleton_nl and steps:
                proof_skeleton_nl = "\n".join(str(step.get("derives") or "").strip() for step in steps if str(step.get("derives") or "").strip())
            final_step = bool(raw_plan.get("final_step_yields_exact_root", True))
            return {
                "steps": steps,
                "proof_skeleton_nl": proof_skeleton_nl,
                "final_step_yields_exact_root": final_step,
            }

        lines: list[str] = []
        if isinstance(raw_plan, list):
            lines = [str(item).strip() for item in raw_plan if str(item).strip()]
        elif isinstance(raw_plan, str) and raw_plan.strip():
            lines = [raw_plan.strip()]

        steps = self._normalize_assembly_plan_steps(lines)
        proof_skeleton_nl = "\n".join(lines) if lines else f"Derive: {theorem_nl}"
        return {
            "steps": steps,
            "proof_skeleton_nl": proof_skeleton_nl,
            "final_step_yields_exact_root": True,
        }

    def _normalize_lemma_payloads(
        self,
        raw_lemmas: Any,
        *,
        candidate_index: int,
        default_role: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_lemmas, list):
            return []

        normalized_lemmas: list[dict[str, Any]] = []
        for lemma_index, raw_lemma in enumerate(raw_lemmas):
            lemma = self._coerce_dict(raw_lemma)
            local_id = str(
                lemma.get("local_id")
                or lemma.get("name")
                or lemma.get("id")
                or f"L{candidate_index + 1}_{lemma_index + 1}"
            )
            statement_nl = str(
                lemma.get("statement_nl")
                or lemma.get("statement")
                or lemma.get("claim")
                or ""
            )
            role_in_assembly = str(
                lemma.get("role_in_assembly")
                or lemma.get("purpose")
                or default_role
            )
            formalization_cost_estimate = self._coerce_float(
                lemma.get("formalization_cost_estimate", lemma.get("formalization_cost")),
                0.5,
            )
            self_check_true = bool(lemma.get("self_check_true", True))
            self_check_notes = str(lemma.get("self_check_notes") or "normalized from alternate Agent2 schema")
            semantic_sketch = self._normalize_semantic_sketch(lemma.get("semantic_sketch"))
            relation_raw = str(
                lemma.get("lemma_relation_to_parent")
                or ("bottleneck" if lemma.get("bottleneck_reason") else "strictly_weaker")
            ).strip().lower()
            if relation_raw not in {"strictly_weaker", "orthogonal", "bottleneck"}:
                relation_raw = "strictly_weaker"

            normalized_lemma = {
                "local_id": local_id,
                "statement_nl": statement_nl,
                "semantic_sketch": semantic_sketch,
                "role_in_assembly": role_in_assembly,
                "lemma_relation_to_parent": relation_raw,
                "strictly_easier_reason": (
                    None if lemma.get("strictly_easier_reason") in {None, ""} else str(lemma.get("strictly_easier_reason"))
                ),
                "bottleneck_reason": (
                    None if lemma.get("bottleneck_reason") in {None, ""} else str(lemma.get("bottleneck_reason"))
                ),
                "formalization_cost_estimate": formalization_cost_estimate,
                "self_check_true": self_check_true,
                "self_check_notes": self_check_notes,
            }
            if "proof_nl" in lemma:
                normalized_lemma["proof_nl"] = None if lemma.get("proof_nl") in {None, ""} else str(lemma.get("proof_nl"))
            normalized_lemmas.append(normalized_lemma)
        return normalized_lemmas

    def _normalize_agent7_output(self, parsed: Any, *, theorem_nl: str) -> dict[str, Any]:
        normalized = self._coerce_dict(parsed)
        normalized["shared_context"] = self._normalize_shared_context(normalized.get("shared_context"))
        normalized["context_items"] = self._normalize_shared_context(normalized.get("context_items"))
        normalized["lemmas"] = self._normalize_lemma_payloads(
            normalized.get("lemmas"),
            candidate_index=0,
            default_role="supports the parent proof assembly",
        )
        normalized["assembly_plan"] = self._normalize_agent2_assembly_plan(
            normalized.get("assembly_plan"),
            theorem_nl=theorem_nl,
        )
        return normalized

    def _normalize_agent2_output(self, parsed: Any, *, theorem_nl: str) -> dict[str, Any]:
        envelope = self._coerce_dict(parsed)
        if isinstance(parsed, list):
            candidate_rows = parsed
        else:
            candidate_rows = envelope.get("candidates")
            if not isinstance(candidate_rows, list):
                decompositions = envelope.get("decompositions")
                candidate_rows = decompositions if isinstance(decompositions, list) else []

        normalized_candidates: list[dict[str, Any]] = []
        for candidate_index, raw_candidate in enumerate(candidate_rows):
            candidate = self._coerce_dict(raw_candidate)
            normalized_lemmas = self._normalize_lemma_payloads(
                candidate.get("lemmas"),
                candidate_index=candidate_index,
                default_role=f"supports candidate {candidate_index + 1} assembly",
            )

            if normalized_lemmas:
                total_cost = sum(lemma["formalization_cost_estimate"] for lemma in normalized_lemmas)
            else:
                total_cost = self._coerce_float(candidate.get("formalization_cost_estimate_total"), 0.0)

            strategy_summary = str(candidate.get("strategy_summary") or candidate.get("strategy") or "")
            shared_context = self._normalize_shared_context(candidate.get("shared_context"))
            assembly_plan = self._normalize_agent2_assembly_plan(candidate.get("assembly_plan"), theorem_nl=theorem_nl)

            drift_self_check = candidate.get("drift_self_check")
            if not isinstance(drift_self_check, dict):
                drift_self_check = {"all_lemmas_consistent_with_root_sketch": True, "inconsistencies_noted": []}

            normalized_candidates.append(
                {
                    "candidate_index": self._coerce_int(candidate.get("candidate_index", candidate_index), candidate_index),
                    "strategy_summary": strategy_summary,
                    "shared_context": shared_context,
                    "context_items": shared_context,
                    "lemmas": normalized_lemmas,
                    "assembly_plan": assembly_plan,
                    "formalization_cost_estimate_total": self._coerce_float(
                        candidate.get("formalization_cost_estimate_total", total_cost),
                        total_cost,
                    ),
                    "drift_self_check": drift_self_check,
                }
            )

        return {
            "status": "completed",
            "candidates": cast(list[dict[str, Any]], normalized_candidates),
        }

    def _normalize_agent3_output(self, parsed: Any) -> dict[str, Any]:
        envelope = self._coerce_dict(parsed)
        required = {
            "status",
            "decision",
            "summary",
            "lemma_findings",
            "assembly_check",
            "coverage_check",
            "drift_assessment",
            "formalization_risk",
            "fixes_required",
            "fatal_reason",
        }
        if required.issubset(set(envelope.keys())):
            return envelope

        check1 = self._coerce_dict(envelope.get("check_1_individual_lemma_validity"))
        lemma_findings: list[dict[str, Any]] = []
        has_false = False
        has_suspect = False
        for local_id, raw in check1.items():
            row = self._coerce_dict(raw)
            status_raw = str(row.get("status", "")).strip().lower()
            if status_raw in {"true", "plausible", "valid", "correct"}:
                statement_status = "plausible"
            elif status_raw in {"false", "invalid", "wrong"}:
                statement_status = "false"
                has_false = True
            else:
                statement_status = "suspect"
                has_suspect = True
            lemma_findings.append(
                {
                    "local_id": str(local_id),
                    "statement_status": statement_status,
                    "evidence": str(row.get("reason") or row.get("evidence") or ""),
                    "counterexample": (
                        None if row.get("counterexample") in {None, ""} else str(row.get("counterexample"))
                    ),
                }
            )

        check2 = self._coerce_dict(envelope.get("check_2_assembly_skeleton_completeness"))
        check2_status = str(check2.get("status", "")).strip().lower()
        assembly_verdict = "valid"
        final_step_matches_root = True
        if check2_status in {"small_gap", "gap", "incomplete", "partial"}:
            assembly_verdict = "small_gap"
        elif check2_status in {"broken", "invalid", "fatal"}:
            assembly_verdict = "broken"
            final_step_matches_root = False
        assembly_details = str(check2.get("reason") or check2.get("details") or "")
        hidden_steps = self._coerce_string_list(check2.get("hidden_steps_found"))
        hidden_steps.extend(self._coerce_string_list(check2.get("minor_notes")))

        check3 = self._coerce_dict(envelope.get("check_3_coverage_and_redundancy"))
        redundant_lemmas = check3.get("redundant_lemmas")
        if not isinstance(redundant_lemmas, list):
            redundant_lemmas = []
        missing_coverage = check3.get("missing_coverage")
        if not isinstance(missing_coverage, list):
            missing_coverage = check3.get("missing_pieces")
        if not isinstance(missing_coverage, list):
            missing_coverage = []
        disguised_difficulty = check3.get("disguised_difficulty")
        if not isinstance(disguised_difficulty, list):
            disguised_difficulty = check3.get("lemmas_as_hard_as_original")
        if not isinstance(disguised_difficulty, list):
            disguised_difficulty = []

        check4 = self._coerce_dict(envelope.get("check_4_semantic_drift"))
        per_lemma_drift: list[dict[str, Any]] = []
        drift_major = False
        drift_minor = False
        for local_id, raw in check4.items():
            if str(local_id).lower() == "overall":
                continue
            row = self._coerce_dict(raw)
            drift_raw = str(row.get("drift") or row.get("status") or "none").strip().lower()
            drift = "none"
            if drift_raw == "major":
                drift = "major"
                drift_major = True
            elif drift_raw == "minor":
                drift = "minor"
                drift_minor = True
            per_lemma_drift.append(
                {
                    "local_id": str(local_id),
                    "drift": drift,
                    "description": row.get("reason") or row.get("description"),
                }
            )
        drift_severity = "major" if drift_major else "minor" if drift_minor else "none"
        drift_detected = drift_severity != "none"

        overall = self._coerce_dict(envelope.get("overall_verdict"))
        soundness = str(overall.get("soundness", "")).strip().lower()
        main_issues = self._coerce_string_list(overall.get("main_issues"))

        decision = "accepted"
        fatal_reason: str | None = None
        if has_false:
            decision = "fatal"
            fatal_reason = "at least one lemma marked false"
        elif drift_severity == "major":
            decision = "fatal"
            fatal_reason = "major semantic drift detected"
        elif assembly_verdict == "broken":
            decision = "fatal"
            fatal_reason = "assembly skeleton is broken"
        elif has_suspect or assembly_verdict == "small_gap" or bool(missing_coverage):
            decision = "minor_fix"
        elif soundness in {"suspect", "uncertain", "needs_work"}:
            decision = "minor_fix"
        elif soundness in {"incorrect", "false", "fatal"}:
            decision = "fatal"
            fatal_reason = f"overall soundness={soundness}"

        formalization_risk = "low"
        if decision == "minor_fix":
            formalization_risk = "medium"
        if decision == "fatal":
            formalization_risk = "high"

        fixes_required: list[str] = []
        fixes_required.extend(self._coerce_string_list(missing_coverage))
        fixes_required.extend(main_issues)
        if decision == "minor_fix" and not fixes_required and has_suspect:
            fixes_required.append("resolve suspect lemma validity findings")
        if decision == "minor_fix" and not fixes_required and assembly_verdict == "small_gap":
            fixes_required.append("close assembly skeleton gaps")
        summary = (
            str(overall.get("overall_assessment") or "")
            or str(check3.get("overall_assessment") or "")
            or assembly_details
            or "decomposition vetted"
        )

        return {
            "status": "completed",
            "decision": decision,
            "summary": summary,
            "lemma_findings": lemma_findings,
            "assembly_check": {
                "verdict": assembly_verdict,
                "details": assembly_details,
                "hidden_steps_found": hidden_steps,
                "final_step_matches_root": final_step_matches_root,
            },
            "coverage_check": {
                "redundant_lemmas": redundant_lemmas,
                "missing_coverage": missing_coverage,
                "disguised_difficulty": disguised_difficulty,
            },
            "drift_assessment": {
                "drift_detected": drift_detected,
                "drift_severity": drift_severity,
                "per_lemma_drift": per_lemma_drift,
                "assembly_conclusion_matches_root": not drift_major,
            },
            "formalization_risk": formalization_risk,
            "fixes_required": fixes_required,
            "fatal_reason": fatal_reason,
        }

    def _responses_create_kwargs(
        self,
        *,
        model: str,
        reasoning_effort: ReasoningEffort | None = None,
        text_verbosity: TextVerbosity | None = None,
        timeout_seconds: int,
        system_prompt: str,
        user_payload: str,
        temperature: float,
    ) -> dict[str, Any]:
        normalized_model = self._normalize_model_name(model) or "gpt-5.4-nano"
        effort = reasoning_effort or self.settings.openai_reasoning_effort
        effort = self._normalize_effort_for_model(normalized_model, effort)
        verbosity = text_verbosity or self.settings.openai_text_verbosity
        use_background = normalized_model in self._BACKGROUND_ELIGIBLE_MODELS
        kwargs: dict[str, Any] = {
            "model": normalized_model,
            "reasoning": {"effort": effort},
            "text": {"verbosity": verbosity},
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_payload}]},
            ],
        }
        kwargs["timeout"] = None if timeout_seconds <= 0 else float(timeout_seconds)
        if use_background:
            kwargs["background"] = True
            kwargs["store"] = True
        # GPT-5.4 supports temperature only when reasoning effort is "none".
        if effort == "none":
            kwargs["temperature"] = temperature
        return kwargs

    def _validate_agent_output_payload(
        self,
        *,
        agent_key: str,
        payload: Any,
        request_payload: dict[str, Any],
        artifact_prefix: str,
    ) -> dict[str, Any]:
        if agent_key == "agent1":
            payload_dict = self._coerce_dict(payload)
            statement_nl = str(request_payload.get("statement_nl") or "")
            if "semantic_sketch" in payload_dict:
                normalized = {
                    "status": payload_dict.get("status", "completed"),
                    "statement_nl_received": payload_dict.get("statement_nl_received", statement_nl),
                    "semantic_sketch": self._normalize_semantic_sketch(payload_dict.get("semantic_sketch")),
                    "implicit_assumptions_surfaced": self._coerce_string_list(payload_dict.get("implicit_assumptions_surfaced", [])),
                    "ambiguities": self._coerce_string_list(payload_dict.get("ambiguities", [])),
                }
            else:
                normalized = {
                    "status": "completed",
                    "statement_nl_received": statement_nl,
                    "semantic_sketch": self._normalize_semantic_sketch(payload),
                    "implicit_assumptions_surfaced": [],
                    "ambiguities": [],
                }
            return Agent1Output.model_validate(normalized).model_dump()
        if agent_key == "agent2":
            theorem_nl = str(request_payload.get("theorem_nl") or "")
            normalized = self._normalize_agent2_output(payload, theorem_nl=theorem_nl)
            return Agent2Output.model_validate(normalized).model_dump()
        if agent_key == "agent3":
            normalized = self._normalize_agent3_output(payload)
            return Agent3Output.model_validate(normalized).model_dump()
        if agent_key == "agent4":
            return Agent4Output.model_validate(self._coerce_dict(payload)).model_dump()
        if agent_key == "agent5":
            return Agent5Output.model_validate(self._coerce_dict(payload)).model_dump()
        if agent_key == "agent6":
            return Agent6Output.model_validate(self._coerce_dict(payload)).model_dump()
        if agent_key == "agent7":
            theorem_nl = str(request_payload.get("parent_statement_nl") or "")
            normalized = self._normalize_agent7_output(payload, theorem_nl=theorem_nl)
            return Agent7Output.model_validate(normalized).model_dump()
        if agent_key == "agent8":
            return Agent8Output.model_validate(self._coerce_dict(payload)).model_dump()
        return self._coerce_dict(payload)

    def finalize_background_request_record(
        self,
        request_record: Any,
        *,
        response: Any | None = None,
    ) -> dict[str, Any]:
        if not self.client:
            raise RuntimeError("OpenAI client initialization failed")
        response_id = str(
            getattr(request_record, "provider_response_id", None)
            or getattr(request_record, "response_id", None)
            or ""
        ).strip()
        if not response_id:
            raise ValueError("request record has no provider response id")
        if response is None:
            response = self.client.responses.retrieve(response_id)

        artifact_prefix = str(getattr(request_record, "request_artifact_key", "") or "").rsplit("/", 1)[0]
        agent_key = str(getattr(request_record, "source", "") or "").strip()
        if not artifact_prefix or not agent_key:
            raise ValueError("request record is missing artifact routing metadata")

        request_payload: dict[str, Any] = {}
        request_artifact_key = str(getattr(request_record, "request_artifact_key", "") or "").strip()
        if request_artifact_key and self.store.exists(request_artifact_key):
            loaded_payload = self.store.load_json(request_artifact_key)
            if isinstance(loaded_payload, dict):
                request_payload = dict(loaded_payload)

        problem_id, lemma_id = self._extract_problem_and_lemma_ids(artifact_prefix)
        raw_text = str(getattr(response, "output_text", "") or "")
        parsed = self._parse_json_with_invalid_backslash_repair(raw_text)
        validated = self._validate_agent_output_payload(
            agent_key=agent_key,
            payload=parsed,
            request_payload=request_payload,
            artifact_prefix=artifact_prefix,
        )

        raw_output_key = f"{artifact_prefix}/{agent_key}_raw_output_attempt_1.txt"
        parsed_output_key = f"{artifact_prefix}/{agent_key}_parsed_output_attempt_1.json"
        self.store.save_text(raw_output_key, raw_text)
        self.store.save_json(parsed_output_key, validated)
        self.store.save_json(
            f"{artifact_prefix}/{agent_key}_request_state_attempt_1.json",
            {
                "agent_key": agent_key,
                "attempt": 1,
                "status": "completed",
                "updated_at": datetime.now(UTC).isoformat(),
                "response_id": response_id,
                "provider_status": str(getattr(response, "status", "") or "completed"),
                "worker_job_id": getattr(request_record, "worker_job_id", None),
            },
        )

        if not (problem_id and is_problem_stop_requested(problem_id)):
            record_llm_usage(
                problem_id=problem_id,
                lemma_id=lemma_id,
                worker_job_id=getattr(request_record, "worker_job_id", None),
                stage=agent_key,
                provider="openai",
                model=str(getattr(request_record, "llm_model", None) or getattr(response, "model", "") or ""),
                response=response,
                db_session=self.db_session,
                usage_persistence_mode=self.usage_persistence_mode_override,
                usage_buffer=self.usage_buffer,
                usage_buffer_lock=self.usage_buffer_lock,
            )

        self._update_request_record(
            request_record_id=str(getattr(request_record, "request_record_id")),
            problem_id=problem_id,
            target_id=getattr(request_record, "target_id", None),
            source=agent_key,
            status="completed",
            summary=getattr(request_record, "summary", None),
            request_artifact_key=request_artifact_key or None,
            response_artifact_key=parsed_output_key,
            response_id=response_id,
            provider_response_id=response_id,
            provider_status=str(getattr(response, "status", "") or "completed"),
            last_provider_contact_at=datetime.now(UTC),
            error_class=None,
            llm_model=getattr(request_record, "llm_model", None),
            llm_reasoning_effort=getattr(request_record, "llm_reasoning_effort", None),
            llm_text_verbosity=getattr(request_record, "llm_text_verbosity", None),
            llm_timeout_seconds=getattr(request_record, "llm_timeout_seconds", None),
        )
        return validated

    def _agent2_retry_prompt_override(self, payload: Agent2Input) -> str | None:
        if not payload.previous_attempt_summaries:
            return None
        base = self._prompt("agent2")
        retry_context = json.dumps(payload.previous_attempt_summaries, indent=2, sort_keys=True)

        # Extract self-containment violations for explicit guidance
        sc_lines: list[str] = []
        for attempt in payload.previous_attempt_summaries:
            violations = attempt.get("self_containment_violations")
            if isinstance(violations, list):
                for v in violations:
                    if isinstance(v, dict):
                        sc_lines.append(f"- Lemma {v.get('local_id', '?')} {v.get('violation', '')}")

        sc_section = ""
        if sc_lines:
            sc_section = (
                "\nSELF-CONTAINMENT VIOLATIONS IN PREVIOUS ATTEMPT (MUST FIX):\n"
                + "\n".join(sc_lines)
                + "\nYou MUST inline all definitions. Do NOT reference other lemmas by ID.\n"
            )

        # Extract disguised_difficulty items — frame as recursive decomposition targets
        dd_lines: list[str] = []
        for attempt in payload.previous_attempt_summaries:
            dd = attempt.get("disguised_difficulty")
            if isinstance(dd, list) and dd:
                for item in dd:
                    dd_lines.append(f"  - {item}")

        dd_section = ""
        if dd_lines:
            dd_section = (
                "\nNOTE ON HARD LEMMAS FROM PREVIOUS ATTEMPTS:\n"
                "The following lemmas were flagged as potentially hard or equivalent to the root theorem.\n"
                "Do NOT simply restate the parent claim in another form. If a hard lemma is unavoidable,\n"
                "label it as the explicit bottleneck and keep supporting lemmas genuinely simplifying.\n"
                + "\n".join(dd_lines)
                + "\n"
            )

        # Detect pre-vet rejections (final_step_yields_exact_root=false — agent3 never ran)
        prevet_lines: list[str] = []
        equivalence_lines: list[str] = []
        for attempt in payload.previous_attempt_summaries:
            if attempt.get("decision") is None and attempt.get("llm_vetting_status") == "rejected_fatal":
                strat = attempt.get("strategy_summary") or "unknown strategy"
                prevet_lines.append(f"  - {strat}")
            if str(attempt.get("equivalence_risk") or "").lower() == "high":
                strat = attempt.get("strategy_summary") or "unknown strategy"
                equivalence_lines.append(f"  - {strat}")

        prevet_section = ""
        if prevet_lines:
            prevet_section = (
                "\nPRE-VET REJECTIONS (assembly did not prove the theorem):\n"
                "These attempts were rejected before vetting because final_step_yields_exact_root=false.\n"
                "Fix: ensure every assembly gap is covered by an explicit lemma so the assembly\n"
                "IS complete assuming all lemmas are true. final_step_yields_exact_root MUST be true.\n"
                + "\n".join(prevet_lines)
                + "\n"
            )

        equivalence_section = ""
        if equivalence_lines:
            equivalence_section = (
                "\nROOT-EQUIVALENCE REJECTION SIGNALS:\n"
                "These strategies were rejected because a child lemma appeared equivalent to the parent theorem.\n"
                "Fix: avoid restatements/renamings of the parent theorem. Keep hard bottlenecks only when truly\n"
                "necessary, explicitly labeled as bottleneck, and supported by genuinely simplifying lemmas.\n"
                + "\n".join(equivalence_lines)
                + "\n"
            )

        return (
            f"{base}\n\n"
            "RETRY MODE (MANDATORY):\n"
            "- Previous decomposition attempts were rejected.\n"
            "- You MUST address prior failures explicitly.\n"
            "- For every candidate, include in strategy_summary a brief sentence explaining how it differs from failed attempts.\n"
            "- Incorporate fixes_required and missing_coverage items.\n"
            "- Do not repeat rejected patterns unless you explicitly explain the correction.\n"
            f"{sc_section}"
            f"{dd_section}"
            f"{prevet_section}"
            f"{equivalence_section}"
            "Previous attempt summaries (JSON):\n"
            f"{retry_context}\n"
        )

    def semantic_sketch(self, statement_nl: str, artifact_prefix: str) -> Agent1Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent1",
            default_model=self.settings.openai_model_agent1,
        )
        payload = Agent1Input(statement_nl=statement_nl).model_dump()
        parsed = self._run_json_agent(
            agent_key="agent1",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload,
            artifact_prefix=artifact_prefix,
            temperature=0,
        )
        if isinstance(parsed, dict) and "semantic_sketch" in parsed:
            normalized = {
                "status": parsed.get("status", "completed"),
                "statement_nl_received": parsed.get("statement_nl_received", statement_nl),
                "semantic_sketch": self._normalize_semantic_sketch(parsed.get("semantic_sketch")),
                "implicit_assumptions_surfaced": self._coerce_string_list(parsed.get("implicit_assumptions_surfaced", [])),
                "ambiguities": self._coerce_string_list(parsed.get("ambiguities", [])),
            }
            try:
                return Agent1Output.model_validate(normalized)
            except Exception as exc:
                self._raise_validation_error(
                    agent_key="agent1",
                    artifact_prefix=artifact_prefix,
                    payload=normalized,
                    exc=exc,
                )

        normalized = {
            "status": "completed",
            "statement_nl_received": statement_nl,
            "semantic_sketch": self._normalize_semantic_sketch(parsed),
            "implicit_assumptions_surfaced": [],
            "ambiguities": [],
        }
        try:
            return Agent1Output.model_validate(normalized)
        except Exception as exc:
            self._raise_validation_error(
                agent_key="agent1",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def decompose(self, payload: Agent2Input, artifact_prefix: str, *, override_key: str | None = None) -> Agent2Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent2",
            default_model=self.settings.openai_model_agent2,
            override_key=override_key,
        )
        prompt_override = self._agent2_retry_prompt_override(payload)
        parsed = self._run_json_agent(
            agent_key="agent2",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            system_prompt_override=prompt_override,
            temperature=0,
        )
        normalized = self._normalize_agent2_output(parsed, theorem_nl=payload.theorem_nl)
        try:
            return Agent2Output.model_validate(normalized)
        except Exception as exc:
            self._raise_validation_error(
                agent_key="agent2",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def vet_decomposition(self, payload: Agent3Input, artifact_prefix: str) -> Agent3Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent3",
            default_model=self.settings.openai_model_agent3,
        )
        parsed = self._run_json_agent(
            agent_key="agent3",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            temperature=0,
        )
        normalized = self._normalize_agent3_output(parsed)
        try:
            return Agent3Output.model_validate(normalized)
        except Exception as exc:
            self._raise_validation_error(
                agent_key="agent3",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def solve_lemma(self, payload: Agent4Input, artifact_prefix: str, *, override_key: str | None = None) -> Agent4Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent4",
            default_model=self.settings.openai_model_agent4,
            override_key=override_key,
        )
        temperature = 0.2 if payload.attempt_number > 1 else 0
        parsed = self._run_json_agent(
            agent_key="agent4",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            temperature=temperature,
        )
        try:
            return Agent4Output.model_validate(parsed)
        except Exception as exc:
            normalized = self._coerce_dict(parsed)
            self._raise_validation_error(
                agent_key="agent4",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def vet_lemma_proof(self, payload: Agent5Input, artifact_prefix: str) -> Agent5Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent5",
            default_model=self.settings.openai_model_agent5,
        )
        parsed = self._run_json_agent(
            agent_key="agent5",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            temperature=0,
        )
        try:
            return Agent5Output.model_validate(parsed)
        except Exception as exc:
            normalized = self._coerce_dict(parsed)
            self._raise_validation_error(
                agent_key="agent5",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def final_check(self, payload: Agent6Input, artifact_prefix: str) -> Agent6Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent6",
            default_model=self.settings.openai_model_agent6,
        )
        parsed = self._run_json_agent(
            agent_key="agent6",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            temperature=0,
        )
        normalized = self._coerce_dict(parsed)
        # Ensure required fields
        if "status" not in normalized:
            normalized["status"] = "completed"
        if "verdict" not in normalized:
            normalized["verdict"] = "approved"
        if "confidence" not in normalized:
            normalized["confidence"] = 0.5
        if "summary" not in normalized:
            normalized["summary"] = ""
        for key in ("decomposition_findings", "assembly_findings", "lemma_findings"):
            if key not in normalized or not isinstance(normalized.get(key), list):
                normalized[key] = []
        if normalized.get("verdict") not in {
            "approved",
            "problem_issue",
            "decomposition_issue",
            "assembly_issue",
            "lemma_issues",
            "localized_lemma_repair",
        }:
            normalized["verdict"] = "approved"

        proof_bundle = payload.proof_bundle if isinstance(payload.proof_bundle, dict) else {}
        default_decomposition_id = str(
            proof_bundle.get("decomposition", {}).get("decomposition_id")
            or proof_bundle.get("decomposition", {}).get("node_id")
            or ""
        ).strip()

        def _normalize_findings(rows: Any, *, default_scope: str) -> list[dict[str, Any]]:
            if not isinstance(rows, list):
                return []
            out: list[dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                finding = dict(item)
                lemma_id = str(finding.get("lemma_id") or "").strip()
                if lemma_id and not finding.get("target_id"):
                    finding["target_scope"] = "lemma"
                    finding["target_id"] = lemma_id
                target_scope = str(finding.get("target_scope") or default_scope).strip() or default_scope
                if target_scope not in {"problem", "decomposition", "lemma"}:
                    target_scope = default_scope
                finding["target_scope"] = target_scope
                target_id = str(finding.get("target_id") or "").strip()
                if not target_id:
                    if target_scope == "problem":
                        target_id = payload.problem_id
                    elif target_scope == "decomposition":
                        target_id = default_decomposition_id
                    elif lemma_id:
                        target_id = lemma_id
                finding["target_id"] = target_id
                severity = str(finding.get("severity") or "warning").strip().lower()
                finding["severity"] = "fatal" if severity == "fatal" else "warning"
                recommended_action = str(finding.get("recommended_action") or "").strip().lower()
                if recommended_action not in {"retry_solver", "decompose_further"}:
                    if target_scope == "lemma":
                        recommended_action = "retry_solver"
                    else:
                        recommended_action = ""
                if recommended_action:
                    finding["recommended_action"] = recommended_action
                else:
                    finding.pop("recommended_action", None)
                out.append(finding)
            return out

        normalized["decomposition_findings"] = _normalize_findings(
            normalized.get("decomposition_findings"),
            default_scope="decomposition",
        )
        normalized["assembly_findings"] = _normalize_findings(
            normalized.get("assembly_findings"),
            default_scope="decomposition",
        )
        normalized["lemma_findings"] = _normalize_findings(
            normalized.get("lemma_findings"),
            default_scope="lemma",
        )
        try:
            return Agent6Output.model_validate(normalized)
        except Exception as exc:
            self._raise_validation_error(
                agent_key="agent6",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def split_existing_proof(self, payload: Agent7Input, artifact_prefix: str) -> Agent7Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent7",
            default_model=self.settings.openai_model_agent7,
        )
        parsed = self._run_json_agent(
            agent_key="agent7",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            temperature=0,
        )
        normalized = self._normalize_agent7_output(parsed, theorem_nl=payload.parent_statement_nl)
        if "status" not in normalized:
            normalized["status"] = "completed"
        try:
            return Agent7Output.model_validate(normalized)
        except Exception as exc:
            self._raise_validation_error(
                agent_key="agent7",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )

    def vet_split_existing_proof(self, payload: Agent8Input, artifact_prefix: str) -> Agent8Output:
        model, reasoning_effort, text_verbosity, timeout_seconds, max_attempts = self._resolve_agent_model_and_effort(
            agent_key="agent8",
            default_model=self.settings.openai_model_agent8,
        )
        parsed = self._run_json_agent(
            agent_key="agent8",
            model=model,
            reasoning_effort=reasoning_effort,
            text_verbosity=text_verbosity,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            payload=payload.model_dump(),
            artifact_prefix=artifact_prefix,
            temperature=0,
        )
        normalized = self._coerce_dict(parsed)
        if "status" not in normalized:
            normalized["status"] = "completed"
        try:
            return Agent8Output.model_validate(normalized)
        except Exception as exc:
            self._raise_validation_error(
                agent_key="agent8",
                artifact_prefix=artifact_prefix,
                payload=normalized,
                exc=exc,
            )
