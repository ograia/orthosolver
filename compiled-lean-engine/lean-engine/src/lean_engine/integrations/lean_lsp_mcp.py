from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .types import IntegrationInventory, IntegrationPreflight


def inventory(*, repo_root: Path, mcp_command: str) -> IntegrationInventory:
    resolved = repo_root.expanduser().resolve()
    script_path = Path(mcp_command).expanduser()
    if not script_path.is_absolute():
        script_path = resolved / script_path
    script_path = script_path.resolve()

    details = {
        "mcp_command": mcp_command,
        "resolved_script_path": str(script_path),
        "script_exists": script_path.exists(),
        "script_is_file": script_path.is_file(),
        "script_executable": os.access(script_path, os.X_OK) if script_path.exists() else False,
    }
    return IntegrationInventory(
        name="lean_lsp_mcp",
        repo_root=resolved,
        exists=resolved.exists() and resolved.is_dir(),
        details=details,
    )


def preflight(
    *,
    repo_root: Path,
    mcp_command: str,
    workspace_root: Path | None = None,
) -> IntegrationPreflight:
    resolved = repo_root.expanduser().resolve()
    script_path = Path(mcp_command).expanduser()
    if not script_path.is_absolute():
        script_path = resolved / script_path
    script_path = script_path.resolve()

    checks = {
        "repo_exists": resolved.exists() and resolved.is_dir(),
        "script_exists": script_path.exists() and script_path.is_file(),
        "script_executable": os.access(script_path, os.X_OK) if script_path.exists() else False,
        "uvx_available": shutil_which("uvx") is not None,
    }

    if workspace_root is not None:
        workspace = workspace_root.expanduser().resolve()
        checks["workspace_exists"] = workspace.exists() and workspace.is_dir()
        checks["lean_project_markers_present"] = _lean_project_markers_present(workspace)

    diagnostics: list[str] = []
    for key, ok in checks.items():
        if not ok:
            diagnostics.append(f"{key} failed")

    status = "ok" if all(checks.values()) else "degraded"
    return IntegrationPreflight(
        name="lean_lsp_mcp",
        status=status,
        checks=checks,
        diagnostics=tuple(diagnostics),
        details={
            "repo_root": str(resolved),
            "script_path": str(script_path),
            "workspace_root": str(workspace_root) if workspace_root else None,
        },
    )


def health_payload(*, repo_root: Path, mcp_command: str, workspace_root: Path | None = None) -> dict[str, Any]:
    return preflight(repo_root=repo_root, mcp_command=mcp_command, workspace_root=workspace_root).to_dict()


def shutil_which(program: str) -> str | None:
    from shutil import which

    return which(program)


def _lean_project_markers_present(workspace_root: Path) -> bool:
    return (workspace_root / "lakefile.toml").exists() and (workspace_root / "lean-toolchain").exists()
