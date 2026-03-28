from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nl_engine.artifacts.store import ArtifactStore

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
    """Safe artifact browser over the configured artifact store boundary."""

    def __init__(self, root_dir: str) -> None:
        self.store = ArtifactStore(root_dir)

    def _normalize_key(self, key: str, *, allow_empty: bool = False) -> str:
        normalized = str(key or "").replace("\\", "/").strip("/")
        if not normalized and allow_empty:
            return ""
        if not normalized:
            raise ArtifactSecurityError("artifact key must not be empty")
        if any(part in {"..", "", "."} for part in normalized.split("/")):
            raise ArtifactSecurityError("artifact key contains invalid path segments")
        return normalized

    def exists(self, artifact_key: str) -> bool:
        normalized = self._normalize_key(artifact_key)
        return self.store.exists(normalized)

    def list_files(self, *, prefix: str = "", limit: int = 500) -> list[ArtifactEntry]:
        rows: list[ArtifactEntry] = []
        normalized_prefix = self._normalize_key(prefix, allow_empty=True)
        for rel_key in self.store.list_keys(prefix=normalized_prefix):
            meta = self.store.stat(rel_key)
            if meta is None:
                continue
            rows.append(
                ArtifactEntry(
                    artifact_key=rel_key,
                    size_bytes=meta.size_bytes,
                    modified_at=meta.modified_at,
                    content_type=meta.content_type,
                )
            )
            if len(rows) >= limit:
                break
        return rows

    def read(self, artifact_key: str) -> ArtifactContent:
        normalized = self._normalize_key(artifact_key)
        meta = self.store.stat(normalized)
        if meta is None:
            raise FileNotFoundError(normalized)

        if normalized.lower().endswith(".json"):
            text = self.store.load_text(normalized)
            try:
                parsed = json.loads(text)
                return ArtifactContent(
                    artifact_key=normalized,
                    format="json",
                    content=parsed,
                    size_bytes=meta.size_bytes,
                    content_type=meta.content_type or "application/json",
                )
            except json.JSONDecodeError:
                # Preserve inspectability when malformed JSON is encountered.
                return ArtifactContent(
                    artifact_key=normalized,
                    format="text",
                    content=text,
                    size_bytes=meta.size_bytes,
                    content_type=meta.content_type or "text/plain",
                )

        text = self.store.load_text(normalized)
        return ArtifactContent(
            artifact_key=normalized,
            format="text",
            content=text,
            size_bytes=meta.size_bytes,
            content_type=meta.content_type or "text/plain",
        )
