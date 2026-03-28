from __future__ import annotations

import contextlib
import fcntl
import hashlib
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

from .artifact_io import workspace_file_digest, write_json
from .config import RuntimeConfig, default_template_dir


ORTHOS_FILE_PATHS = (
    "Orthos/Statements.lean",
    "Orthos/Lemmas.lean",
    "Orthos/Root.lean",
)


@dataclass(frozen=True)
class WorkspaceFiles:
    workspace_root: Path
    statements_file: Path
    lemmas_file: Path
    root_file: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_root": str(self.workspace_root),
            "statements_file": str(self.statements_file),
            "lemmas_file": str(self.lemmas_file),
            "root_file": str(self.root_file),
        }


def compute_cache_key(template_root: Path) -> str:
    """Hash lean-toolchain + lakefile.toml + lake-manifest.json to derive a stable cache key."""
    hasher = hashlib.sha256()
    for filename in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        filepath = template_root / filename
        if filepath.exists():
            hasher.update(filename.encode())
            hasher.update(filepath.read_bytes())
    return hasher.hexdigest()[:16]


def resolve_cache_lake_dir(cache_dir: Path, template_root: Path) -> Path:
    """Return the path to the cached .lake/ directory for a given template."""
    key = compute_cache_key(template_root)
    return cache_dir / key / ".lake"


@contextlib.contextmanager
def _cache_lock(cache_lake_dir: Path, *, exclusive: bool) -> Generator[None, None, None]:
    """Acquire a shared (reader) or exclusive (writer) flock on a cache entry.

    Lock file lives alongside the cache directory:
        <cache_root>/<hash>.lock   (cache_lake_dir = <cache_root>/<hash>/.lake)

    Multiple concurrent readers (shared) are allowed simultaneously.
    A writer (exclusive) blocks until all readers release and then runs alone.
    This prevents concurrent cp -a reads/writes from corrupting the cache.
    """
    # cache_lake_dir = <cache_root>/<hash>/.lake  →  lock at <cache_root>/<hash>.lock
    lock_path = cache_lake_dir.parent.parent / (cache_lake_dir.parent.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    mode_name = "exclusive" if exclusive else "shared"
    with open(lock_path, "w") as lock_fh:
        print(
            f"[lean_engine] cache lock: acquiring {mode_name} lock on {lock_path.name}",
            file=sys.stderr, flush=True,
        )
        fcntl.flock(lock_fh, mode)
        try:
            print(
                f"[lean_engine] cache lock: {mode_name} lock acquired",
                file=sys.stderr, flush=True,
            )
            yield
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def link_lake_cache(workspace_root: Path, cache_lake_dir: Path) -> bool:
    """Full-copy the cached .lake/ into a workspace for complete isolation.

    Uses ``cp -a`` (full copy) so the workspace has its own independent copy
    of all oleans and packages.  This prevents parallel ``lake serve`` processes
    from corrupting the shared cache via hardlinked inodes (which caused SIGBUS
    and lost build/ directories in prior runs).

    The copy takes ~30-60s for a 12GB .lake/ but only happens once per run and
    guarantees the cache remains a read-only golden copy.

    Returns True if the cache was successfully copied.
    """
    if not cache_lake_dir.exists() or not cache_lake_dir.is_dir():
        return False

    with _cache_lock(cache_lake_dir, exclusive=False):
        if not cache_lake_dir.exists() or not cache_lake_dir.is_dir():
            # Cache was removed between the existence check and lock acquisition
            return False

        dest_lake = workspace_root / ".lake"
        if dest_lake.exists():
            shutil.rmtree(dest_lake)

        try:
            subprocess.run(
                ["cp", "-a", str(cache_lake_dir.resolve()), str(dest_lake)],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(
                f"[lean_engine] WARNING: cache copy failed: {exc}",
                file=sys.stderr, flush=True,
            )
            if dest_lake.exists():
                shutil.rmtree(dest_lake)
            return False

    # Restore and touch happen outside the lock — they operate on the
    # workspace copy, not the cache, so they don't need cache protection.
    # cp -a resolves symlinks to regular files, which makes git repos in
    # .lake/packages/ appear dirty ("has local changes").  Restore them
    # to HEAD so lake doesn't try to rebuild the world.
    _restore_package_git_state(dest_lake)
    # git checkout updates source file mtimes to "now", making Lake think
    # the oleans are stale.  Touch all oleans so they appear fresh.
    _touch_oleans(dest_lake)
    return True


def _restore_package_git_state(lake_dir: Path) -> None:
    """Reset git state of packages under .lake/packages/ so lake doesn't
    see them as having local changes (which triggers unwanted rebuilds)."""
    packages_dir = lake_dir / "packages"
    if not packages_dir.is_dir():
        return
    for pkg_dir in packages_dir.iterdir():
        if not pkg_dir.is_dir():
            continue
        git_dir = pkg_dir / ".git"
        if not git_dir.exists():
            continue
        try:
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=str(pkg_dir),
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass


def _touch_oleans(lake_dir: Path) -> None:
    """Touch all .olean files under .lake/ so their mtime is newer than sources.

    After ``_restore_package_git_state`` runs ``git checkout``, source files
    get the current time as mtime.  Lake uses mtime to decide if oleans are
    stale.  Touching oleans ensures they appear fresh and avoids hundreds of
    unnecessary rebuilds.
    """
    try:
        subprocess.run(
            ["find", str(lake_dir), "-name", "*.olean", "-exec", "touch", "{}", "+"],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def update_lake_cache(workspace_root: Path, cache_lake_dir: Path) -> bool:
    """Copy workspace .lake/ back to cache after a successful build.

    Uses ``cp -a`` (full copy) so the cache is an independent snapshot that
    cannot be corrupted by later workspace mutations (parallel lake serve, etc.).
    Returns True if cache was updated.
    """
    source_lake = workspace_root / ".lake"
    if not source_lake.exists() or not source_lake.is_dir():
        return False

    cache_lake_dir = cache_lake_dir.resolve()
    cache_lake_dir.parent.mkdir(parents=True, exist_ok=True)

    with _cache_lock(cache_lake_dir, exclusive=True):
        if cache_lake_dir.exists():
            shutil.rmtree(cache_lake_dir)

        try:
            subprocess.run(
                ["cp", "-a", str(source_lake.resolve()), str(cache_lake_dir)],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            if cache_lake_dir.exists():
                shutil.rmtree(cache_lake_dir)
            print(f"[lean_engine] WARNING: cache update failed: {exc}", file=sys.stderr, flush=True)
            return False

    print(f"[lean_engine] workspace cache UPDATED: {cache_lake_dir}", file=sys.stderr, flush=True)
    return True


@dataclass(frozen=True)
class WorkspaceCreationResult:
    files: WorkspaceFiles
    cache_hit: bool


def create_workspace_from_template(
    destination: Path,
    *,
    template_dir: Path | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> WorkspaceCreationResult:
    resolved_destination = destination.expanduser().resolve()
    resolved_template = (template_dir or default_template_dir()).expanduser().resolve()

    if not resolved_template.exists() or not resolved_template.is_dir():
        raise ValueError(f"template directory does not exist: {resolved_template}")
    _validate_template_is_pinned(resolved_template)

    if resolved_destination.exists() and not resolved_destination.is_dir():
        raise ValueError(f"workspace destination is not a directory: {resolved_destination}")
    if resolved_destination.exists() and any(resolved_destination.iterdir()):
        raise ValueError(f"workspace destination must be empty: {resolved_destination}")

    resolved_destination.mkdir(parents=True, exist_ok=True)

    # Selectively copy project files and symlink read-only Mathlib packages
    # to avoid copying ~6.8GB per run.
    #
    # Strategy:
    # - Copy everything except .lake/ (small project files: Orthos/, lakefile, etc.)
    # - Inside .lake/, symlink .lake/packages/ (read-only Mathlib sources, ~2GB)
    # - Copy .lake/build/ (Lean writes new oleans for Orthos modules here)
    # - Copy other .lake/ contents (manifest, toolchain, etc.)
    lake_src = resolved_template / ".lake"
    packages_src = lake_src / "packages" if lake_src.exists() else None
    if packages_src is not None and packages_src.exists() and packages_src.is_dir():
        # Copy everything except .lake/
        for item in resolved_template.iterdir():
            if item.name == ".lake":
                continue
            dst = resolved_destination / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
        # Create .lake/ structure with symlinked packages/
        lake_dst = resolved_destination / ".lake"
        lake_dst.mkdir(parents=True, exist_ok=True)
        for item in lake_src.iterdir():
            dst = lake_dst / item.name
            if item.name == "packages":
                # Symlink packages/ (read-only Mathlib, biggest directory)
                dst.symlink_to(item.resolve())
            elif item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
    else:
        # Fallback: full copy if no .lake/packages/ directory
        shutil.copytree(resolved_template, resolved_destination, dirs_exist_ok=True)

    # Link cached .lake/ into workspace (avoids 66-minute Mathlib rebuild)
    cache_hit = False
    if runtime_config is not None and runtime_config.workspace_cache.enabled:
        cache_lake_dir = resolve_cache_lake_dir(
            runtime_config.workspace_cache.cache_dir.expanduser().resolve(),
            resolved_template,
        )
        if cache_lake_dir.exists():
            cache_hit = link_lake_cache(resolved_destination, cache_lake_dir)
            print(f"[lean_engine] workspace cache {'HIT' if cache_hit else 'MISS'}: {cache_lake_dir}", file=sys.stderr, flush=True)
        else:
            print(f"[lean_engine] workspace cache MISS (not seeded): {cache_lake_dir}", file=sys.stderr, flush=True)

    if runtime_config is not None:
        _rewrite_statements_imports(resolved_destination, imports=runtime_config.lean.imports)

    files = workspace_files(resolved_destination)
    _ensure_expected_files(files)
    return WorkspaceCreationResult(files=files, cache_hit=cache_hit)


def write_project_mcp_config(
    workspace_root: Path,
    runtime_config: RuntimeConfig,
    *,
    mcp_log_dir: Path | None = None,
) -> Path:
    if not runtime_config.mcp.enabled:
        raise ValueError("runtime config has MCP disabled")

    workspace_root = workspace_root.expanduser().resolve()
    if mcp_log_dir is None:
        mcp_log_dir = workspace_root / ".mcp_logs"
    mcp_log_dir = mcp_log_dir.expanduser().resolve()
    mcp_log_dir.mkdir(parents=True, exist_ok=True)

    config_path = workspace_root / runtime_config.mcp.config_filename
    payload = {
        "mcpServers": {
            runtime_config.mcp.server_name: {
                "command": runtime_config.mcp.command,
                "args": list(runtime_config.mcp.args),
                "env": {
                    "MCP_LOG_DIR": str(mcp_log_dir),
                    "MCP_LOG_NAME": runtime_config.mcp.log_name,
                },
            }
        }
    }
    return write_json(config_path, payload)


def workspace_files(workspace_root: Path) -> WorkspaceFiles:
    root = workspace_root.expanduser().resolve()
    return WorkspaceFiles(
        workspace_root=root,
        statements_file=root / "Orthos" / "Statements.lean",
        lemmas_file=root / "Orthos" / "Lemmas.lean",
        root_file=root / "Orthos" / "Root.lean",
    )


def snapshot_workspace(workspace_root: Path) -> dict[str, Any]:
    root = workspace_root.expanduser().resolve()
    files: list[dict[str, Any]] = []

    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".lake/"):
            continue
        files.append(
            {
                "path": rel,
                "size_bytes": path.stat().st_size,
                "sha256": workspace_file_digest(path),
            }
        )

    return {
        "workspace_root": str(root),
        "file_count": len(files),
        "files": files,
    }


def clone_workspace(
    source_root: Path,
    destination_root: Path,
    *,
    mode: str = "copy",
) -> Path:
    source = source_root.expanduser().resolve()
    destination = destination_root.expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise ValueError(f"workspace source does not exist: {source}")
    if destination.exists():
        raise ValueError(f"workspace clone destination must not already exist: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    cp_command = ["cp", "-a"]
    normalized_mode = mode.strip().lower()
    if normalized_mode == "hardlink":
        cp_command = ["cp", "-al"]
    elif normalized_mode == "reflink":
        cp_command = ["cp", "-a", "--reflink=always"]
    elif normalized_mode != "copy":
        raise ValueError("workspace clone mode must be one of: copy, hardlink, reflink")

    command = [*cp_command, str(source), str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise ValueError(
            f"failed to clone workspace `{source}` -> `{destination}` using mode `{normalized_mode}`: {exc}"
        ) from exc
    return destination


def _rewrite_statements_imports(workspace_root: Path, *, imports: tuple[str, ...]) -> None:
    target = workspace_root / "Orthos" / "Statements.lean"
    if not target.exists():
        return

    current = target.read_text(encoding="utf-8")
    lines = current.splitlines()

    body_start = 0
    while body_start < len(lines) and lines[body_start].startswith("import "):
        body_start += 1

    import_lines = [f"import {name}" for name in imports]
    rewritten = "\n".join(import_lines + lines[body_start:]).strip() + "\n"
    target.write_text(rewritten, encoding="utf-8")


def _ensure_expected_files(files: WorkspaceFiles) -> None:
    for required in (files.statements_file, files.lemmas_file, files.root_file):
        if not required.exists():
            raise ValueError(f"workspace is missing required file: {required}")


def _validate_template_is_pinned(template_root: Path) -> None:
    toolchain_path = template_root / "lean-toolchain"
    if not toolchain_path.exists() or not toolchain_path.is_file():
        raise ValueError(f"template missing lean-toolchain file: {toolchain_path}")
    toolchain = toolchain_path.read_text(encoding="utf-8").strip()
    if not toolchain:
        raise ValueError(f"template lean-toolchain is empty: {toolchain_path}")

    lowered_toolchain = toolchain.lower()
    if (
        toolchain == "stable"
        or ":stable" in lowered_toolchain
        or "nightly" in lowered_toolchain
    ):
        raise ValueError(
            "template lean-toolchain must pin an explicit Lean version (stable/nightly are not deterministic)"
        )

    lakefile_path = template_root / "lakefile.toml"
    if not lakefile_path.exists() or not lakefile_path.is_file():
        raise ValueError(f"template missing lakefile.toml: {lakefile_path}")

    try:
        parsed = tomllib.loads(lakefile_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"template lakefile.toml is invalid TOML: {lakefile_path}") from exc

    requires = parsed.get("require")
    if not isinstance(requires, list):
        raise ValueError("template lakefile.toml must define [[require]] entries")

    mathlib_entries = [entry for entry in requires if isinstance(entry, dict) and entry.get("name") == "mathlib"]
    if not mathlib_entries:
        raise ValueError("template lakefile.toml must include a pinned mathlib requirement")

    for entry in mathlib_entries:
        revision = entry.get("rev")
        if isinstance(revision, str) and revision.strip():
            return
    raise ValueError("template mathlib requirement must include a non-empty rev pin")
