from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_CLAUDE_MODELS = (
    "claude-opus-4-6",
    "claude-opus-4-6[1m]",
    "claude-sonnet-4-6",
    "claude-sonnet-4-6[1m]",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)


@dataclass(frozen=True)
class ClaudeConfig:
    model: str
    timeout_seconds: int
    fallback_model: str | None = None
    stall_timeout_seconds: int = 2700
    tool_wait_timeout_seconds: int = 2700
    init_timeout_seconds: int = 180
    max_output_tokens: int = 131072
    command: str = "claude"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "fallback_model": self.fallback_model,
            "timeout_seconds": self.timeout_seconds,
            "stall_timeout_seconds": self.stall_timeout_seconds,
            "tool_wait_timeout_seconds": self.tool_wait_timeout_seconds,
            "init_timeout_seconds": self.init_timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
            "command": self.command,
        }


@dataclass(frozen=True)
class LeanConfig:
    imports: tuple[str, ...]
    lake_jobs: int = 0  # 0 = use Lake default (nproc); >0 = cap parallelism

    def to_dict(self) -> dict[str, Any]:
        return {
            "imports": list(self.imports),
            "lake_jobs": self.lake_jobs,
        }


@dataclass(frozen=True)
class ArtifactConfig:
    root: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
        }


@dataclass(frozen=True)
class McpConfig:
    enabled: bool
    config_filename: str
    server_name: str
    command: str
    args: tuple[str, ...]
    log_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "config_filename": self.config_filename,
            "server_name": self.server_name,
            "command": self.command,
            "args": list(self.args),
            "log_name": self.log_name,
        }


@dataclass(frozen=True)
class IntegrationsConfig:
    lean_lsp_mcp_root: Path
    lean4_skills_root: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "lean_lsp_mcp_root": str(self.lean_lsp_mcp_root),
            "lean4_skills_root": str(self.lean4_skills_root),
        }


@dataclass(frozen=True)
class WorkspaceCacheConfig:
    enabled: bool
    cache_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "cache_dir": str(self.cache_dir)}


@dataclass(frozen=True)
class Phase04Config:
    canonical_code_root: Path
    worker_snapshot_mode: str
    merge_order: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_code_root": str(self.canonical_code_root),
            "worker_snapshot_mode": self.worker_snapshot_mode,
            "merge_order": self.merge_order,
        }


@dataclass(frozen=True)
class RuntimeConfig:
    claude: ClaudeConfig
    lean: LeanConfig
    artifacts: ArtifactConfig
    mcp: McpConfig
    integrations: IntegrationsConfig
    workspace_cache: WorkspaceCacheConfig
    phase04: Phase04Config

    def to_dict(self) -> dict[str, Any]:
        return {
            "claude": self.claude.to_dict(),
            "lean": self.lean.to_dict(),
            "artifacts": self.artifacts.to_dict(),
            "mcp": self.mcp.to_dict(),
            "integrations": self.integrations.to_dict(),
            "workspace_cache": self.workspace_cache.to_dict(),
            "phase04": self.phase04.to_dict(),
        }


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_template_dir() -> Path:
    return project_root() / "templates" / "lean_project"


def default_mcp_script_path() -> Path:
    return project_root().parent / "lean-lsp-mcp-main" / "numina-lean-mcp.sh"


def default_repo_lean_lsp_mcp_root() -> Path:
    return project_root().parent / "lean-lsp-mcp-main"


def default_lean4_skills_root() -> Path:
    return project_root().parent / "lean4-skills-main" / "plugins" / "lean4"


def default_runtime_config_dict() -> dict[str, Any]:
    return {
        "claude": {
            "model": "claude-sonnet-4-6",
            "timeout_seconds": 2700,
            "stall_timeout_seconds": 2700,
            "tool_wait_timeout_seconds": 2700,
            "init_timeout_seconds": 180,
            "max_output_tokens": 131072,
            "command": "claude",
        },
        "lean": {
            "imports": ["Mathlib"],
            "lake_jobs": 0,
        },
        "artifacts": {
            "root": ".artifacts/lean_engine",
        },
        "mcp": {
            "enabled": True,
            "config_filename": ".mcp.json",
            "server_name": "lean-lsp",
            "command": str(default_mcp_script_path()),
            "args": [],
            "log_name": "lean_lsp_mcp",
        },
        "integrations": {
            "lean_lsp_mcp_root": str(default_repo_lean_lsp_mcp_root()),
            "lean4_skills_root": str(default_lean4_skills_root()),
        },
        "workspace_cache": {
            "enabled": True,
            "cache_dir": ".artifacts/lean_engine/_lake_cache",
        },
        "phase04": {
            "canonical_code_root": str(project_root()),
            "worker_snapshot_mode": "copy",
            "merge_order": "lemma_order",
        },
    }


def load_runtime_config(
    config_path: Path | None = None,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    timeout_seconds: int | None = None,
    stall_timeout_seconds: int | None = None,
    tool_wait_timeout_seconds: int | None = None,
    init_timeout_seconds: int | None = None,
    artifact_root: Path | None = None,
    mcp_command: str | None = None,
    repo_lean_lsp_mcp_root: Path | None = None,
    lean4_skills_root: Path | None = None,
    lake_jobs: int | None = None,
) -> RuntimeConfig:
    merged = copy.deepcopy(default_runtime_config_dict())

    if config_path is not None:
        raw = _load_json_object(config_path)
        _deep_update(merged, raw)

    if model is not None:
        merged.setdefault("claude", {})["model"] = model
    if fallback_model is not None:
        merged.setdefault("claude", {})["fallback_model"] = fallback_model
    if timeout_seconds is not None:
        merged.setdefault("claude", {})["timeout_seconds"] = timeout_seconds
    if stall_timeout_seconds is not None:
        merged.setdefault("claude", {})["stall_timeout_seconds"] = stall_timeout_seconds
    if tool_wait_timeout_seconds is not None:
        merged.setdefault("claude", {})["tool_wait_timeout_seconds"] = tool_wait_timeout_seconds
    if init_timeout_seconds is not None:
        merged.setdefault("claude", {})["init_timeout_seconds"] = init_timeout_seconds
    if artifact_root is not None:
        merged.setdefault("artifacts", {})["root"] = str(artifact_root)
        _default_cache = ".artifacts/lean_engine/_lake_cache"
        _current_cache = merged.get("workspace_cache", {}).get("cache_dir", _default_cache)
        if _current_cache == _default_cache:
            merged.setdefault("workspace_cache", {})["cache_dir"] = str(Path(artifact_root) / "_lake_cache")
    if mcp_command is not None:
        merged.setdefault("mcp", {})["command"] = mcp_command
    if repo_lean_lsp_mcp_root is not None:
        merged.setdefault("integrations", {})["lean_lsp_mcp_root"] = str(repo_lean_lsp_mcp_root)
    if lean4_skills_root is not None:
        merged.setdefault("integrations", {})["lean4_skills_root"] = str(lean4_skills_root)
    if lake_jobs is not None:
        merged.setdefault("lean", {})["lake_jobs"] = lake_jobs

    return parse_runtime_config(merged)


def parse_runtime_config(data: dict[str, Any]) -> RuntimeConfig:
    claude_raw = _expect_mapping(data, "claude")
    lean_raw = _expect_mapping(data, "lean")
    artifacts_raw = _expect_mapping(data, "artifacts")
    mcp_raw = _expect_mapping(data, "mcp")

    model = _expect_string(claude_raw, "model")
    fallback_model = _optional_string(claude_raw.get("fallback_model"))
    _validate_model(model, field_name="claude.model")
    if fallback_model is not None:
        _validate_model(fallback_model, field_name="claude.fallback_model")
        if fallback_model != model:
            raise ValueError(
                "claude.fallback_model is deprecated and must exactly match claude.model when provided"
            )

    timeout_seconds = _expect_int(claude_raw, "timeout_seconds", minimum=0)
    stall_timeout_raw = claude_raw.get("stall_timeout_seconds", 2700)
    stall_timeout_seconds = int(stall_timeout_raw) if isinstance(stall_timeout_raw, (int, float)) else 2700
    tool_wait_timeout_raw = claude_raw.get("tool_wait_timeout_seconds", stall_timeout_seconds)
    tool_wait_timeout_seconds = (
        int(tool_wait_timeout_raw) if isinstance(tool_wait_timeout_raw, (int, float)) else stall_timeout_seconds
    )
    init_timeout_raw = claude_raw.get("init_timeout_seconds", 180)
    init_timeout_seconds = int(init_timeout_raw) if isinstance(init_timeout_raw, (int, float)) else 180
    max_output_tokens_raw = claude_raw.get("max_output_tokens", 131072)
    max_output_tokens = int(max_output_tokens_raw) if isinstance(max_output_tokens_raw, (int, float)) else 131072
    command = _expect_string(claude_raw, "command")

    imports_raw = lean_raw.get("imports", [])
    if not isinstance(imports_raw, list) or not all(isinstance(item, str) and item.strip() for item in imports_raw):
        raise ValueError("lean.imports must be a list of non-empty strings")

    root_raw = _expect_string(artifacts_raw, "root")

    mcp_enabled_raw = mcp_raw.get("enabled", True)
    if not isinstance(mcp_enabled_raw, bool):
        raise ValueError("mcp.enabled must be a boolean")
    mcp_enabled = mcp_enabled_raw
    mcp_filename = _expect_string(mcp_raw, "config_filename")
    mcp_server_name = _expect_string(mcp_raw, "server_name")
    mcp_command = _expect_string(mcp_raw, "command")
    mcp_args_raw = mcp_raw.get("args", [])
    if not isinstance(mcp_args_raw, list) or not all(isinstance(item, str) for item in mcp_args_raw):
        raise ValueError("mcp.args must be a list of strings")
    mcp_log_name = _expect_string(mcp_raw, "log_name")

    integrations_raw = data.get("integrations")
    if integrations_raw is None:
        integrations_raw = default_runtime_config_dict()["integrations"]
    if not isinstance(integrations_raw, dict):
        raise ValueError("integrations must be an object")

    lean_lsp_mcp_root_str = str(
        integrations_raw.get("lean_lsp_mcp_root", str(default_repo_lean_lsp_mcp_root()))
    ).strip()
    lean4_skills_root_str = str(
        integrations_raw.get("lean4_skills_root", str(default_lean4_skills_root()))
    ).strip()

    workspace_cache_raw = data.get("workspace_cache", {})
    if not isinstance(workspace_cache_raw, dict):
        workspace_cache_raw = {}
    wc_enabled = workspace_cache_raw.get("enabled", True)
    if not isinstance(wc_enabled, bool):
        wc_enabled = True
    wc_cache_dir = str(workspace_cache_raw.get("cache_dir", ".artifacts/lean_engine/_lake_cache")).strip()

    phase04_raw = data.get("phase04", {})
    if not isinstance(phase04_raw, dict):
        phase04_raw = {}
    canonical_code_root = str(phase04_raw.get("canonical_code_root", str(project_root()))).strip()
    worker_snapshot_mode = str(phase04_raw.get("worker_snapshot_mode", "copy")).strip().lower() or "copy"
    merge_order = str(phase04_raw.get("merge_order", "lemma_order")).strip().lower() or "lemma_order"
    if worker_snapshot_mode not in {"copy", "hardlink", "reflink"}:
        raise ValueError("phase04.worker_snapshot_mode must be one of: copy, hardlink, reflink")
    if merge_order not in {"lemma_order"}:
        raise ValueError("phase04.merge_order must be `lemma_order`")

    return RuntimeConfig(
        claude=ClaudeConfig(
            model=model,
            fallback_model=fallback_model,
            timeout_seconds=timeout_seconds,
            stall_timeout_seconds=stall_timeout_seconds,
            tool_wait_timeout_seconds=tool_wait_timeout_seconds,
            init_timeout_seconds=init_timeout_seconds,
            max_output_tokens=max_output_tokens,
            command=command,
        ),
        lean=LeanConfig(
            imports=tuple(item.strip() for item in imports_raw),
            lake_jobs=int(lean_raw.get("lake_jobs", 0)),
        ),
        artifacts=ArtifactConfig(root=Path(root_raw).expanduser()),
        mcp=McpConfig(
            enabled=mcp_enabled,
            config_filename=mcp_filename,
            server_name=mcp_server_name,
            command=mcp_command,
            args=tuple(mcp_args_raw),
            log_name=mcp_log_name,
        ),
        integrations=IntegrationsConfig(
            lean_lsp_mcp_root=Path(lean_lsp_mcp_root_str).expanduser(),
            lean4_skills_root=Path(lean4_skills_root_str).expanduser(),
        ),
        workspace_cache=WorkspaceCacheConfig(
            enabled=wc_enabled,
            cache_dir=Path(wc_cache_dir).expanduser(),
        ),
        phase04=Phase04Config(
            canonical_code_root=Path(canonical_code_root).expanduser(),
            worker_snapshot_mode=worker_snapshot_mode,
            merge_order=merge_order,
        ),
    )


def current_code_origin(*, root: Path | None = None) -> dict[str, Any]:
    code_root = (root or project_root()).expanduser().resolve()
    git_sha = None
    git_dirty = None
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=code_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or None
        git_dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=code_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        git_sha = None
        git_dirty = None
    return {
        "code_root": str(code_root),
        "git_sha": git_sha,
        "git_dirty": git_dirty,
    }


def ensure_canonical_code_root(expected_root: Path) -> dict[str, Any]:
    actual_root = project_root().resolve()
    expected = expected_root.expanduser().resolve()
    if actual_root != expected:
        raise ValueError(
            "lean-engine canonical code root mismatch: "
            f"expected `{expected}`, imported from `{actual_root}`"
        )
    return current_code_origin(root=actual_root)


def _validate_model(model: str, *, field_name: str) -> None:
    if model not in ALLOWED_CLAUDE_MODELS:
        allowed = ", ".join(ALLOWED_CLAUDE_MODELS)
        raise ValueError(f"{field_name} must be one of: {allowed}")


def _load_json_object(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"failed to read config: {resolved} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"config is not valid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise ValueError("config top-level value must be an object")
    return payload


def _deep_update(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _expect_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _expect_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string value must be a string when provided")
    normalized = value.strip()
    return normalized or None


def _expect_int(payload: dict[str, Any], key: str, *, minimum: int = 1) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or value < minimum:
        qualifier = "a positive integer" if minimum > 0 else "a non-negative integer"
        raise ValueError(f"{key} must be {qualifier}")
    return value
