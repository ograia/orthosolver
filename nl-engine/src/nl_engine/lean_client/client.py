from __future__ import annotations

import os
from typing import Any

import httpx

from nl_engine.settings import get_settings


class LeanClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.lean_engine_base_url.rstrip("/")
        self.timeout = settings.lean_engine_timeout_seconds
        self.auth_mode = settings.lean_engine_auth_mode
        self.oidc_audience = settings.lean_engine_oidc_audience or self.base_url
        self.oidc_token_source = settings.lean_engine_oidc_token_source
        self.oidc_token_env_var = settings.lean_engine_oidc_token_env_var

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
    ) -> dict[str, Any]:
        headers = {"X-Request-Id": request_id, "X-Idempotency-Key": body["job_id"], **self._auth_headers()}
        if mock_behavior:
            headers["X-Mock-Behavior"] = mock_behavior
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/v1/jobs", json=body, headers=headers)
            response.raise_for_status()
            return response.json()

    def get_job(self, job_id: str) -> dict[str, Any]:
        headers = self._auth_headers()
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/v1/jobs/{job_id}", headers=headers)
            response.raise_for_status()
            return response.json()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        headers = self._auth_headers()
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/v1/jobs/{job_id}/cancel", headers=headers)
            response.raise_for_status()
            return response.json()

    def health(self) -> dict[str, Any]:
        headers = self._auth_headers()
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/v1/health", headers=headers)
            response.raise_for_status()
            return response.json()
