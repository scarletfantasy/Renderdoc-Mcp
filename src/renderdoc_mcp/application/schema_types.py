from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

CaptureId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[0-9A-Fa-f]+$",
        description="Capture id returned by renderdoc_open_capture.",
    ),
]
CapturePath = Annotated[str, Field(min_length=1, description="Absolute or relative path to a local .rdc capture.")]
ResourceId = Annotated[str, Field(min_length=1)]
SearchQuery = Annotated[str, Field(min_length=1, max_length=500)]
ShaderDebugId = Annotated[str, Field(min_length=1)]
PassId = Annotated[str, Field(min_length=1)]
InvestigationId = Annotated[str, Field(min_length=1)]
InvestigationName = Annotated[str, Field(min_length=1, max_length=160)]
InvestigationLabel = Annotated[str, Field(min_length=1, max_length=80)]
EvidenceName = Annotated[str, Field(min_length=1, max_length=120)]
EvidenceReference = Annotated[str, Field(min_length=1, max_length=500)]
EvidenceSummary = Annotated[str, Field(max_length=2000)]

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
EventId = Annotated[int, Field(ge=1)]
Cursor = Annotated[int, Field(ge=0)]
PageLimit = Annotated[int, Field(ge=1, le=200)]
TimingPageLimit = Annotated[int, Field(ge=1, le=500)]
WorklistLimit = Annotated[int, Field(ge=1, le=50)]
ShaderLine = Annotated[int, Field(ge=1)]
ShaderLineCount = Annotated[int, Field(ge=1, le=1000)]
ShaderSearchContextLines = Annotated[int, Field(ge=0, le=10)]
ShaderSearchLimit = Annotated[int, Field(ge=1, le=100)]
ShaderDebugStateLimit = Annotated[int, Field(ge=1, le=128)]
ShaderDebugChangeLimit = Annotated[int, Field(ge=1, le=256)]
ShaderDebugAnalysisLimit = Annotated[int, Field(ge=1, le=8192)]
ShaderDebugInterestingLimit = Annotated[int, Field(ge=1, le=128)]
DossierBindingLimit = Annotated[int, Field(ge=1, le=100)]
BufferReadSize = Annotated[int, Field(ge=1, le=4096)]
ResourceBindingScanLimit = Annotated[int, Field(ge=1, le=500)]
ResourceBindingMatchLimit = Annotated[int, Field(ge=1, le=100)]
TexturePreviewDimension = Annotated[int, Field(ge=1, le=64)]
TextureProbeDimension = Annotated[int, Field(ge=1, le=128)]
TextureProbeThreshold = Annotated[float, Field(ge=0.0, le=1.0)]
TextureDiffThreshold = Annotated[float, Field(ge=0.0)]
DiffChangeLimit = Annotated[int, Field(ge=1, le=500)]
DiffPixelLimit = Annotated[int, Field(ge=1, le=100)]
TextureProbeRegionPixels = Annotated[int, Field(ge=1)]
TextureProbeRegionLimit = Annotated[int, Field(ge=1, le=32)]
TextureProbeCandidateLimit = Annotated[int, Field(ge=1, le=16)]

WorklistFocus = Literal["performance", "structure", "resources", "correctness"]
PassCategory = Literal[
    "setup",
    "copy_resolve",
    "shadow_depth",
    "depth_prepass",
    "geometry",
    "lighting",
    "transparency",
    "post_process",
    "ui_overlay",
    "presentation",
    "compute",
    "unknown",
]
PassSort = Literal["event_order", "event", "event_id", "gpu_time", "draw_calls", "dispatches", "name"]
TimingSort = Literal["event_order", "event", "event_id", "gpu_time"]
ResourceKind = Literal["all", "textures", "texture", "buffers", "buffer"]
ResourceSort = Literal["name", "size"]
ResourceUsageKind = Literal[
    "all",
    "color_output",
    "depth_output",
    "copy_source",
    "copy_destination",
    "resolve_source",
    "resolve_destination",
]
BufferEncoding = Literal["hex", "base64"]
TextureProbeChannel = Literal["luma", "max_rgb", "alpha", "any", "nan_inf", "local_outlier", "gradient"]
EvidenceKind = Literal["event", "pass", "resource", "texture", "shader_debug", "finding", "note"]

ShaderStage = Literal[
    "vertex",
    "Vertex",
    "vs",
    "hull",
    "Hull",
    "hs",
    "domain",
    "Domain",
    "ds",
    "geometry",
    "Geometry",
    "gs",
    "pixel",
    "Pixel",
    "fragment",
    "ps",
    "compute",
    "Compute",
    "cs",
    "task",
    "Task",
    "amplification",
    "as",
    "mesh",
    "Mesh",
    "raygen",
    "RayGen",
    "raygeneration",
    "intersection",
    "Intersection",
    "anyhit",
    "AnyHit",
    "closesthit",
    "ClosestHit",
    "miss",
    "Miss",
    "callable",
    "Callable",
]

PipelineBindingKind = Literal[
    "descriptor_accesses",
    "descriptor",
    "descriptors",
    "vertex_buffers",
    "vertex_inputs",
    "output_targets",
    "output",
    "outputs",
    "shaders",
    "api_details",
    "api",
    "read_only_resources",
    "read_only_resource",
    "readonly_resources",
    "resources",
    "textures",
    "srvs",
    "srv",
    "read_write_resources",
    "read_write_resource",
    "write_resources",
    "uavs",
    "uav",
    "samplers",
    "sampler",
    "constant_blocks",
    "constant_block",
    "constant_buffers",
    "constant_buffer",
    "cbuffers",
    "cbvs",
    "buffers",
]

EventIdList = Annotated[list[EventId], Field(min_length=1, max_length=32)]
PipelineBindingKindList = Annotated[list[PipelineBindingKind], Field(min_length=1, max_length=8)]
CaptureIdList = Annotated[list[CaptureId], Field(min_length=1, max_length=8)]
InvestigationLabelList = Annotated[list[InvestigationLabel], Field(min_length=1, max_length=8)]
