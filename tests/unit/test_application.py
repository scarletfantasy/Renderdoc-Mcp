from __future__ import annotations

import threading
from pathlib import Path

import pytest

from renderdoc_mcp.application import RenderDocApplication
from renderdoc_mcp.application.compare import compare_event_dossiers
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
        self.action_name = "Draw"
        self.texture_value = 0.0
        self.call_barrier: threading.Barrier | None = None
        self.barrier_methods: set[str] = set()

    def ensure_capture_loaded(self, capture_path: str):
        self.loaded.append(capture_path)
        return {"loaded": True, "filename": capture_path}

    def call(self, method: str, params=None):
        payload = params or {}
        self.calls.append((method, payload))
        if self.call_barrier is not None and method in self.barrier_methods:
            self.call_barrier.wait(timeout=2.0)

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
                    "name": self.action_name,
                    "flags": ["draw"],
                    "depth": 2,
                    "child_count": 0,
                    "parent_event_id": 1,
                    "resource_usage_summary": {"output_count": 1, "has_depth_output": True},
                },
                "meta": {},
            }
        if method == "search_actions":
            return {
                "actions": [{"event_id": 42, "name": self.action_name, "flags": ["draw"]}],
                "meta": {"page": {"limit": payload["limit"], "returned_count": 1, "has_more": False}},
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
        if method == "get_event_dossier":
            return {
                "event_id": payload["event_id"],
                "api": "D3D12",
                "action": {"event_id": payload["event_id"], "name": self.action_name, "flags": ["draw"]},
                "pass": {"pass_id": "pass:1-10", "name": "BasePass", "category": "geometry"},
                "pipeline": {
                    "available": True,
                    "topology": "TriangleList",
                    "counts": {"shaders": 2},
                    "shaders": [{"stage": "Pixel", "shader_id": "shader-1", "shader_name": "MainPS"}],
                },
                "bindings": {
                    kind: {"items": [], "returned_count": 0, "total_count": 0, "truncated": False}
                    for kind in payload.get("binding_kinds", [])
                },
                "meta": {},
            }
        if method == "get_event_dossiers":
            return {
                "items": [{"event_id": event_id, "ok": True, "dossier": {"event_id": event_id}} for event_id in payload["event_ids"]],
                "requested_count": len(payload["event_ids"]),
                "success_count": len(payload["event_ids"]),
                "error_count": 0,
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
        if method == "search_shader_code":
            return {
                "event_id": payload["event_id"],
                "query": payload["query"],
                "total_match_count": 1,
                "matches": [{"line_number": 7, "line": "sample", "context_text": "sample"}],
                "meta": {"page": {"returned_count": 1, "has_more": False}},
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
        if method == "search_resource_bindings":
            return {
                "resource": {"resource_id": payload["resource_id"]},
                "matches": [],
                "matched_event_count": 0,
                "matched_binding_count": 0,
                "meta": {"scan": {"scanned_count": 0, "has_more": False}},
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
        if method == "start_compute_shader_debug":
            return {
                "shader_debug_id": "debug-1",
                "event_id": payload["event_id"],
                "target": {"group_id": payload["group_id"], "thread_id": payload["thread_id"]},
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
        if method == "analyze_shader_debug":
            return {
                "shader_debug_id": payload["shader_debug_id"],
                "analysis": {"analyzed_state_count": 12, "interesting_steps": []},
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
            return {
                "texture": {"resource_id": payload["texture_id"]},
                "pixels": [
                    [[self.texture_value, 0.0, 0.0, 1.0] for _ in range(payload["width"])]
                    for _ in range(payload["height"])
                ],
                "meta": {},
            }
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
    assert response["session_reused"] is False
    assert response["meta"] == {"backend": "qrenderdoc", "renderdoc_version": "1.43"}
    assert created[0].loaded == [capture_path]
    assert created[0].calls == [("get_capture_overview", {})]


def test_open_capture_is_idempotent_and_lists_the_reused_session(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)

    first = application.captures.renderdoc_open_capture(capture_path)
    second = application.captures.renderdoc_open_capture(capture_path)
    opened = application.captures.renderdoc_list_open_captures()

    assert second["capture_id"] == first["capture_id"]
    assert second["session_reused"] is True
    assert second["session_open_count"] == 2
    assert opened["count"] == 1
    assert opened["captures"][0]["capture_id"] == first["capture_id"]
    assert len(created) == 1


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


def test_invalid_capture_id_reports_reusable_sessions(tmp_path: Path) -> None:
    application, _ = _application()
    opened = application.captures.renderdoc_open_capture(_capture(tmp_path))

    with pytest.raises(InvalidCaptureIDError) as caught:
        application.captures.renderdoc_get_capture_overview("deadbeef")

    assert caught.value.details["available_capture_ids"] == [opened["capture_id"]]
    assert caught.value.details["suggested_call"]["tool"] == "renderdoc_list_open_captures"


def test_analysis_worklist_uses_distinct_bridge_method(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    response = application.captures.renderdoc_get_analysis_worklist(opened["capture_id"], focus="structure", limit=5)

    assert response["focus"] == "structure"
    assert created[0].calls[-1] == ("get_analysis_worklist", {"focus": "structure", "limit": 5})


def test_recursive_search_dossiers_and_semantic_aliases_forward_compact_calls(tmp_path: Path) -> None:
    application, created = _application()
    opened = application.captures.renderdoc_open_capture(_capture(tmp_path))

    searched = application.actions.renderdoc_search_actions(
        opened["capture_id"],
        query="draw",
        flags_filter="draw",
        resource_id="ResourceId::1",
        limit=10,
    )
    dossier = application.actions.renderdoc_get_event_dossier(
        opened["capture_id"],
        42,
        binding_kinds=["resources", "constant_buffers"],
    )
    batch = application.actions.renderdoc_get_event_dossiers(opened["capture_id"], [42, 43])

    assert searched["actions"][0]["event_id"] == 42
    assert set(dossier["bindings"]) == {"read_only_resources", "constant_blocks"}
    assert batch["success_count"] == 2
    assert created[0].calls[-3:] == [
        (
            "search_actions",
            {
                "limit": 10,
                "query": "draw",
                "flags_filter": "draw",
                "resource_id": "ResourceId::1",
            },
        ),
        (
            "get_event_dossier",
            {
                "event_id": 42,
                "binding_kinds": ["read_only_resources", "constant_blocks"],
                "binding_limit": 20,
            },
        ),
        (
            "get_event_dossiers",
            {"event_ids": [42, 43], "binding_kinds": ["output_targets", "shaders"], "binding_limit": 20},
        ),
    ]


def test_shader_and_resource_server_side_search_handlers(tmp_path: Path) -> None:
    application, created = _application()
    opened = application.captures.renderdoc_open_capture(_capture(tmp_path))

    shader = application.actions.renderdoc_search_shader_code(
        opened["capture_id"],
        42,
        "Pixel",
        "sample",
        regex="false",  # type: ignore[arg-type]
        context_lines="3",  # type: ignore[arg-type]
    )
    resource = application.resources.renderdoc_search_resource_bindings(
        opened["capture_id"],
        "ResourceId::1",
        event_id_min="10",  # type: ignore[arg-type]
        event_id_max="100",  # type: ignore[arg-type]
    )
    trace = application.resources.renderdoc_analyze_shader_debug(opened["capture_id"], "debug-1")

    assert shader["total_match_count"] == 1
    assert resource["matched_event_count"] == 0
    assert trace["analysis"]["analyzed_state_count"] == 12
    assert created[0].calls[-3:] == [
        (
            "search_shader_code",
            {
                "event_id": 42,
                "stage": "Pixel",
                "query": "sample",
                "regex": False,
                "case_sensitive": False,
                "context_lines": 3,
                "cursor": 0,
                "limit": 25,
            },
        ),
        (
            "search_resource_bindings",
            {
                "resource_id": "ResourceId::1",
                "cursor": 0,
                "scan_limit": 100,
                "match_limit": 50,
                "event_id_min": 10,
                "event_id_max": 100,
            },
        ),
        (
            "analyze_shader_debug",
            {"shader_debug_id": "debug-1", "max_steps": 4096, "max_interesting_steps": 32},
        ),
    ]


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
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_get_buffer_data(opened["capture_id"], "BufferId::1", size=0)
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_get_buffer_data(opened["capture_id"], "BufferId::1", size=-1)


def test_pipeline_binding_aliases_normalize_before_forwarding(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    outputs = application.actions.renderdoc_list_pipeline_bindings(opened["capture_id"], event_id=7, binding_kind=" outputs ")
    descriptors = application.actions.renderdoc_list_pipeline_bindings(
        opened["capture_id"], event_id=7, binding_kind="descriptors"
    )
    buffers = application.actions.renderdoc_list_pipeline_bindings(
        opened["capture_id"], event_id=7, binding_kind="buffers"
    )
    api = application.actions.renderdoc_list_pipeline_bindings(opened["capture_id"], event_id=7, binding_kind="api")

    assert outputs["binding_kind"] == "output_targets"
    assert descriptors["binding_kind"] == "descriptor_accesses"
    assert buffers["binding_kind"] == "vertex_buffers"
    assert api["binding_kind"] == "api_details"
    assert created[0].calls[-4:] == [
        ("list_pipeline_bindings", {"event_id": 7, "binding_kind": "output_targets", "limit": 50}),
        ("list_pipeline_bindings", {"event_id": 7, "binding_kind": "descriptor_accesses", "limit": 50}),
        ("list_pipeline_bindings", {"event_id": 7, "binding_kind": "vertex_buffers", "limit": 50}),
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


def test_compute_shader_debug_handler_normalizes_and_forwards_arguments(tmp_path: Path) -> None:
    application, created = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    started = application.resources.renderdoc_start_compute_shader_debug(
        opened["capture_id"],
        event_id="8659",
        group_x="492",
        group_y="262",
        group_z="0",
        thread_x="1",
        thread_y="0",
        thread_z="0",
        state_limit="16",
    )

    assert started["shader_debug_id"] == "debug-1"
    assert started["target"]["group_id"] == [492, 262, 0]
    assert started["target"]["thread_id"] == [1, 0, 0]
    assert created[0].calls[-1] == (
        "start_compute_shader_debug",
        {
            "event_id": 8659,
            "group_id": [492, 262, 0],
            "thread_id": [1, 0, 0],
            "state_limit": 16,
        },
    )


def test_shader_debug_validation_errors_raise_domain_exceptions(tmp_path: Path) -> None:
    application, _ = _application()
    capture_path = _capture(tmp_path)
    opened = application.captures.renderdoc_open_capture(capture_path)

    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_start_pixel_shader_debug(opened["capture_id"], event_id=7, x=0, y=0, state_limit=999)
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_start_compute_shader_debug(
            opened["capture_id"],
            event_id=7,
            group_x=0,
            group_y=0,
            thread_x=0,
            state_limit=999,
        )
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_get_shader_debug_step(opened["capture_id"], "debug-1", step_index=0, change_limit=999)
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_start_pixel_shader_debug(opened["capture_id"], event_id=7, x=0, y=0, state_limit=0)
    with pytest.raises(ReplayFailureError):
        application.resources.renderdoc_get_shader_debug_step(opened["capture_id"], "debug-1", step_index=0, change_limit=0)


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
        "renderdoc_start_compute_shader_debug",
        "renderdoc_continue_shader_debug",
        "renderdoc_list_investigations",
        "renderdoc_get_shader_debug_step",
        "renderdoc_end_shader_debug",
    }.issubset(tool_names)
    assert "renderdoc://capture/{capture_id}/overview" in resource_uris


def test_investigation_restores_focus_and_pinned_evidence(tmp_path: Path) -> None:
    application, _ = _application()
    opened = application.captures.renderdoc_open_capture(_capture(tmp_path))

    created = application.investigation.renderdoc_create_investigation(
        "Regression",
        capture_ids=[opened["capture_id"]],
        labels=["baseline"],
    )
    focused = application.investigation.renderdoc_set_investigation_focus(
        created["investigation_id"],
        capture_id=opened["capture_id"],
        event_id=42,
        texture_id="ResourceId::1",
        x=3,
        y=4,
    )
    pinned = application.investigation.renderdoc_pin_investigation_evidence(
        created["investigation_id"],
        "writer",
        "event",
        "42",
        "Final writer",
        capture_id=opened["capture_id"],
    )
    restored = application.investigation.renderdoc_get_investigation_summary(created["investigation_id"])
    listed = application.investigation.renderdoc_list_investigations()

    assert focused["focus"]["event_id"] == 42
    assert pinned["evidence"]["writer"]["summary"] == "Final writer"
    assert restored["captures"]["baseline"]["open"] is True
    assert restored["recommended_calls"][0]["tool"] == "renderdoc_get_event_dossier"
    assert listed["investigations"][0]["investigation_id"] == created["investigation_id"]
    assert listed["investigations"][0]["focus"]["event_id"] == 42


def test_investigation_focus_auto_attaches_capture_and_rejects_duplicate_labels(tmp_path: Path) -> None:
    application, _ = _application()
    baseline = application.captures.renderdoc_open_capture(_capture(tmp_path, "baseline.rdc"))
    candidate = application.captures.renderdoc_open_capture(_capture(tmp_path, "candidate.rdc"))
    created = application.investigation.renderdoc_create_investigation("Auto attach")

    focused = application.investigation.renderdoc_set_investigation_focus(
        created["investigation_id"],
        capture_id=baseline["capture_id"],
        event_id=42,
    )

    assert focused["captures"]["baseline"]["capture_id"] == baseline["capture_id"]
    with pytest.raises(ReplayFailureError, match="labels must be unique"):
        application.investigation.renderdoc_create_investigation(
            "Duplicates",
            capture_ids=[baseline["capture_id"], candidate["capture_id"]],
            labels=["same", "same"],
        )


def test_cross_capture_event_and_texture_diffs_are_compact(tmp_path: Path) -> None:
    application, created = _application()
    baseline = application.captures.renderdoc_open_capture(_capture(tmp_path, "baseline.rdc"))
    candidate = application.captures.renderdoc_open_capture(_capture(tmp_path, "candidate.rdc"))
    created[1].action_name = "Changed Draw"
    created[1].texture_value = 0.25

    event_diff = application.investigation.renderdoc_compare_events(
        baseline["capture_id"],
        42,
        candidate["capture_id"],
        84,
    )
    texture_diff = application.investigation.renderdoc_compare_texture_regions(
        baseline["capture_id"],
        "ResourceId::A",
        candidate["capture_id"],
        "ResourceId::B",
        x=0,
        y=0,
        width=2,
        height=2,
        threshold=0.01,
    )

    assert event_diff["equivalent"] is False
    assert any(change["path"].endswith(".name") for change in event_diff["changes"])
    assert "baseline_snapshot" not in event_diff
    assert texture_diff["summary"]["changed_pixel_count"] == 4
    assert texture_diff["summary"]["max_finite_abs_difference"] == 0.25
    assert len(texture_diff["top_changed_pixels"]) == 4


def test_cross_capture_comparison_reads_independent_bridges_in_parallel(tmp_path: Path) -> None:
    application, created = _application()
    baseline = application.captures.renderdoc_open_capture(_capture(tmp_path, "baseline.rdc"))
    candidate = application.captures.renderdoc_open_capture(_capture(tmp_path, "candidate.rdc"))
    barrier = threading.Barrier(2)
    for bridge in created:
        bridge.call_barrier = barrier
        bridge.barrier_methods = {"get_event_dossier"}

    result = application.investigation.renderdoc_compare_events(
        baseline["capture_id"],
        42,
        candidate["capture_id"],
        84,
    )

    assert result["baseline"]["event_id"] == 42
    assert result["candidate"]["event_id"] == 84
    assert barrier.n_waiting == 0


def test_event_diff_only_marks_truncated_when_an_additional_change_exists() -> None:
    one_change = compare_event_dossiers(
        {"action": {"name": "before"}},
        {"action": {"name": "after"}},
        max_changes=1,
    )
    two_changes = compare_event_dossiers(
        {"action": {"name": "before", "index_count": 3}},
        {"action": {"name": "after", "index_count": 6}},
        max_changes=1,
    )

    assert one_change["change_count"] == 1
    assert one_change["changes_truncated"] is False
    assert two_changes["change_count"] == 1
    assert two_changes["changes_truncated"] is True


def test_event_diff_ignores_volatile_ids_across_captures() -> None:
    baseline = {
        "action": {"event_id": 10, "name": "Draw"},
        "pass": {"pass_id": "pass:1-10", "start_event_id": 1, "end_event_id": 10, "name": "BasePass"},
        "pipeline": {"graphics_pipeline_object": "pipe-a", "shaders": [{"shader_id": "shader-a", "shader_name": "MainPS"}]},
        "bindings": {"output_targets": {"items": [{"resource_id": "texture-a", "resource_name": "SceneColor"}]}},
    }
    candidate = {
        "action": {"event_id": 20, "name": "Draw"},
        "pass": {"pass_id": "pass:11-20", "start_event_id": 11, "end_event_id": 20, "name": "BasePass"},
        "pipeline": {"graphics_pipeline_object": "pipe-b", "shaders": [{"shader_id": "shader-b", "shader_name": "MainPS"}]},
        "bindings": {"output_targets": {"items": [{"resource_id": "texture-b", "resource_name": "SceneColor"}]}},
    }

    comparison = compare_event_dossiers(baseline, candidate)

    assert comparison["equivalent"] is True
    assert comparison["changes"] == []


def test_server_status_is_read_only_and_reports_extension_freshness(tmp_path: Path, monkeypatch) -> None:
    application, _ = _application()
    executable = tmp_path / "qrenderdoc.exe"
    executable.write_text("x", encoding="utf-8")
    monkeypatch.setattr("renderdoc_mcp.application.handlers.captures.current_backend_name", lambda: "qrenderdoc")
    monkeypatch.setattr("renderdoc_mcp.application.handlers.captures.resolve_qrenderdoc_path", lambda: executable)
    monkeypatch.setattr(
        "renderdoc_mcp.application.handlers.captures.inspect_extension_install",
        lambda: {"path": "extension", "installed": True, "current": True},
    )

    status = application.captures.renderdoc_get_server_status()

    assert status["ready"] is True
    assert status["backend"]["qrenderdoc_path"] == str(executable)
    assert status["extension"]["current"] is True


def test_recent_captures_reports_backend_meta(tmp_path: Path, monkeypatch) -> None:
    application, _ = _application()
    config_path = tmp_path / "UI.config"
    config_path.write_text('{"RecentCaptureFiles":["C:\\\\captures\\\\sample.rdc"]}', encoding="utf-8")

    monkeypatch.setattr("renderdoc_mcp.application.context.ui_config_path", lambda: config_path)

    response = application.captures.renderdoc_recent_captures()

    assert response["count"] == 1
    assert response["meta"] == {"backend": "qrenderdoc"}
