from __future__ import annotations

import json
import re
import shutil
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..config import RuntimeConfig, load_runtime_config
from ..integrations import IntegrationPreflight, lean_lsp_mcp
from ..workspace import create_workspace_from_template
from .jobs import ServiceJobManager
from .store import JobStore

JOB_PATH_RE = re.compile(r"^/v1/jobs/([^/]+)$")


class LeanEngineServiceApp:
    def __init__(
        self,
        *,
        store: JobStore,
        jobs: ServiceJobManager,
        default_runtime_config: RuntimeConfig,
    ) -> None:
        self._store = store
        self._jobs = jobs
        self._default_runtime_config = default_runtime_config

    @classmethod
    def from_defaults(
        cls,
        *,
        db_path: Path,
        max_workers: int,
        config_path: Path | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        artifact_root: Path | None = None,
        mcp_command: str | None = None,
        repo_lean_lsp_mcp_root: Path | None = None,
        lean4_skills_root: Path | None = None,
    ) -> "LeanEngineServiceApp":
        runtime_config = load_runtime_config(
            config_path,
            model=model,
            fallback_model=fallback_model,
            artifact_root=artifact_root,
            mcp_command=mcp_command,
            repo_lean_lsp_mcp_root=repo_lean_lsp_mcp_root,
            lean4_skills_root=lean4_skills_root,
        )
        store = JobStore(db_path)
        jobs = ServiceJobManager(
            store=store,
            default_config_path=config_path,
            default_runtime_config=runtime_config,
            max_workers=max_workers,
        )
        return cls(store=store, jobs=jobs, default_runtime_config=runtime_config)

    def shutdown(self) -> None:
        self._jobs.shutdown()

    def submit_job(
        self,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "request body must be a JSON object")

        request = dict(body)
        if idempotency_key is not None and idempotency_key.strip():
            key = idempotency_key.strip()
            existing_job_id = request.get("job_id")
            if existing_job_id is not None and str(existing_job_id).strip() != key:
                return _json_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "X-Idempotency-Key must match body.job_id when both are present",
                )
            request["job_id"] = key

        try:
            record, created = self._jobs.submit(request)
        except ValueError as exc:
            return _json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))

        status_code = HTTPStatus.ACCEPTED if created else HTTPStatus.CONFLICT
        return int(status_code), record.to_submit_response()

    def get_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        record = self._store.get(job_id)
        if record is None:
            return _json_error(HTTPStatus.NOT_FOUND, "job_not_found", f"job not found: {job_id}")
        return int(HTTPStatus.OK), record.to_poll_response()

    def health(self) -> tuple[int, dict[str, Any]]:
        template_ok = _lean_template_ok(self._default_runtime_config)
        mcp_available = _mcp_config_available(self._default_runtime_config)
        integration_health = _integration_health(self._default_runtime_config)

        status = "healthy" if template_ok and mcp_available and integration_health["ok"] else "degraded"
        payload = {
            "status": status,
            "default_model": self._default_runtime_config.claude.model,
            "lean_project_template_ok": template_ok,
            "mcp_config_available": mcp_available,
            "integration_preflight": integration_health["preflight"],
            "active_jobs": self._store.active_job_count(),
            "queue_depth": self._store.queue_depth(),
        }
        return int(HTTPStatus.OK), payload


def run_http_service(
    *,
    host: str,
    port: int,
    db_path: Path,
    max_workers: int,
    config_path: Path | None = None,
    model: str | None = None,
    fallback_model: str | None = None,
    artifact_root: Path | None = None,
    mcp_command: str | None = None,
    repo_lean_lsp_mcp_root: Path | None = None,
    lean4_skills_root: Path | None = None,
) -> None:
    app = LeanEngineServiceApp.from_defaults(
        db_path=db_path,
        max_workers=max_workers,
        config_path=config_path,
        model=model,
        fallback_model=fallback_model,
        artifact_root=artifact_root,
        mcp_command=mcp_command,
        repo_lean_lsp_mcp_root=repo_lean_lsp_mcp_root,
        lean4_skills_root=lean4_skills_root,
    )

    handler_class = _build_handler_class(app)
    server = ThreadingHTTPServer((host, port), handler_class)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.shutdown()


def _build_handler_class(app: LeanEngineServiceApp):
    class ServiceHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/jobs":
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "status": "fatal",
                        "error_class": "not_found",
                        "message": f"unsupported endpoint: {self.path}",
                    },
                )
                return

            body, error = self._read_json_body()
            if error is not None:
                self._send_json(HTTPStatus.BAD_REQUEST, error)
                return

            idempotency_key = self.headers.get("X-Idempotency-Key")
            status_code, payload = app.submit_job(body, idempotency_key=idempotency_key)
            self._send_json(status_code, payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/health":
                status_code, payload = app.health()
                self._send_json(status_code, payload)
                return

            job_match = JOB_PATH_RE.match(self.path)
            if job_match is not None:
                status_code, payload = app.get_job(job_match.group(1))
                self._send_json(status_code, payload)
                return

            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "status": "fatal",
                    "error_class": "not_found",
                    "message": f"unsupported endpoint: {self.path}",
                },
            )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _read_json_body(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                return None, {
                    "status": "fatal",
                    "error_class": "invalid_request",
                    "message": "missing Content-Length header",
                }

            try:
                length = int(content_length)
            except ValueError:
                return None, {
                    "status": "fatal",
                    "error_class": "invalid_request",
                    "message": "invalid Content-Length header",
                }

            raw = self.rfile.read(max(length, 0)).decode("utf-8")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                return None, {
                    "status": "fatal",
                    "error_class": "invalid_request",
                    "message": f"invalid JSON body: {exc.msg}",
                }

            if not isinstance(payload, dict):
                return None, {
                    "status": "fatal",
                    "error_class": "invalid_request",
                    "message": "request body must be a JSON object",
                }
            return payload, None

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ServiceHandler


def _json_error(status: int | HTTPStatus, error_class: str, message: str) -> tuple[int, dict[str, Any]]:
    return int(status), {
        "status": "fatal",
        "error_class": error_class,
        "message": message,
    }


def _lean_template_ok(runtime_config: RuntimeConfig) -> bool:
    try:
        with TemporaryDirectory(prefix="lean_engine_service_health_") as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            create_workspace_from_template(workspace, runtime_config=runtime_config)  # result unused in health check
    except Exception:
        return False
    return True


def _mcp_config_available(runtime_config: RuntimeConfig) -> bool:
    if not runtime_config.mcp.enabled:
        return True

    command = runtime_config.mcp.command.strip()
    if not command:
        return False

    if "/" in command:
        path = Path(command).expanduser().resolve()
        return path.exists() and path.is_file()

    return shutil.which(command) is not None


def _integration_health(runtime_config: RuntimeConfig) -> dict[str, Any]:
    rows: list[IntegrationPreflight] = [
        lean_lsp_mcp.preflight(
            repo_root=runtime_config.integrations.lean_lsp_mcp_root,
            mcp_command=runtime_config.mcp.command,
        ),
    ]
    return {
        "ok": all(row.status in {"ok", "skipped"} for row in rows),
        "preflight": [row.to_dict() for row in rows],
    }
