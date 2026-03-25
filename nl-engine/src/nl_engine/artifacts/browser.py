from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ArtifactSecurityError(ValueError):
    """Raised when an artifact key escapes the configured artifact root."""


@dataclass
class ArtifactEntry:
    artifact_key: str
    size_bytes: int
    modified_at: datetime
    content_type: str | None


@dataclass
class ArtifactContent:
    artifact_key: str
    format: str
    content: Any
    size_bytes: int
    content_type: str | None


class ArtifactBrowser:
    """Safe filesystem browser rooted at a configured artifact directory."""

    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _normalize_key(self, key: str, *, allow_empty: bool = False) -> str:
        normalized = key.replace("\\", "/").strip("/")
        if not normalized and allow_empty:
            return ""
        if not normalized:
            raise ArtifactSecurityError("artifact key must not be empty")
        if any(part in {"..", ""} for part in Path(normalized).parts):
            raise ArtifactSecurityError("artifact key contains invalid path segments")
        return normalized

    def _resolve(self, key: str, *, allow_empty: bool = False) -> tuple[str, Path]:
        normalized = self._normalize_key(key, allow_empty=allow_empty)
        if not normalized:
            return normalized, self.root

        path = (self.root / normalized).resolve()
        if path != self.root and self.root not in path.parents:
            raise ArtifactSecurityError("artifact key escapes artifact root")
        return normalized, path

    def exists(self, artifact_key: str) -> bool:
        _, path = self._resolve(artifact_key)
        return path.exists()

    def list_files(self, *, prefix: str = "", limit: int = 500) -> list[ArtifactEntry]:
        _, base = self._resolve(prefix, allow_empty=True)
        if not base.exists():
            return []

        files: list[Path]
        if base.is_file():
            files = [base]
        else:
            files = [path for path in sorted(base.rglob("*")) if path.is_file()]

        rows: list[ArtifactEntry] = []
        for path in files:
            resolved = path.resolve()
            if resolved != self.root and self.root not in resolved.parents:
                # Defensive: skip symlink escapes.
                continue
            rel_key = resolved.relative_to(self.root).as_posix()
            stat = resolved.stat()
            rows.append(
                ArtifactEntry(
                    artifact_key=rel_key,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    content_type=mimetypes.guess_type(rel_key)[0],
                )
            )
            if len(rows) >= limit:
                break
        return rows

    def read(self, artifact_key: str) -> ArtifactContent:
        normalized, path = self._resolve(artifact_key)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(normalized)

        stat = path.stat()
        content_type = mimetypes.guess_type(normalized)[0]

        if path.suffix.lower() == ".json":
            text = path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(text)
                return ArtifactContent(
                    artifact_key=normalized,
                    format="json",
                    content=parsed,
                    size_bytes=stat.st_size,
                    content_type=content_type or "application/json",
                )
            except json.JSONDecodeError:
                # Preserve inspectability when malformed JSON is encountered.
                return ArtifactContent(
                    artifact_key=normalized,
                    format="text",
                    content=text,
                    size_bytes=stat.st_size,
                    content_type=content_type or "text/plain",
                )

        text = path.read_text(encoding="utf-8", errors="replace")
        return ArtifactContent(
            artifact_key=normalized,
            format="text",
            content=text,
            size_bytes=stat.st_size,
            content_type=content_type or "text/plain",
        )
