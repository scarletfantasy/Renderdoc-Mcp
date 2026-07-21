from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - the project is Windows-first
    msvcrt = None  # type: ignore[assignment]

from renderdoc_mcp.paths import extension_install_dir, ui_config_path, user_qrenderdoc_dir

EXTENSION_PACKAGE = "renderdoc_mcp.qrenderdoc_extension"
EXTENSION_NAME = "renderdoc_mcp_bridge"
SHARED_ANALYSIS_PACKAGE = "renderdoc_mcp.analysis"
SHARED_ANALYSIS_TARGET_DIR = "analysis"
INSTALL_METADATA_FILENAME = ".renderdoc_mcp_install.json"
TRUE_LIKE_VALUES = {"1", "true", "yes", "on"}
FALSE_LIKE_VALUES = {"0", "false", "no", "off"}
GENERATED_DIRECTORY_NAMES = {"__pycache__"}
GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
INSTALL_LOCK_TIMEOUT_SECONDS = 30.0
DIRECTORY_RENAME_ATTEMPTS = 10
DIRECTORY_RENAME_RETRY_SECONDS = 0.05
_FALLBACK_INSTALL_LOCK = threading.Lock()


def _is_generated_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        any(part in GENERATED_DIRECTORY_NAMES for part in relative.parts)
        or path.suffix.lower() in GENERATED_FILE_SUFFIXES
    )


def _iter_installable_files(root: Path):
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if not _is_generated_path(path, root):
            yield path


def _ignore_generated_files(directory: str, names: list[str]) -> list[str]:
    ignored = []
    for name in names:
        path = Path(directory) / name
        if name in GENERATED_DIRECTORY_NAMES or path.suffix.lower() in GENERATED_FILE_SUFFIXES:
            ignored.append(name)
    return ignored


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_ignore_generated_files)


@contextmanager
def _installation_lock(target_dir: Path, timeout_seconds: float = INSTALL_LOCK_TIMEOUT_SECONDS):
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir.parent / (".{}-install.lock".format(target_dir.name))

    if msvcrt is None:  # pragma: no cover - exercised only on non-Windows hosts
        acquired = _FALLBACK_INSTALL_LOCK.acquire(timeout=timeout_seconds)
        if not acquired:
            raise TimeoutError("Timed out waiting for the RenderDoc extension installation lock.")
        try:
            yield
        finally:
            _FALLBACK_INSTALL_LOCK.release()
        return

    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()

    deadline = time.monotonic() + timeout_seconds
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for the RenderDoc extension installation lock."
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _env_optional_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None

    normalized = raw.strip().lower()
    if normalized in TRUE_LIKE_VALUES:
        return True
    if normalized in FALSE_LIKE_VALUES:
        return False
    return None


def _resolve_always_load(always_load: bool | None) -> bool:
    if always_load is not None:
        return always_load

    env_value = _env_optional_bool("RENDERDOC_INSTALL_ALWAYS_LOAD")
    if env_value is not None:
        return env_value

    return True


def _build_install_metadata() -> dict[str, object]:
    digest = hashlib.sha256()
    installed_files: list[str] = []
    file_hashes: dict[str, str] = {}
    source_root = resources.files(EXTENSION_PACKAGE).joinpath(EXTENSION_NAME)

    with resources.as_file(source_root) as source_dir:
        for source_path in _iter_installable_files(source_dir):
            relative_path = source_path.relative_to(source_dir).as_posix()
            if relative_path.startswith(SHARED_ANALYSIS_TARGET_DIR + "/"):
                continue
            content = source_path.read_bytes()
            installed_files.append(relative_path)
            file_hashes[relative_path] = hashlib.sha256(content).hexdigest()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")

    analysis_root = resources.files(SHARED_ANALYSIS_PACKAGE)
    with resources.as_file(analysis_root) as source_dir:
        for source_path in _iter_installable_files(source_dir):
            relative_path = source_path.relative_to(source_dir).as_posix()
            install_path = "{}/{}".format(SHARED_ANALYSIS_TARGET_DIR, relative_path)
            content = source_path.read_bytes()
            installed_files.append(install_path)
            file_hashes[install_path] = hashlib.sha256(content).hexdigest()
            digest.update(install_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")

    return {
        "version": 2,
        "extension_name": EXTENSION_NAME,
        "source_hash": digest.hexdigest(),
        "files": sorted(installed_files),
        "file_hashes": dict(sorted(file_hashes.items())),
    }


def _read_install_metadata(target_dir: Path) -> dict[str, object] | None:
    metadata_path = target_dir / INSTALL_METADATA_FILENAME
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _install_is_current(target_dir: Path, metadata: dict[str, object]) -> bool:
    if not target_dir.is_dir():
        return False

    installed_files = metadata.get("files")
    if not isinstance(installed_files, list) or not installed_files:
        return False
    file_hashes = metadata.get("file_hashes")
    if not isinstance(file_hashes, dict):
        return False

    expected_files: set[str] = set()
    for relative_path in installed_files:
        if not isinstance(relative_path, str):
            return False
        expected_files.add(relative_path)
        installed_path = target_dir / relative_path
        expected_hash = file_hashes.get(relative_path)
        if not installed_path.is_file() or not isinstance(expected_hash, str):
            return False
        try:
            installed_hash = hashlib.sha256(installed_path.read_bytes()).hexdigest()
        except OSError:
            return False
        if installed_hash != expected_hash:
            return False

    actual_files = {
        path.relative_to(target_dir).as_posix()
        for path in _iter_installable_files(target_dir)
        if path != target_dir / INSTALL_METADATA_FILENAME
    }
    if actual_files != expected_files:
        return False

    return _read_install_metadata(target_dir) == metadata


def _write_install_metadata(target_dir: Path, metadata: dict[str, object]) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target_dir / INSTALL_METADATA_FILENAME, json.dumps(metadata, indent=2))


def _copy_extension_files(target_dir: Path) -> None:
    source_root = resources.files(EXTENSION_PACKAGE).joinpath(EXTENSION_NAME)
    with resources.as_file(source_root) as source_dir:
        _copy_tree(source_dir, target_dir)
    _sync_shared_analysis_package(target_dir)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _rename_directory(source: Path, target: Path) -> None:
    for attempt in range(DIRECTORY_RENAME_ATTEMPTS):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt + 1 >= DIRECTORY_RENAME_ATTEMPTS:
                raise
            time.sleep(DIRECTORY_RENAME_RETRY_SECONDS)


def _install_snapshot_atomically(target_dir: Path, metadata: dict[str, object]) -> None:
    suffix = uuid.uuid4().hex
    staging_dir = target_dir.parent / (".{}-staging-{}".format(target_dir.name, suffix))
    backup_dir = target_dir.parent / (".{}-backup-{}".format(target_dir.name, suffix))
    moved_existing = False
    installation_complete = False

    try:
        _copy_extension_files(staging_dir)
        _write_install_metadata(staging_dir, metadata)

        if target_dir.exists():
            _rename_directory(target_dir, backup_dir)
            moved_existing = True
        _rename_directory(staging_dir, target_dir)
        installation_complete = True
    except Exception:
        if moved_existing and backup_dir.exists() and not target_dir.exists():
            try:
                _rename_directory(backup_dir, target_dir)
            except OSError:
                warnings.warn(
                    "The previous RenderDoc extension snapshot could not be restored and was preserved at: {}".format(
                        backup_dir
                    ),
                    RuntimeWarning,
                    stacklevel=2,
                )
        raise
    finally:
        if staging_dir.exists():
            try:
                _remove_path(staging_dir)
            except OSError:
                warnings.warn(
                    "The incomplete RenderDoc extension staging directory could not be removed: {}".format(
                        staging_dir
                    ),
                    RuntimeWarning,
                    stacklevel=2,
                )
        if installation_complete and backup_dir.exists():
            try:
                _remove_path(backup_dir)
            except OSError:
                warnings.warn(
                    "The previous RenderDoc extension snapshot could not be removed: {}".format(backup_dir),
                    RuntimeWarning,
                    stacklevel=2,
                )


def install_extension(always_load: bool | None = None) -> Path:
    user_qrenderdoc_dir().mkdir(parents=True, exist_ok=True)

    target_dir = extension_install_dir()
    with _installation_lock(target_dir):
        metadata = _build_install_metadata()
        if not _install_is_current(target_dir, metadata):
            _install_snapshot_atomically(target_dir, metadata)

        if _resolve_always_load(always_load):
            _ensure_always_load()
    return target_dir


def _sync_shared_analysis_package(target_dir: Path) -> None:
    source_root = resources.files(SHARED_ANALYSIS_PACKAGE)
    destination = target_dir / SHARED_ANALYSIS_TARGET_DIR
    with resources.as_file(source_root) as source_dir:
        _copy_tree(source_dir, destination)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(".{}-{}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        temporary_path.write_text(text, encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                warnings.warn(
                    "The temporary file used for an atomic write could not be removed: {}".format(temporary_path),
                    RuntimeWarning,
                    stacklevel=2,
                )


def _ensure_always_load(config_path: Path | None = None) -> bool:
    config_path = config_path or ui_config_path()
    config: dict[str, object]

    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            warnings.warn(
                "Could not update invalid qrenderdoc UI config: {}. Use --no-always-load to skip this step.".format(
                    config_path
                ),
                RuntimeWarning,
                stacklevel=2,
            )
            return False
        if not isinstance(payload, dict):
            warnings.warn(
                "Could not update qrenderdoc UI config because its root is not an object: {}".format(config_path),
                RuntimeWarning,
                stacklevel=2,
            )
            return False
        config = payload
    else:
        config = {}

    existing_value = config.get("AlwaysLoad_Extensions", [])
    always_load = list(existing_value) if isinstance(existing_value, list) else []
    if EXTENSION_NAME not in always_load:
        always_load.append(EXTENSION_NAME)
    else:
        return False

    config["AlwaysLoad_Extensions"] = always_load

    _atomic_write_text(config_path, json.dumps(config, indent=2))
    return True
