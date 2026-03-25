from __future__ import annotations

from nl_engine.services.agents import AgentService


def test_per_agent_model_and_thinking_override_resolution() -> None:
    service = AgentService(
        llm_overrides={
            "agent2": {"model": "gpt-5-mini", "thinking_level": "high", "timeout_seconds": 900},
            "agent4": {"model": "gpt-5.4", "thinking_level": "none", "timeout_seconds": 120},
        }
    )

    model2, effort2, verbosity2, timeout2, max_attempts2 = service._resolve_agent_model_and_effort(
        agent_key="agent2",
        default_model="default-agent2",
    )
    assert model2 == "gpt-5-mini"
    assert effort2 == "high"
    assert verbosity2 == "medium"
    assert timeout2 == 900
    assert max_attempts2 == 2

    model4, effort4, verbosity4, timeout4, max_attempts4 = service._resolve_agent_model_and_effort(
        agent_key="agent4",
        default_model="default-agent4",
    )
    assert model4 == "gpt-5.4"
    assert effort4 == "none"
    assert verbosity4 == "medium"
    assert timeout4 == 120
    assert max_attempts4 == 2


def test_per_agent_reasoning_effort_alias_supported() -> None:
    service = AgentService(llm_overrides={"agent3": {"model": "gpt-5.4", "reasoning_effort": "low"}})
    model, effort, verbosity, timeout_seconds, max_attempts = service._resolve_agent_model_and_effort(
        agent_key="agent3",
        default_model="default-agent3",
    )
    assert model == "gpt-5.4"
    assert effort == "low"
    assert verbosity == "medium"
    assert timeout_seconds == 600
    assert max_attempts == 2


def test_gpt5mini_xhigh_is_capped_to_high() -> None:
    service = AgentService(llm_overrides={"agent1": {"model": "gpt-5-mini", "thinking_level": "xhigh"}})
    model, effort, verbosity, timeout_seconds, max_attempts = service._resolve_agent_model_and_effort(
        agent_key="agent1",
        default_model="gpt-5.4",
    )
    assert model == "gpt-5-mini"
    assert effort == "high"
    assert verbosity == "medium"
    assert timeout_seconds == 600
    assert max_attempts == 2


def test_zero_timeout_override_is_preserved() -> None:
    service = AgentService(llm_overrides={"agent2": {"model": "gpt-5.4", "timeout_seconds": 0}})
    model, effort, verbosity, timeout_seconds, max_attempts = service._resolve_agent_model_and_effort(
        agent_key="agent2",
        default_model="gpt-5.4",
    )
    assert model == "gpt-5.4"
    assert effort == service.settings.openai_reasoning_effort
    assert verbosity == "medium"
    assert timeout_seconds == 0
    assert max_attempts == 2
