from __future__ import annotations

import json
import mimetypes
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from nl_engine.settings import get_settings


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size_bytes: int
    modified_at: datetime
    content_type: str | None = None


class ObjectStore:
    """Small storage boundary used by both state and artifact persistence.

    The local filesystem remains the default backend. When `storage_backend`
    is set to `gcs`, objects are mirrored into a local cache directory so the
    rest of the runtime can keep using path-based helpers while durable writes
    go through the shared bucket prefix.
    """

    def __init__(self, root_dir: str, *, purpose: Literal["state", "artifacts"]) -> None:
        settings = get_settings()
        self._mode = settings.storage_backend
        self._purpose = purpose
        self.root = Path(root_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._bucket_name = settings.gcs_bucket
        self._prefix = (
            settings.gcs_state_prefix.strip("/")
            if purpose == "state"
            else settings.gcs_artifact_prefix.strip("/")
        )
        self._client = None

    @property
    def mode(self) -> str:
        return self._mode

    def normalize_key(self, key: str) -> str:
        normalized = str(key or "").replace("\\", "/").strip("/")
        if not normalized:
            raise ValueError("storage key must not be empty")
        parts = Path(normalized).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("storage key contains invalid path segments")
        return normalized

    def _local_path(self, key: str) -> Path:
        return self.root / self.normalize_key(key)

    def _remote_key(self, key: str) -> str:
        normalized = self.normalize_key(key)
        return f"{self._prefix}/{normalized}" if self._prefix else normalized

    def _ensure_gcs_client(self):
        if self._mode != "gcs":
            return None
        if not self._bucket_name:
            raise RuntimeError("gcs_bucket is required when storage_backend=gcs")
        if self._client is None:
            try:
                from google.cloud import storage
            except ModuleNotFoundError as exc:  # pragma: no cover - optional runtime dependency
                raise RuntimeError("google-cloud-storage is required when storage_backend=gcs") from exc
            self._client = storage.Client()
        return self._client

    def _blob(self, key: str):
        client = self._ensure_gcs_client()
        assert client is not None
        bucket = client.bucket(self._bucket_name)
        return bucket.blob(self._remote_key(key))

    def exists(self, key: str) -> bool:
        normalized = self.normalize_key(key)
        local_path = self._local_path(normalized)
        if self._mode == "filesystem":
            return local_path.exists()
        return bool(self._blob(normalized).exists())

    def write_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        if_absent: bool = False,
    ) -> bool:
        normalized = self.normalize_key(key)
        local_path = self._local_path(normalized)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".tmp")
        tmp.write_bytes(data)
        if if_absent and local_path.exists() and self._mode == "filesystem":
            tmp.unlink(missing_ok=True)
            return False
        os.replace(str(tmp), str(local_path))

        if self._mode == "filesystem":
            return True

        blob = self._blob(normalized)
        try:
            if if_absent:
                blob.upload_from_string(
                    data,
                    content_type=content_type,
                    if_generation_match=0,
                )
            else:
                blob.upload_from_string(data, content_type=content_type)
        except Exception as exc:
            try:
                from google.api_core.exceptions import PreconditionFailed
            except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
                PreconditionFailed = None
            if if_absent and PreconditionFailed is not None and isinstance(exc, PreconditionFailed):
                return False
            raise
        return True

    def write_text(
        self,
        key: str,
        body: str,
        *,
        content_type: str | None = None,
        if_absent: bool = False,
    ) -> bool:
        resolved_type = content_type or mimetypes.guess_type(key)[0] or "text/plain"
        return self.write_bytes(
            key,
            body.encode("utf-8"),
            content_type=resolved_type,
            if_absent=if_absent,
        )

    def write_json(self, key: str, payload: Any, *, if_absent: bool = False) -> bool:
        return self.write_text(
            key,
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            content_type="application/json",
            if_absent=if_absent,
        )

    def read_bytes(self, key: str) -> bytes | None:
        normalized = self.normalize_key(key)
        local_path = self._local_path(normalized)
        if self._mode == "filesystem":
            if not local_path.exists():
                return None
            return local_path.read_bytes()

        blob = self._blob(normalized)
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return data

    def read_text(self, key: str) -> str | None:
        data = self.read_bytes(key)
        if data is None:
            return None
        return data.decode("utf-8")

    def read_json(self, key: str) -> Any | None:
        raw = self.read_text(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def delete(self, key: str) -> None:
        normalized = self.normalize_key(key)
        local_path = self._local_path(normalized)
        local_path.unlink(missing_ok=True)
        if self._mode == "gcs":
            self._blob(normalized).delete(if_generation_match=None)

    def list_keys(self, *, prefix: str = "") -> list[str]:
        normalized_prefix = prefix.replace("\\", "/").strip("/")
        if self._mode == "filesystem":
            base = self.root / normalized_prefix if normalized_prefix else self.root
            if not base.exists():
                return []
            files = [path for path in base.rglob("*") if path.is_file()]
            return sorted(path.relative_to(self.root).as_posix() for path in files)

        remote_prefix = self._remote_key(normalized_prefix) if normalized_prefix else self._prefix
        client = self._ensure_gcs_client()
        assert client is not None
        bucket = client.bucket(self._bucket_name)
        items: list[str] = []
        for blob in bucket.list_blobs(prefix=remote_prefix):
            name = str(blob.name)
            if self._prefix:
                prefix_with_slash = f"{self._prefix}/"
                if name.startswith(prefix_with_slash):
                    name = name[len(prefix_with_slash):]
                elif name == self._prefix:
                    name = ""
            name = name.strip("/")
            if name:
                items.append(name)
        return sorted(items)

    def sync_key(self, key: str) -> Path | None:
        normalized = self.normalize_key(key)
        data = self.read_bytes(normalized)
        if data is None:
            return None
        return self._local_path(normalized)

    def sync_prefix(self, prefix: str) -> list[Path]:
        keys = self.list_keys(prefix=prefix)
        paths: list[Path] = []
        for key in keys:
            synced = self.sync_key(key)
            if synced is not None:
                paths.append(synced)
        return paths

    def stat(self, key: str) -> ObjectMetadata | None:
        normalized = self.normalize_key(key)
        if self._mode == "filesystem":
            path = self._local_path(normalized)
            if not path.exists():
                return None
            stats = path.stat()
            return ObjectMetadata(
                key=normalized,
                size_bytes=stats.st_size,
                modified_at=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
                content_type=mimetypes.guess_type(normalized)[0],
            )

        blob = self._blob(normalized)
        if not blob.exists():
            return None
        blob.reload()
        updated = blob.updated or datetime.now(UTC)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return ObjectMetadata(
            key=normalized,
            size_bytes=int(blob.size or 0),
            modified_at=updated,
            content_type=blob.content_type,
        )

    def clear_prefix(self, prefix: str) -> int:
        keys = self.list_keys(prefix=prefix)
        for key in keys:
            self.delete(key)
        local_dir = self.root / prefix.strip("/")
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)
        return len(keys)

    def clear_all(self) -> None:
        if self._mode == "filesystem":
            for child in self.root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            return
        self.clear_prefix("")
        for child in self.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def acquire_lease(
        self,
        key: str,
        *,
        owner: str,
        ttl_seconds: float = 10.0,
        wait_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.05,
    ) -> bool:
        normalized = self.normalize_key(key)
        deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
        while True:
            now = datetime.now(UTC)
            lease_payload = {
                "owner": owner,
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=max(ttl_seconds, 0.1))).isoformat(),
            }
            if self.write_json(normalized, lease_payload, if_absent=True):
                return True

            existing = self.read_json(normalized)
            expires_at = None
            if isinstance(existing, dict):
                raw_expiry = existing.get("expires_at")
                if isinstance(raw_expiry, str):
                    try:
                        expires_at = datetime.fromisoformat(raw_expiry)
                    except ValueError:
                        expires_at = None
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at < now:
                    self._delete_expired_lease(normalized, owner=owner)
                    continue

            if time.monotonic() >= deadline:
                return False
            time.sleep(max(poll_interval_seconds, 0.01))

    def release_lease(self, key: str, *, owner: str) -> None:
        normalized = self.normalize_key(key)
        existing = self.read_json(normalized)
        if not isinstance(existing, dict):
            return
        if str(existing.get("owner") or "") != owner:
            return
        if self._mode == "filesystem":
            self.delete(normalized)
            return
        blob = self._blob(normalized)
        if not blob.exists():
            return
        blob.reload()
        generation = int(blob.generation) if blob.generation is not None else None
        try:
            blob.delete(if_generation_match=generation)
        except Exception:
            return
        self._local_path(normalized).unlink(missing_ok=True)

    def _delete_expired_lease(self, key: str, *, owner: str) -> None:
        if self._mode == "filesystem":
            self.delete(key)
            return
        blob = self._blob(key)
        if not blob.exists():
            return
        blob.reload()
        generation = int(blob.generation) if blob.generation is not None else None
        try:
            blob.delete(if_generation_match=generation)
        except Exception:
            return
        self._local_path(key).unlink(missing_ok=True)
