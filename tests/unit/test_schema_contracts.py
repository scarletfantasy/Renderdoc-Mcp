from __future__ import annotations

from renderdoc_mcp.application import RenderDocApplication
from renderdoc_mcp.server import create_mcp_app


def _tool_schema(name: str) -> dict:
    app = create_mcp_app(RenderDocApplication())
    tool = next(item for item in app._tool_manager.list_tools() if item.name == name)
    return tool.parameters


def test_common_enum_guesses_are_declared_in_tool_schemas() -> None:
    bindings = _tool_schema("renderdoc_list_pipeline_bindings")["properties"]["binding_kind"]["enum"]
    resources = _tool_schema("renderdoc_list_resources")["properties"]["kind"]["enum"]
    worklist = _tool_schema("renderdoc_get_analysis_worklist")["properties"]["focus"]["anyOf"][0]["enum"]

    assert {"resources", "read_only_resources", "constant_buffers", "buffers"}.issubset(bindings)
    assert {"texture", "textures", "buffer", "buffers"}.issubset(resources)
    assert "correctness" in worklist


def test_numeric_bounds_are_visible_before_tool_execution() -> None:
    probe = _tool_schema("renderdoc_probe_texture_regions")["properties"]
    debug = _tool_schema("renderdoc_analyze_shader_debug")["properties"]
    dossiers = _tool_schema("renderdoc_get_event_dossiers")["properties"]

    assert probe["width"]["anyOf"][0] == {"maximum": 128, "minimum": 1, "type": "integer"}
    assert probe["threshold"]["anyOf"][0] == {"maximum": 1.0, "minimum": 0.0, "type": "number"}
    assert debug["max_steps"]["anyOf"][0]["maximum"] == 8192
    assert dossiers["event_ids"]["maxItems"] == 32
