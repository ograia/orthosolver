from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nl_engine.settings import get_settings


class ArtifactStore:
    """Local artifact store boundary.

    The runtime path is configurable via ARTIFACT_STORE_DIR. In production this
    maps to the GCS artifact boundary from the architecture spec.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.root = Path(settings.artifact_store_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def save_text(self, key: str, body: str) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return key

    def save_json(self, key: str, payload: Any) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return key

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def load_json(self, key: str) -> Any:
        path = self.root / key
        return json.loads(path.read_text())
