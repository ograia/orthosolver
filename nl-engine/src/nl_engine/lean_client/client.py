from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

from nl_engine.settings import get_settings


class LeanClient:
    def __init__(self, *, base_url: str | None = None, api_version: str | None = None) -> None:
        settings = get_settings()
        resolved_base_url = str(base_url or settings.lean_engine_base_url).strip()
        self.base_url = resolved_base_url.rstrip("/")
        requested_version = str(api_version or getattr(settings, "lean_engine_api_version", "v1") or "v1").strip().lower()
        self.api_version = requested_version if requested_version in {"v1", "v2"} else "v1"
        self.api_prefix = f"/{self.api_version}"
        self.timeout = settings.lean_engine_timeout_seconds
        self.submit_http_retries = max(1, int(settings.lean_engine_submit_http_retries))
        self.poll_http_retries = max(1, int(settings.lean_engine_poll_http_retries))
        self.retry_backoff_seconds = max(0.0, float(settings.lean_engine_retry_backoff_seconds))
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

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 425, 429, 500, 502, 503, 504}

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(self.retry_backoff_seconds * max(1, attempt))

    def _probe_existing_job(self, job_id: str, *, version: str | None = None) -> dict[str, Any] | None:
        try:
            return self.get_job(job_id, version=version)
        except Exception:
            return None

    def _probe_existing_operation(self, operation_id: str, *, version: str = "v2") -> dict[str, Any] | None:
        try:
            return self.get_operation(operation_id, version=version)
        except Exception:
            return None

    def _request_with_retries(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        retry_attempts: int,
        recovery_probe: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, retry_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(method, url, json=json_body, headers=headers)
                if self._is_retryable_status(response.status_code):
                    if recovery_probe is not None:
                        recovered = recovery_probe()
                        if recovered is not None:
                            return recovered
                    if attempt < retry_attempts:
                        self._sleep_before_retry(attempt)
                        continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if not self._is_retryable_status(exc.response.status_code):
                    raise
                if recovery_probe is not None:
                    recovered = recovery_probe()
                    if recovered is not None:
                        return recovered
                if attempt < retry_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise
            except httpx.RequestError as exc:
                last_error = exc
                if recovery_probe is not None:
                    recovered = recovery_probe()
                    if recovered is not None:
                        return recovered
                if attempt < retry_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("lean HTTP request failed without an error")

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
        job_id = str(body.get("job_id") or "").strip()
        return self._request_with_retries(
            method="POST",
            url=f"{self.base_url}{prefix}/jobs",
            json_body=body,
            headers=headers,
            retry_attempts=self.submit_http_retries,
            recovery_probe=(lambda: self._probe_existing_job(job_id, version=version)) if job_id else None,
        )

    def get_job(self, job_id: str, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        return self._request_with_retries(
            method="GET",
            url=f"{self.base_url}{prefix}/jobs/{job_id}",
            headers=headers,
            retry_attempts=self.poll_http_retries,
        )

    def cancel_job(self, job_id: str, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}{prefix}/jobs/{job_id}/cancel", headers=headers)
            if response.status_code in {404, 405}:
                response = client.delete(f"{self.base_url}{prefix}/jobs/{job_id}", headers=headers)
            response.raise_for_status()
            return response.json()

    def cancel_operation(self, operation_id: str, *, version: str = "v2") -> dict[str, Any]:
        return self.cancel_job(operation_id, version=version)

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
        recovery_probe = None
        if operation_id:
            recovery_probe = lambda: (
                self._probe_existing_operation(operation_id, version=version)
                or self._probe_existing_job(operation_id, version=version)
            )
        return self._request_with_retries(
            method="POST",
            url=endpoint,
            json_body=body,
            headers=headers,
            retry_attempts=self.submit_http_retries,
            recovery_probe=recovery_probe,
        )

    def get_operation(self, operation_id: str, *, version: str = "v2") -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        return self._request_with_retries(
            method="GET",
            url=f"{self.base_url}{prefix}/operations/{operation_id}",
            headers=headers,
            retry_attempts=self.poll_http_retries,
        )

    def health(self, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{prefix}/health", headers=headers)
            response.raise_for_status()
            return response.json()

    def health_live(self, *, version: str | None = None) -> dict[str, Any]:
        headers = self._auth_headers()
        prefix = self._prefix(version)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{prefix}/health/live", headers=headers)
            response.raise_for_status()
            return response.json()
