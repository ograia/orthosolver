from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonObjectStore:
    """Tiny JSON object store with filesystem and optional GCS backing."""

    def __init__(self, root_dir: Path) -> None:
        self.root = root_dir.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._mode = os.getenv("LEAN_ENGINE_STORAGE_BACKEND", "filesystem").strip().lower()
        if self._mode not in {"filesystem", "gcs"}:
            self._mode = "filesystem"
        self._bucket_name = os.getenv("GCS_BUCKET")
        self._prefix = os.getenv("LEAN_ENGINE_GCS_STATE_PREFIX", "orthosolver/lean-service-state").strip("/")
        self._client = None

    @property
    def mode(self) -> str:
        return self._mode

    def _normalize_key(self, key: str) -> str:
        normalized = str(key or "").replace("\\", "/").strip("/")
        if not normalized:
            raise ValueError("storage key must not be empty")
        parts = Path(normalized).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("storage key contains invalid path segments")
        return normalized

    def _local_path(self, key: str) -> Path:
        return self.root / self._normalize_key(key)

    def _remote_key(self, key: str) -> str:
        normalized = self._normalize_key(key)
        return f"{self._prefix}/{normalized}" if self._prefix else normalized

    def _blob(self, key: str):
        if self._mode != "gcs":
            return None
        if not self._bucket_name:
            raise RuntimeError("GCS_BUCKET is required when LEAN_ENGINE_STORAGE_BACKEND=gcs")
        if self._client is None:
            try:
                from google.cloud import storage
            except ModuleNotFoundError as exc:  # pragma: no cover - optional runtime dependency
                raise RuntimeError("google-cloud-storage is required when LEAN_ENGINE_STORAGE_BACKEND=gcs") from exc
            self._client = storage.Client()
        bucket = self._client.bucket(self._bucket_name)
        return bucket.blob(self._remote_key(key))

    def create_json_if_absent(self, key: str, payload: dict[str, Any]) -> bool:
        data = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        normalized = self._normalize_key(key)
        path = self._local_path(normalized)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._mode == "filesystem":
            if path.exists():
                return False
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(str(tmp), str(path))
            return True

        blob = self._blob(normalized)
        assert blob is not None
        try:
            blob.upload_from_string(data, content_type="application/json", if_generation_match=0)
        except Exception as exc:
            try:
                from google.api_core.exceptions import PreconditionFailed
            except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
                PreconditionFailed = None
            if PreconditionFailed is not None and isinstance(exc, PreconditionFailed):
                return False
            raise
        path.write_bytes(data)
        return True

    def write_json(self, key: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        normalized = self._normalize_key(key)
        path = self._local_path(normalized)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(path))
        if self._mode == "gcs":
            blob = self._blob(normalized)
            assert blob is not None
            blob.upload_from_string(data, content_type="application/json")

    def read_json(self, key: str) -> dict[str, Any] | None:
        normalized = self._normalize_key(key)
        path = self._local_path(normalized)
        if self._mode == "filesystem":
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

        blob = self._blob(normalized)
        assert blob is not None
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        parsed = json.loads(data.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None

    def list_keys(self, *, prefix: str = "") -> list[str]:
        normalized_prefix = prefix.replace("\\", "/").strip("/")
        if self._mode == "filesystem":
            base = self.root / normalized_prefix if normalized_prefix else self.root
            if not base.exists():
                return []
            return sorted(
                path.relative_to(self.root).as_posix()
                for path in base.rglob("*.json")
                if path.is_file()
            )

        blob_prefix = self._remote_key(normalized_prefix) if normalized_prefix else self._prefix
        blob = self._blob("jobs/dummy.json")
        assert blob is not None
        bucket = blob.bucket
        keys: list[str] = []
        for item in bucket.list_blobs(prefix=blob_prefix):
            name = str(item.name)
            if self._prefix:
                prefix_with_slash = f"{self._prefix}/"
                if name.startswith(prefix_with_slash):
                    name = name[len(prefix_with_slash):]
            name = name.strip("/")
            if name.endswith(".json"):
                keys.append(name)
        return sorted(keys)

    def clear_prefix(self, prefix: str) -> None:
        keys = self.list_keys(prefix=prefix)
        for key in keys:
            self.delete(key)
        local_dir = self.root / prefix.strip("/")
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)

    def delete(self, key: str) -> None:
        normalized = self._normalize_key(key)
        self._local_path(normalized).unlink(missing_ok=True)
        if self._mode == "gcs":
            blob = self._blob(normalized)
            assert blob is not None
            blob.delete(if_generation_match=None)

    def modified_at(self, key: str) -> datetime | None:
        normalized = self._normalize_key(key)
        path = self._local_path(normalized)
        if self._mode == "filesystem":
            if not path.exists():
                return None
            return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        blob = self._blob(normalized)
        assert blob is not None
        if not blob.exists():
            return None
        blob.reload()
        updated = blob.updated
        if updated is None:
            return datetime.now(UTC)
        if updated.tzinfo is None:
            return updated.replace(tzinfo=UTC)
        return updated
