from __future__ import annotations

from typing import Any

from nl_engine.lean_client.client import LeanClient
from nl_engine.settings import get_settings


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _HttpClientMock:
    last_headers: dict[str, str] | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        _HttpClientMock.last_headers = headers
        return _Resp({"ok": True})

    def get(self, url: str, headers: dict[str, str] | None = None):
        _HttpClientMock.last_headers = headers
        return _Resp({"ok": True})


def test_lean_client_without_auth(monkeypatch) -> None:
    monkeypatch.setenv("LEAN_ENGINE_AUTH_MODE", "none")
    get_settings.cache_clear()

    import httpx

    monkeypatch.setattr(httpx, "Client", _HttpClientMock)
    client = LeanClient()
    client.submit_job(
        {"job_id": "job_1", "problem_id": "p", "target_id": "t", "target_kind": "lemma", "mode": "formalize_lemma"},
        request_id="req_1",
    )
    headers = _HttpClientMock.last_headers or {}
    assert "Authorization" not in headers
    assert headers["X-Request-Id"] == "req_1"


def test_lean_client_oidc_auth_from_env(monkeypatch) -> None:
    monkeypatch.setenv("LEAN_ENGINE_AUTH_MODE", "oidc")
    monkeypatch.setenv("LEAN_ENGINE_OIDC_TOKEN_SOURCE", "env")
    monkeypatch.setenv("LEAN_ENGINE_OIDC_TOKEN", "test-token")
    get_settings.cache_clear()

    import httpx

    monkeypatch.setattr(httpx, "Client", _HttpClientMock)
    client = LeanClient()
    client.get_job("job_2")
    headers = _HttpClientMock.last_headers or {}
    assert headers["Authorization"] == "Bearer test-token"
