from __future__ import annotations

from pathlib import Path
from typing import Any

from renderdoc_mcp.analysis.frame_analysis import MAX_PAGE_LIMIT, MAX_TIMING_EVENT_PAGE_LIMIT, RESOURCE_USAGE_KINDS
from renderdoc_mcp.application.command_specs import GetResourceSummaryCommand
from renderdoc_mcp.application.context import ApplicationContext
from renderdoc_mcp.application.response import attach_capture, ensure_meta
from renderdoc_mcp.errors import ReplayFailureError

SUPPORTED_RESOURCE_KINDS = {"all", "textures", "buffers"}
SUPPORTED_RESOURCE_USAGE_KINDS = {"all", *RESOURCE_USAGE_KINDS}
SUPPORTED_RESOURCE_SORT_OPTIONS = {"name", "size"}
SUPPORTED_BUFFER_ENCODINGS = {"hex", "base64"}
SUPPORTED_TEXTURE_PROBE_CHANNEL_MODES = {"luma", "max_rgb", "alpha", "any"}
DEFAULT_RESOURCE_PAGE_LIMIT = 50
DEFAULT_PIXEL_HISTORY_LIMIT = 100
DEFAULT_BUFFER_READ_SIZE = 256
MAX_BUFFER_READ_SIZE = 4096
MAX_TEXTURE_PREVIEW_DIMENSION = 64
MAX_TEXTURE_PREVIEW_PIXELS = 1024
MAX_TEXTURE_PROBE_DIMENSION = 128
MAX_TEXTURE_PROBE_PIXELS = 16384
DEFAULT_TEXTURE_PROBE_THRESHOLD = 0.05
DEFAULT_TEXTURE_PROBE_MIN_REGION_PIXELS = 4
DEFAULT_TEXTURE_PROBE_MAX_REGIONS = 10
DEFAULT_TEXTURE_PROBE_MAX_CANDIDATE_PIXELS = 5
MAX_TEXTURE_PROBE_MAX_REGIONS = 32
MAX_TEXTURE_PROBE_MAX_CANDIDATE_PIXELS = 16
DEFAULT_SHADER_DEBUG_STATE_LIMIT = 32
MAX_SHADER_DEBUG_STATE_LIMIT = 128
DEFAULT_SHADER_DEBUG_CHANGE_LIMIT = 64
MAX_SHADER_DEBUG_CHANGE_LIMIT = 256
SUPPORTED_TEXTURE_EXPORT_TYPES = {
    ".dds": "DDS",
    ".hdr": "HDR",
    ".jpeg": "JPG",
    ".jpg": "JPG",
    ".png": "PNG",
}


class ResourceHandlers:
    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def renderdoc_list_resources(
        self,
        capture_id: str,
        kind: str = "all",
        cursor: int | str | None = None,
        limit: int | str | None = None,
        name_filter: str | None = None,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        normalized_kind = self.context.normalize_optional_string(kind) or "all"
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")
        normalized_name_filter = self.context.normalize_optional_string(name_filter)
        normalized_sort_by = (self.context.normalize_optional_string(sort_by) or "name").lower()

        if normalized_kind not in SUPPORTED_RESOURCE_KINDS:
            raise ReplayFailureError(
                "kind must be one of 'all', 'textures', or 'buffers'.",
                {"kind": normalized_kind},
            )
        if normalized_sort_by not in SUPPORTED_RESOURCE_SORT_OPTIONS:
            raise ReplayFailureError(
                "sort_by must be one of name or size.",
                {"sort_by": normalized_sort_by},
            )
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_PAGE_LIMIT)

        params: dict[str, Any] = {
            "kind": normalized_kind,
            "limit": normalized_limit or DEFAULT_RESOURCE_PAGE_LIMIT,
            "sort_by": normalized_sort_by,
        }
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor
        if normalized_name_filter:
            params["name_filter"] = normalized_name_filter

        session, result = self.context.capture_tool(capture_id, "list_resources", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_resource_summary(self, capture_id: str, resource_id: str) -> dict[str, Any]:
        command = GetResourceSummaryCommand.from_raw(self.context.normalizer, capture_id, resource_id)
        session, result = self.context.sessions.capture_tool_normalized(
            command.capture_id,
            "get_resource_summary",
            {"resource_id": command.resource_id},
        )
        return attach_capture(ensure_meta(result), session)

    def renderdoc_list_resource_usages(
        self,
        capture_id: str,
        resource_id: str,
        usage_kind: str = "all",
        cursor: int | str | None = None,
        limit: int | str | None = None,
    ) -> dict[str, Any]:
        normalized_resource_id = self.context.normalize_required_string(resource_id, "resource_id")
        normalized_usage_kind = (self.context.normalize_optional_string(usage_kind) or "all").lower()
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")

        if normalized_usage_kind not in SUPPORTED_RESOURCE_USAGE_KINDS:
            raise ReplayFailureError(
                "usage_kind must be one of {}.".format(", ".join(sorted(SUPPORTED_RESOURCE_USAGE_KINDS))),
                {"usage_kind": normalized_usage_kind},
            )
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_PAGE_LIMIT)

        params: dict[str, Any] = {
            "resource_id": normalized_resource_id,
            "usage_kind": normalized_usage_kind,
            "limit": normalized_limit or DEFAULT_RESOURCE_PAGE_LIMIT,
        }
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor

        session, result = self.context.capture_tool(capture_id, "list_resource_usages", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_pixel_history(
        self,
        capture_id: str,
        texture_id: str,
        x: int,
        y: int,
        mip_level: int | None = 0,
        array_slice: int | None = 0,
        sample: int | None = 0,
        cursor: int | str | None = None,
        limit: int | str | None = None,
    ) -> dict[str, Any]:
        params = self._normalize_pixel_params(texture_id, x, y, mip_level, array_slice, sample)
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")
        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_TIMING_EVENT_PAGE_LIMIT)
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor
        params["limit"] = normalized_limit or DEFAULT_PIXEL_HISTORY_LIMIT
        session, result = self.context.capture_tool(capture_id, "get_pixel_history", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_debug_pixel(
        self,
        capture_id: str,
        texture_id: str,
        x: int,
        y: int,
        mip_level: int | None = 0,
        array_slice: int | None = 0,
        sample: int | None = 0,
    ) -> dict[str, Any]:
        params = self._normalize_pixel_params(texture_id, x, y, mip_level, array_slice, sample)
        session, result = self.context.capture_tool(capture_id, "debug_pixel", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_trace_bad_pixel(
        self,
        capture_id: str,
        texture_id: str,
        x: int,
        y: int,
        mip_level: int | None = 0,
        array_slice: int | None = 0,
        sample: int | None = 0,
    ) -> dict[str, Any]:
        params = self._normalize_pixel_params(texture_id, x, y, mip_level, array_slice, sample)
        session, result = self.context.capture_tool(capture_id, "trace_bad_pixel", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_probe_texture_regions(
        self,
        capture_id: str,
        texture_id: str,
        x: int | str | None = 0,
        y: int | str | None = 0,
        width: int | str | None = None,
        height: int | str | None = None,
        mip_level: int | str | None = 0,
        array_slice: int | str | None = 0,
        sample: int | str | None = 0,
        channel_mode: str | None = "luma",
        threshold: float | str | None = None,
        min_region_pixels: int | str | None = None,
        max_regions: int | str | None = None,
        max_candidate_pixels_per_region: int | str | None = None,
    ) -> dict[str, Any]:
        normalized_channel_mode = (self.context.normalize_optional_string(channel_mode) or "luma").lower()
        normalized_threshold = self.context.normalize_optional_float(threshold, "threshold")
        normalized_width = self.context.normalize_optional_int(width, "width")
        normalized_height = self.context.normalize_optional_int(height, "height")
        normalized_min_region_pixels = self.context.normalize_optional_int(min_region_pixels, "min_region_pixels")
        normalized_max_regions = self.context.normalize_optional_int(max_regions, "max_regions")
        normalized_max_candidate_pixels = self.context.normalize_optional_int(
            max_candidate_pixels_per_region,
            "max_candidate_pixels_per_region",
        )

        if normalized_channel_mode not in SUPPORTED_TEXTURE_PROBE_CHANNEL_MODES:
            raise ReplayFailureError(
                "channel_mode must be one of alpha, any, luma, or max_rgb.",
                {"channel_mode": normalized_channel_mode},
            )
        if normalized_threshold is not None and (normalized_threshold < 0.0 or normalized_threshold > 1.0):
            raise ReplayFailureError(
                "threshold must be between 0.0 and 1.0.",
                {"threshold": normalized_threshold},
            )
        if normalized_width is not None and (
            normalized_width <= 0 or normalized_width > MAX_TEXTURE_PROBE_DIMENSION
        ):
            raise ReplayFailureError(
                "width must be between 1 and {}.".format(MAX_TEXTURE_PROBE_DIMENSION),
                {"width": normalized_width},
            )
        if normalized_height is not None and (
            normalized_height <= 0 or normalized_height > MAX_TEXTURE_PROBE_DIMENSION
        ):
            raise ReplayFailureError(
                "height must be between 1 and {}.".format(MAX_TEXTURE_PROBE_DIMENSION),
                {"height": normalized_height},
            )
        if normalized_width is not None and normalized_height is not None:
            if normalized_width * normalized_height > MAX_TEXTURE_PROBE_PIXELS:
                raise ReplayFailureError(
                    "width * height must be less than or equal to {}.".format(MAX_TEXTURE_PROBE_PIXELS),
                    {"width": normalized_width, "height": normalized_height},
                )
        if normalized_min_region_pixels is not None and normalized_min_region_pixels <= 0:
            raise ReplayFailureError(
                "min_region_pixels must be greater than 0.",
                {"min_region_pixels": normalized_min_region_pixels},
            )
        if normalized_max_regions is not None and (
            normalized_max_regions <= 0 or normalized_max_regions > MAX_TEXTURE_PROBE_MAX_REGIONS
        ):
            raise ReplayFailureError(
                "max_regions must be between 1 and {}.".format(MAX_TEXTURE_PROBE_MAX_REGIONS),
                {"max_regions": normalized_max_regions},
            )
        if normalized_max_candidate_pixels is not None and (
            normalized_max_candidate_pixels <= 0
            or normalized_max_candidate_pixels > MAX_TEXTURE_PROBE_MAX_CANDIDATE_PIXELS
        ):
            raise ReplayFailureError(
                "max_candidate_pixels_per_region must be between 1 and {}.".format(
                    MAX_TEXTURE_PROBE_MAX_CANDIDATE_PIXELS
                ),
                {"max_candidate_pixels_per_region": normalized_max_candidate_pixels},
            )

        params = self._normalize_pixel_params(texture_id, x, y, mip_level, array_slice, sample)
        params.update(
            {
                "channel_mode": normalized_channel_mode,
                "threshold": normalized_threshold if normalized_threshold is not None else DEFAULT_TEXTURE_PROBE_THRESHOLD,
                "min_region_pixels": normalized_min_region_pixels or DEFAULT_TEXTURE_PROBE_MIN_REGION_PIXELS,
                "max_regions": normalized_max_regions or DEFAULT_TEXTURE_PROBE_MAX_REGIONS,
                "max_candidate_pixels_per_region": normalized_max_candidate_pixels
                or DEFAULT_TEXTURE_PROBE_MAX_CANDIDATE_PIXELS,
            }
        )
        if normalized_width is not None:
            params["width"] = normalized_width
        if normalized_height is not None:
            params["height"] = normalized_height

        session, result = self.context.capture_tool(capture_id, "probe_texture_regions", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_start_pixel_shader_debug(
        self,
        capture_id: str,
        event_id: int,
        x: int,
        y: int,
        texture_id: str | None = None,
        sample: int | str | None = None,
        primitive_id: int | str | None = None,
        view: int | str | None = None,
        state_limit: int | str | None = None,
    ) -> dict[str, Any]:
        params = {
            "event_id": self.context.normalize_required_int(event_id, "event_id"),
            "x": self.context.normalize_non_negative_int(x, "x"),
            "y": self.context.normalize_non_negative_int(y, "y"),
            "state_limit": self._normalize_state_limit(state_limit),
        }
        normalized_texture_id = self.context.normalize_optional_string(texture_id)
        if normalized_texture_id:
            params["texture_id"] = normalized_texture_id

        normalized_sample = self._normalize_optional_non_negative_int(sample, "sample")
        normalized_primitive_id = self._normalize_optional_non_negative_int(primitive_id, "primitive_id")
        normalized_view = self._normalize_optional_non_negative_int(view, "view")
        if normalized_sample is not None:
            params["sample"] = normalized_sample
        if normalized_primitive_id is not None:
            params["primitive_id"] = normalized_primitive_id
        if normalized_view is not None:
            params["view"] = normalized_view

        session, result = self.context.capture_tool(capture_id, "start_pixel_shader_debug", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_start_compute_shader_debug(
        self,
        capture_id: str,
        event_id: int,
        group_x: int,
        group_y: int,
        group_z: int = 0,
        thread_x: int = 0,
        thread_y: int = 0,
        thread_z: int = 0,
        state_limit: int | str | None = None,
    ) -> dict[str, Any]:
        params = {
            "event_id": self.context.normalize_required_int(event_id, "event_id"),
            "group_id": [
                self.context.normalize_non_negative_int(group_x, "group_x"),
                self.context.normalize_non_negative_int(group_y, "group_y"),
                self.context.normalize_non_negative_int(group_z, "group_z"),
            ],
            "thread_id": [
                self.context.normalize_non_negative_int(thread_x, "thread_x"),
                self.context.normalize_non_negative_int(thread_y, "thread_y"),
                self.context.normalize_non_negative_int(thread_z, "thread_z"),
            ],
            "state_limit": self._normalize_state_limit(state_limit),
        }

        session, result = self.context.capture_tool(capture_id, "start_compute_shader_debug", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_continue_shader_debug(
        self,
        capture_id: str,
        shader_debug_id: str,
        state_limit: int | str | None = None,
    ) -> dict[str, Any]:
        params = {
            "shader_debug_id": self.context.normalize_required_string(shader_debug_id, "shader_debug_id"),
            "state_limit": self._normalize_state_limit(state_limit),
        }
        session, result = self.context.capture_tool(capture_id, "continue_shader_debug", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_shader_debug_step(
        self,
        capture_id: str,
        shader_debug_id: str,
        step_index: int,
        change_limit: int | str | None = None,
    ) -> dict[str, Any]:
        params = {
            "shader_debug_id": self.context.normalize_required_string(shader_debug_id, "shader_debug_id"),
            "step_index": self.context.normalize_non_negative_int(step_index, "step_index"),
            "change_limit": self._normalize_change_limit(change_limit),
        }
        session, result = self.context.capture_tool(capture_id, "get_shader_debug_step", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_end_shader_debug(self, capture_id: str, shader_debug_id: str) -> dict[str, Any]:
        params = {"shader_debug_id": self.context.normalize_required_string(shader_debug_id, "shader_debug_id")}
        session, result = self.context.capture_tool(capture_id, "end_shader_debug", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_texture_data(
        self,
        capture_id: str,
        texture_id: str,
        mip_level: int,
        x: int,
        y: int,
        width: int,
        height: int,
        array_slice: int = 0,
        sample: int = 0,
    ) -> dict[str, Any]:
        params = {
            "texture_id": self.context.normalize_required_string(texture_id, "texture_id"),
            "mip_level": self.context.normalize_non_negative_int(mip_level, "mip_level"),
            "x": self.context.normalize_non_negative_int(x, "x"),
            "y": self.context.normalize_non_negative_int(y, "y"),
            "width": self.context.normalize_positive_int(width, "width"),
            "height": self.context.normalize_positive_int(height, "height"),
            "array_slice": self.context.normalize_non_negative_int(array_slice, "array_slice"),
            "sample": self.context.normalize_non_negative_int(sample, "sample"),
        }

        if params["width"] > MAX_TEXTURE_PREVIEW_DIMENSION:
            raise ReplayFailureError(
                f"width must be less than or equal to {MAX_TEXTURE_PREVIEW_DIMENSION}.",
                {"width": params["width"]},
            )
        if params["height"] > MAX_TEXTURE_PREVIEW_DIMENSION:
            raise ReplayFailureError(
                f"height must be less than or equal to {MAX_TEXTURE_PREVIEW_DIMENSION}.",
                {"height": params["height"]},
            )
        if params["width"] * params["height"] > MAX_TEXTURE_PREVIEW_PIXELS:
            raise ReplayFailureError(
                f"width * height must be less than or equal to {MAX_TEXTURE_PREVIEW_PIXELS}.",
                {"width": params["width"], "height": params["height"]},
            )

        session, result = self.context.capture_tool(capture_id, "get_texture_data", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_buffer_data(
        self,
        capture_id: str,
        buffer_id: str,
        offset: int | str | None = 0,
        size: int | str | None = None,
        encoding: str | None = None,
    ) -> dict[str, Any]:
        normalized_size = self.context.normalize_optional_int(size, "size")
        normalized_encoding = (self.context.normalize_optional_string(encoding) or "hex").lower()
        params = {
            "buffer_id": self.context.normalize_required_string(buffer_id, "buffer_id"),
            "offset": self.context.normalize_non_negative_int(offset, "offset"),
            "size": normalized_size or DEFAULT_BUFFER_READ_SIZE,
            "encoding": normalized_encoding,
        }

        if params["size"] > MAX_BUFFER_READ_SIZE:
            raise ReplayFailureError(
                f"size must be less than or equal to {MAX_BUFFER_READ_SIZE}.",
                {"size": params["size"]},
            )
        if normalized_encoding not in SUPPORTED_BUFFER_ENCODINGS:
            raise ReplayFailureError(
                "encoding must be one of hex or base64.",
                {"encoding": normalized_encoding},
            )

        session, result = self.context.capture_tool(capture_id, "get_buffer_data", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_save_texture_to_file(
        self,
        capture_id: str,
        texture_id: str,
        output_path: str,
        mip_level: int = 0,
        array_slice: int = 0,
    ) -> dict[str, Any]:
        normalized_output_path = self.context.normalize_required_string(output_path, "output_path")
        params = {
            "texture_id": self.context.normalize_required_string(texture_id, "texture_id"),
            "output_path": normalized_output_path,
            "mip_level": self.context.normalize_non_negative_int(mip_level, "mip_level"),
            "array_slice": self.context.normalize_non_negative_int(array_slice, "array_slice"),
        }

        extension = Path(normalized_output_path).suffix.lower()
        if extension not in SUPPORTED_TEXTURE_EXPORT_TYPES:
            raise ReplayFailureError(
                "output_path must end in one of: .dds, .hdr, .jpeg, .jpg, .png.",
                {"output_path": normalized_output_path},
            )

        session, result = self.context.capture_tool(capture_id, "save_texture_to_file", params)
        return attach_capture(ensure_meta(result), session)

    def _normalize_pixel_params(
        self,
        texture_id: str,
        x: int,
        y: int,
        mip_level: int | None,
        array_slice: int | None,
        sample: int | None,
    ) -> dict[str, Any]:
        return {
            "texture_id": self.context.normalize_required_string(texture_id, "texture_id"),
            "x": self.context.normalize_non_negative_int(x, "x"),
            "y": self.context.normalize_non_negative_int(y, "y"),
            "mip_level": self.context.normalize_non_negative_int(mip_level, "mip_level"),
            "array_slice": self.context.normalize_non_negative_int(array_slice, "array_slice"),
            "sample": self.context.normalize_non_negative_int(sample, "sample"),
        }

    def _normalize_optional_non_negative_int(self, value: Any, field_name: str) -> int | None:
        normalized = self.context.normalize_optional_int(value, field_name)
        if normalized is None:
            return None
        if normalized < 0:
            raise ReplayFailureError(f"{field_name} must be greater than or equal to 0.", {field_name: normalized})
        return normalized

    def _normalize_state_limit(self, value: Any) -> int:
        normalized = self.context.normalize_optional_int(value, "state_limit") or DEFAULT_SHADER_DEBUG_STATE_LIMIT
        if normalized <= 0 or normalized > MAX_SHADER_DEBUG_STATE_LIMIT:
            raise ReplayFailureError(
                "state_limit must be between 1 and {}.".format(MAX_SHADER_DEBUG_STATE_LIMIT),
                {"state_limit": normalized},
            )
        return normalized

    def _normalize_change_limit(self, value: Any) -> int:
        normalized = self.context.normalize_optional_int(value, "change_limit") or DEFAULT_SHADER_DEBUG_CHANGE_LIMIT
        if normalized <= 0 or normalized > MAX_SHADER_DEBUG_CHANGE_LIMIT:
            raise ReplayFailureError(
                "change_limit must be between 1 and {}.".format(MAX_SHADER_DEBUG_CHANGE_LIMIT),
                {"change_limit": normalized},
            )
        return normalized
