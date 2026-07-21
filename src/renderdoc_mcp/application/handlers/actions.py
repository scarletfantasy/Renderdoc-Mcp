from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from renderdoc_mcp.analysis.frame_analysis import DEFAULT_ACTION_PAGE_LIMIT, MAX_PAGE_LIMIT
from renderdoc_mcp.application.command_specs import ListActionsCommand
from renderdoc_mcp.application.context import ApplicationContext
from renderdoc_mcp.application.response import attach_capture, ensure_meta
from renderdoc_mcp.application.schema_types import (
    CaptureId,
    Cursor,
    DossierBindingLimit,
    EventId,
    EventIdList,
    PageLimit,
    PipelineBindingKind,
    PipelineBindingKindList,
    ResourceId,
    SearchQuery,
    ShaderLine,
    ShaderLineCount,
    ShaderSearchContextLines,
    ShaderSearchLimit,
    ShaderStage,
)
from renderdoc_mcp.errors import ReplayFailureError

SUPPORTED_SHADER_STAGES = {
    "vertex": "Vertex",
    "vs": "Vertex",
    "hull": "Hull",
    "hs": "Hull",
    "domain": "Domain",
    "ds": "Domain",
    "geometry": "Geometry",
    "gs": "Geometry",
    "pixel": "Pixel",
    "fragment": "Pixel",
    "ps": "Pixel",
    "compute": "Compute",
    "cs": "Compute",
    "task": "Task",
    "amplification": "Task",
    "as": "Task",
    "mesh": "Mesh",
    "raygen": "RayGen",
    "raygeneration": "RayGen",
    "intersection": "Intersection",
    "anyhit": "AnyHit",
    "closesthit": "ClosestHit",
    "miss": "Miss",
    "callable": "Callable",
}
SUPPORTED_PIPELINE_BINDING_KINDS = {
    "descriptor_accesses",
    "vertex_buffers",
    "vertex_inputs",
    "output_targets",
    "shaders",
    "api_details",
    "read_only_resources",
    "read_write_resources",
    "samplers",
    "constant_blocks",
}
PIPELINE_BINDING_KIND_ALIASES = {
    "output": "output_targets",
    "outputs": "output_targets",
    "descriptor": "descriptor_accesses",
    "descriptors": "descriptor_accesses",
    "api": "api_details",
    "read_only_resource": "read_only_resources",
    "readonly_resources": "read_only_resources",
    "resources": "read_only_resources",
    "textures": "read_only_resources",
    "srvs": "read_only_resources",
    "srv": "read_only_resources",
    "read_write_resource": "read_write_resources",
    "write_resources": "read_write_resources",
    "uavs": "read_write_resources",
    "uav": "read_write_resources",
    "sampler": "samplers",
    "constant_block": "constant_blocks",
    "constant_buffers": "constant_blocks",
    "constant_buffer": "constant_blocks",
    "cbuffers": "constant_blocks",
    "cbvs": "constant_blocks",
    "buffers": "vertex_buffers",
}
DEFAULT_PIPELINE_BINDING_LIMIT = 50
DEFAULT_SHADER_LINE_COUNT = 200
MAX_SHADER_LINE_COUNT = 1000


def _normalize_shader_stage(stage: str | None) -> str | None:
    if stage is None:
        return None
    key = stage.strip().replace("_", "").replace("-", "").replace(" ", "").lower()
    return SUPPORTED_SHADER_STAGES.get(key)


def _normalize_binding_kind(binding_kind: str) -> str:
    key = binding_kind.strip().replace("-", "_").replace(" ", "_").lower()
    return PIPELINE_BINDING_KIND_ALIASES.get(key, key)


class ActionHandlers:
    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def renderdoc_list_actions(
        self,
        capture_id: CaptureId,
        parent_event_id: EventId | None = None,
        name_filter: str | None = None,
        flags_filter: str | None = None,
        cursor: Cursor | None = None,
        limit: PageLimit | None = None,
    ) -> dict[str, Any]:
        command = ListActionsCommand.from_raw(
            self.context.normalizer,
            capture_id=capture_id,
            parent_event_id=parent_event_id,
            name_filter=name_filter,
            flags_filter=flags_filter,
            cursor=cursor,
            limit=limit,
        )
        normalized_parent_event_id = command.parent_event_id
        normalized_name_filter = command.name_filter
        normalized_flags_filter = command.flags_filter
        normalized_cursor = command.cursor
        normalized_limit = command.limit

        if normalized_parent_event_id is not None and normalized_parent_event_id <= 0:
            raise ReplayFailureError(
                "parent_event_id must be greater than 0 when provided.",
                {"parent_event_id": normalized_parent_event_id},
            )
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_PAGE_LIMIT)

        params: dict[str, Any] = {"limit": normalized_limit or DEFAULT_ACTION_PAGE_LIMIT}
        if normalized_parent_event_id is not None:
            params["parent_event_id"] = normalized_parent_event_id
        if normalized_name_filter:
            params["name_filter"] = normalized_name_filter
        if normalized_flags_filter:
            params["flags_filter"] = normalized_flags_filter
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor

        session, result = self.context.sessions.capture_tool_normalized(command.capture_id, "list_actions", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_search_actions(
        self,
        capture_id: CaptureId,
        query: SearchQuery | None = None,
        flags_filter: str | None = None,
        parent_event_id: EventId | None = None,
        resource_id: ResourceId | None = None,
        event_id_min: EventId | None = None,
        event_id_max: EventId | None = None,
        cursor: Cursor | None = None,
        limit: PageLimit | None = None,
    ) -> dict[str, Any]:
        normalized_parent = self.context.normalize_optional_int(parent_event_id, "parent_event_id")
        normalized_min = self.context.normalize_optional_int(event_id_min, "event_id_min")
        normalized_max = self.context.normalize_optional_int(event_id_max, "event_id_max")
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")
        normalized_query = self.context.normalize_optional_string(query)
        if normalized_query is not None and len(normalized_query) > 500:
            raise ReplayFailureError("query must contain at most 500 characters.", {"query_length": len(normalized_query)})
        if normalized_min is not None and normalized_max is not None and normalized_min > normalized_max:
            raise ReplayFailureError(
                "event_id_min must be less than or equal to event_id_max.",
                {"event_id_min": normalized_min, "event_id_max": normalized_max},
            )
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_PAGE_LIMIT)
        params: dict[str, Any] = {"limit": normalized_limit or DEFAULT_ACTION_PAGE_LIMIT}
        optional_values = {
            "query": normalized_query,
            "flags_filter": self.context.normalize_optional_string(flags_filter),
            "resource_id": self.context.normalize_optional_string(resource_id),
            "parent_event_id": normalized_parent,
            "event_id_min": normalized_min,
            "event_id_max": normalized_max,
            "cursor": normalized_cursor,
        }
        params.update({key: value for key, value in optional_values.items() if value is not None})
        session, result = self.context.capture_tool(capture_id, "search_actions", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_action_summary(self, capture_id: CaptureId, event_id: EventId) -> dict[str, Any]:
        normalized_event_id = self.context.normalize_required_int(event_id, "event_id")
        session, result = self.context.capture_tool(capture_id, "get_action_summary", {"event_id": normalized_event_id})
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_pipeline_overview(self, capture_id: CaptureId, event_id: EventId) -> dict[str, Any]:
        normalized_event_id = self.context.normalize_required_int(event_id, "event_id")
        session, result = self.context.capture_tool(
            capture_id,
            "get_pipeline_overview",
            {"event_id": normalized_event_id},
        )
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_event_dossier(
        self,
        capture_id: CaptureId,
        event_id: EventId,
        binding_kinds: PipelineBindingKindList | None = None,
        binding_limit: DossierBindingLimit | None = None,
    ) -> dict[str, Any]:
        normalized_binding_limit = self.context.normalize_optional_int(binding_limit, "binding_limit") or 20
        if normalized_binding_limit <= 0 or normalized_binding_limit > 100:
            raise ReplayFailureError(
                "binding_limit must be between 1 and 100.",
                {"binding_limit": normalized_binding_limit},
            )
        params = {
            "event_id": self.context.normalize_required_int(event_id, "event_id"),
            "binding_kinds": self._normalize_binding_kinds(binding_kinds),
            "binding_limit": normalized_binding_limit,
        }
        session, result = self.context.capture_tool(capture_id, "get_event_dossier", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_event_dossiers(
        self,
        capture_id: CaptureId,
        event_ids: EventIdList,
        binding_kinds: PipelineBindingKindList | None = None,
        binding_limit: DossierBindingLimit | None = None,
    ) -> dict[str, Any]:
        normalized_event_ids = [self.context.normalize_required_int(value, "event_ids") for value in event_ids]
        if not normalized_event_ids or len(normalized_event_ids) > 32:
            raise ReplayFailureError(
                "event_ids must contain between 1 and 32 ids.",
                {"event_id_count": len(normalized_event_ids)},
            )
        normalized_binding_limit = self.context.normalize_optional_int(binding_limit, "binding_limit") or 20
        if normalized_binding_limit <= 0 or normalized_binding_limit > 100:
            raise ReplayFailureError(
                "binding_limit must be between 1 and 100.",
                {"binding_limit": normalized_binding_limit},
            )
        params = {
            "event_ids": normalized_event_ids,
            "binding_kinds": self._normalize_binding_kinds(binding_kinds),
            "binding_limit": normalized_binding_limit,
        }
        session, result = self.context.capture_tool(capture_id, "get_event_dossiers", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_list_pipeline_bindings(
        self,
        capture_id: CaptureId,
        event_id: EventId,
        binding_kind: PipelineBindingKind,
        cursor: Cursor | None = None,
        limit: PageLimit | None = None,
    ) -> dict[str, Any]:
        normalized_event_id = self.context.normalize_required_int(event_id, "event_id")
        normalized_binding_kind = _normalize_binding_kind(
            self.context.normalize_required_string(binding_kind, "binding_kind")
        )
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")

        if normalized_binding_kind not in SUPPORTED_PIPELINE_BINDING_KINDS:
            raise ReplayFailureError(
                "binding_kind must be one of {}.".format(", ".join(sorted(SUPPORTED_PIPELINE_BINDING_KINDS))),
                {
                    "binding_kind": normalized_binding_kind,
                    "supported_values": sorted(SUPPORTED_PIPELINE_BINDING_KINDS),
                    "aliases": dict(sorted(PIPELINE_BINDING_KIND_ALIASES.items())),
                },
            )
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_PAGE_LIMIT)

        params: dict[str, Any] = {
            "event_id": normalized_event_id,
            "binding_kind": normalized_binding_kind,
            "limit": normalized_limit or DEFAULT_PIPELINE_BINDING_LIMIT,
        }
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor

        session, result = self.context.capture_tool(capture_id, "list_pipeline_bindings", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_shader_summary(
        self,
        capture_id: CaptureId,
        event_id: EventId,
        stage: ShaderStage,
    ) -> dict[str, Any]:
        normalized_event_id = self.context.normalize_required_int(event_id, "event_id")
        normalized_stage = _normalize_shader_stage(self.context.normalize_optional_string(stage))

        if normalized_stage is None:
            raise ReplayFailureError(
                "stage must name a supported shader stage.",
                {"stage": stage, "supported_stages": sorted(set(SUPPORTED_SHADER_STAGES.values()))},
            )

        session, result = self.context.capture_tool(
            capture_id,
            "get_shader_summary",
            {"event_id": normalized_event_id, "stage": normalized_stage},
        )
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_shader_code_chunk(
        self,
        capture_id: CaptureId,
        event_id: EventId,
        stage: ShaderStage,
        target: str | None = None,
        start_line: ShaderLine | None = None,
        line_count: ShaderLineCount | None = None,
    ) -> dict[str, Any]:
        normalized_event_id = self.context.normalize_required_int(event_id, "event_id")
        normalized_stage = _normalize_shader_stage(self.context.normalize_optional_string(stage))
        normalized_target = self.context.normalize_optional_string(target)
        normalized_start_line = self.context.normalize_optional_int(start_line, "start_line")
        normalized_line_count = self.context.normalize_optional_int(line_count, "line_count")

        if normalized_stage is None:
            raise ReplayFailureError(
                "stage must name a supported shader stage.",
                {"stage": stage, "supported_stages": sorted(set(SUPPORTED_SHADER_STAGES.values()))},
            )
        if normalized_start_line is not None and normalized_start_line <= 0:
            raise ReplayFailureError(
                "start_line must be greater than 0.",
                {"start_line": normalized_start_line},
            )
        if normalized_line_count is not None and (
            normalized_line_count <= 0 or normalized_line_count > MAX_SHADER_LINE_COUNT
        ):
            raise ReplayFailureError(
                "line_count must be between 1 and {}.".format(MAX_SHADER_LINE_COUNT),
                {"line_count": normalized_line_count},
            )

        params: dict[str, Any] = {
            "event_id": normalized_event_id,
            "stage": normalized_stage,
            "start_line": normalized_start_line or 1,
            "line_count": normalized_line_count or DEFAULT_SHADER_LINE_COUNT,
        }
        if normalized_target:
            params["target"] = normalized_target

        session, result = self.context.capture_tool(capture_id, "get_shader_code_chunk", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_search_shader_code(
        self,
        capture_id: CaptureId,
        event_id: EventId,
        stage: ShaderStage,
        query: SearchQuery,
        target: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        context_lines: ShaderSearchContextLines = 2,
        cursor: Cursor | None = None,
        limit: ShaderSearchLimit | None = None,
    ) -> dict[str, Any]:
        normalized_stage = _normalize_shader_stage(self.context.normalize_optional_string(stage))
        if normalized_stage is None:
            raise ReplayFailureError(
                "stage must name a supported shader stage.",
                {"stage": stage, "supported_stages": sorted(set(SUPPORTED_SHADER_STAGES.values()))},
            )
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, 100)
        normalized_context_lines = self.context.normalize_non_negative_int(context_lines, "context_lines")
        if normalized_context_lines > 10:
            raise ReplayFailureError(
                "context_lines must be between 0 and 10.",
                {"context_lines": normalized_context_lines},
            )
        normalized_query = self.context.normalize_required_string(query, "query")
        if len(normalized_query) > 500:
            raise ReplayFailureError("query must contain at most 500 characters.", {"query_length": len(normalized_query)})
        params: dict[str, Any] = {
            "event_id": self.context.normalize_required_int(event_id, "event_id"),
            "stage": normalized_stage,
            "query": normalized_query,
            "regex": bool(self.context.normalize_optional_bool(regex, "regex")),
            "case_sensitive": bool(self.context.normalize_optional_bool(case_sensitive, "case_sensitive")),
            "context_lines": normalized_context_lines,
            "cursor": normalized_cursor or 0,
            "limit": normalized_limit or 25,
        }
        normalized_target = self.context.normalize_optional_string(target)
        if normalized_target:
            params["target"] = normalized_target
        session, result = self.context.capture_tool(capture_id, "search_shader_code", params)
        return attach_capture(ensure_meta(result), session)

    def _normalize_binding_kinds(self, binding_kinds: Sequence[str] | None) -> list[str]:
        if binding_kinds is not None and (not binding_kinds or len(binding_kinds) > 8):
            raise ReplayFailureError(
                "binding_kinds must contain between 1 and 8 values.",
                {"binding_kind_count": len(binding_kinds)},
            )
        values = binding_kinds or ["output_targets", "shaders"]
        normalized = [
            _normalize_binding_kind(self.context.normalize_required_string(item, "binding_kinds"))
            for item in values
        ]
        invalid = sorted({item for item in normalized if item not in SUPPORTED_PIPELINE_BINDING_KINDS})
        if invalid:
            raise ReplayFailureError(
                "binding_kinds contains unsupported values.",
                {
                    "invalid_values": invalid,
                    "supported_values": sorted(SUPPORTED_PIPELINE_BINDING_KINDS),
                    "aliases": dict(sorted(PIPELINE_BINDING_KIND_ALIASES.items())),
                },
            )
        return list(dict.fromkeys(normalized))
