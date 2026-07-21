from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from renderdoc_mcp import install as install_module
from renderdoc_mcp import install_cli


def test_ensure_always_load_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "UI.config"
    original_text = '{\n  "OtherSetting": true,\n  "AlwaysLoad_Extensions": ["renderdoc_mcp_bridge"]\n}'
    config_path.write_text(original_text, encoding="utf-8")

    changed = install_module._ensure_always_load(config_path)

    assert changed is False
    assert config_path.read_text(encoding="utf-8") == original_text


def test_ensure_always_load_preserves_existing_key_order(tmp_path: Path) -> None:
    config_path = tmp_path / "UI.config"
    config_path.write_text(
        '{\n  "First": 1,\n  "AlwaysLoad_Extensions": ["existing_extension"],\n  "Last": 2\n}',
        encoding="utf-8",
    )

    changed = install_module._ensure_always_load(config_path)
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert changed is True
    assert list(payload.keys()) == ["First", "AlwaysLoad_Extensions", "Last"]
    assert payload["AlwaysLoad_Extensions"] == ["existing_extension", "renderdoc_mcp_bridge"]
    assert payload["First"] == 1
    assert payload["Last"] == 2


def test_ensure_always_load_warns_for_invalid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "UI.config"
    config_path.write_text("{invalid", encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="invalid qrenderdoc UI config"):
        changed = install_module._ensure_always_load(config_path)

    assert changed is False
    assert config_path.read_text(encoding="utf-8") == "{invalid"


def test_ensure_always_load_warns_for_non_object_config(tmp_path: Path) -> None:
    config_path = tmp_path / "UI.config"
    config_path.write_text("[]", encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="root is not an object"):
        changed = install_module._ensure_always_load(config_path)

    assert changed is False
    assert config_path.read_text(encoding="utf-8") == "[]"


def test_install_extension_skips_ui_config_write_when_always_load_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    installed_to: list[Path] = []
    ensured_calls: list[Path] = []
    user_dir = tmp_path / "qrenderdoc"
    extension_dir = user_dir / "extensions" / "renderdoc_mcp_bridge"

    monkeypatch.setattr(install_module, "user_qrenderdoc_dir", lambda: user_dir)
    monkeypatch.setattr(install_module, "extension_install_dir", lambda: extension_dir)
    monkeypatch.setattr(
        install_module,
        "_install_snapshot_atomically",
        lambda target_dir, metadata: installed_to.append(target_dir),
    )
    monkeypatch.setattr(install_module, "_ensure_always_load", lambda config_path=None: ensured_calls.append(config_path))

    target = install_module.install_extension(always_load=False)

    assert target == extension_dir
    assert user_dir.is_dir()
    assert installed_to == [extension_dir]
    assert ensured_calls == []


def test_install_extension_respects_env_opt_out(tmp_path: Path, monkeypatch) -> None:
    installed_to: list[Path] = []
    ensured_calls: list[Path] = []
    user_dir = tmp_path / "qrenderdoc"
    extension_dir = user_dir / "extensions" / "renderdoc_mcp_bridge"

    monkeypatch.setenv("RENDERDOC_INSTALL_ALWAYS_LOAD", "0")
    monkeypatch.setattr(install_module, "user_qrenderdoc_dir", lambda: user_dir)
    monkeypatch.setattr(install_module, "extension_install_dir", lambda: extension_dir)
    monkeypatch.setattr(
        install_module,
        "_install_snapshot_atomically",
        lambda target_dir, metadata: installed_to.append(target_dir),
    )
    monkeypatch.setattr(install_module, "_ensure_always_load", lambda config_path=None: ensured_calls.append(config_path))

    target = install_module.install_extension()

    assert target == extension_dir
    assert installed_to == [extension_dir]
    assert ensured_calls == []


def test_install_extension_skips_copy_when_snapshot_matches(tmp_path: Path, monkeypatch) -> None:
    installed_to: list[Path] = []
    user_dir = tmp_path / "qrenderdoc"
    extension_dir = user_dir / "extensions" / "renderdoc_mcp_bridge"
    metadata = {
        "version": 2,
        "extension_name": "renderdoc_mcp_bridge",
        "source_hash": "abc123",
        "files": ["__init__.py", "extension.json", "timing.py"],
        "file_hashes": {
            "__init__.py": hashlib.sha256(b"").hexdigest(),
            "extension.json": hashlib.sha256(b"").hexdigest(),
            "timing.py": hashlib.sha256(b"").hexdigest(),
        },
    }

    extension_dir.mkdir(parents=True, exist_ok=True)
    for relative_path in metadata["files"]:
        target_path = extension_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("", encoding="utf-8")
    (extension_dir / install_module.INSTALL_METADATA_FILENAME).write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    monkeypatch.setattr(install_module, "user_qrenderdoc_dir", lambda: user_dir)
    monkeypatch.setattr(install_module, "extension_install_dir", lambda: extension_dir)
    monkeypatch.setattr(install_module, "_build_install_metadata", lambda: metadata)
    monkeypatch.setattr(
        install_module,
        "_install_snapshot_atomically",
        lambda target_dir, metadata: installed_to.append(target_dir),
    )

    target = install_module.install_extension(always_load=False)

    assert target == extension_dir
    assert installed_to == []


def test_install_cli_passes_explicit_opt_out(monkeypatch, capsys) -> None:
    captured: list[bool | None] = []
    target_path = Path(r"C:\temp\renderdoc_mcp_bridge")

    monkeypatch.setattr(
        install_cli,
        "install_extension",
        lambda always_load=None: captured.append(always_load) or target_path,
    )

    exit_code = install_cli.main(["--no-always-load"])

    assert exit_code == 0
    assert captured == [False]
    assert capsys.readouterr().out.strip() == str(target_path)


def test_install_metadata_includes_analysis_package_tree() -> None:
    metadata = install_module._build_install_metadata()

    assert "analysis/__init__.py" in metadata["files"]
    assert "analysis/frame_analysis.py" in metadata["files"]
    assert metadata["version"] == 2
    assert set(metadata["file_hashes"]) == set(metadata["files"])
    assert not any("__pycache__" in path or path.endswith((".pyc", ".pyo")) for path in metadata["files"])


def test_install_snapshot_detects_modified_files(tmp_path: Path) -> None:
    target_dir = tmp_path / "renderdoc_mcp_bridge"
    target_dir.mkdir()
    source = b"expected"
    metadata = {
        "version": 2,
        "extension_name": "renderdoc_mcp_bridge",
        "source_hash": hashlib.sha256(source).hexdigest(),
        "files": ["client.py"],
        "file_hashes": {"client.py": hashlib.sha256(source).hexdigest()},
    }
    (target_dir / "client.py").write_bytes(source)
    (target_dir / install_module.INSTALL_METADATA_FILENAME).write_text(json.dumps(metadata), encoding="utf-8")

    assert install_module._install_is_current(target_dir, metadata) is True

    (target_dir / "client.py").write_bytes(b"corrupted")

    assert install_module._install_is_current(target_dir, metadata) is False


def test_copy_tree_excludes_generated_python_cache(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    cache_dir = source_dir / "__pycache__"
    cache_dir.mkdir(parents=True)
    (source_dir / "client.py").write_text("value = 1", encoding="utf-8")
    (cache_dir / "client.cpython-314.pyc").write_bytes(b"cache")

    install_module._copy_tree(source_dir, target_dir)

    assert (target_dir / "client.py").is_file()
    assert not (target_dir / "__pycache__").exists()


def test_install_snapshot_atomically_replaces_stale_tree(tmp_path: Path) -> None:
    target_dir = tmp_path / "renderdoc_mcp_bridge"
    target_dir.mkdir()
    (target_dir / "stale.py").write_text("stale = True", encoding="utf-8")
    metadata = install_module._build_install_metadata()

    install_module._install_snapshot_atomically(target_dir, metadata)

    assert install_module._install_is_current(target_dir, metadata) is True
    assert not (target_dir / "stale.py").exists()
    assert not any(path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"} for path in target_dir.rglob("*"))


def test_install_snapshot_preserves_backup_when_install_and_rollback_fail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_dir = tmp_path / "renderdoc_mcp_bridge"
    target_dir.mkdir()
    (target_dir / "stale.py").write_text("stale = True", encoding="utf-8")
    original_rename = Path.rename

    def fail_staging_and_rollback(path: Path, target: Path) -> Path:
        if "-staging-" in path.name or "-backup-" in path.name:
            raise PermissionError("simulated directory rename failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_staging_and_rollback)
    monkeypatch.setattr(install_module.time, "sleep", lambda _seconds: None)

    with pytest.warns(RuntimeWarning, match="could not be restored"), pytest.raises(PermissionError):
        install_module._install_snapshot_atomically(target_dir, install_module._build_install_metadata())

    backups = list(tmp_path.glob(".renderdoc_mcp_bridge-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "stale.py").is_file()


def test_install_snapshot_restores_previous_tree_when_staging_swap_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_dir = tmp_path / "renderdoc_mcp_bridge"
    target_dir.mkdir()
    (target_dir / "stale.py").write_text("stale = True", encoding="utf-8")
    original_rename = Path.rename

    def fail_staging_swap(path: Path, target: Path) -> Path:
        if "-staging-" in path.name:
            raise PermissionError("simulated staging rename failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_staging_swap)
    monkeypatch.setattr(install_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        install_module._install_snapshot_atomically(target_dir, install_module._build_install_metadata())

    assert (target_dir / "stale.py").is_file()
    assert not list(tmp_path.glob(".renderdoc_mcp_bridge-backup-*"))


def test_bundled_extension_manifest_supports_renderdoc_136() -> None:
    manifest = resources.files("renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge").joinpath("extension.json")
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert payload["minimum_renderdoc"] == "1.36"


def test_repo_time_extension_analysis_shim_is_importable() -> None:
    from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge.analysis import frame_analysis

    assert hasattr(frame_analysis, "AnalysisCache")


def test_copied_analysis_package_is_self_contained(tmp_path: Path) -> None:
    standalone_root = tmp_path / "standalone"
    analysis_target = standalone_root / "analysis"

    with resources.as_file(resources.files("renderdoc_mcp.analysis")) as source_dir:
        shutil.copytree(source_dir, analysis_target)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(standalone_root)
    completed = subprocess.run(
        [sys.executable, "-c", "import analysis; print(hasattr(analysis, 'AnalysisCache'))"],
        cwd=standalone_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "True"
