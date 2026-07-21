from __future__ import annotations

import json
from pathlib import Path

from renderdoc_mcp import __version__
from renderdoc_mcp.application.registry import RESOURCE_SPECS, TOOL_SPECS

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_MANIFEST = (
    REPO_ROOT
    / "src"
    / "renderdoc_mcp"
    / "qrenderdoc_extension"
    / "renderdoc_mcp_bridge"
    / "extension.json"
)


def test_package_and_extension_versions_match() -> None:
    extension = json.loads(EXTENSION_MANIFEST.read_text(encoding="utf-8"))

    assert extension["version"] == __version__


def test_readme_lists_every_registered_tool_and_resource() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for spec in (*TOOL_SPECS, *RESOURCE_SPECS):
        assert f"`{spec.name}`" in readme or f"`{spec.uri}`" in readme
