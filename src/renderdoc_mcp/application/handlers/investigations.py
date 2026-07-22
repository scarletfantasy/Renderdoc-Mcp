from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from renderdoc_mcp.application.compare import compare_event_dossiers
from renderdoc_mcp.application.context import ApplicationContext
from renderdoc_mcp.application.handlers.actions import (
    PIPELINE_BINDING_KIND_ALIASES,
    SUPPORTED_PIPELINE_BINDING_KINDS,
    _normalize_binding_kind,
)
from renderdoc_mcp.application.response import bridge_meta, runtime_meta
from renderdoc_mcp.application.schema_types import (
    CaptureId,
    CaptureIdList,
    DiffChangeLimit,
    DiffPixelLimit,
    EventId,
    EvidenceKind,
    EvidenceName,
    EvidenceReference,
    EvidenceSummary,
    InvestigationId,
    InvestigationLabel,
    InvestigationLabelList,
    InvestigationName,
    NonNegativeInt,
    PipelineBindingKindList,
    ResourceId,
    ShaderDebugId,
    TextureDiffThreshold,
    TexturePreviewDimension,
)
from renderdoc_mcp.errors import ReplayFailureError

MAX_INVESTIGATION_CAPTURES = 8
MAX_INVESTIGATION_EVIDENCE = 100


class InvestigationHandlers:
    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def renderdoc_create_investigation(
        self,
        name: InvestigationName | None = None,
        capture_ids: CaptureIdList | None = None,
        labels: InvestigationLabelList | None = None,
    ) -> dict[str, Any]:
        normalized_ids = [self.context.normalize_required_capture_id(value) for value in (capture_ids or [])]
        normalized_labels = [self.context.normalize_required_string(value, "labels") for value in (labels or [])]
        if len(normalized_ids) > MAX_INVESTIGATION_CAPTURES:
            raise ReplayFailureError(
                "An investigation can contain at most eight captures.",
                {"capture_id_count": len(normalized_ids)},
            )
        if normalized_labels and len(normalized_labels) != len(normalized_ids):
            raise ReplayFailureError(
                "labels must contain exactly one label for each capture_id.",
                {"capture_id_count": len(normalized_ids), "label_count": len(normalized_labels)},
            )
        if len(set(normalized_labels)) != len(normalized_labels):
            raise ReplayFailureError("labels must be unique.", {"labels": normalized_labels})
        sessions = [self.context.sessions.get_normalized_session(capture_id) for capture_id in normalized_ids]
        investigation = self.context.investigations.create(self.context.normalize_optional_string(name))
        for index, session in enumerate(sessions):
            label = normalized_labels[index] if normalized_labels else _next_capture_label(investigation["captures"])
            investigation = self.context.investigations.add_capture(
                investigation["investigation_id"],
                label,
                session.capture_id,
                session.capture_path,
            )
        return self._decorate_summary(investigation)

    def renderdoc_add_investigation_capture(
        self,
        investigation_id: InvestigationId,
        capture_id: CaptureId,
        label: InvestigationLabel | None = None,
    ) -> dict[str, Any]:
        normalized_investigation_id = self.context.normalize_required_string(investigation_id, "investigation_id")
        session = self.context.get_session(capture_id)
        existing = self.context.investigations.get(normalized_investigation_id)
        normalized_label = self.context.normalize_optional_string(label) or _next_capture_label(existing["captures"])
        existing_capture = existing["captures"].get(normalized_label)
        if existing_capture is not None and existing_capture["capture_id"] != session.capture_id:
            raise ReplayFailureError(
                "The investigation already contains a different capture under this label.",
                {"label": normalized_label, "capture_id": existing_capture["capture_id"]},
            )
        if existing_capture is None and len(existing["captures"]) >= MAX_INVESTIGATION_CAPTURES:
            raise ReplayFailureError("An investigation can contain at most eight captures.")
        result = self.context.investigations.add_capture(
            normalized_investigation_id,
            normalized_label,
            session.capture_id,
            session.capture_path,
        )
        return self._decorate_summary(result)

    def renderdoc_set_investigation_focus(
        self,
        investigation_id: InvestigationId,
        capture_id: CaptureId | None = None,
        event_id: EventId | None = None,
        resource_id: ResourceId | None = None,
        texture_id: ResourceId | None = None,
        x: NonNegativeInt | None = None,
        y: NonNegativeInt | None = None,
        shader_debug_id: ShaderDebugId | None = None,
    ) -> dict[str, Any]:
        normalized_investigation_id = self.context.normalize_required_string(investigation_id, "investigation_id")
        investigation = self.context.investigations.get(normalized_investigation_id)
        values: dict[str, Any] = {}
        focus_session = None
        normalized_capture_id = self.context.normalize_optional_string(capture_id)
        if normalized_capture_id:
            focus_session = self.context.get_session(normalized_capture_id)
            values["capture_id"] = focus_session.capture_id
        optional_strings = {"resource_id": resource_id, "texture_id": texture_id, "shader_debug_id": shader_debug_id}
        for key, value in optional_strings.items():
            normalized_string = self.context.normalize_optional_string(value)
            if normalized_string:
                values[key] = normalized_string
        optional_ints = {"event_id": event_id, "x": x, "y": y}
        for int_key, int_value in optional_ints.items():
            if int_value is not None:
                values[int_key] = self.context.normalize_non_negative_int(int_value, int_key)
        if not values:
            raise ReplayFailureError("At least one focus field must be supplied.", {"investigation_id": investigation_id})
        if focus_session is not None:
            attached_ids = {item["capture_id"] for item in investigation["captures"].values()}
            if focus_session.capture_id not in attached_ids:
                if len(investigation["captures"]) >= MAX_INVESTIGATION_CAPTURES:
                    raise ReplayFailureError("An investigation can contain at most eight captures.")
                self.context.investigations.add_capture(
                    normalized_investigation_id,
                    _next_capture_label(investigation["captures"]),
                    focus_session.capture_id,
                    focus_session.capture_path,
                )
        result = self.context.investigations.set_focus(
            normalized_investigation_id,
            values,
        )
        return self._decorate_summary(result)

    def renderdoc_pin_investigation_evidence(
        self,
        investigation_id: InvestigationId,
        name: EvidenceName,
        kind: EvidenceKind,
        reference_id: EvidenceReference,
        summary: EvidenceSummary = "",
        capture_id: CaptureId | None = None,
    ) -> dict[str, Any]:
        normalized_investigation_id = self.context.normalize_required_string(investigation_id, "investigation_id")
        normalized_name = self.context.normalize_required_string(name, "name")
        investigation = self.context.investigations.get(normalized_investigation_id)
        if normalized_name not in investigation["evidence"] and len(investigation["evidence"]) >= MAX_INVESTIGATION_EVIDENCE:
            raise ReplayFailureError(
                "An investigation can contain at most 100 pinned evidence items.",
                {"evidence_count": len(investigation["evidence"])},
            )
        evidence: dict[str, Any] = {
            "kind": self.context.normalize_required_string(kind, "kind"),
            "reference_id": self.context.normalize_required_string(reference_id, "reference_id"),
            "summary": self.context.normalize_optional_string(summary) or "",
        }
        normalized_capture_id = self.context.normalize_optional_string(capture_id)
        if normalized_capture_id:
            evidence["capture_id"] = self.context.get_session(normalized_capture_id).capture_id
        result = self.context.investigations.pin_evidence(
            normalized_investigation_id,
            normalized_name,
            evidence,
        )
        return self._decorate_summary(result)

    def renderdoc_get_investigation_summary(self, investigation_id: InvestigationId) -> dict[str, Any]:
        result = self.context.investigations.get(
            self.context.normalize_required_string(investigation_id, "investigation_id")
        )
        return self._decorate_summary(result)

    def renderdoc_list_investigations(self) -> dict[str, Any]:
        investigations = self.context.investigations.list()
        items = [
            {
                "investigation_id": item["investigation_id"],
                "name": item["name"],
                "updated_at_unix": item["updated_at_unix"],
                "capture_count": len(item["captures"]),
                "focus": dict(item["focus"]),
                "evidence_count": len(item["evidence"]),
            }
            for item in investigations
        ]
        return {"investigations": items, "count": len(items), "meta": runtime_meta()}

    def renderdoc_close_investigation(self, investigation_id: InvestigationId) -> dict[str, Any]:
        result = self.context.investigations.close(
            self.context.normalize_required_string(investigation_id, "investigation_id")
        )
        return {
            "investigation_id": result["investigation_id"],
            "name": result["name"],
            "closed": True,
            "meta": runtime_meta(),
        }

    def renderdoc_compare_events(
        self,
        baseline_capture_id: CaptureId,
        baseline_event_id: EventId,
        candidate_capture_id: CaptureId,
        candidate_event_id: EventId,
        binding_kinds: PipelineBindingKindList | None = None,
        max_changes: DiffChangeLimit | None = None,
        include_snapshots: bool = False,
    ) -> dict[str, Any]:
        requested_kinds = binding_kinds or [
            "output_targets",
            "shaders",
            "read_only_resources",
            "read_write_resources",
            "constant_blocks",
            "samplers",
        ]
        kinds = list(
            dict.fromkeys(
                _normalize_binding_kind(self.context.normalize_required_string(value, "binding_kinds"))
                for value in requested_kinds
            )
        )
        invalid_kinds = sorted(set(kinds) - SUPPORTED_PIPELINE_BINDING_KINDS)
        if invalid_kinds:
            raise ReplayFailureError(
                "binding_kinds contains unsupported values.",
                {
                    "invalid_values": invalid_kinds,
                    "supported_values": sorted(SUPPORTED_PIPELINE_BINDING_KINDS),
                    "aliases": dict(sorted(PIPELINE_BINDING_KIND_ALIASES.items())),
                },
            )
        if not kinds or len(kinds) > 8:
            raise ReplayFailureError(
                "binding_kinds must contain between 1 and 8 values.",
                {"binding_kind_count": len(kinds)},
            )
        baseline_event = self.context.normalize_required_int(baseline_event_id, "baseline_event_id")
        candidate_event = self.context.normalize_required_int(candidate_event_id, "candidate_event_id")
        if baseline_event <= 0 or candidate_event <= 0:
            raise ReplayFailureError(
                "Event ids must be greater than 0.",
                {"baseline_event_id": baseline_event, "candidate_event_id": candidate_event},
            )
        params_base = {"binding_kinds": list(kinds), "binding_limit": 50}
        (baseline_session, baseline), (candidate_session, candidate) = self._capture_tool_pair(
            baseline_capture_id,
            "get_event_dossier",
            {**params_base, "event_id": baseline_event},
            candidate_capture_id,
            "get_event_dossier",
            {**params_base, "event_id": candidate_event},
        )
        normalized_max_changes = self.context.normalize_optional_int(max_changes, "max_changes") or 200
        if normalized_max_changes <= 0 or normalized_max_changes > 500:
            raise ReplayFailureError(
                "max_changes must be between 1 and 500.",
                {"max_changes": normalized_max_changes},
            )
        comparison = compare_event_dossiers(baseline, candidate, max_changes=normalized_max_changes)
        baseline_snapshot = comparison.pop("baseline_snapshot")
        candidate_snapshot = comparison.pop("candidate_snapshot")
        response: dict[str, Any] = {
            "baseline": {
                "capture_id": baseline_session.capture_id,
                "event_id": baseline_event,
                "action": baseline.get("action"),
                "pass": baseline.get("pass"),
            },
            "candidate": {
                "capture_id": candidate_session.capture_id,
                "event_id": candidate_event,
                "action": candidate.get("action"),
                "pass": candidate.get("pass"),
            },
            **comparison,
            "meta": {
                "comparison_mode": "semantic_event_snapshot_v1",
                "baseline_bridge": bridge_meta(baseline_session),
                "candidate_bridge": bridge_meta(candidate_session),
            },
        }
        normalized_include_snapshots = bool(
            self.context.normalize_optional_bool(include_snapshots, "include_snapshots")
        )
        if normalized_include_snapshots:
            response["baseline_snapshot"] = baseline_snapshot
            response["candidate_snapshot"] = candidate_snapshot
        return response

    def renderdoc_compare_texture_regions(
        self,
        baseline_capture_id: CaptureId,
        baseline_texture_id: ResourceId,
        candidate_capture_id: CaptureId,
        candidate_texture_id: ResourceId,
        x: NonNegativeInt,
        y: NonNegativeInt,
        width: TexturePreviewDimension,
        height: TexturePreviewDimension,
        mip_level: NonNegativeInt = 0,
        array_slice: NonNegativeInt = 0,
        sample: NonNegativeInt = 0,
        threshold: TextureDiffThreshold = 0.001,
        top_pixel_limit: DiffPixelLimit = 20,
    ) -> dict[str, Any]:
        normalized_width = self.context.normalize_positive_int(width, "width")
        normalized_height = self.context.normalize_positive_int(height, "height")
        if normalized_width > 64 or normalized_height > 64:
            raise ReplayFailureError(
                "width and height must each be less than or equal to 64.",
                {"width": normalized_width, "height": normalized_height},
            )
        if normalized_width * normalized_height > 1024:
            raise ReplayFailureError(
                "width * height must be less than or equal to 1024.",
                {"width": normalized_width, "height": normalized_height},
            )
        common = {
            "mip_level": self.context.normalize_non_negative_int(mip_level, "mip_level"),
            "x": self.context.normalize_non_negative_int(x, "x"),
            "y": self.context.normalize_non_negative_int(y, "y"),
            "width": normalized_width,
            "height": normalized_height,
            "array_slice": self.context.normalize_non_negative_int(array_slice, "array_slice"),
            "sample": self.context.normalize_non_negative_int(sample, "sample"),
        }
        (baseline_session, baseline), (candidate_session, candidate) = self._capture_tool_pair(
            baseline_capture_id,
            "get_texture_data",
            {**common, "texture_id": self.context.normalize_required_string(baseline_texture_id, "baseline_texture_id")},
            candidate_capture_id,
            "get_texture_data",
            {**common, "texture_id": self.context.normalize_required_string(candidate_texture_id, "candidate_texture_id")},
        )
        normalized_top_pixel_limit = self.context.normalize_positive_int(top_pixel_limit, "top_pixel_limit")
        if normalized_top_pixel_limit > 100:
            raise ReplayFailureError(
                "top_pixel_limit must be between 1 and 100.",
                {"top_pixel_limit": normalized_top_pixel_limit},
            )
        return _compare_texture_payloads(
            baseline_session,
            baseline,
            candidate_session,
            candidate,
            common,
            self.context.normalize_non_negative_float(threshold, "threshold"),
            normalized_top_pixel_limit,
        )

    def _capture_tool_pair(
        self,
        baseline_capture_id: str,
        baseline_method: str,
        baseline_params: dict[str, Any],
        candidate_capture_id: str,
        candidate_method: str,
        candidate_params: dict[str, Any],
    ) -> tuple[
        tuple[Any, dict[str, Any]],
        tuple[Any, dict[str, Any]],
    ]:
        normalized_baseline_id = self.context.normalizer.normalize_required_capture_id(baseline_capture_id)
        normalized_candidate_id = self.context.normalizer.normalize_required_capture_id(candidate_capture_id)
        if normalized_baseline_id == normalized_candidate_id:
            return (
                self.context.sessions.capture_tool_normalized(
                    normalized_baseline_id,
                    baseline_method,
                    baseline_params,
                ),
                self.context.sessions.capture_tool_normalized(
                    normalized_candidate_id,
                    candidate_method,
                    candidate_params,
                ),
            )

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="renderdoc_compare") as executor:
            baseline_future = executor.submit(
                self.context.sessions.capture_tool_normalized,
                normalized_baseline_id,
                baseline_method,
                baseline_params,
            )
            candidate_future = executor.submit(
                self.context.sessions.capture_tool_normalized,
                normalized_candidate_id,
                candidate_method,
                candidate_params,
            )
            return baseline_future.result(), candidate_future.result()

    def _decorate_summary(self, investigation: dict[str, Any]) -> dict[str, Any]:
        open_ids = {str(item["capture_id"]) for item in self.context.sessions.list_sessions()}
        captures = {
            label: {**value, "open": value["capture_id"] in open_ids}
            for label, value in investigation["captures"].items()
        }
        focus = dict(investigation["focus"])
        recommended_calls = []
        if focus.get("capture_id") and focus.get("event_id"):
            recommended_calls.append(
                {
                    "tool": "renderdoc_get_event_dossier",
                    "arguments": {"capture_id": focus["capture_id"], "event_id": focus["event_id"]},
                }
            )
        if focus.get("capture_id") and focus.get("texture_id") and focus.get("x") is not None and focus.get("y") is not None:
            recommended_calls.append(
                {
                    "tool": "renderdoc_trace_bad_pixel",
                    "arguments": {
                        "capture_id": focus["capture_id"],
                        "texture_id": focus["texture_id"],
                        "x": focus["x"],
                        "y": focus["y"],
                    },
                }
            )
        if focus.get("capture_id") and focus.get("resource_id"):
            recommended_calls.append(
                {
                    "tool": "renderdoc_get_resource_summary",
                    "arguments": {"capture_id": focus["capture_id"], "resource_id": focus["resource_id"]},
                }
            )
        return {
            **investigation,
            "captures": captures,
            "recommended_calls": recommended_calls,
            "meta": runtime_meta(),
        }


def _next_capture_label(captures: dict[str, Any]) -> str:
    for preferred in ("baseline", "candidate"):
        if preferred not in captures:
            return preferred
    index = 3
    while f"capture_{index}" in captures:
        index += 1
    return f"capture_{index}"


def _compare_texture_payloads(
    baseline_session: Any,
    baseline: dict[str, Any],
    candidate_session: Any,
    candidate: dict[str, Any],
    query: dict[str, Any],
    threshold: float,
    top_pixel_limit: int,
) -> dict[str, Any]:
    baseline_rows = list(baseline.get("pixels") or [])
    candidate_rows = list(candidate.get("pixels") or [])
    if len(baseline_rows) != len(candidate_rows) or any(
        len(left) != len(right) for left, right in zip(baseline_rows, candidate_rows, strict=False)
    ):
        raise ReplayFailureError(
            "The two texture reads returned different grid dimensions.",
            {
                "baseline_rows": len(baseline_rows),
                "candidate_rows": len(candidate_rows),
            },
        )

    changed_pixels: list[dict[str, Any]] = []
    finite_delta_sum = 0.0
    finite_component_count = 0
    non_finite_pixel_count = 0
    for row_index, (left_row, right_row) in enumerate(zip(baseline_rows, candidate_rows, strict=False)):
        for column_index, (left_pixel, right_pixel) in enumerate(zip(left_row, right_row, strict=False)):
            left = _pixel_components(left_pixel)
            right = _pixel_components(right_pixel)
            finite_deltas = [
                abs(a - b)
                for a, b in zip(left, right, strict=False)
                if math.isfinite(a) and math.isfinite(b)
            ]
            non_finite = any(not math.isfinite(value) for value in [*left, *right])
            finite_delta_sum += sum(finite_deltas)
            finite_component_count += len(finite_deltas)
            max_delta = max(finite_deltas) if finite_deltas else 0.0
            if non_finite:
                non_finite_pixel_count += 1
            if non_finite or max_delta > threshold:
                changed_pixels.append(
                    {
                        "x": int(query["x"]) + column_index,
                        "y": int(query["y"]) + row_index,
                        "max_abs_difference": None if non_finite else max_delta,
                        "non_finite": non_finite,
                        "baseline": _display_components(left),
                        "candidate": _display_components(right),
                        "_score": math.inf if non_finite else max_delta,
                    }
                )
    changed_pixels.sort(key=lambda item: (-item["_score"], item["y"], item["x"]))
    top_pixels: list[dict[str, Any]] = []
    for item in changed_pixels[: max(1, top_pixel_limit)]:
        payload = dict(item)
        payload.pop("_score", None)
        top_pixels.append(payload)
    total_pixels = sum(len(row) for row in baseline_rows)
    finite_maxima = [item["_score"] for item in changed_pixels if math.isfinite(item["_score"])]
    return {
        "baseline": {
            "capture_id": baseline_session.capture_id,
            "texture": baseline.get("texture"),
        },
        "candidate": {
            "capture_id": candidate_session.capture_id,
            "texture": candidate.get("texture"),
        },
        "query": {**query, "threshold": threshold},
        "summary": {
            "pixel_count": total_pixels,
            "changed_pixel_count": len(changed_pixels),
            "changed_pixel_ratio": len(changed_pixels) / float(total_pixels or 1),
            "mean_abs_component_difference": finite_delta_sum / float(finite_component_count or 1),
            "max_finite_abs_difference": max(finite_maxima) if finite_maxima else 0.0,
            "non_finite_pixel_count": non_finite_pixel_count,
        },
        "top_changed_pixels": top_pixels,
        "meta": {
            "comparison_mode": "texture_region_abs_diff_v1",
            "baseline_bridge": bridge_meta(baseline_session),
            "candidate_bridge": bridge_meta(candidate_session),
            "top_pixels_truncated": len(changed_pixels) > len(top_pixels),
        },
    }


def _pixel_components(value: Any) -> list[float]:
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values[:4]:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            result.append(math.nan)
    while len(result) < 4:
        result.append(0.0)
    return result


def _display_components(values: list[float]) -> list[float | str]:
    result: list[float | str] = []
    for value in values:
        if math.isnan(value):
            result.append("NaN")
        elif math.isinf(value):
            result.append("Infinity" if value > 0 else "-Infinity")
        else:
            result.append(value)
    return result
