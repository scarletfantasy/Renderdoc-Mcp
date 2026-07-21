from __future__ import annotations

from functools import lru_cache

from mcp.server.fastmcp import FastMCP

from renderdoc_mcp.application import RenderDocApplication
from renderdoc_mcp.application.registry import build_resource_registry, build_tool_registry
from renderdoc_mcp.bootstrap import prepare_runtime


@lru_cache(maxsize=1)
def get_application() -> RenderDocApplication:
    return RenderDocApplication()


def create_mcp_app(application: RenderDocApplication | None = None) -> FastMCP:
    application = application or get_application()
    app = FastMCP(
        name="renderdoc-mcp",
        instructions=(
            "Use renderdoc_get_server_status when setup is uncertain, then renderdoc_open_capture; opening the same path is idempotent. "
            "Reuse the returned capture_id and start with renderdoc_get_analysis_worklist. Use renderdoc_search_actions for recursive discovery "
            "and renderdoc_get_event_dossier instead of separate action, pipeline, and binding calls. Create an investigation to persist focus "
            "across turns, recover it with renderdoc_list_investigations, and use semantic event or texture comparisons for regressions. "
            "Open sessions stay alive until explicitly closed or evicted."
        ),
    )

    for tool in build_tool_registry(application):
        app.add_tool(
            tool.handler,
            name=tool.name,
            description=tool.description,
            structured_output=True,
        )

    for resource in build_resource_registry(application):
        app.resource(
            resource.uri,
            name=resource.name,
            description=resource.description,
            mime_type="application/json",
        )(resource.handler)

    return app


@lru_cache(maxsize=1)
def get_mcp_app() -> FastMCP:
    return create_mcp_app(get_application())


def main() -> None:
    prepare_runtime()
    get_mcp_app().run(transport="stdio")
