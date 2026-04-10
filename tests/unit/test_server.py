from __future__ import annotations

import importlib
import sys


def test_importing_server_has_no_runtime_side_effects(monkeypatch) -> None:
    called = []
    sys.modules.pop("renderdoc_mcp.server", None)
    monkeypatch.setattr("renderdoc_mcp.bootstrap.prepare_runtime", lambda: called.append("prepare_runtime"))

    server = importlib.import_module("renderdoc_mcp.server")

    assert called == []
    assert callable(server.create_mcp_app)


def test_server_instructions_and_pipeline_tool_description_expose_ui_navigation_hint() -> None:
    server = importlib.import_module("renderdoc_mcp.server")

    app = server.create_mcp_app()

    assert "renderdoc_get_pipeline_overview also selects the supplied event_id" in app.instructions
    pipeline_tool = next(tool for tool in app._tool_manager.list_tools() if tool.name == "renderdoc_get_pipeline_overview")
    assert "selects the supplied event_id (EID) in the UI" in pipeline_tool.description
