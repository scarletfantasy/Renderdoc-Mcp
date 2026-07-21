from __future__ import annotations

from renderdoc_mcp.analysis import frame_analysis


def _resource(name: str) -> dict[str, str]:
    return {"resource_id": name.lower(), "resource_name": name}


def _subresource(mip: int = 0, slice_index: int = 0, sample: int = 0) -> dict[str, int]:
    return {"mip": mip, "slice": slice_index, "sample": sample}


def _action(
    event_id: int,
    name: str,
    flags: list[str] | None = None,
    outputs: list[dict[str, str]] | None = None,
    depth_output: dict[str, str] | None = None,
    copy_source: dict | None = None,
    copy_destination: dict | None = None,
    children: list[dict] | None = None,
    num_indices: int = 0,
    num_instances: int = 1,
    dispatch_dimension: list[int] | None = None,
    dispatch_threads_dimension: list[int] | None = None,
) -> dict:
    payload = {
        "event_id": event_id,
        "action_id": event_id,
        "name": name,
        "custom_name": "",
        "flags": list(flags or []),
        "child_count": 0,
        "is_fake_marker": False,
        "num_indices": num_indices,
        "num_instances": num_instances,
        "dispatch_dimension": list(dispatch_dimension or [0, 0, 0]),
        "dispatch_threads_dimension": list(dispatch_threads_dimension or [0, 0, 0]),
        "outputs": list(outputs or []),
        "copy_source": copy_source or {"resource_id": "", "resource_name": "", "subresource": _subresource()},
        "copy_destination": copy_destination
        or {"resource_id": "", "resource_name": "", "subresource": _subresource()},
        "depth_output": depth_output or {"resource_id": "", "resource_name": ""},
        "parent_event_id": None,
        "children": list(children or []),
    }

    for child in payload["children"]:
        child["parent_event_id"] = event_id
    payload["child_count"] = len(payload["children"])
    return payload


def _count_stats(nodes: list[dict]) -> dict[str, int]:
    stats = {"total_actions": 0, "draw_calls": 0, "dispatches": 0, "copies": 0, "clears": 0, "resolves": 0}
    for node in nodes:
        stats["total_actions"] += 1
        flags = set(node["flags"])
        if "draw" in flags:
            stats["draw_calls"] += 1
        if "dispatch" in flags:
            stats["dispatches"] += 1
        if "copy" in flags:
            stats["copies"] += 1
        if "clear" in flags:
            stats["clears"] += 1
        if "resolve" in flags:
            stats["resolves"] += 1
        child_stats = _count_stats(node["children"])
        for key, value in child_stats.items():
            stats[key] += value
    return stats


def _metadata(nodes: list[dict]) -> dict:
    return {
        "capture": {"loaded": True, "filename": "sample.rdc"},
        "api": "D3D12",
        "frame": {"frame_number": 1},
        "statistics": _count_stats(nodes),
        "resource_counts": {"textures": 4, "buffers": 2},
    }


def test_build_frame_analysis_indexes_nested_passes_and_actions() -> None:
    nodes = [
        _action(
            10,
            "Scene",
            ["push_marker"],
            children=[
                _action(
                    20,
                    "BasePass",
                    ["push_marker"],
                    children=[
                        _action(
                            21,
                            "Draw",
                            ["draw"],
                            outputs=[_resource("GBufferA"), _resource("GBufferB")],
                            depth_output=_resource("SceneDepth"),
                        )
                    ],
                )
            ],
        ),
        _action(30, "Present(Backbuffer)", []),
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))

    assert analysis["root_pass_ids"] == ["pass:10-21", "pass:30-30"]
    assert analysis["pass_children_index"]["pass:10-21"] == ["pass:20-21"]
    assert analysis["action_children_index"][""] == [10, 30]
    assert analysis["action_children_index"]["10"] == [20]


def test_list_passes_can_drill_into_parent_pass_id() -> None:
    nodes = [
        _action(
            10,
            "Scene",
            ["push_marker"],
            children=[
                _action(20, "ShadowDepths", ["push_marker"], children=[_action(21, "Draw", ["draw"])]),
                _action(
                    30,
                    "BasePass",
                    ["push_marker"],
                    children=[_action(31, "Draw", ["draw"], outputs=[_resource("Color")])],
                ),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    root = frame_analysis.list_passes(analysis, limit=50)
    scene_children = frame_analysis.list_passes(analysis, parent_pass_id="pass:10-31", limit=50)

    assert [item["name"] for item in root["passes"]] == ["Scene"]
    assert {item["name"] for item in scene_children["passes"]} == {"ShadowDepths", "BasePass"}


def test_get_innermost_pass_for_event_prefers_smallest_nested_range() -> None:
    nodes = [
        _action(
            10,
            "Scene",
            ["push_marker"],
            children=[
                _action(
                    20,
                    "BasePass",
                    ["push_marker"],
                    children=[
                        _action(30, "Opaque", ["push_marker"], children=[_action(31, "Draw", ["draw"])]),
                    ],
                ),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    matched = frame_analysis.get_innermost_pass_for_event(analysis, 31)

    assert matched is not None
    assert matched["name"] == "Opaque"
    assert matched["pass_id"] == "pass:30-31"


def test_list_actions_returns_direct_children_only() -> None:
    nodes = [
        _action(
            10,
            "Scene",
            ["push_marker"],
            children=[
                _action(20, "Compute", ["dispatch"]),
                _action(30, "BasePass", ["push_marker"], children=[_action(31, "Draw", ["draw"])]),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    root = frame_analysis.build_action_children_result(analysis, limit=50)
    scene_children = frame_analysis.build_action_children_result(analysis, parent_event_id=10, flags_filter="push_marker")

    assert [item["event_id"] for item in root["actions"]] == [10]
    assert [item["event_id"] for item in scene_children["actions"]] == [30]


def test_search_actions_recurses_and_can_filter_known_resource_writes() -> None:
    nodes = [
        _action(
            10,
            "Scene",
            ["push_marker"],
            children=[
                _action(
                    20,
                    "BasePass",
                    ["push_marker"],
                    children=[
                        _action(21, "Opaque Draw", ["draw"], outputs=[_resource("SceneColor")]),
                        _action(22, "Other Draw", ["draw"], outputs=[_resource("Other")]),
                    ],
                )
            ],
        )
    ]
    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))

    result = frame_analysis.build_action_search_result(
        analysis,
        query="opaque",
        flags_filter="draw",
        resource_id="scenecolor",
        limit=50,
    )

    assert result is not None
    assert [item["event_id"] for item in result["actions"]] == [21]
    assert result["actions"][0]["matched_resource_usage_kinds"] == ["color_output"]
    assert result["meta"]["search_scope"] == "recursive_descendants"


def test_list_timing_events_pages_gpu_rows() -> None:
    nodes = [
        _action(
            100,
            "BasePass",
            ["push_marker"],
            children=[
                _action(101, "Depth", ["draw"]),
                _action(102, "Color", ["draw"]),
                _action(103, "Light", ["draw"]),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    result = frame_analysis.list_timing_events(
        analysis,
        "pass:100-103",
        {
            "timing_available": True,
            "counter_name": "EventGPUDuration",
            "rows": [
                {"event_id": 101, "gpu_time_ms": 0.5},
                {"event_id": 102, "gpu_time_ms": 1.25},
                {"event_id": 103, "gpu_time_ms": 0.75},
            ],
        },
        cursor=0,
        limit=2,
        sort_by="gpu_time",
    )

    assert result["basis"] == "gpu_timing"
    assert result["meta"]["page"]["returned_count"] == 2
    assert result["events"][0]["event_id"] == 102
    assert result["total_gpu_time_ms"] == 2.5


def test_list_passes_gpu_time_builds_reusable_timing_index() -> None:
    nodes = [
        _action(
            100,
            "Frame",
            ["push_marker"],
            children=[
                _action(110, "Shadow", ["push_marker"], children=[_action(111, "ShadowDraw", ["draw"])]),
                _action(120, "BasePass", ["push_marker"], children=[_action(121, "BaseDraw", ["draw"])]),
            ],
        )
    ]
    timing_payload = {
        "timing_available": True,
        "counter_name": "EventGPUDuration",
        "rows": [
            {"event_id": 121, "gpu_time_ms": 2.0},
            {"event_id": 111, "gpu_time_ms": 1.0},
        ],
    }

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    first = frame_analysis.list_passes(analysis, parent_pass_id="pass:100-121", limit=50, sort_by="gpu_time", timing_payload=timing_payload)
    second = frame_analysis.list_passes(analysis, parent_pass_id="pass:100-121", limit=50, sort_by="gpu_time", timing_payload=timing_payload)

    assert [item["name"] for item in first["passes"]] == ["BasePass", "Shadow"]
    assert first["passes"][0]["gpu_time_ms"] == 2.0
    assert first["passes"][1]["gpu_time_ms"] == 1.0
    assert second["passes"] == first["passes"]
    assert "_timing_index" in timing_payload
    assert [item["event_id"] for item in timing_payload["rows"]] == [111, 121]


def test_build_performance_hotspots_uses_nested_passes() -> None:
    nodes = [
        _action(
            10,
            "Scene",
            ["push_marker"],
            children=[
                _action(
                    20,
                    "BasePass",
                    ["push_marker"],
                    children=[_action(21, "Draw", ["draw"], num_indices=1000)],
                )
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    hotspots = frame_analysis.build_performance_hotspots(
        analysis,
        {"timing_available": False, "counter_name": "EventGPUDuration", "rows": [], "reason": "unsupported"},
    )

    assert hotspots["basis"] == "heuristic"
    assert hotspots["top_passes"][0]["name"] == "BasePass"


def test_build_performance_hotspots_orders_synthetic_gpu_rows() -> None:
    nodes = [
        _action(
            100,
            "Frame",
            ["push_marker"],
            children=[
                _action(110, "Shadow", ["push_marker"], children=[_action(111, "ShadowDraw", ["draw"])]),
                _action(120, "BasePass", ["push_marker"], children=[_action(121, "BaseDraw", ["draw"])]),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    hotspots = frame_analysis.build_performance_hotspots(
        analysis,
        {
            "timing_available": True,
            "counter_name": "EventGPUDuration",
            "rows": [
                {"event_id": 121, "gpu_time_ms": 2.0},
                {"event_id": 111, "gpu_time_ms": 1.0},
            ],
        },
        limit=2,
    )

    assert hotspots["basis"] == "gpu_timing"
    assert [item["name"] for item in hotspots["top_passes"]] == ["BasePass", "Shadow"]
    assert [item["event_id"] for item in hotspots["top_events"]] == [121, 111]


def test_build_frame_analysis_indexes_texture_resource_usages() -> None:
    nodes = [
        _action(
            10,
            "Frame",
            ["push_marker"],
            children=[
                _action(
                    11,
                    "BasePass",
                    ["draw"],
                    outputs=[_resource("SceneColor"), _resource("SceneColor")],
                    depth_output=_resource("SceneDepth"),
                ),
                _action(
                    12,
                    "CopyColor",
                    ["copy"],
                    copy_source={
                        "resource_id": "scenecolor",
                        "resource_name": "SceneColor",
                        "subresource": _subresource(mip=1, slice_index=2, sample=0),
                    },
                    copy_destination={
                        "resource_id": "historycolor",
                        "resource_name": "HistoryColor",
                        "subresource": _subresource(mip=0, slice_index=0, sample=0),
                    },
                ),
                _action(
                    13,
                    "ResolveDepth",
                    ["resolve"],
                    copy_source={
                        "resource_id": "scenedepth",
                        "resource_name": "SceneDepth",
                        "subresource": _subresource(mip=0, slice_index=0, sample=4),
                    },
                    copy_destination={
                        "resource_id": "resolveddepth",
                        "resource_name": "ResolvedDepth",
                        "subresource": _subresource(mip=0, slice_index=0, sample=0),
                    },
                ),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))

    scene_color = analysis["resource_usage_index"]["scenecolor"]
    assert [item["event_id"] for item in scene_color] == [11, 12]
    assert scene_color[0]["matched_usage_kinds"] == ["color_output"]
    assert scene_color[0]["bindings"] == [
        {"usage_kind": "color_output", "slot_kind": "color", "slot_index": 0},
        {"usage_kind": "color_output", "slot_kind": "color", "slot_index": 1},
    ]
    assert scene_color[1]["bindings"] == [
        {
            "usage_kind": "copy_source",
            "subresource": {"mip": 1, "slice": 2, "sample": 0},
        }
    ]

    scene_depth = analysis["resource_usage_index"]["scenedepth"]
    assert [item["matched_usage_kinds"] for item in scene_depth] == [["depth_output"], ["resolve_source"]]
    assert scene_depth[0]["bindings"] == [
        {"usage_kind": "depth_output", "slot_kind": "depth", "slot_index": -1}
    ]
    assert scene_depth[1]["bindings"] == [
        {
            "usage_kind": "resolve_source",
            "subresource": {"mip": 0, "slice": 0, "sample": 4},
        }
    ]


def test_list_resource_usages_filters_and_pages() -> None:
    nodes = [
        _action(
            10,
            "Frame",
            ["push_marker"],
            children=[
                _action(11, "DrawA", ["draw"], outputs=[_resource("SceneColor")]),
                _action(
                    12,
                    "CopyA",
                    ["copy"],
                    copy_source={
                        "resource_id": "scenecolor",
                        "resource_name": "SceneColor",
                        "subresource": _subresource(mip=0, slice_index=0, sample=0),
                    },
                ),
                _action(13, "DrawB", ["draw"], outputs=[_resource("SceneColor")]),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))

    filtered = frame_analysis.list_resource_usages(
        analysis,
        "scenecolor",
        usage_kind="color_output",
        cursor=1,
        limit=1,
    )
    assert [item["event_id"] for item in filtered["events"]] == [13]
    assert filtered["meta"]["page"]["total_count"] == 3
    assert filtered["meta"]["page"]["matched_count"] == 2
    assert filtered["meta"]["page"]["next_cursor"] == ""

    empty = frame_analysis.list_resource_usages(analysis, "missingtexture", usage_kind="all", limit=10)
    assert empty["events"] == []
    assert empty["meta"]["page"]["total_count"] == 0


def test_build_resource_usage_overview_summarizes_counts() -> None:
    nodes = [
        _action(
            10,
            "Frame",
            ["push_marker"],
            children=[
                _action(
                    11,
                    "Draw",
                    ["draw"],
                    outputs=[_resource("SceneColor")],
                    depth_output=_resource("SceneDepth"),
                ),
                _action(
                    12,
                    "Copy",
                    ["copy"],
                    copy_destination={
                        "resource_id": "scenecolor",
                        "resource_name": "SceneColor",
                        "subresource": _subresource(mip=0, slice_index=0, sample=0),
                    },
                ),
            ],
        )
    ]

    analysis = frame_analysis.build_frame_analysis(nodes, _metadata(nodes))
    overview = frame_analysis.build_resource_usage_overview(analysis, "scenecolor")

    assert overview["available"] is True
    assert overview["supported_scope"] == "rt_texture_v1"
    assert overview["total_matching_events"] == 2
    assert overview["counts_by_kind"]["color_output"] == 1
    assert overview["counts_by_kind"]["copy_destination"] == 1
    assert overview["first_event_id"] == 11
    assert overview["last_event_id"] == 12
    assert overview["representative_events"][0]["event_id"] == 11
