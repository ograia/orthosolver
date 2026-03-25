from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import ClaudeConfig, RuntimeConfig
from .contracts import NormalizedProblemBundle

_MIN_TIMEOUT_SECONDS = 0
_MAX_TIMEOUT_SECONDS = 43200

_TIMEOUT_KEYS = (
    "agent_hard_seconds",
    "agent_idle_seconds",
    "tool_wait_seconds",
    "init_silence_seconds",
    "lean_check_seconds",
    "lean_build_seconds",
)

_PHASE_KEYS = (
    "workspace_prepare",
    "phase03_statement",
    "phase04_lemma",
    "phase05_semantic",
    "phase06_root",
)


@dataclass(frozen=True)
class PhaseTimeoutPolicy:
    agent_hard_seconds: int
    agent_idle_seconds: int
    tool_wait_seconds: int
    init_silence_seconds: int
    lean_check_seconds: int
    lean_build_seconds: int

    def to_dict(self) -> dict[str, int]:
        return {
            "agent_hard_seconds": self.agent_hard_seconds,
            "agent_idle_seconds": self.agent_idle_seconds,
            "tool_wait_seconds": self.tool_wait_seconds,
            "init_silence_seconds": self.init_silence_seconds,
            "lean_check_seconds": self.lean_check_seconds,
            "lean_build_seconds": self.lean_build_seconds,
        }


@dataclass(frozen=True)
class ResolvedTimeoutPolicy:
    version: int
    defaults: PhaseTimeoutPolicy
    phases: dict[str, PhaseTimeoutPolicy]
    warnings: tuple[str, ...]
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "defaults": self.defaults.to_dict(),
            "phases": {name: policy.to_dict() for name, policy in self.phases.items()},
            "warnings": list(self.warnings),
            "sources": list(self.sources),
        }


def resolve_timeout_policy(
    *,
    bundle: NormalizedProblemBundle | None,
    runtime_config: RuntimeConfig,
    cli_timeout_seconds: int | None = None,
    cli_workspace_timeout_seconds: int | None = None,
    timeout_policy_path: Path | None = None,
) -> ResolvedTimeoutPolicy:
    warnings: list[str] = []
    sources: list[str] = ["runtime_config"]

    defaults = {
        "agent_hard_seconds": _clamp_timeout(runtime_config.claude.timeout_seconds, "claude.timeout_seconds"),
        "agent_idle_seconds": _clamp_timeout(
            runtime_config.claude.stall_timeout_seconds,
            "claude.stall_timeout_seconds",
        ),
        "tool_wait_seconds": _clamp_timeout(
            runtime_config.claude.tool_wait_timeout_seconds,
            "claude.tool_wait_timeout_seconds",
        ),
        "init_silence_seconds": _clamp_timeout(
            runtime_config.claude.init_timeout_seconds,
            "claude.init_timeout_seconds",
        ),
        "lean_check_seconds": _clamp_timeout(runtime_config.claude.timeout_seconds, "claude.timeout_seconds"),
        "lean_build_seconds": _clamp_timeout(runtime_config.claude.timeout_seconds, "claude.timeout_seconds"),
    }
    phase_overrides: dict[str, dict[str, int]] = {name: {} for name in _PHASE_KEYS}
    version = 1

    if bundle is not None:
        payload = _extract_timeout_payload(bundle.extra.get("engine_controls"))
        if payload is not None:
            sources.append("normalized_input")
            version = _apply_timeout_payload(
                payload=payload,
                defaults=defaults,
                phase_overrides=phase_overrides,
                warnings=warnings,
                source_label="normalized_input",
                fallback_version=version,
            )

    if timeout_policy_path is not None:
        raw = _load_json_object(timeout_policy_path)
        payload = _extract_timeout_payload(raw)
        if payload is None:
            payload = _extract_timeout_payload(raw.get("engine_controls"))
        if payload is None:
            raise ValueError(
                "timeout policy file must contain `engine_controls.timeouts` or top-level `timeouts` object"
            )
        sources.append(str(timeout_policy_path.expanduser().resolve()))
        version = _apply_timeout_payload(
            payload=payload,
            defaults=defaults,
            phase_overrides=phase_overrides,
            warnings=warnings,
            source_label="timeout_policy_file",
            fallback_version=version,
        )

    if cli_timeout_seconds is not None:
        seconds = _coerce_cli_timeout(cli_timeout_seconds, "--timeout")
        for key in _TIMEOUT_KEYS:
            defaults[key] = seconds
        for phase in _PHASE_KEYS:
            phase_overrides[phase].update({key: seconds for key in _TIMEOUT_KEYS})
        sources.append("cli_timeout_override")

    if cli_workspace_timeout_seconds is not None:
        seconds = _coerce_cli_timeout(cli_workspace_timeout_seconds, "--workspace-timeout")
        phase_overrides["workspace_prepare"]["lean_build_seconds"] = seconds
        phase_overrides["workspace_prepare"]["lean_check_seconds"] = seconds
        sources.append("cli_workspace_timeout_override")

    defaults_policy = _materialize(defaults)
    phases: dict[str, PhaseTimeoutPolicy] = {}
    for phase_name in _PHASE_KEYS:
        merged = copy.deepcopy(defaults)
        merged.update(phase_overrides.get(phase_name, {}))
        phases[phase_name] = _materialize(merged)

    return ResolvedTimeoutPolicy(
        version=version,
        defaults=defaults_policy,
        phases=phases,
        warnings=tuple(warnings),
        sources=tuple(sources),
    )


def runtime_config_for_phase(runtime_config: RuntimeConfig, phase_policy: PhaseTimeoutPolicy) -> RuntimeConfig:
    claude = runtime_config.claude
    phase_claude = ClaudeConfig(
        model=claude.model,
        fallback_model=claude.fallback_model,
        timeout_seconds=phase_policy.agent_hard_seconds,
        stall_timeout_seconds=phase_policy.agent_idle_seconds,
        tool_wait_timeout_seconds=phase_policy.tool_wait_seconds,
        init_timeout_seconds=phase_policy.init_silence_seconds,
        max_output_tokens=claude.max_output_tokens,
        command=claude.command,
    )
    return replace(runtime_config, claude=phase_claude)


def _materialize(values: dict[str, int]) -> PhaseTimeoutPolicy:
    return PhaseTimeoutPolicy(
        agent_hard_seconds=values["agent_hard_seconds"],
        agent_idle_seconds=values["agent_idle_seconds"],
        tool_wait_seconds=values["tool_wait_seconds"],
        init_silence_seconds=values["init_silence_seconds"],
        lean_check_seconds=values["lean_check_seconds"],
        lean_build_seconds=values["lean_build_seconds"],
    )


def _extract_timeout_payload(container: Any) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    timeouts = container.get("timeouts")
    if isinstance(timeouts, dict):
        return timeouts
    return None


def _apply_timeout_payload(
    *,
    payload: dict[str, Any],
    defaults: dict[str, int],
    phase_overrides: dict[str, dict[str, int]],
    warnings: list[str],
    source_label: str,
    fallback_version: int,
) -> int:
    version = payload.get("version", fallback_version)
    if not isinstance(version, int) or version <= 0:
        warnings.append(f"{source_label}: invalid `version` ({version!r}); using {fallback_version}")
        version = fallback_version

    defaults_raw = payload.get("defaults")
    if defaults_raw is not None:
        if not isinstance(defaults_raw, dict):
            warnings.append(f"{source_label}: `defaults` must be an object; ignoring")
        else:
            _apply_timeout_map(
                source=defaults_raw,
                target=defaults,
                source_label=f"{source_label}.defaults",
                warnings=warnings,
            )

    phases_raw = payload.get("phases")
    if phases_raw is not None:
        if not isinstance(phases_raw, dict):
            warnings.append(f"{source_label}: `phases` must be an object; ignoring")
        else:
            for phase_name, phase_values in phases_raw.items():
                if phase_name not in _PHASE_KEYS:
                    warnings.append(f"{source_label}: unknown phase key `{phase_name}` ignored")
                    continue
                if not isinstance(phase_values, dict):
                    warnings.append(f"{source_label}.phases.{phase_name}: must be an object; ignoring")
                    continue
                _apply_timeout_map(
                    source=phase_values,
                    target=phase_overrides[phase_name],
                    source_label=f"{source_label}.phases.{phase_name}",
                    warnings=warnings,
                )
    return version


def _apply_timeout_map(
    *,
    source: dict[str, Any],
    target: dict[str, int],
    source_label: str,
    warnings: list[str],
) -> None:
    for key, value in source.items():
        if key not in _TIMEOUT_KEYS:
            warnings.append(f"{source_label}: unknown timeout key `{key}` ignored")
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            warnings.append(f"{source_label}.{key}: must be integer seconds; ignored")
            continue
        if value < _MIN_TIMEOUT_SECONDS:
            warnings.append(f"{source_label}.{key}: must be >= {_MIN_TIMEOUT_SECONDS}; ignored")
            continue
        target[key] = _clamp_timeout(value, f"{source_label}.{key}")


def _clamp_timeout(value: int, label: str) -> int:
    if value < _MIN_TIMEOUT_SECONDS:
        raise ValueError(f"{label} must be >= {_MIN_TIMEOUT_SECONDS}")
    if value > _MAX_TIMEOUT_SECONDS:
        return _MAX_TIMEOUT_SECONDS
    return value


def _coerce_cli_timeout(value: int, flag_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"CLI {flag_name} must be an integer")
    if value <= 0:
        return 0
    return _clamp_timeout(value, flag_name)


def _load_json_object(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"failed to read timeout policy file: {resolved} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"timeout policy file is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("timeout policy file top-level value must be an object")
    return payload
