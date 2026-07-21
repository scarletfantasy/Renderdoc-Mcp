from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from renderdoc_mcp.protocol import BRIDGE_PROTOCOL_VERSION, read_message, send_message


def _fatal(code: str, message: str, details: dict[str, Any] | None = None) -> int:
    send_message(
        sys.stdout,
        {
            "type": "fatal",
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
        },
    )
    return 1


def _iter_dll_search_dirs(module_dir: str, dll_dir: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(path: Path | str | None) -> None:
        if path is None:
            return
        candidate = Path(path)
        if not candidate.is_dir():
            return
        normalized = str(candidate.resolve())
        if normalized in seen:
            return
        seen.add(normalized)
        ordered.append(normalized)

    module_path = Path(module_dir) if module_dir else None
    dll_path = Path(dll_dir) if dll_dir else None
    add(module_path)
    add(dll_path)

    roots: list[Path] = []
    for base in (module_path, dll_path):
        if base is None:
            continue
        roots.extend(base.parents[:4])

    for root in roots:
        for arch in ("x64", "Win32"):
            python_dir = root / "qrenderdoc" / "3rdparty" / "python" / arch
            if any(python_dir.glob("python*.dll")):
                add(python_dir)

    return ordered


def _configure_renderdoc_paths(module_dir: str, dll_dir: str) -> list[Any]:
    if module_dir and module_dir not in sys.path:
        sys.path.insert(0, module_dir)

    discovered_dirs = _iter_dll_search_dirs(module_dir, dll_dir)
    dll_directory_handles: list[Any] = []
    if discovered_dirs:
        existing_path = os.environ.get("PATH", "")
        path_entries = list(discovered_dirs)
        if existing_path:
            path_entries.append(existing_path)
        os.environ["PATH"] = os.pathsep.join(path_entries)
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if callable(add_dll_directory):
            for path in discovered_dirs:
                dll_directory_handles.append(add_dll_directory(path))
    return dll_directory_handles


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RenderDoc native Python helper for renderdoc-mcp.")
    parser.add_argument("--module-dir", required=True)
    parser.add_argument("--dll-dir", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    dll_directory_handles = _configure_renderdoc_paths(args.module_dir, args.dll_dir)

    try:
        import renderdoc as rd
    except Exception as exc:
        return _fatal(
            "native_python_import_failed",
            "Failed to import the standalone RenderDoc Python module.",
            {
                "module_dir": args.module_dir,
                "dll_dir": args.dll_dir,
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
            },
        )

    try:
        from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge.client import BridgeClient
        from renderdoc_mcp.standalone_context import StandaloneRenderDocContext
    except Exception as exc:
        return _fatal(
            "native_helper_startup_failed",
            "Failed to import renderdoc-mcp helper modules.",
            {
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
            },
        )

    initialized = False
    context = StandaloneRenderDocContext(rd)
    client = BridgeClient(context, renderdoc_version=str(getattr(rd, "GetVersionString", lambda: "")() or ""))

    try:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
    except Exception as exc:
        return _fatal(
            "native_helper_startup_failed",
            "Failed to initialise the standalone RenderDoc replay API.",
            {
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": traceback.format_exc(),
            },
        )

    send_message(
        sys.stdout,
        {
            "type": "hello",
            "backend": "native_python",
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "renderdoc_version": client.renderdoc_version,
        },
    )

    try:
        while True:
            try:
                request = read_message(sys.stdin)
            except ConnectionError:
                break

            if request.get("type") != "request":
                continue

            request_id = request.get("id")
            try:
                result = client._dispatch(str(request.get("method", "")), request.get("params", {}) or {})
                response = {"type": "response", "id": request_id, "result": result}
            except Exception as exc:
                response = {"type": "response", "id": request_id, "error": client._parse_exception(exc)}

            send_message(sys.stdout, response)
    finally:
        try:
            context.CloseCapture()
        finally:
            try:
                if initialized:
                    try:
                        rd.ShutdownReplay()
                    except Exception:
                        pass
            finally:
                for handle in reversed(dll_directory_handles):
                    try:
                        handle.close()
                    except Exception:
                        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
