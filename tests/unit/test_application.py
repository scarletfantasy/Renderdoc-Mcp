from __future__ import annotations

from pathlib import Path

import pytest

from renderdoc_mcp.application import RenderDocApplication
from renderdoc_mcp.application.registry import build_resource_registry, build_tool_registry
from renderdoc_mcp.errors import InvalidCaptureIDError, RenderDocMCPError, ReplayFailureError
from renderdoc_mcp.session_pool import CaptureSessionPool


class DummyBridge:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.closed = 0
        self.backend_name = "qrenderdoc"
        self.renderdoc_version = "1.43"

    def ensure_capture_loaded(self, capture_path: str):
        self.loaded.append(capture_path)
        return {"loaded": True, "filename": capture_path}

    def call(self, method: str, params=None):
        payload = params or {}
        self.calls.append((method, payload))

        if method == "get_capture_overview":
            return {
                "capture": {"loaded": True, "filename": "sample.rdc"},
                "api": "D3D12",
                "frame": {"frame_number": 1},
                "statistics": {"total_actions": 2},
                "resource_counts": {"textures": 1, "buffers": 1},
                "root_pass_count": 1,
                "action_root_count": 1,
                "capabilities": {
                    "timing_data": True,
                    "pixel_history": True,
                    "shader_disassembly": True,
                    "shader_debugging": True,
                },
                "meta": {},
            }
        if method == "get_analysis_worklist":
            return {
                "focus": payload.get("focus", "performance"),
                "count": 1,
                "items": [
                    {
                        "kind": "pass",
                        "id": "pass:1-10",
                        "label": "BasePass",
                        "reason": "Hot path",
                        "recommended_call": {"tool": "renderdoc_get_pass_summary", "arguments": {"pass_id": "pass:1-10"}},
                    }
                ],
                "meta": {},
            }
        if method == "list_actions":
            return {
                "parent_event_id": str(payload.get("parent_event_id", "")),
                "name_filter": payload.get("name_filter", ""),
                "flags_filter": payload.get("flags_filter", ""),
                "actions": [],
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 50)),
                        "returned_count": 0,
                        "total_count": 0,
                        "matched_count": 0,
                        "has_more": False,
                    }
                },
            }
        if method == "list_passes":
            return {
                "parent_pass_id": payload.get("parent_pass_id", ""),
                "passes": [],
                "sort_by": payload.get("sort_by", "event_order"),
                "effective_sort_by": payload.get("sort_by", "event_order"),
                "category_filter": payload.get("category_filter", ""),
                "name_filter": payload.get("name_filter", ""),
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 50)),
                        "returned_count": 0,
                        "total_count": 0,
                        "matched_count": 0,
                        "has_more": False,
                    }
                },
            }
        if method == "get_pass_summary":
            return {"pass_id": payload["pass_id"], "parent_pass_id": "", "child_pass_count": 0, "meta": {}}
        if method == "list_timing_events":
            return {
                "pass": {"pass_id": payload["pass_id"]},
                "basis": "gpu_timing",
                "sort_by": payload.get("sort_by", "event_order"),
                "effective_sort_by": payload.get("sort_by", "event_order"),
                "total_gpu_time_ms": 0.0,
                "timed_event_count": 0,
                "events": [],
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 100)),
                        "returned_count": 0,
                        "total_count": 0,
                        "matched_count": 0,
                        "has_more": False,
                    },
                    "timing": {"timing_available": True, "counter_name": "EventGPUDuration"},
                },
            }
        if method == "get_action_summary":
            return {
                "action": {
                    "event_id": payload["event_id"],
                    "name": "Draw",
                    "flags": ["draw"],
                    "depth": 2,
                    "child_count": 0,
                    "parent_event_id": 1,
                    "resource_usage_summary": {"output_count": 1, "has_depth_output": True},
                },
                "meta": {},
            }
        if method == "get_pipeline_overview":
            return {
                "event_id": payload["event_id"],
                "api": "D3D12",
                "action": {"event_id": payload["event_id"], "name": "Draw", "flags": ["draw"]},
                "pipeline": {
                    "available": True,
                    "topology": "TriangleList",
                    "graphics_pipeline_object": "pipe",
                    "compute_pipeline_object": "",
                    "counts": {
                        "descriptor_accesses": 2,
                        "vertex_buffers": 1,
                        "vertex_inputs": 1,
                        "output_targets": 1,
                        "shaders": 2,
                    },
                    "shaders": [],
                    "api_details_available": True,
                    "api_details_api": "D3D12",
                },
                "meta": {},
            }
        if method == "list_pipeline_bindings":
            return {
                "event_id": payload["event_id"],
                "binding_kind": payload["binding_kind"],
                "items": [],
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 50)),
                        "returned_count": 0,
                        "total_count": 0,
                        "matched_count": 0,
                        "has_more": False,
                    }
                },
            }
        if method == "get_shader_summary":
            return {
                "event_id": payload["event_id"],
                "shader": {"stage": payload["stage"], "counts": {}},
                "disassembly": {"available": True, "available_targets": ["dxil"], "default_target": "dxil"},
                "meta": {},
            }
        if method == "get_shader_code_chunk":
            return {
                "event_id": payload["event_id"],
                "shader": {"stage": payload["stage"]},
                "target": payload.get("target", ""),
                "start_line": int(payload.get("start_line", 1)),
                "returned_line_count": 1,
                "total_lines": 1,
                "has_more": False,
                "available": True,
                "reason": "",
                "text": "shader",
                "meta": {},
            }
        if method == "list_resources":
            return {
                "kind": payload["kind"],
                "sort_by": payload.get("sort_by", "name"),
                "name_filter": payload.get("name_filter", ""),
                "items": [],
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 50)),
                        "returned_count": 0,
                        "total_count": 0,
                        "matched_count": 0,
                        "has_more": False,
                    }
                },
            }
        if method == "get_resource_summary":
            if payload["resource_id"] == "BufferId::1":
                return {
                    "resource": {"resource_id": payload["resource_id"], "kind": "buffer"},
                    "usage_overview": {
                        "available": False,
                        "reason": "Resource usage listing currently supports texture RT and copy usage only.",
                    },
                    "recommended_calls": [{"tool": "renderdoc_get_buffer_data", "arguments": {"buffer_id": payload["resource_id"], "offset": 0}}],
                    "meta": {},
                }
            return {
                "resource": {"resource_id": payload["resource_id"], "kind": "texture"},
                "usage_overview": {
                    "available": True,
                    "supported_scope": "rt_texture_v1",
                    "total_matching_events": 1,
                    "counts_by_kind": {
                        "color_output": 1,
                        "depth_output": 0,
                        "copy_source": 0,
                        "copy_destination": 0,
                        "resolve_source": 0,
                        "resolve_destination": 0,
                    },
                    "first_event_id": 42,
                    "last_event_id": 42,
                    "representative_events": [{"event_id": 42, "name": "Draw", "flags": ["draw"]}],
                },
                "recommended_calls": [{"tool": "renderdoc_list_resource_usages", "arguments": {"resource_id": payload["resource_id"]}}],
                "meta": {},
            }
        if method == "list_resource_usages":
            if payload["resource_id"] == "BufferId::1":
                raise RenderDocMCPError(
                    "resource_usage_unsupported",
                    "Resource usage listing currently supports texture RT and copy usage only.",
                    {"resource_id": payload["resource_id"], "resource_kind": "buffer"},
                )
            return {
                "resource_id": payload["resource_id"],
                "usage_kind": payload.get("usage_kind", "all"),
                "events": [
                    {
                        "event_id": 42,
                        "name": "Draw",
                        "flags": ["draw"],
                        "parent_event_id": 1,
                        "matched_usage_kinds": ["color_output"],
                        "bindings": [{"usage_kind": "color_output", "slot_kind": "color", "slot_index": 0}],
                    }
                ],
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 50)),
                        "returned_count": 1,
                        "total_count": 1,
                        "matched_count": 1,
                        "has_more": False,
                    }
                },
            }
        if method == "get_pixel_history":
            return {
                "texture": {"resource_id": payload["texture_id"]},
                "query": {"x": payload["x"], "y": payload["y"]},
                "modifications": [],
                "total_modification_count": 0,
                "meta": {
                    "page": {
                        "cursor": str(payload.get("cursor", 0)),
                        "next_cursor": "",
                        "limit": int(payload.get("limit", 100)),
                        "returned_count": 0,
                        "total_count": 0,
                        "matched_count": 0,
                        "has_more": False,
                    }
                },
            }
        if method == "debug_pixel":
            return {"texture": {"resource_id": payload["texture_id"]}, "draws": [], "meta": {}}
        if method == "trace_bad_pixel":
            return {
                "query": {
                    "texture_id": payload["texture_id"],
                    "x": payload["x"],
                    "y": payload["y"],
                    "mip_level": payload["mip_level"],
                    "array_slice": payload["array_slice"],
                    "sample": payload["sample"],
                },
                "texture": {"resource_id": payload["texture_id"]},
                "conclusion": {"category": "no_modifications", "summary": "No modifications.", "confidence": 1.0},
                "history_summary": {
                    "usage_event_count": 0,
                    "total_modification_count": 0,
                    "draw_count": 0,
                    "latest_attempt_event_id": None,
                    "final_writer_event_id": None,
                },
                "primary_event": None,
                "visible_source_event": None,
                "primary_pass": None,
                "visible_source_pass": None,
                "pipeline": {"available": False, "reason": "No primary event was identified for pipeline inspection."},
                "shader_debug": {"used": False, "attempted": False, "reason": "no_final_writer", "event_id": None},
                "key_evidence": [],
                "breadcrumb": [],
                "related_ids": {
                    "texture_id": payload["texture_id"],
                    "primary_event_id": None,
                    "visible_source_event_id": None,
                    "primary_pass_id": None,
                    "visible_source_pass_id": None,
                    "latest_attempt_event_id": None,
                    "final_writer_event_id": None,
                },
                "recommended_calls": [{"tool": "renderdoc_get_pixel_history", "arguments": {"texture_id": payload["texture_id"]}}],
                "meta": {},
            }
        if method == "probe_texture_regions":
            return {
                "texture": {"resource_id": payload["texture_id"], "name": "SceneColor"},
                "query": {
                    "texture_id": payload["texture_id"],
                    "x": payload["x"],
                    "y": payload["y"],
                    "width": payload.get("width", 4),
                    "height": payload.get("height", 4),
                    "mip_level": payload["mip_level"],
                    "array_slice": payload["array_slice"],
                    "sample": payload["sample"],
                    "channel_mode": payload["channel_mode"],
                    "threshold": payload["threshold"],
                },
                "summary": {
                    "scanned_pixel_count": 16,
                    "active_pixel_count": 4,
                    "active_coverage_ratio": 0.25,
                    "threshold_mode": payload["channel_mode"],
                },
                "regions": [
                    {
                        "region_index": 0,
                        "pixel_count": 4,
                        "bbox": {"min_x": 1, "min_y": 1, "max_x": 2, "max_y": 2},
                        "coverage_ratio": 0.25,
                        "centroid": {"x": 1.5, "y": 1.5},
                        "representative_pixel": {"x": 1, "y": 1},
                        "candidate_pixels": [{"x": 1, "y": 1}, {"x": 2, "y": 2}],
                        "sampled_peak_value": 1.0,
                    }
                ],
                "recommended_pixels": [{"x": 1, "y": 1}],
                "recommended_calls": [
                    {"tool": "renderdoc_trace_bad_pixel", "arguments": {"texture_id": payload["texture_id"], "x": 1, "y": 1}}
                ],
                "meta": {},
            }
        if method == "start_pixel_shader_debug":
            return {
                "shader_debug_id": "debug-1",
                "event_id": payload["event_id"],
                "states": [],
                "returned_state_count": 0,
                "meta": {"completed": False, "has_more": True},
            }
        if method == "continue_shader_debug":
            return {
                "shader_debug_id": payload["shader_debug_id"],
                "states": [],
                "returned_state_count": 0,
                "meta": {"completed": True, "has_more": False},
            }
        if method == "get_shader_debug_step":
            return {
                "shader_debug_id": payload["shader_debug_id"],
                "step_index": payload["step_index"],
                "changes": [],
                "returned_change_count": 0,
                "meta": {"changes_truncated": False},
            }
        if method == "end_shader_debug":
            return {"shader_debug_id": payload["shader_debug_id"], "closed": True, "meta": {}}
        if method == "get_texture_data":
            return {"texture": {"resource_id": payload["texture_id"]}, "pixels": [], "meta": {}}
        if method == "get_buffer_data":
            return {
                "buffer": {"resource_id": payload["buffer_id"]},
                "returned_size": 4,
                "encoding": payload.get("encoding", "hex"),
                "data": "00 00 00 00",
                "meta": {},
            }
        if method == "save_texture_to_file":
            return {"saved": True, "output_path": payload["output_path"], "meta": {}}
        return {"ok": True, "meta": {}}

    def close(self) -> None:
        self.closed += 1


def _capture(tmp_path: Path, name: str = "sample.rdc") -> str:
    capture_path = tmp_path / name
    capture_path.write_text("x", encoding="utf-8")
    return str(capture_path.resolve())


def _application() -> tuple[RenderDocApplication, list[DummyBridge]]:
    created: list[DummyBridge] = []
    pool = CaptureSessionPool(bridge_factory=lambda: created.append(DummyBridge()) or created[-1])
    return RenderDocApplication(session_pool=pool), created


def test_open_capture_returns_capture_id_and_overview(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)

    response = application.captures.renderdoc_open_capture(capture_path)

    assert response["capture_id"]
    assert response["capture_path"] == capture_path
    assert response["api"] == "D3D12"
    assert response["root_pass_count"] == 1
    assert response["meta"] == {"backend": "qrenderdoc", "renderdoc_version": "1.43"}
    assert created[0].loaded == [capture_path]
    assert created[0].calls == [("get_capture_overview", {})]


def test_open_capture_reuses_existing_session_for_same_capture_path(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)

    first = application.captures.renderdoc_open_capture(capture_path)
    second = application.captures.renderdoc_open_capture(capture_path)

    assert first["capture_id"] == second["capture_id"]
    assert first["capture_path"] == second["capture_path"]
    assert len(created) == 1
    assert created[0].loaded == [capture_path, capture_path]
    assert created[0].calls == [("get_capture_overview", {}), ("get_capture_overview", {})]


def test_handlers_reuse_capture_id_session_and_attach_meta(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    actions = application.actions.renderdoc_list_actions(opened["capture_id"], cursor="10", limit="25")
    passes = application.captures.renderdoc_list_passes(opened["capture_id"], limit=5, sort_by="gpu_time")
    pipeline = application.actions.renderdoc_get_pipeline_overview(opened["capture_id"], event_id="42")

    assert actions["capture_id"] == opened["capture_id"]
    assert actions["meta"]["backend"] == "qrenderdoc"
    assert actions["meta"]["renderdoc_version"] == "1.43"
    assert actions["meta"]["page"]["cursor"] == "10"
    assert passes["meta"]["page"]["limit"] == 5
    assert passes["sort_by"] == "gpu_time"
    assert pipeline["pipeline"]["api_details_available"] is True
    assert [call[0] for call in created[0].calls] == [
        "get_capture_overview",
        "list_actions",
        "list_passes",
        "get_pipeline_overview",
    ]


def test_close_capture_invalidates_session(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    closed = application.captures.renderdoc_close_capture(opened["capture_id"])

    assert closed["closed"] is True
    assert closed["meta"]["backend"] == "qrenderdoc"
    assert closed["meta"]["renderdoc_version"] == "1.43"
    assert created[0].closed == 1
    with pytest.raises(InvalidCaptureIDError):
        application.captures.renderdoc_get_capture_overview(opened["capture_id"])


def test_analysis_worklist_uses_distinct_bridge_method(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    response = application.captures.renderdoc_get_analysis_worklist(opened["capture_id"], focus="structure", limit=5)

    assert response["focus"] == "structure"
    assert created[0].calls[-1] == ("get_analysis_worklist", {"focus": "structure", "limit": 5})


def test_buffer_reads_default_to_hex(tmp_path: Path) -> None:
    application, _ = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    response = application.resources.renderdoc_get_buffer_data(opened["capture_id"], " buf123 ", "16", "32")

    assert response["encoding"] == "hex"
    assert response["data"] == "00 00 00 00"


def test_validation_errors_raise_domain_exceptions(tmp_path: Path) -> None:
    application, _ = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    with pytest.raises(ReplayFailureError):
        application.actions.renderdoc_list_actions(opened["capture_id"], limit="2000")
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_list_resources(opened["capture_id"], kind="bogus")
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_list_resource_usages(opened["capture_id"], "ResourceId::1", usage_kind="bogus")
    with pytest.raises(ReplayFailureError):
        application.actions.renderdoc_list_pipeline_bindings(opened["capture_id"], event_id=7, binding_kind="bogus")


def test_pipeline_binding_aliases_normalize_before_forwarding(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    outputs = application.actions.renderdoc_list_pipeline_bindings(opened["capture_id"], event_id=7, binding_kind=" outputs ")
    descriptors = application.actions.renderdoc_list_pipeline_bindings(
        opened["capture_id"], event_id=7, binding_kind="descriptors"
    )
    api = application.actions.renderdoc_list_pipeline_bindings(opened["capture_id"], event_id=7, binding_kind="api")

    assert outputs["binding_kind"] == "output_targets"
    assert descriptors["binding_kind"] == "descriptor_accesses"
    assert api["binding_kind"] == "api_details"
    assert created[0].calls[-3:] == [
        ("list_pipeline_bindings", {"event_id": 7, "binding_kind": "output_targets", "limit": 50}),
        ("list_pipeline_bindings", {"event_id": 7, "binding_kind": "descriptor_accesses", "limit": 50}),
        ("list_pipeline_bindings", {"event_id": 7, "binding_kind": "api_details", "limit": 50}),
    ]


def test_resource_usage_handlers_forward_and_attach_meta(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    summary = application.resources.renderdoc_get_resource_summary(opened["capture_id"], " ResourceId::123 ")
    usages = application.resources.renderdoc_list_resource_usages(
        opened["capture_id"],
        " ResourceId::123 ",
        usage_kind=" color_output ",
        cursor="0",
        limit="25",
    )

    assert summary["usage_overview"]["supported_scope"] == "rt_texture_v1"
    assert summary["recommended_calls"][0]["tool"] == "renderdoc_list_resource_usages"
    assert usages["events"][0]["event_id"] == 42
    assert usages["meta"]["page"]["limit"] == 25
    assert created[0].calls[-2:] == [
        ("get_resource_summary", {"resource_id": "ResourceId::123"}),
        (
            "list_resource_usages",
            {"resource_id": "ResourceId::123", "usage_kind": "color_output", "limit": 25, "cursor": 0},
        ),
    ]


def test_buffer_resource_usage_surface_reports_unsupported(tmp_path: Path) -> None:
    application, _ = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    summary = application.resources.renderdoc_get_resource_summary(opened["capture_id"], "BufferId::1")

    assert summary["resource"]["kind"] == "buffer"
    assert summary["usage_overview"]["available"] is False

    with pytest.raises(RenderDocMCPError) as exc_info:
        application.resources.renderdoc_list_resource_usages(opened["capture_id"], "BufferId::1")
    assert exc_info.value.code == "resource_usage_unsupported"


def test_trace_bad_pixel_handler_normalizes_and_forwards_arguments(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    traced = application.resources.renderdoc_trace_bad_pixel(
        opened["capture_id"],
        " ResourceId::123 ",
        x="3",
        y="4",
        mip_level="1",
        array_slice="2",
        sample="0",
    )

    assert traced["conclusion"]["category"] == "no_modifications"
    assert traced["meta"]["backend"] == "qrenderdoc"
    assert created[0].calls[-1] == (
        "trace_bad_pixel",
        {
            "texture_id": "ResourceId::123",
            "x": 3,
            "y": 4,
            "mip_level": 1,
            "array_slice": 2,
            "sample": 0,
        },
    )


def test_probe_texture_regions_handler_normalizes_and_forwards_arguments(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    probed = application.resources.renderdoc_probe_texture_regions(
        opened["capture_id"],
        " ResourceId::123 ",
        x="3",
        y="4",
        width="8",
        height="9",
        mip_level="1",
        array_slice="2",
        sample="0",
        channel_mode=" alpha ",
        threshold="0.25",
        min_region_pixels="6",
        max_regions="4",
        max_candidate_pixels_per_region="3",
    )

    assert probed["regions"][0]["bbox"] == {"min_x": 1, "min_y": 1, "max_x": 2, "max_y": 2}
    assert probed["recommended_pixels"][0] == {"x": 1, "y": 1}
    assert created[0].calls[-1] == (
        "probe_texture_regions",
        {
            "texture_id": "ResourceId::123",
            "x": 3,
            "y": 4,
            "width": 8,
            "height": 9,
            "mip_level": 1,
            "array_slice": 2,
            "sample": 0,
            "channel_mode": "alpha",
            "threshold": 0.25,
            "min_region_pixels": 6,
            "max_regions": 4,
            "max_candidate_pixels_per_region": 3,
        },
    )


def test_shader_debug_handlers_normalize_and_forward_arguments(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    started = application.resources.renderdoc_start_pixel_shader_debug(
        opened["capture_id"],
        event_id="42",
        x="3",
        y="4",
        texture_id=" ResourceId::123 ",
        sample="1",
        primitive_id="2",
        view="0",
        state_limit="16",
    )
    continued = application.resources.renderdoc_continue_shader_debug(opened["capture_id"], " debug-1 ", state_limit="8")
    step = application.resources.renderdoc_get_shader_debug_step(opened["capture_id"], "debug-1", step_index="7", change_limit="5")
    ended = application.resources.renderdoc_end_shader_debug(opened["capture_id"], "debug-1")

    assert started["shader_debug_id"] == "debug-1"
    assert continued["shader_debug_id"] == "debug-1"
    assert step["step_index"] == 7
    assert ended["closed"] is True
    assert created[0].calls[-4:] == [
        (
            "start_pixel_shader_debug",
            {
                "event_id": 42,
                "x": 3,
                "y": 4,
                "texture_id": "ResourceId::123",
                "sample": 1,
                "primitive_id": 2,
                "view": 0,
                "state_limit": 16,
            },
        ),
        ("continue_shader_debug", {"shader_debug_id": "debug-1", "state_limit": 8}),
        ("get_shader_debug_step", {"shader_debug_id": "debug-1", "step_index": 7, "change_limit": 5}),
        ("end_shader_debug", {"shader_debug_id": "debug-1"}),
    ]


def test_shader_debug_validation_errors_raise_domain_exceptions(tmp_path: Path) -> None:
    application, _ = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_start_pixel_shader_debug(opened["capture_id"], event_id=7, x=0, y=0, state_limit=999)
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_get_shader_debug_step(opened["capture_id"], "debug-1", step_index=0, change_limit=999)


def test_registry_contains_new_breaking_api_surface() -> None:
    application, _ = _application()
    tool_names = {tool.name for tool in build_tool_registry(application)}
    resource_uris = {resource.uri for resource in build_resource_registry(application)}

    assert {
        "renderdoc_open_capture",
        "renderdoc_get_capture_overview",
        "renderdoc_get_analysis_worklist",
        "renderdoc_get_pipeline_overview",
        "renderdoc_get_shader_code_chunk",
        "renderdoc_list_resource_usages",
        "renderdoc_probe_texture_regions",
        "renderdoc_trace_bad_pixel",
        "renderdoc_start_pixel_shader_debug",
        "renderdoc_continue_shader_debug",
        "renderdoc_get_shader_debug_step",
        "renderdoc_end_shader_debug",
    }.issubset(tool_names)
    assert "renderdoc://capture/{capture_id}/overview" in resource_uris


def test_recent_captures_reports_backend_meta(tmp_path: Path, monkeypatch) -> None:
    application, _ = _application()
    config_path = tmp_path / "UI.config"
    config_path.write_text('{"RecentCaptureFiles":["C:\\\\captures\\\\sample.rdc"]}', encoding="utf-8")

    monkeypatch.setattr("renderdoc_mcp.application.context.ui_config_path", lambda: config_path)

    response = application.captures.renderdoc_recent_captures()

    assert response["count"] == 1
    assert response["meta"] == {"backend": "qrenderdoc"}
