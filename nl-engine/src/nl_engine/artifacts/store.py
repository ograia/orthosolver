from __future__ import annotations

import json
import mimetypes
from typing import Any

from nl_engine.settings import get_settings
from nl_engine.storage import ObjectMetadata, ObjectStore


class ArtifactStore:
    """Local artifact store boundary.

    The runtime path is configurable via ARTIFACT_STORE_DIR. In production this
    maps to the GCS artifact boundary from the architecture spec.
    """

    def __init__(self, root_dir: str | None = None) -> None:
        settings = get_settings()
        self.objects = ObjectStore(root_dir or settings.artifact_store_dir, purpose="artifacts")
        self.root = self.objects.root

    def save_text(self, key: str, body: str) -> str:
        content_type = mimetypes.guess_type(key)[0] or "text/plain"
        self.objects.write_text(key, body, content_type=content_type)
        return key

    def save_json(self, key: str, payload: Any) -> str:
        self.objects.write_text(
            key,
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            content_type="application/json",
        )
        return key

    def exists(self, key: str) -> bool:
        return self.objects.exists(key)

    def load_json(self, key: str) -> Any:
        payload = self.objects.read_json(key)
        if payload is None:
            raise FileNotFoundError(key)
        return payload

    def load_text(self, key: str) -> str:
        payload = self.objects.read_text(key)
        if payload is None:
            raise FileNotFoundError(key)
        return payload

    def delete(self, key: str) -> None:
        if self.exists(key):
            self.objects.delete(key)

    def stat(self, key: str) -> ObjectMetadata | None:
        return self.objects.stat(key)

    def list_keys(self, *, prefix: str = "") -> list[str]:
        return self.objects.list_keys(prefix=prefix)

    def append_text(self, key: str, line: str) -> str:
        existing = self.objects.read_text(key) or ""
        self.objects.write_text(key, existing + line, content_type="application/x-ndjson")
        return key
