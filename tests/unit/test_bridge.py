from __future__ import annotations

import gc
import weakref
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from renderdoc_mcp import bridge as bridge_module
from renderdoc_mcp.bridge import QRenderDocBridge
from renderdoc_mcp.errors import BridgeHandshakeTimeoutError, ReplayFailureError
from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge import client as bridge_client_module
from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge import serialization as serialization_module
from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge.client import BridgeClient


class FakeMiniQt:
    def InvokeOntoUIThread(self, callback):
        callback()


class FakeExtensions:
    def GetMiniQtHelper(self):
        return FakeMiniQt()


class FakeReplay:
    def __init__(self, controller) -> None:
        self.controller = controller

    def BlockInvoke(self, callback):
        self.controller.block_invoke_count = getattr(self.controller, "block_invoke_count", 0) + 1
        callback(self.controller)


class FakeAction:
    eventId = 7
    customName = ""
    flags = 0
    outputs = ["tex-1"]
    depthOut = None

    def GetName(self, structured_file):
        return "Draw"


class FakeContext:
    def __init__(self, controller) -> None:
        self.controller = controller
        self.loaded = True
        self.set_event_calls: list[tuple] = []

    def Extensions(self):
        return FakeExtensions()

    def Replay(self):
        return FakeReplay(self.controller)

    def IsCaptureLoaded(self):
        return self.loaded

    def GetAction(self, event_id):
        return FakeAction()

    def SetEventID(self, *args):
        self.set_event_calls.append(tuple(args))
        return None

    def GetResourceName(self, resource_id):
        return str(resource_id)

    def GetTextures(self):
        return []

    def GetBuffers(self):
        return []

    def CloseCapture(self):
        self.loaded = False

    def GetCaptureFilename(self):
        return str(Path(__file__).resolve())


class FakeState:
    def __init__(self, *, shader_bound: bool = False) -> None:
        self.shader_bound = shader_bound

    def GetPrimitiveTopology(self):
        return "TriangleList"

    def GetShader(self, stage):
        if self.shader_bound:
            return "shader-1"
        return None

    def GetShaderReflection(self, stage):
        if not self.shader_bound:
            return None
        return SimpleNamespace(
            resourceId="shader-1",
            entryPoint="main",
            encoding="DXBC",
            inputSignature=[],
            outputSignature=[],
            constantBlocks=[],
        )

    def GetShaderEntryPoint(self, stage):
        if self.shader_bound:
            return "main"
        return ""


class FakeController:
    def __init__(self, *, api_name: str = "D3D12", state: FakeState | None = None, shader_debugging: bool = False) -> None:
        self.api_name = api_name
        self.state = state or FakeState()
        self.shader_debugging = shader_debugging
        self.debug_pixel_calls: list[tuple[int, int, object]] = []
        self.debug_thread_calls: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
        self.continue_debug_batches: list[list[object]] = []
        self.freed_traces: list[object] = []
        self.block_invoke_count = 0

    def GetStructuredFile(self):
        return object()

    def GetAPIProperties(self):
        return SimpleNamespace(pipelineType=self.api_name, shaderDebugging=self.shader_debugging)

    def GetPipelineState(self):
        return self.state

    def DebugPixel(self, x, y, inputs):
        self.debug_pixel_calls.append((x, y, inputs))
        return getattr(self, "trace", None)

    def DebugThread(self, group_id, thread_id):
        self.debug_thread_calls.append((tuple(group_id), tuple(thread_id)))
        return getattr(self, "trace", None)

    def ContinueDebug(self, debugger):
        if self.continue_debug_batches:
            return self.continue_debug_batches.pop(0)
        return []

    def FreeTrace(self, trace):
        self.freed_traces.append(trace)


class FakeDebugPixelInputs:
    def __init__(self) -> None:
        self.sample = None
        self.primitive = None
        self.view = None


class FakeUInt32DebugPixelInputs:
    def __init__(self) -> None:
        self._sample = None
        self._primitive = None
        self._view = None

    @property
    def sample(self):
        return self._sample

    @sample.setter
    def sample(self, value):
        if int(value) < 0:
            raise OverflowError("sample must be uint32")
        self._sample = int(value)

    @property
    def primitive(self):
        return self._primitive

    @primitive.setter
    def primitive(self, value):
        if int(value) < 0:
            raise OverflowError("primitive must be uint32")
        self._primitive = int(value)

    @property
    def view(self):
        return self._view

    @view.setter
    def view(self, value):
        if int(value) < 0:
            raise OverflowError("view must be uint32")
        self._view = int(value)


class _EnumGraphicsAPI(Enum):
    D3D12 = 1


class _EnumTopology(Enum):
    Unknown = 0


def _shader_variable(name: str, values: list[float], type_name: str = "Float") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        type=type_name,
        rows=1,
        columns=len(values),
        members=[],
        value=SimpleNamespace(
            f16v=list(values),
            f32v=list(values),
            f64v=list(values),
            s8v=[int(item) for item in values],
            s16v=[int(item) for item in values],
            s32v=[int(item) for item in values],
            s64v=[int(item) for item in values],
            u8v=[int(item) for item in values],
            u16v=[int(item) for item in values],
            u32v=[int(item) for item in values],
            u64v=[int(item) for item in values],
        ),
    )


def test_qrenderdoc_bridge_records_renderdoc_version_from_hello() -> None:
    bridge = QRenderDocBridge(timeout_seconds=1.0)

    bridge._accept_hello(
        {"type": "hello", "token": "token", "protocol_version": 1, "renderdoc_version": "1.43"},
        "token",
    )

    assert bridge.renderdoc_version == "1.43"


def test_qrenderdoc_bridge_rejects_protocol_mismatch() -> None:
    bridge = QRenderDocBridge(timeout_seconds=1.0)

    with pytest.raises(ReplayFailureError):
        bridge._accept_hello(
            {"type": "hello", "token": "token", "protocol_version": 999, "renderdoc_version": "1.43"},
            "token",
        )


def test_closed_bridge_is_not_retained_by_process_exit_callbacks() -> None:
    bridge = QRenderDocBridge(timeout_seconds=1.0)
    reference = weakref.ref(bridge)

    bridge.close()
    del bridge
    gc.collect()

    assert reference() is None


def test_qrenderdoc_bridge_timeout_does_not_shell_out_to_kill_external_processes(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self._terminated = False

        def poll(self):
            return 0 if self._terminated else None

        def terminate(self) -> None:
            self._terminated = True

        def wait(self, timeout=None) -> int:
            return 0

        def kill(self) -> None:
            self._terminated = True

    class FakeListenSocket:
        def setsockopt(self, *args) -> None:
            return None

        def bind(self, address) -> None:
            return None

        def listen(self, backlog) -> None:
            return None

        def settimeout(self, value) -> None:
            return None

        def getsockname(self):
            return ("127.0.0.1", 43210)

        def accept(self):
            raise TimeoutError()

        def close(self) -> None:
            return None

    monotonic_values = iter([0.0, 0.0, 1.0, 1.0, 1.0, 2.0])
    spawned: list[FakeProcess] = []
    subprocess_run_calls: list[list[str]] = []

    monkeypatch.setattr(bridge_module, "resolve_qrenderdoc_path", lambda: Path(r"C:\RenderDoc\qrenderdoc.exe"))
    monkeypatch.setattr(bridge_module.socket, "socket", lambda *args, **kwargs: FakeListenSocket())
    monkeypatch.setattr(bridge_module.subprocess, "Popen", lambda *args, **kwargs: spawned.append(FakeProcess()) or spawned[-1])
    monkeypatch.setattr(
        bridge_module.subprocess,
        "run",
        lambda args, **kwargs: subprocess_run_calls.append(list(args)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(bridge_module.time, "sleep", lambda seconds: None)

    bridge = QRenderDocBridge(timeout_seconds=1.0)

    with pytest.raises(BridgeHandshakeTimeoutError):
        bridge.ensure_started()

    assert len(spawned) == 2
    assert all(process.poll() is not None for process in spawned)
    assert subprocess_run_calls == []


def test_bridge_client_pipeline_overview_gracefully_handles_missing_descriptor_access() -> None:
    client = BridgeClient(FakeContext(FakeController()))

    response = client._get_pipeline_overview(7)

    assert response["pipeline"]["available"] is True
    assert response["pipeline"]["counts"]["descriptor_accesses"] == 0
    assert response["pipeline"]["graphics_pipeline_object"] == ""


def test_enum_name_uses_python_enum_names_for_native_renderdoc_bindings() -> None:
    assert serialization_module._enum_name(_EnumGraphicsAPI.D3D12) == "D3D12"
    assert serialization_module._enum_name(_EnumTopology.Unknown) == "Unknown"


def test_frame_action_serialization_memoizes_repeated_resource_names() -> None:
    class CountingContext:
        def __init__(self) -> None:
            self.resource_name_calls = 0

        def GetResourceName(self, resource_id):
            self.resource_name_calls += 1
            return "SharedTexture"

    def action(event_id, children=None):
        node = SimpleNamespace(
            eventId=event_id,
            actionId=event_id,
            customName="",
            flags=0,
            children=list(children or []),
            numIndices=3,
            numInstances=1,
            dispatchDimension=[0, 0, 0],
            dispatchThreadsDimension=[0, 0, 0],
            outputs=["tex-1"],
            copySource=None,
            copySourceSubresource=None,
            copyDestination=None,
            copyDestinationSubresource=None,
            depthOut="tex-1",
            parent=None,
            GetName=lambda structured_file: "Draw",
            IsFakeMarker=lambda: False,
        )
        for child in node.children:
            child.parent = node
        return node

    context = CountingContext()
    root = action(1, [action(2)])

    serialization_module._serialize_action_analysis_node(context, root, object(), {})

    assert context.resource_name_calls == 1


def test_bridge_client_pipeline_overview_uses_enum_names_for_api_and_topology() -> None:
    class EnumState(FakeState):
        def GetPrimitiveTopology(self):
            return _EnumTopology.Unknown

    class EnumController(FakeController):
        def __init__(self) -> None:
            super().__init__(api_name=_EnumGraphicsAPI.D3D12, state=EnumState())

        def GetD3D12PipelineState(self):
            return SimpleNamespace(
                pipelineResourceId="pipe-1",
                descriptorHeaps=[],
                rootSignature=SimpleNamespace(resourceId=None, parameters=[], staticSamplers=[]),
            )

    client = BridgeClient(FakeContext(EnumController()))

    response = client._get_pipeline_overview(7)

    assert response["api"] == "D3D12"
    assert response["pipeline"]["topology"] == "Unknown"
    assert response["pipeline"]["api_details_available"] is True
    assert response["pipeline"]["api_details_api"] == "D3D12"


def test_bridge_client_pipeline_bindings_degrades_when_accessor_signature_changes() -> None:
    class BrokenController(FakeController):
        def GetD3D12PipelineState(self):
            raise TypeError("signature changed")

    client = BridgeClient(FakeContext(BrokenController(api_name="D3D12")))

    response = client._list_pipeline_bindings(7, "api_details", 0, 50)

    assert response["items"][0]["available"] is False
    assert "compatible D3D12 pipeline accessor" in response["items"][0]["reason"]


def test_pipeline_overview_combines_generic_and_api_state_and_reuses_cache() -> None:
    class CachedController(FakeController):
        def __init__(self) -> None:
            super().__init__(api_name="D3D12")
            self.api_pipeline_calls = 0

        def GetD3D12PipelineState(self):
            self.api_pipeline_calls += 1
            return SimpleNamespace(
                pipelineResourceId="pipe-1",
                descriptorHeaps=[],
                rootSignature=SimpleNamespace(resourceId=None, parameters=[], staticSamplers=[]),
            )

    controller = CachedController()
    context = FakeContext(controller)
    client = BridgeClient(context)

    first = client._get_pipeline_overview(7)
    second = client._get_pipeline_overview(7)

    assert first == second
    assert controller.block_invoke_count == 1
    assert controller.api_pipeline_calls == 1
    assert len(context.set_event_calls) == 1


def test_non_api_pipeline_bindings_skip_api_specific_pipeline_serialization() -> None:
    class CountingController(FakeController):
        def __init__(self) -> None:
            super().__init__(api_name="D3D12")
            self.api_pipeline_calls = 0

        def GetD3D12PipelineState(self):
            self.api_pipeline_calls += 1
            return None

    controller = CountingController()
    client = BridgeClient(FakeContext(controller))

    response = client._list_pipeline_bindings(7, "output_targets", 0, 50)

    assert response["binding_kind"] == "output_targets"
    assert controller.block_invoke_count == 1
    assert controller.api_pipeline_calls == 0


def test_capture_overview_checks_timing_support_without_fetching_counters(monkeypatch) -> None:
    event_counter = object()

    class TimingController(FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.fetch_counter_calls = 0

        def EnumerateCounters(self):
            return [event_counter]

        def DescribeCounter(self, counter):
            assert counter is event_counter
            return SimpleNamespace(resultType="Double", resultByteWidth=8)

        def FetchCounters(self, counters):
            self.fetch_counter_calls += 1
            assert counters == [event_counter]
            return []

    controller = TimingController()
    client = BridgeClient(FakeContext(controller))
    monkeypatch.setattr(
        bridge_client_module,
        "rd",
        SimpleNamespace(GPUCounter=SimpleNamespace(EventGPUDuration=event_counter)),
    )
    monkeypatch.setattr(
        client,
        "_get_capture_summary",
        lambda: {"capture": {}, "api": "D3D12", "frame": {}, "statistics": {}, "resource_counts": {}},
    )
    monkeypatch.setattr(client, "_ensure_frame_analysis", lambda: {"root_pass_ids": [], "root_action_ids": []})

    overview = client._get_capture_overview()

    assert overview["capabilities"]["timing_data"] is True
    assert overview["capabilities"]["timing_data_collected"] is False
    assert controller.fetch_counter_calls == 0
    assert controller.block_invoke_count == 1

    timing = client._ensure_timing_data()

    assert timing["timing_available"] is True
    assert controller.fetch_counter_calls == 1
    assert controller.block_invoke_count == 2


def test_bridge_client_flattens_semantic_shader_bindings_with_stage_context() -> None:
    client = BridgeClient(FakeContext(FakeController()))
    pipeline = {
        "shaders": [
            {
                "stage": "Pixel",
                "shader_id": "shader-1",
                "shader_name": "MainPS",
                "read_only_resources": [
                    {"access": {"index": 3}, "descriptor": {"resource_id": "tex-1", "resource_name": "SceneColor"}}
                ],
            }
        ]
    }

    items = client._pipeline_binding_items(pipeline, {}, "read_only_resources")

    assert items[0]["stage"] == "Pixel"
    assert items[0]["shader_name"] == "MainPS"
    assert items[0]["descriptor"]["resource_id"] == "tex-1"


def test_resource_binding_search_reports_actual_and_bounded_return_counts(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    analysis = {"action_index": {event_id: {"flags": ["draw"]} for event_id in range(7, 12)}}
    bindings = [
        {
            "stage": "Pixel",
            "access": {"type": "ReadOnlyResource", "index": index},
            "descriptor": {"resource_id": "tex-1"},
        }
        for index in range(40)
    ]
    monkeypatch.setattr(client, "_resource_usage_target", lambda resource_id: ("texture", {"resourceId": resource_id}))
    monkeypatch.setattr(client, "_ensure_frame_analysis", lambda: analysis)
    monkeypatch.setattr(client, "_get_pipeline_state", lambda event_id: {"pipeline": {}})
    monkeypatch.setattr(
        client,
        "_pipeline_binding_items",
        lambda pipeline, api_details, kind: bindings if kind == "read_only_resources" else [],
    )
    monkeypatch.setattr(client, "_compact_texture", lambda resource: {"resource_id": "tex-1"})
    monkeypatch.setattr(
        bridge_client_module.frame_analysis,
        "build_action_summary_result",
        lambda frame, event_id: {"action": {"event_id": event_id}},
    )

    result = client._search_resource_bindings("tex-1", None, None, None, 100, 100)

    assert result["matched_event_count"] == 5
    assert result["matched_binding_count"] == 200
    assert result["returned_event_count"] == 4
    assert result["returned_binding_count"] == 128
    assert result["matches"][0]["binding_count"] == 40
    assert result["matches"][0]["bindings_truncated"] is True
    assert result["meta"]["scan"]["matches_truncated"] is True


def test_resource_binding_search_batches_replay_and_caches_event_results(monkeypatch) -> None:
    access = SimpleNamespace(
        stage="Pixel",
        type="ReadOnlyResource",
        index=3,
        arrayElement=0,
        descriptorStore=None,
        byteOffset=0,
        byteSize=0,
        staticallyUnused=False,
    )
    descriptor = SimpleNamespace(
        type="Image",
        resource="tex-1",
        secondary=None,
        view=None,
        byteOffset=0,
        byteSize=0,
        elementByteSize=0,
        firstMip=0,
        numMips=1,
        firstSlice=0,
        numSlices=1,
        format=None,
    )
    used = SimpleNamespace(access=access, descriptor=descriptor)

    class BindingState(FakeState):
        def GetShader(self, stage):
            return "shader-1"

        def GetReadOnlyResources(self, stage, only_used=True):
            return [used]

        def GetReadWriteResources(self, stage, only_used=True):
            return []

        def GetConstantBlocks(self, stage, only_used=True):
            return []

    class BatchController(FakeController):
        def __init__(self) -> None:
            super().__init__(state=BindingState())
            self.set_frame_event_calls: list[tuple[int, bool]] = []

        def SetFrameEvent(self, event_id, force):
            self.set_frame_event_calls.append((int(event_id), bool(force)))

    controller = BatchController()
    client = BridgeClient(FakeContext(controller))
    analysis = {"action_index": {event_id: {"flags": ["draw"]} for event_id in range(7, 12)}}
    monkeypatch.setattr(bridge_client_module, "_shader_stage_values", lambda: ["Pixel"])
    monkeypatch.setattr(client, "_resource_usage_target", lambda resource_id: ("texture", {"resourceId": resource_id}))
    monkeypatch.setattr(client, "_ensure_frame_analysis", lambda: analysis)
    monkeypatch.setattr(client, "_compact_texture", lambda resource: {"resource_id": "tex-1"})
    monkeypatch.setattr(
        bridge_client_module.frame_analysis,
        "build_action_summary_result",
        lambda frame, event_id: {"action": {"event_id": event_id}},
    )

    first = client._search_resource_bindings("tex-1", None, None, None, 100, 100)
    first_block_invoke_count = controller.block_invoke_count
    second = client._search_resource_bindings("tex-1", None, None, None, 100, 100)

    assert first["matched_event_count"] == 5
    assert first["meta"]["scan"]["performance"]["mode"] == "batched_replay"
    assert controller.set_frame_event_calls == [(event_id, False) for event_id in range(7, 12)]
    assert first_block_invoke_count == 1
    assert controller.block_invoke_count == first_block_invoke_count
    assert second["meta"]["scan"]["performance"] == {
        "mode": "cache",
        "cache_hit_count": 5,
        "replayed_event_count": 0,
    }


def test_bridge_client_texture_recommendations_include_probe_tool() -> None:
    client = BridgeClient(FakeContext(FakeController()))

    recommendations = client._resource_recommendations({"kind": "texture", "resource_id": "tex-1"})

    assert any(item["tool"] == "renderdoc_probe_texture_regions" for item in recommendations)


def test_texture_grid_cache_avoids_repeated_pick_pixel_calls(monkeypatch) -> None:
    class Subresource:
        mip = 0
        slice = 0
        sample = 0

    class TextureController(FakeController):
        def __init__(self) -> None:
            super().__init__()
            self.pick_pixel_calls = 0

        def PickPixel(self, resource_id, x, y, subresource, comp_type):
            self.pick_pixel_calls += 1
            return SimpleNamespace(x=float(x), y=float(y), z=0.0, w=1.0)

    texture = SimpleNamespace(
        resourceId="tex-1",
        format=SimpleNamespace(compType="Float", compCount=4, compByteWidth=4, type="Regular"),
        dimension="Texture2D",
        type="Texture2D",
        width=8,
        height=8,
        depth=1,
        mips=1,
        arraysize=1,
        msSamp=1,
        byteSize=1024,
        creationFlags="ShaderRead",
    )
    controller = TextureController()
    client = BridgeClient(FakeContext(controller))
    monkeypatch.setattr(bridge_client_module, "rd", SimpleNamespace(Subresource=Subresource))
    monkeypatch.setattr(client, "_ensure_final_event", lambda: {})
    monkeypatch.setattr(client, "_find_texture_by_id", lambda texture_id: texture)
    monkeypatch.setattr(
        client,
        "_validate_texture_request",
        lambda *args, **kwargs: {"mip_width": 8, "mip_height": 8, "mip_depth": 1},
    )

    first = client._get_texture_data("tex-1", 0, 1, 2, 2, 2, 0, 0)
    second = client._get_texture_data("tex-1", 0, 1, 2, 2, 2, 0, 0)

    assert first["meta"]["pixel_grid_cache_hit"] is False
    assert second["meta"]["pixel_grid_cache_hit"] is True
    assert controller.pick_pixel_calls == 4


def test_bridge_client_probe_texture_regions_detects_single_active_block(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    captured = {}
    monkeypatch.setattr(client, "_ensure_capture_loaded", lambda: None)
    monkeypatch.setattr(
        client,
        "_probe_texture_pixel_grid",
        lambda texture_id, mip_level, array_slice, sample, x, y, width, height: captured.update(
            {
                "texture_id": texture_id,
                "mip_level": mip_level,
                "array_slice": array_slice,
                "sample": sample,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )
        or {
            "texture": {"resource_id": texture_id, "name": "SceneColor"},
            "query": {
                "texture_id": texture_id,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "mip_level": mip_level,
                "array_slice": array_slice,
                "sample": sample,
            },
            "pixels": [
                [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]],
                [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0]],
                [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0]],
                [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]],
            ],
        },
        raising=False,
    )

    response = client._probe_texture_regions("tex-1", 5, 6, 7, 8, 1, 2, 3, "any", 0.5, 1, 10, 3)

    assert response["summary"]["active_pixel_count"] == 4
    assert response["summary"]["scanned_pixel_count"] == 16
    assert response["regions"][0]["pixel_count"] == 4
    assert response["regions"][0]["bbox"] == {"min_x": 6, "min_y": 7, "max_x": 7, "max_y": 8}
    assert response["regions"][0]["representative_pixel"] == {"x": 6, "y": 7}
    assert response["recommended_pixels"][0] == {"x": 6, "y": 7}
    assert response["recommended_calls"][0]["tool"] == "renderdoc_trace_bad_pixel"
    assert captured == {
        "texture_id": "tex-1",
        "mip_level": 1,
        "array_slice": 2,
        "sample": 3,
        "x": 5,
        "y": 6,
        "width": 7,
        "height": 8,
    }


def test_probe_activity_grid_supports_non_finite_outlier_and_gradient_modes() -> None:
    pixels = [
        [[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0], [float("nan"), 0.0, 0.0, 1.0]],
        [[0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 1.0]],
    ]

    non_finite = bridge_client_module._probe_activity_grid(pixels, "nan_inf")
    outlier = bridge_client_module._probe_activity_grid(pixels, "local_outlier")
    gradient = bridge_client_module._probe_activity_grid(pixels, "gradient")

    assert non_finite[0][2] == 1.0
    assert non_finite[1][1] == 0.0
    assert outlier[1][1] > 0.5
    assert gradient[0][0] == 1.0


def test_probe_outlier_mode_preserves_negative_signal_and_defaults_to_64_square() -> None:
    pixels = [
        [[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0]],
        [[0.0, 0.0, 0.0, 1.0], [-1.0, -1.0, -1.0, 1.0], [0.0, 0.0, 0.0, 1.0]],
        [[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0]],
    ]
    client = BridgeClient(FakeContext(FakeController()))

    outlier = bridge_client_module._probe_activity_grid(pixels, "local_outlier")

    assert outlier[1][1] == pytest.approx(1.0)
    assert client._resolve_probe_dimensions(512, 512, 0, 0, None, None) == (64, 64)


def test_event_dossier_enforces_a_global_binding_budget(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    monkeypatch.setattr(client, "_ensure_frame_analysis", lambda: {})
    monkeypatch.setattr(
        bridge_client_module.frame_analysis,
        "build_action_summary_result",
        lambda analysis, event_id: {"action": {"event_id": event_id}},
    )
    monkeypatch.setattr(bridge_client_module.frame_analysis, "get_innermost_pass_for_event", lambda analysis, event_id: None)
    monkeypatch.setattr(client, "_get_pipeline_state", lambda event_id: {"pipeline": {"available": True}})
    monkeypatch.setattr(client, "_get_api_pipeline_state", lambda event_id: {})
    monkeypatch.setattr(
        client,
        "_pipeline_overview_from_payload",
        lambda event_id, pipeline, api_pipeline: {"api": "D3D12", "pipeline": {}},
    )
    monkeypatch.setattr(client, "_pipeline_binding_items", lambda pipeline, api_pipeline, kind: [{"index": i} for i in range(100)])

    result = client._get_event_dossier(
        7,
        ["read_only_resources", "read_write_resources", "constant_blocks"],
        100,
    )

    assert result["meta"]["returned_binding_count"] == 128
    assert [item["returned_count"] for item in result["bindings"].values()] == [100, 28, 0]
    assert result["meta"]["bindings_truncated"] is True


def test_event_dossier_batch_returns_unprocessed_ids_when_response_budget_is_reached(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    monkeypatch.setattr(client, "_get_event_dossier", lambda event_id, kinds, limit: {"text": "x" * 100_000})

    result = client._get_event_dossiers([1, 2, 3], ["shaders"], 20)

    assert result["processed_count"] == 1
    assert result["unprocessed_event_ids"] == [2, 3]
    assert result["meta"]["response_truncated"] is True


def test_bridge_client_pixel_modification_preserves_unknown_pixel_value_payloads() -> None:
    client = BridgeClient(FakeContext(FakeController()))
    modification = SimpleNamespace(
        eventId=7,
        preMod=SimpleNamespace(foo=1),
        shaderOut=SimpleNamespace(bar=2),
        postMod=SimpleNamespace(baz=3),
    )

    response = client._serialize_pixel_modification(modification, object())

    assert response["pre_mod"] == "namespace(foo=1)"
    assert response["shader_output"] == "namespace(bar=2)"
    assert response["post_mod"] == "namespace(baz=3)"


def test_bridge_client_get_texture_data_serializes_wrapped_pick_pixel_values(monkeypatch) -> None:
    controller = FakeController()
    client = BridgeClient(FakeContext(controller))
    texture = SimpleNamespace(resourceId="tex-1")

    monkeypatch.setattr(client, "_ensure_capture_loaded", lambda: None)
    monkeypatch.setattr(client, "_ensure_final_event", lambda: None)
    monkeypatch.setattr(client, "_find_texture_by_id", lambda texture_id: texture)
    monkeypatch.setattr(
        client,
        "_validate_texture_request",
        lambda texture, mip_level, array_slice, sample, x=None, y=None, width=None, height=None: {
            "mip_width": 16,
            "mip_height": 16,
            "mip_depth": 1,
        },
    )
    monkeypatch.setattr(client, "_default_comp_type", lambda texture: 0)
    monkeypatch.setattr(bridge_client_module, "_subresource", lambda mip_level, array_slice, sample: object())
    monkeypatch.setattr(
        bridge_client_module,
        "_serialize_texture",
        lambda ctx, texture: {"resource_id": str(texture.resourceId)},
    )
    monkeypatch.setattr(
        controller,
        "PickPixel",
        lambda resource_id, x, y, subresource, comp_type: SimpleNamespace(
            floatValue=SimpleNamespace(r=1.0, g=0.5, b=0.25, a=1.0)
        ),
        raising=False,
    )

    response = client._get_texture_data("tex-1", 0, 2, 3, 1, 1, 0, 0)

    assert response["pixels"] == [[[1.0, 0.5, 0.25, 1.0]]]


def test_bridge_client_probe_texture_pixel_grid_serializes_rgba_pick_pixel_values(monkeypatch) -> None:
    controller = FakeController()
    client = BridgeClient(FakeContext(controller))
    texture = SimpleNamespace(resourceId="tex-1")

    monkeypatch.setattr(client, "_ensure_capture_loaded", lambda: None)
    monkeypatch.setattr(client, "_ensure_final_event", lambda: None)
    monkeypatch.setattr(client, "_find_texture_by_id", lambda texture_id: texture)
    monkeypatch.setattr(
        client,
        "_validate_texture_request",
        lambda texture, mip_level, array_slice, sample, x=None, y=None, width=None, height=None: {
            "mip_width": 16,
            "mip_height": 16,
            "mip_depth": 1,
        },
    )
    monkeypatch.setattr(client, "_resolve_probe_dimensions", lambda mip_width, mip_height, x, y, width, height: (1, 1))
    monkeypatch.setattr(client, "_default_comp_type", lambda texture: 0)
    monkeypatch.setattr(bridge_client_module, "_subresource", lambda mip_level, array_slice, sample: object())
    monkeypatch.setattr(
        bridge_client_module,
        "_serialize_texture",
        lambda ctx, texture: {"resource_id": str(texture.resourceId)},
    )
    monkeypatch.setattr(
        controller,
        "PickPixel",
        lambda resource_id, x, y, subresource, comp_type: SimpleNamespace(r=0.1, g=0.2, b=0.3, a=0.4),
        raising=False,
    )

    response = client._probe_texture_pixel_grid("tex-1", 0, 0, 0, 5, 6, 1, 1)

    assert response["pixels"] == [[[0.1, 0.2, 0.3, 0.4]]]


def test_bridge_client_shader_summary_returns_unavailable_when_disassembly_targets_missing(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController(state=FakeState(shader_bound=True))))
    monkeypatch.setattr(bridge_client_module, "_shader_stage_from_name", lambda stage_name: "Pixel")
    monkeypatch.setattr(bridge_client_module, "_shader_stage_values", lambda: ["Pixel"])

    response = client._get_shader_summary(7, "pixel")

    assert response["shader"]["stage"] == "Pixel"
    assert response["disassembly"]["available"] is False
    assert "did not report any shader disassembly targets" in response["disassembly"]["reason"]


def test_bridge_client_shader_code_chunk_pages_cached_disassembly(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController(state=FakeState(shader_bound=True))))
    monkeypatch.setattr(
        client,
        "_get_shader_code",
        lambda event_id, stage_name, target: {
            "event_id": event_id,
            "api": "D3D12",
            "action": {"event_id": event_id},
            "shader": {"stage": "Pixel", "shader_id": "shader-1", "shader_name": "MainPS"},
            "disassembly": {
                "available": True,
                "reason": "",
                "target": "dxil",
                "available_targets": ["dxil"],
                "text": "line1\nline2\nline3",
            },
        },
    )

    response = client._get_shader_code_chunk(7, "Pixel", None, 2, 2)

    assert response["start_line"] == 2
    assert response["returned_line_count"] == 2
    assert response["has_more"] is False
    assert response["text"] == "line2\nline3"


def test_bridge_client_searches_complete_cached_shader_disassembly(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController(state=FakeState(shader_bound=True))))
    monkeypatch.setattr(
        client,
        "_get_shader_code",
        lambda event_id, stage_name, target: {
            "event_id": event_id,
            "api": "D3D12",
            "action": {"event_id": event_id},
            "shader": {"stage": "Pixel", "shader_id": "shader-1", "shader_name": "MainPS"},
            "disassembly": {
                "available": True,
                "reason": "",
                "target": "dxil",
                "available_targets": ["dxil"],
                "text": "load r0\nadd r1, r0\nstore r1\nadd r2, r1",
            },
        },
    )

    response = client._search_shader_code(7, "Pixel", None, r"add r[12]", True, False, 1, 0, 25)

    assert response["total_match_count"] == 2
    assert [item["line_number"] for item in response["matches"]] == [2, 4]
    assert "load r0" in response["matches"][0]["context_text"]


def test_shader_search_response_budget_preserves_a_resumable_cursor(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController(state=FakeState(shader_bound=True))))
    monkeypatch.setattr(bridge_client_module, "MAX_SHADER_SEARCH_RESPONSE_BYTES", 500)
    monkeypatch.setattr(
        client,
        "_get_shader_code",
        lambda event_id, stage_name, target: {
            "event_id": event_id,
            "api": "D3D12",
            "action": {"event_id": event_id},
            "shader": {"stage": "Pixel", "shader_id": "shader-1", "shader_name": "MainPS"},
            "disassembly": {
                "available": True,
                "reason": "",
                "target": "dxil",
                "available_targets": ["dxil"],
                "text": "\n".join("match " + ("x" * 300) for _ in range(4)),
            },
        },
    )

    response = client._search_shader_code(7, "Pixel", None, "match", False, False, 0, 0, 25)

    assert len(response["matches"]) == 1
    assert response["meta"]["page"]["next_cursor"] == "1"
    assert response["meta"]["response_truncated"] is True


def test_bridge_client_detects_shader_debugging_support() -> None:
    client = BridgeClient(FakeContext(FakeController(shader_debugging=True)))

    assert client._controller_shader_debugging_supported(client.ctx.controller) is True


def test_bridge_client_trace_bad_pixel_handles_empty_history(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    monkeypatch.setattr(
        client,
        "_pixel_history_payload",
        lambda texture_id, x, y, mip_level, array_slice, sample: {
            "query": {"texture_id": texture_id, "x": x, "y": y, "mip_level": mip_level, "array_slice": array_slice, "sample": sample},
            "texture": {"resource_id": texture_id, "name": "SceneColor"},
            "usage_event_count": 0,
            "modifications": [],
        },
    )

    response = client._trace_bad_pixel("tex-1", 4, 5, 0, 0, 0)

    assert response["conclusion"]["category"] == "no_modifications"
    assert response["primary_event"] is None
    assert response["visible_source_event"] is None
    assert response["related_ids"]["primary_event_id"] is None
    assert response["shader_debug"]["reason"] == "no_final_writer"
    assert response["recommended_calls"][0]["tool"] == "renderdoc_get_pixel_history"
    probe_call = next(item for item in response["recommended_calls"] if item["tool"] == "renderdoc_probe_texture_regions")
    assert probe_call["arguments"] == {
        "texture_id": "tex-1",
        "x": 4,
        "y": 5,
        "mip_level": 0,
        "array_slice": 0,
        "sample": 0,
    }


def test_bridge_client_trace_bad_pixel_uses_latest_successful_writer(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    monkeypatch.setattr(
        client,
        "_pixel_history_payload",
        lambda texture_id, x, y, mip_level, array_slice, sample: {
            "query": {"texture_id": texture_id, "x": x, "y": y, "mip_level": mip_level, "array_slice": array_slice, "sample": sample},
            "texture": {"resource_id": texture_id, "name": "SceneColor"},
            "usage_event_count": 2,
            "modifications": [
                {"event_id": 20, "action": {"name": "BasePass"}, "passed": True, "failed_tests": []},
            ],
        },
    )
    monkeypatch.setattr(client, "_ensure_frame_analysis", lambda: {})
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_action_summary",
        lambda event_id: {"event_id": event_id, "name": "BasePass", "flags": ["draw"], "resource_usage_summary": {"output_count": 1}},
    )
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_pass_summary",
        lambda analysis, event_id: {"pass_id": "pass:20-20", "name": "BasePass"},
    )
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_pipeline_payload",
        lambda event_id: {"available": True, "reason": "", "event_id": event_id},
    )
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_shader_debug",
        lambda event_id, action_summary, texture_id, x, y, sample: {
            "used": False,
            "attempted": False,
            "reason": "not_supported",
            "event_id": event_id,
        },
    )

    response = client._trace_bad_pixel("tex-1", 4, 5, 0, 0, 0)

    assert response["conclusion"]["category"] == "final_writer"
    assert response["primary_event"]["event_id"] == 20
    assert response["visible_source_event"]["event_id"] == 20
    assert response["related_ids"]["primary_pass_id"] == "pass:20-20"
    assert response["recommended_calls"][-1]["tool"] == "renderdoc_start_pixel_shader_debug"
    assert response["recommended_calls"][-1]["arguments"]["event_id"] == 20


def test_bridge_client_trace_bad_pixel_prefers_earlier_visible_writer_when_latest_attempt_failed(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    shader_debug_calls = []
    monkeypatch.setattr(
        client,
        "_pixel_history_payload",
        lambda texture_id, x, y, mip_level, array_slice, sample: {
            "query": {"texture_id": texture_id, "x": x, "y": y, "mip_level": mip_level, "array_slice": array_slice, "sample": sample},
            "texture": {"resource_id": texture_id, "name": "SceneColor"},
            "usage_event_count": 2,
            "modifications": [
                {"event_id": 10, "action": {"name": "BasePass"}, "passed": True, "failed_tests": []},
                {
                    "event_id": 20,
                    "action": {"name": "OverlayPass"},
                    "passed": False,
                    "failed_tests": ["depth_test_failed"],
                },
            ],
        },
    )
    monkeypatch.setattr(client, "_ensure_frame_analysis", lambda: {})
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_action_summary",
        lambda event_id: {
            "event_id": event_id,
            "name": "BasePass" if event_id == 10 else "OverlayPass",
            "flags": ["draw"],
            "resource_usage_summary": {"output_count": 1},
        },
    )
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_pass_summary",
        lambda analysis, event_id: {"pass_id": "pass:{0}-{0}".format(event_id), "name": "Pass {0}".format(event_id)},
    )
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_pipeline_payload",
        lambda event_id: {"available": True, "reason": "", "event_id": event_id},
    )
    monkeypatch.setattr(
        client,
        "_trace_bad_pixel_shader_debug",
        lambda event_id, action_summary, texture_id, x, y, sample: shader_debug_calls.append(event_id)
        or {"used": False, "attempted": False, "reason": "not_supported", "event_id": event_id},
    )

    response = client._trace_bad_pixel("tex-1", 4, 5, 0, 0, 0)

    assert response["conclusion"]["category"] == "blocked_write"
    assert response["primary_event"]["event_id"] == 20
    assert response["visible_source_event"]["event_id"] == 10
    assert response["history_summary"]["final_writer_event_id"] == 10
    assert shader_debug_calls == [10]


def test_bridge_client_trace_bad_pixel_shader_debug_returns_first_state_and_closes_session(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    ended = {}
    monkeypatch.setattr(client, "_capture_shader_debugging_supported", lambda: True)
    monkeypatch.setattr(
        client,
        "_start_pixel_shader_debug",
        lambda event_id, x, y, texture_id, sample, primitive_id, view, state_limit: {
            "shader_debug_id": "debug-1",
            "shader": {"stage": "Pixel", "shader_id": "shader-1"},
            "target": {"texture_id": texture_id, "validated": True},
            "trace_summary": {"instruction_count": 3},
            "states": [
                {
                    "step_index": 0,
                    "next_instruction": 1,
                    "flags": [],
                    "line_info": {"line_start": 12},
                    "source_variable_names": ["color"],
                    "change_count": 1,
                    "has_callstack": False,
                }
            ],
        },
    )
    monkeypatch.setattr(client, "_end_shader_debug", lambda shader_debug_id: ended.setdefault("shader_debug_id", shader_debug_id))

    response = client._trace_bad_pixel_shader_debug(7, {"flags": ["draw"]}, "tex-1", 4, 5, 0)

    assert response["used"] is True
    assert response["first_state"]["step_index"] == 0
    assert response["target"]["validated"] is True
    assert ended["shader_debug_id"] == "debug-1"


def test_bridge_client_trace_bad_pixel_shader_debug_skips_when_shader_debugging_is_unavailable(monkeypatch) -> None:
    client = BridgeClient(FakeContext(FakeController()))
    monkeypatch.setattr(client, "_capture_shader_debugging_supported", lambda: False)

    response = client._trace_bad_pixel_shader_debug(7, {"flags": ["draw"]}, "tex-1", 4, 5, 0)

    assert response["used"] is False
    assert response["attempted"] is False
    assert response["reason"] == "not_supported"


def test_bridge_client_start_pixel_shader_debug_requires_draw_event(monkeypatch) -> None:
    controller = FakeController(state=FakeState(shader_bound=True), shader_debugging=True)
    client = BridgeClient(FakeContext(controller))

    monkeypatch.setattr(bridge_client_module, "rd", SimpleNamespace(DebugPixelInputs=FakeDebugPixelInputs, NoPreference=-1))
    monkeypatch.setattr(bridge_client_module, "_shader_stage_from_name", lambda stage_name: "Pixel")
    monkeypatch.setattr(bridge_client_module, "_action_flags", lambda action: ["dispatch"])

    with pytest.raises(bridge_client_module.BridgeError) as exc_info:
        client._start_pixel_shader_debug(7, 4, 5, None, None, None, None, 2)

    assert exc_info.value.code == "shader_debug_requires_draw_event"


def test_bridge_client_pixel_shader_debug_sessions_buffer_continue_states(monkeypatch) -> None:
    controller = FakeController(state=FakeState(shader_bound=True), shader_debugging=True)
    trace = SimpleNamespace(
        debugger=object(),
        stage="Pixel",
        inputs=[_shader_variable("input0", [1.0])],
        constantBlocks=[],
        readOnlyResources=[],
        readWriteResources=[],
        samplers=[],
        sourceVars=[SimpleNamespace(name="color")],
        instInfo=[
            SimpleNamespace(
                instruction=0,
                lineInfo=SimpleNamespace(fileIndex=0, lineStart=12, lineEnd=12, colStart=1, colEnd=8, disassemblyLine=1),
                sourceVars=[SimpleNamespace(name="color")],
            ),
            SimpleNamespace(
                instruction=1,
                lineInfo=SimpleNamespace(fileIndex=0, lineStart=13, lineEnd=13, colStart=1, colEnd=8, disassemblyLine=2),
                sourceVars=[SimpleNamespace(name="outputColor")],
            ),
            SimpleNamespace(
                instruction=2,
                lineInfo=SimpleNamespace(fileIndex=0, lineStart=14, lineEnd=14, colStart=1, colEnd=8, disassemblyLine=3),
                sourceVars=[SimpleNamespace(name="outputColor")],
            ),
        ],
    )
    state0 = SimpleNamespace(
        stepIndex=0,
        nextInstruction=0,
        flags="ShaderEvents.None",
        changes=[SimpleNamespace(before=_shader_variable("color", [0.0, 0.0, 0.0, 1.0]), after=_shader_variable("color", [1.0, 0.0, 0.0, 1.0]))],
        callstack=["main"],
    )
    state1 = SimpleNamespace(
        stepIndex=1,
        nextInstruction=1,
        flags="ShaderEvents.SampleLoadGather",
        changes=[],
        callstack=[],
    )
    state2 = SimpleNamespace(
        stepIndex=2,
        nextInstruction=2,
        flags="ShaderEvents.None",
        changes=[],
        callstack=[],
    )
    controller.trace = trace
    controller.continue_debug_batches = [[state0, state1], [state2], []]

    client = BridgeClient(FakeContext(controller))
    monkeypatch.setattr(bridge_client_module, "rd", SimpleNamespace(DebugPixelInputs=FakeDebugPixelInputs, NoPreference=-1))
    monkeypatch.setattr(bridge_client_module, "_shader_stage_from_name", lambda stage_name: "Pixel")
    monkeypatch.setattr(bridge_client_module, "_action_flags", lambda action: ["draw"])

    started = client._start_pixel_shader_debug(7, 4, 5, "tex-1", None, None, None, 1)

    assert started["shader"]["stage"] == "Pixel"
    assert started["target"]["validated"] is True
    assert started["returned_state_count"] == 1
    assert started["states"][0]["step_index"] == 0
    assert started["meta"]["completed"] is False
    assert started["meta"]["has_more"] is True
    assert controller.debug_pixel_calls[0][0:2] == (4, 5)
    assert controller.debug_pixel_calls[0][2].sample == 0xFFFFFFFF
    assert set(client.shader_debug_sessions[started["shader_debug_id"]]["instruction_info_by_index"]) == {0, 1, 2}
    monkeypatch.setattr(
        client,
        "_build_instruction_info_index",
        lambda trace: pytest.fail("instruction metadata should be indexed once per debug session"),
    )

    continued = client._continue_shader_debug(started["shader_debug_id"], 1)
    assert continued["returned_state_count"] == 1
    assert continued["states"][0]["step_index"] == 1
    assert continued["meta"]["has_more"] is True

    continued_again = client._continue_shader_debug(started["shader_debug_id"], 2)
    assert continued_again["returned_state_count"] == 1
    assert continued_again["states"][0]["step_index"] == 2
    assert continued_again["meta"]["completed"] is True
    assert continued_again["meta"]["has_more"] is False

    step = client._get_shader_debug_step(started["shader_debug_id"], 0, 10)
    assert step["step_index"] == 0
    assert step["returned_change_count"] == 1
    assert step["changes"][0]["name"] == "color"
    assert step["changes"][0]["before_value"] == [0.0, 0.0, 0.0, 1.0]
    assert step["changes"][0]["after_value"] == [1.0, 0.0, 0.0, 1.0]

    analysis = client._analyze_shader_debug(started["shader_debug_id"], 4096, 32)
    assert analysis["analysis"]["analyzed_state_count"] == 3
    assert analysis["analysis"]["changed_variables"][0] == {"name": "color", "change_step_count": 1}
    assert analysis["analysis"]["flag_counts"]["SampleLoadGather"] == 1
    assert analysis["meta"]["completed"] is True

    closed = client._end_shader_debug(started["shader_debug_id"])
    assert closed["closed"] is True
    assert controller.freed_traces == [trace]


def test_bridge_client_pixel_shader_debug_converts_no_preference_to_uint32(monkeypatch) -> None:
    controller = FakeController(state=FakeState(shader_bound=True), shader_debugging=True)
    controller.trace = SimpleNamespace(
        debugger=object(),
        stage="Pixel",
        inputs=[],
        constantBlocks=[],
        readOnlyResources=[],
        readWriteResources=[],
        samplers=[],
        sourceVars=[],
        instInfo=[],
    )
    controller.continue_debug_batches = [[]]

    client = BridgeClient(FakeContext(controller))
    monkeypatch.setattr(bridge_client_module, "rd", SimpleNamespace(DebugPixelInputs=FakeUInt32DebugPixelInputs, NoPreference=-1))
    monkeypatch.setattr(bridge_client_module, "_shader_stage_from_name", lambda stage_name: "Pixel")
    monkeypatch.setattr(bridge_client_module, "_action_flags", lambda action: ["draw"])

    started = client._start_pixel_shader_debug(7, 4, 5, "tex-1", None, None, None, 1)

    assert started["returned_state_count"] == 0
    assert started["meta"]["completed"] is True
    assert controller.debug_pixel_calls[0][2].sample == 0xFFFFFFFF
    assert controller.debug_pixel_calls[0][2].primitive == 0xFFFFFFFF
    assert controller.debug_pixel_calls[0][2].view == 0xFFFFFFFF


def test_bridge_client_compute_shader_debug_sessions_buffer_continue_states(monkeypatch) -> None:
    controller = FakeController(state=FakeState(shader_bound=True), shader_debugging=True)
    trace = SimpleNamespace(
        debugger=object(),
        stage="Compute",
        inputs=[_shader_variable("dispatchThreadId", [3937, 2096, 0], "UInt")],
        constantBlocks=[],
        readOnlyResources=[],
        readWriteResources=[],
        samplers=[],
        sourceVars=[SimpleNamespace(name="result")],
        instInfo=[
            SimpleNamespace(
                instruction=0,
                lineInfo=SimpleNamespace(fileIndex=0, lineStart=42, lineEnd=42, colStart=1, colEnd=12, disassemblyLine=1),
                sourceVars=[SimpleNamespace(name="result")],
            )
        ],
    )
    state0 = SimpleNamespace(
        stepIndex=0,
        nextInstruction=0,
        flags="ShaderEvents.None",
        changes=[
            SimpleNamespace(
                before=_shader_variable("result", [0.0, 0.0, 0.0, 1.0]),
                after=_shader_variable("result", [1.0, 0.5, 0.25, 1.0]),
            )
        ],
        callstack=["CSMain"],
    )
    controller.trace = trace
    controller.continue_debug_batches = [[state0], []]

    client = BridgeClient(FakeContext(controller))
    monkeypatch.setattr(bridge_client_module, "_shader_stage_from_name", lambda stage_name: "Compute")
    monkeypatch.setattr(bridge_client_module, "_action_flags", lambda action: ["dispatch"])

    started = client._start_compute_shader_debug(8659, [492, 262, 0], [1, 0, 0], 1)

    assert started["shader"]["stage"] == "Compute"
    assert started["target"] == {"group_id": [492, 262, 0], "thread_id": [1, 0, 0]}
    assert started["states"][0]["step_index"] == 0
    assert controller.debug_thread_calls == [((492, 262, 0), (1, 0, 0))]

    step = client._get_shader_debug_step(started["shader_debug_id"], 0, 10)
    assert step["shader"]["stage"] == "Compute"
    assert step["changes"][0]["name"] == "result"

    closed = client._end_shader_debug(started["shader_debug_id"])
    assert closed["closed"] is True
    assert controller.freed_traces == [trace]


def test_bridge_client_get_buffer_data_uses_checked_block_invoke(monkeypatch) -> None:
    controller = FakeController()
    client = BridgeClient(FakeContext(controller))
    buffer_desc = SimpleNamespace(resourceId="buf-1", length=4)
    used_checked_invoke = {"used": False}

    monkeypatch.setattr(client, "_ensure_capture_loaded", lambda: None)
    monkeypatch.setattr(client, "_ensure_final_event", lambda: None)
    monkeypatch.setattr(client, "_find_buffer_by_id", lambda buffer_id: buffer_desc)
    monkeypatch.setattr(
        client,
        "_compact_buffer",
        lambda desc: {
            "kind": "buffer",
            "resource_id": str(desc.resourceId),
            "name": "Buffer",
            "byte_size": int(desc.length),
            "usage_flags": "NoFlags",
        },
    )
    monkeypatch.setattr(
        controller,
        "GetBufferData",
        lambda resource_id, offset, size: b"\x00\x01\x7f\xff",
        raising=False,
    )

    def fake_block_invoke_checked(callback):
        used_checked_invoke["used"] = True
        callback(controller)

    monkeypatch.setattr(client, "_block_invoke_checked", fake_block_invoke_checked)

    payload = BridgeClient._get_buffer_data(client, "buf-1", 0, 4, "hex")

    assert used_checked_invoke["used"] is True
    assert payload["data"] == "00 01 7f ff"


def test_bridge_client_shader_debug_step_requires_cached_history(monkeypatch) -> None:
    controller = FakeController(state=FakeState(shader_bound=True), shader_debugging=True)
    client = BridgeClient(FakeContext(controller))
    client.shader_debug_sessions["debug-1"] = {
        "shader_debug_id": "debug-1",
        "event_id": 7,
        "api": "D3D12",
        "action": {"event_id": 7},
        "shader": {"stage": "Pixel"},
        "target": {"texture_id": "", "validated": False, "slot_kind": "", "slot_index": -1},
        "trace": SimpleNamespace(instInfo=[]),
        "history": [],
        "history_by_step": {},
        "pending_states": [],
        "completed": False,
    }

    with pytest.raises(bridge_client_module.BridgeError) as exc_info:
        client._get_shader_debug_step("debug-1", 3, 10)

    assert exc_info.value.code == "shader_debug_trace_unavailable"


def test_bridge_client_clear_analysis_cache_releases_shader_debug_sessions() -> None:
    controller = FakeController(shader_debugging=True)
    trace = object()
    client = BridgeClient(FakeContext(controller))
    client.analysis_cache.store("analysis", {"frame": 1})
    client.timing_capability_cache.store("timing-capability", {"supported": True})
    client.shader_debug_capability_cache.store("shader-capability", {"supported": True})
    client.timing_cache.store("timing", {"event": 7})
    client.resource_catalog_cache.store("resources", ["tex-1"])
    client.shader_code_cache["shader-1"] = {"source": "main"}
    client.pipeline_cache[7] = {"pipeline": {}}
    client.resource_binding_cache[(7, "tex-1")] = {"bindings": []}
    client.texture_grid_cache[("tex-1", 0, 0, 0, 0, 2, 2)] = {
        "response": {"pixels": []},
        "pixel_count": 4,
    }
    client.texture_grid_cache_pixels = 4
    client.shader_debug_sessions["debug-1"] = {
        "shader_debug_id": "debug-1",
        "trace": trace,
        "pending_states": [],
        "history": [],
        "history_by_step": {},
        "completed": True,
    }

    client._clear_analysis_cache()

    assert client.analysis_cache.get("analysis") is None
    assert client.timing_capability_cache.get("timing-capability") is None
    assert client.shader_debug_capability_cache.get("shader-capability") is None
    assert client.timing_cache.get("timing") is None
    assert client.resource_catalog_cache.get("resources") is None
    assert client.shader_code_cache == {}
    assert client.pipeline_cache == {}
    assert client.resource_binding_cache == {}
    assert client.texture_grid_cache == {}
    assert client.texture_grid_cache_pixels == 0
    assert client.shader_debug_sessions == {}
    assert controller.freed_traces == [trace]
