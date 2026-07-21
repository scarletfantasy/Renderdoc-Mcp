from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from renderdoc_mcp import __version__
from renderdoc_mcp.analysis.frame_analysis import (
    DEFAULT_PASS_PAGE_LIMIT,
    DEFAULT_TIMING_EVENT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    MAX_TIMING_EVENT_PAGE_LIMIT,
    PASS_CATEGORIES,
    PASS_SORT_OPTIONS,
)
from renderdoc_mcp.application.command_specs import OpenCaptureCommand
from renderdoc_mcp.application.context import ApplicationContext
from renderdoc_mcp.application.response import attach_capture, bridge_meta, ensure_meta, runtime_meta
from renderdoc_mcp.application.schema_types import (
    CaptureId,
    CapturePath,
    Cursor,
    PageLimit,
    PassCategory,
    PassId,
    PassSort,
    TimingPageLimit,
    TimingSort,
    WorklistFocus,
    WorklistLimit,
)
from renderdoc_mcp.backend import DEFAULT_BACKEND, current_backend_name, resolve_native_python_config
from renderdoc_mcp.errors import RenderDocMCPError, ReplayFailureError
from renderdoc_mcp.install import inspect_extension_install
from renderdoc_mcp.paths import resolve_qrenderdoc_path

SUPPORTED_PASS_CATEGORIES = set(PASS_CATEGORIES)
SUPPORTED_PASS_SORT_OPTIONS = set(PASS_SORT_OPTIONS)
SUPPORTED_WORKLIST_FOCI = {"performance", "structure", "resources", "correctness"}
SORT_ALIASES = {"event": "event_order", "event_id": "event_order"}
DEFAULT_WORKLIST_LIMIT = 10
MAX_WORKLIST_LIMIT = 50


class CaptureHandlers:
    def __init__(self, context: ApplicationContext) -> None:
        self.context = context

    def renderdoc_open_capture(self, capture_path: CapturePath) -> dict[str, Any]:
        command = OpenCaptureCommand.from_raw(self.context.normalizer, capture_path)
        with self.context.sessions.open_normalized_capture_lease_with_status(command.capture_path) as (session, reused):
            try:
                session.bridge.ensure_capture_loaded(session.capture_path)
                overview = ensure_meta(session.bridge.call("get_capture_overview"))
            except Exception:
                if not reused:
                    self.context.sessions.close_normalized_capture(session.capture_id)
                raise
            overview["session_reused"] = reused
            overview["session_open_count"] = session.open_count
            return attach_capture(overview, session)

    def renderdoc_list_open_captures(self) -> dict[str, Any]:
        captures = self.context.sessions.list_sessions()
        return {
            "captures": captures,
            "count": len(captures),
            "meta": runtime_meta(),
        }

    def renderdoc_get_server_status(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        backend: dict[str, Any]
        try:
            backend_name = current_backend_name()
            backend = {"name": backend_name, "configured": True}
        except RenderDocMCPError as exc:
            backend_name = "invalid"
            backend = {"name": backend_name, "configured": False, "error": exc.to_payload()}
            issues.append(exc.to_payload())

        extension: dict[str, Any] | None = None
        if backend_name == DEFAULT_BACKEND:
            try:
                backend["qrenderdoc_path"] = str(resolve_qrenderdoc_path())
            except RenderDocMCPError as exc:
                backend["configured"] = False
                backend["error"] = exc.to_payload()
                issues.append(exc.to_payload())
            try:
                extension = dict(inspect_extension_install())
                if not extension.get("current"):
                    issues.append(
                        {
                            "code": "extension_out_of_date",
                            "message": "The installed qrenderdoc extension does not match this server build.",
                            "details": {"path": extension.get("path")},
                        }
                    )
            except Exception as exc:
                extension = {"installed": False, "current": False, "error": str(exc)}
                issues.append({"code": "extension_status_failed", "message": str(exc)})
        elif backend_name == "native_python":
            try:
                config = resolve_native_python_config()
                backend.update(
                    {
                        "python_executable": config.python_executable,
                        "module_dir": str(config.module_dir),
                        "dll_dir": str(config.dll_dir),
                    }
                )
            except RenderDocMCPError as exc:
                backend["configured"] = False
                backend["error"] = exc.to_payload()
                issues.append(exc.to_payload())

        captures = self.context.sessions.list_sessions()
        return {
            "server": {
                "name": "renderdoc-mcp",
                "version": __version__,
                "python": sys.version.split()[0],
            },
            "backend": backend,
            "extension": extension,
            "open_captures": captures,
            "open_capture_count": len(captures),
            "ready": bool(backend.get("configured")) and not any(
                issue.get("code")
                in {
                    "renderdoc_not_installed",
                    "invalid_backend",
                    "native_python_not_configured",
                    "native_python_module_not_found",
                    "extension_out_of_date",
                    "extension_status_failed",
                }
                for issue in issues
            ),
            "issues": issues,
            "meta": {"backend": backend_name},
        }

    def renderdoc_close_capture(self, capture_id: CaptureId) -> dict[str, Any]:
        session = self.context.get_session(capture_id)
        self.context.close_capture(capture_id)
        return {
            "capture_id": session.capture_id,
            "capture_path": session.capture_path,
            "closed": True,
            "meta": bridge_meta(session),
        }

    def renderdoc_get_capture_overview(self, capture_id: CaptureId) -> dict[str, Any]:
        session, result = self.context.capture_tool(capture_id, "get_capture_overview")
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_analysis_worklist(
        self,
        capture_id: CaptureId,
        focus: WorklistFocus | None = None,
        limit: WorklistLimit | None = None,
    ) -> dict[str, Any]:
        normalized_focus = (self.context.normalize_optional_string(focus) or "performance").lower()
        normalized_limit = self.context.normalize_optional_int(limit, "limit")

        if normalized_focus not in SUPPORTED_WORKLIST_FOCI:
            raise ReplayFailureError(
                "focus must be one of correctness, performance, resources, or structure.",
                {"focus": normalized_focus, "supported_values": sorted(SUPPORTED_WORKLIST_FOCI)},
            )
        self.context.normalizer.validate_pagination(None, normalized_limit, MAX_WORKLIST_LIMIT)

        params = {"focus": normalized_focus, "limit": normalized_limit or DEFAULT_WORKLIST_LIMIT}
        session, result = self.context.capture_tool(capture_id, "get_analysis_worklist", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_list_passes(
        self,
        capture_id: CaptureId,
        parent_pass_id: PassId | None = None,
        cursor: Cursor | None = None,
        limit: PageLimit | None = None,
        category_filter: PassCategory | None = None,
        name_filter: str | None = None,
        sort_by: PassSort | None = None,
    ) -> dict[str, Any]:
        normalized_parent_pass_id = self.context.normalize_optional_string(parent_pass_id)
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")
        normalized_category_filter = self.context.normalize_optional_string(category_filter)
        normalized_name_filter = self.context.normalize_optional_string(name_filter)
        normalized_sort_by = (self.context.normalize_optional_string(sort_by) or "event_order").lower()
        normalized_sort_by = SORT_ALIASES.get(normalized_sort_by, normalized_sort_by)

        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_PAGE_LIMIT)
        if normalized_category_filter and normalized_category_filter not in SUPPORTED_PASS_CATEGORIES:
            raise ReplayFailureError(
                "category_filter must be one of {}.".format(", ".join(sorted(SUPPORTED_PASS_CATEGORIES))),
                {"category_filter": normalized_category_filter},
            )
        if normalized_sort_by not in SUPPORTED_PASS_SORT_OPTIONS:
            raise ReplayFailureError(
                "sort_by must be one of {}.".format(", ".join(sorted(SUPPORTED_PASS_SORT_OPTIONS))),
                {"sort_by": normalized_sort_by},
            )

        params: dict[str, Any] = {"limit": normalized_limit or DEFAULT_PASS_PAGE_LIMIT, "sort_by": normalized_sort_by}
        if normalized_parent_pass_id:
            params["parent_pass_id"] = normalized_parent_pass_id
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor
        if normalized_category_filter:
            params["category_filter"] = normalized_category_filter
        if normalized_name_filter:
            params["name_filter"] = normalized_name_filter

        session, result = self.context.capture_tool(capture_id, "list_passes", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_get_pass_summary(self, capture_id: CaptureId, pass_id: PassId) -> dict[str, Any]:
        normalized_pass_id = self.context.normalize_required_string(pass_id, "pass_id")
        session, result = self.context.capture_tool(capture_id, "get_pass_summary", {"pass_id": normalized_pass_id})
        return attach_capture(ensure_meta(result), session)

    def renderdoc_list_timing_events(
        self,
        capture_id: CaptureId,
        pass_id: PassId,
        cursor: Cursor | None = None,
        limit: TimingPageLimit | None = None,
        sort_by: TimingSort | None = None,
    ) -> dict[str, Any]:
        normalized_pass_id = self.context.normalize_required_string(pass_id, "pass_id")
        normalized_cursor = self.context.normalize_optional_int(cursor, "cursor")
        normalized_limit = self.context.normalize_optional_int(limit, "limit")
        normalized_sort_by = (self.context.normalize_optional_string(sort_by) or "event_order").lower()
        normalized_sort_by = SORT_ALIASES.get(normalized_sort_by, normalized_sort_by)

        self.context.normalizer.validate_pagination(normalized_cursor, normalized_limit, MAX_TIMING_EVENT_PAGE_LIMIT)
        if normalized_sort_by not in {"event_order", "gpu_time"}:
            raise ReplayFailureError(
                "sort_by must be one of event_order or gpu_time.",
                {"sort_by": normalized_sort_by},
            )

        params: dict[str, Any] = {
            "pass_id": normalized_pass_id,
            "limit": normalized_limit or DEFAULT_TIMING_EVENT_PAGE_LIMIT,
            "sort_by": normalized_sort_by,
        }
        if normalized_cursor is not None:
            params["cursor"] = normalized_cursor

        session, result = self.context.capture_tool(capture_id, "list_timing_events", params)
        return attach_capture(ensure_meta(result), session)

    def renderdoc_recent_captures(self) -> dict[str, Any]:
        config = self.context.read_ui_config()
        recent_paths = list(config.get("RecentCaptureFiles", []))
        captures = []

        for raw_path in recent_paths:
            path = Path(raw_path)
            captures.append({"path": str(path), "exists": path.is_file()})

        return {"recent_captures": captures, "count": len(captures), "meta": runtime_meta()}

    def renderdoc_capture_overview_resource(self, capture_id: CaptureId) -> dict[str, Any]:
        return self.renderdoc_get_capture_overview(capture_id)
