from __future__ import annotations

import os
from typing import Any

import httpx

from nl_engine.settings import get_settings


class LeanClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.lean_engine_base_url.rstrip("/")
        requested_version = str(getattr(settings, "lean_engine_api_version", "v1") or "v1").strip().lower()
        self.api_version = requested_version if requested_version in {"v1", "v2"} else "v1"
        self.api_prefix = f"/{self.api_version}"
        self.timeout = settings.lean_engine_timeout_seconds
        self.auth_mode = settings.lean_engine_auth_mode
        self.oidc_audience = settings.lean_engine_oidc_audience or self.base_url
        self.oidc_token_source = settings.lean_engine_oidc_token_source
        self.oidc_token_env_var = settings.lean_engine_oidc_token_env_var

    @staticmethod
    def _normalize_version(version: str | None, default: str) -> str:
        candidate = str(version or default).strip().lower()
        return candidate if candidate in {"v1", "v2"} else default

    def _prefix(self, version: str | None = None) -> str:
        normalized = self._normalize_version(version, self.api_version)
        return f"/{normalized}"

    def _oidc_token_from_google_adc(self) -> str:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token
        except ModuleNotFoundError as exc:  # pragma: no cover - optional runtime dependency
            raise RuntimeError("google-auth is required for lean_engine_oidc_token_source=google_adc") from exc
        return id_token.fetch_id_token(Request(), self.oidc_audience)

    def _auth_headers(self) -> dict[str, str]:
        if self.auth_mode == "none":
            return {}
        if self.auth_mode != "oidc":
            raise RuntimeError(f"unsupported lean auth mode: {self.auth_mode}")

        if self.oidc_token_source == "env":
            token = os.getenv(self.oidc_token_env_var)
            if not token:
                raise RuntimeError(f"{self.oidc_token_env_var} is required when lean_engine_oidc_token_source=env")
            return {"Authorization": f"Bearer {token}"}

        if self.oidc_token_source == "google_adc":
            token = self._oidc_token_from_google_adc()
            return {"Authorization": f"Bearer {token}"}

        raise RuntimeError(f"unsupported lean OIDC token source: {self.oidc_token_source}")

    def submit_job(
        self,
        body: dict[str, Any],
        request_id: str,
        *,
        mock_behavior: str | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        headers = {"X-Request-Id": request_id, "X-Idempotency-Key": body["job_id"], **self._auth_headers()}
        if mock_behavior:
            headers["X-Mock-Behavior"] = mock_behavior
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}{prefix}/jobs", json=body, headers=headers)
            response.raise_for_status()
            return response.json()

    def get_job(self, job_id: str, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{prefix}/jobs/{job_id}", headers=headers)
            response.raise_for_status()
            return response.json()

    def cancel_job(self, job_id: str, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}{prefix}/jobs/{job_id}/cancel", headers=headers)
            if response.status_code in {404, 405}:
                response = client.delete(f"{self.base_url}{prefix}/jobs/{job_id}", headers=headers)
            response.raise_for_status()
            return response.json()

    def submit_operation(
        self,
        operation: str,
        body: dict[str, Any],
        request_id: str,
        *,
        version: str = "v2",
        mock_behavior: str | None = None,
    ) -> dict[str, Any]:
        operation_id = str(body.get("operation_id") or body.get("job_id") or "").strip()
        headers = {
            "X-Request-Id": request_id,
            "X-Idempotency-Key": operation_id or request_id,
            **self._auth_headers(),
        }
        if mock_behavior:
            headers["X-Mock-Behavior"] = mock_behavior

        prefix = self._prefix(version)
        endpoint = f"{self.base_url}{prefix}/operations/{operation}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(endpoint, json=body, headers=headers)
            response.raise_for_status()
            return response.json()

    def get_operation(self, operation_id: str, *, version: str = "v2") -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{prefix}/operations/{operation_id}", headers=headers)
            response.raise_for_status()
            return response.json()

    def health(self, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{prefix}/health", headers=headers)
            response.raise_for_status()
            return response.json()
