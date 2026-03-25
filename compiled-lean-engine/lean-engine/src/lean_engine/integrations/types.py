from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IntegrationInventory:
    name: str
    repo_root: Path
    exists: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repo_root": str(self.repo_root),
            "exists": self.exists,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class IntegrationPreflight:
    name: str
    status: str
    checks: dict[str, bool]
    diagnostics: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "checks": dict(self.checks),
            "diagnostics": list(self.diagnostics),
            "details": dict(self.details),
        }


@dataclass
class IntegrationUsageTracker:
    profile: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    repos_seen: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_runs: list[dict[str, Any]] = field(default_factory=list)

    def note_inventory(self, inventory: IntegrationInventory) -> None:
        self.repos_seen[inventory.name] = {
            "repo_root": str(inventory.repo_root),
            "exists": inventory.exists,
            "inventory_details": dict(inventory.details),
        }

    def note_preflight(self, preflight: IntegrationPreflight) -> None:
        existing = self.repos_seen.setdefault(preflight.name, {})
        existing["preflight_status"] = preflight.status
        existing["preflight_checks"] = dict(preflight.checks)
        existing["preflight_diagnostics"] = list(preflight.diagnostics)

    def note_tool_run(
        self,
        *,
        repo: str,
        tool: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.tool_runs.append(
            {
                "repo": repo,
                "tool": tool,
                "status": status,
                "details": dict(details or {}),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "started_at": self.started_at,
            "repos_seen": dict(self.repos_seen),
            "tool_runs": list(self.tool_runs),
        }
