from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path

from nl_engine.domain.models import LlmUsageRecordORM
from nl_engine.persistence.db import FileStore
from nl_engine.persistence.repositories import EventRepository, LlmUsageRepository
from nl_engine.settings import get_settings
from nl_engine.storage.object_store import ObjectStore


def _reset_settings(monkeypatch, *, data_dir: Path, artifact_dir: Path) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "filesystem")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(artifact_dir))
    get_settings.cache_clear()


class _FakePreconditionFailed(Exception):
    pass


class _FakeBlob:
    def __init__(self, bucket: "_FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def upload_from_string(self, data, content_type=None, if_generation_match=None) -> None:
        existing = self.bucket.objects.get(self.name)
        if if_generation_match == 0 and existing is not None:
            raise _FakePreconditionFailed("already exists")
        if if_generation_match not in (None, 0):
            current_generation = existing["generation"] if existing is not None else None
            if current_generation != if_generation_match:
                raise _FakePreconditionFailed("generation mismatch")
        payload = data if isinstance(data, bytes) else str(data).encode("utf-8")
        generation = 1 if existing is None else int(existing["generation"]) + 1
        self.bucket.objects[self.name] = {
            "data": payload,
            "content_type": content_type,
            "generation": generation,
            "updated": datetime.now(UTC),
        }

    def download_as_bytes(self) -> bytes:
        return bytes(self.bucket.objects[self.name]["data"])

    def delete(self, if_generation_match=None) -> None:
        existing = self.bucket.objects.get(self.name)
        if existing is None:
            return
        if if_generation_match is not None and existing["generation"] != if_generation_match:
            raise _FakePreconditionFailed("generation mismatch")
        del self.bucket.objects[self.name]

    def reload(self) -> None:
        return None

    @property
    def generation(self):
        existing = self.bucket.objects.get(self.name)
        return None if existing is None else existing["generation"]

    @property
    def size(self):
        existing = self.bucket.objects.get(self.name)
        return 0 if existing is None else len(existing["data"])

    @property
    def updated(self):
        existing = self.bucket.objects.get(self.name)
        return None if existing is None else existing["updated"]

    @property
    def content_type(self):
        existing = self.bucket.objects.get(self.name)
        return None if existing is None else existing["content_type"]


class _FakeBucket:
    def __init__(self, name: str, objects: dict[str, dict[str, object]]) -> None:
        self.name = name
        self.objects = objects

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self, name)

    def list_blobs(self, prefix: str = "") -> list[_FakeBlob]:
        return [self.blob(name) for name in sorted(self.objects) if name.startswith(prefix)]


class _FakeClient:
    def __init__(self, buckets: dict[str, dict[str, dict[str, object]]]) -> None:
        self._buckets = buckets

    def bucket(self, name: str) -> _FakeBucket:
        objects = self._buckets.setdefault(name, {})
        return _FakeBucket(name, objects)


def _install_fake_gcs(monkeypatch) -> None:
    buckets: dict[str, dict[str, dict[str, object]]] = {}

    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    storage_mod = types.ModuleType("google.cloud.storage")
    api_core_mod = types.ModuleType("google.api_core")
    exceptions_mod = types.ModuleType("google.api_core.exceptions")

    storage_mod.Client = lambda: _FakeClient(buckets)
    exceptions_mod.PreconditionFailed = _FakePreconditionFailed
    cloud_mod.storage = storage_mod
    api_core_mod.exceptions = exceptions_mod
    google_mod.cloud = cloud_mod
    google_mod.api_core = api_core_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
    monkeypatch.setitem(sys.modules, "google.api_core", api_core_mod)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exceptions_mod)


def test_object_store_filesystem_roundtrip_and_listing(tmp_path, monkeypatch) -> None:
    _reset_settings(monkeypatch, data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts")
    store = ObjectStore(str(tmp_path / "objects"), purpose="state")

    assert store.write_json("alpha/item.json", {"value": 1}, if_absent=True) is True
    assert store.write_json("alpha/item.json", {"value": 2}, if_absent=True) is False
    assert store.read_json("alpha/item.json") == {"value": 1}
    assert store.list_keys(prefix="alpha") == ["alpha/item.json"]


def test_object_store_filesystem_lease_roundtrip(tmp_path, monkeypatch) -> None:
    _reset_settings(monkeypatch, data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts")
    store = ObjectStore(str(tmp_path / "objects"), purpose="state")

    assert store.acquire_lease("_leases/prob.json", owner="worker-a", ttl_seconds=1.0, wait_timeout_seconds=0.1)
    assert not store.acquire_lease("_leases/prob.json", owner="worker-b", ttl_seconds=1.0, wait_timeout_seconds=0.1)
    store.release_lease("_leases/prob.json", owner="worker-a")
    assert store.acquire_lease("_leases/prob.json", owner="worker-b", ttl_seconds=1.0, wait_timeout_seconds=0.1)


def test_file_store_problem_lock_is_reentrant_and_updates_index(tmp_path, monkeypatch) -> None:
    _reset_settings(monkeypatch, data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts")
    store = FileStore(str(tmp_path / "data"))

    with store.lock_for("prob_demo"):
        with store.lock_for("prob_demo"):
            store.update_index("prob_demo", "Demo", "created", "2026-03-26T00:00:00Z")

    assert store.read_index()["prob_demo"]["title"] == "Demo"


def test_object_store_gcs_roundtrip_listing_and_lease(tmp_path, monkeypatch) -> None:
    _install_fake_gcs(monkeypatch)
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARTIFACT_STORE_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("GCS_BUCKET", "orthos-test")
    monkeypatch.setenv("GCS_STATE_PREFIX", "orthos/state")
    monkeypatch.setenv("GCS_ARTIFACT_PREFIX", "orthos/artifacts")
    get_settings.cache_clear()

    store = ObjectStore(str(tmp_path / "objects"), purpose="state")

    assert store.mode == "gcs"
    assert store.write_json("alpha/item.json", {"value": 1}, if_absent=True) is True
    assert store.write_json("alpha/item.json", {"value": 2}, if_absent=True) is False
    assert store.read_json("alpha/item.json") == {"value": 1}
    assert store.list_keys(prefix="alpha") == ["alpha/item.json"]
    metadata = store.stat("alpha/item.json")
    assert metadata is not None
    assert metadata.size_bytes > 0

    assert store.acquire_lease("_leases/prob.json", owner="worker-a", ttl_seconds=1.0, wait_timeout_seconds=0.1)
    assert not store.acquire_lease("_leases/prob.json", owner="worker-b", ttl_seconds=1.0, wait_timeout_seconds=0.1)
    store.release_lease("_leases/prob.json", owner="worker-a")
    assert store.acquire_lease("_leases/prob.json", owner="worker-b", ttl_seconds=1.0, wait_timeout_seconds=0.1)


def test_event_and_usage_repositories_write_object_per_record_layout(tmp_path, monkeypatch) -> None:
    _reset_settings(monkeypatch, data_dir=tmp_path / "data", artifact_dir=tmp_path / "artifacts")
    store = FileStore(str(tmp_path / "data"))
    problem_id = "prob_demo"

    event = EventRepository(store).append(problem_id, "problem.start", None, "running")
    usage = LlmUsageRepository(store).create(
        LlmUsageRecordORM(
            usage_id="usage_1",
            problem_id=problem_id,
            stage="agent1",
            provider="openai",
            model="gpt-test",
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.01,
        )
    )

    assert event.event_id == 1
    assert usage.usage_id == "usage_1"
    assert (tmp_path / "data" / problem_id / "events" / "00000001.json").exists()
    assert not (tmp_path / "data" / problem_id / "events.jsonl").exists()
    assert (tmp_path / "data" / problem_id / "llm_usage" / "usage_1.json").exists()
    assert not (tmp_path / "data" / problem_id / "llm_usage.jsonl").exists()
