from __future__ import annotations

from nl_engine.services.agents import AgentService
from nl_engine.settings import get_settings


def test_gpt54_none_effort_includes_temperature(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "none")
    monkeypatch.setenv("OPENAI_TEXT_VERBOSITY", "low")
    get_settings.cache_clear()

    service = AgentService()
    kwargs = service._responses_create_kwargs(
        model="gpt-5.4",
        timeout_seconds=600,
        system_prompt="system",
        user_payload='{"x":1}',
        temperature=0.2,
    )

    assert kwargs["model"] == "gpt-5.4"
    assert kwargs["reasoning"] == {"effort": "none"}
    assert kwargs["text"] == {"verbosity": "low"}
    assert kwargs["timeout"] == 600.0
    assert kwargs["temperature"] == 0.2
    get_settings.cache_clear()


def test_gpt54_non_none_effort_omits_temperature(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("OPENAI_TEXT_VERBOSITY", "high")
    get_settings.cache_clear()

    service = AgentService()
    kwargs = service._responses_create_kwargs(
        model="gpt-5.4",
        timeout_seconds=777,
        system_prompt="system",
        user_payload='{"x":1}',
        temperature=0.2,
    )

    assert kwargs["reasoning"] == {"effort": "medium"}
    assert kwargs["text"] == {"verbosity": "high"}
    assert kwargs["timeout"] == 777.0
    assert "temperature" not in kwargs
    get_settings.cache_clear()


def test_gpt5mini_xhigh_reasoning_is_capped(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("OPENAI_TEXT_VERBOSITY", "medium")
    get_settings.cache_clear()

    service = AgentService()
    kwargs = service._responses_create_kwargs(
        model="gpt-5-mini",
        timeout_seconds=500,
        system_prompt="system",
        user_payload='{"x":1}',
        temperature=0.2,
    )

    assert kwargs["model"] == "gpt-5-mini"
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs["text"] == {"verbosity": "medium"}
    assert "temperature" not in kwargs
    get_settings.cache_clear()


def test_zero_timeout_disables_request_timeout(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("OPENAI_TEXT_VERBOSITY", "medium")
    get_settings.cache_clear()

    service = AgentService()
    kwargs = service._responses_create_kwargs(
        model="gpt-5.4",
        timeout_seconds=0,
        system_prompt="system",
        user_payload='{"x":1}',
        temperature=0.2,
    )

    assert kwargs["timeout"] is None
    get_settings.cache_clear()
