from __future__ import annotations

import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from renderdoc_mcp._bridge_base import BaseBridge
from renderdoc_mcp.backend import DEFAULT_BACKEND, NATIVE_PYTHON_BACKEND, current_backend_name
from renderdoc_mcp.errors import (
    BridgeHandshakeTimeoutError,
    ReplayFailureError,
)
from renderdoc_mcp.paths import resolve_qrenderdoc_path
from renderdoc_mcp.protocol import BRIDGE_PROTOCOL_VERSION, close_socket, read_message, send_message


class RenderDocBridge(Protocol):
    backend_name: str
    renderdoc_version: str | None

    def ensure_capture_loaded(self, capture_path: str) -> dict[str, Any]:
        ...

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class QRenderDocBridge(BaseBridge):
    """Owns the qrenderdoc process handshake and request/response socket."""

    backend_name = DEFAULT_BACKEND

    def __init__(self, timeout_seconds: float | None = None) -> None:
        super().__init__(timeout_seconds)
        self._server_socket: socket.socket | None = None
        self._connection: socket.socket | None = None
        self._log_path: str | None = None
        self._cleanup_log_on_close = False

    def _close_extra_resources(self) -> None:
        log_path = self._log_path
        cleanup_log = self._cleanup_log_on_close
        close_socket(self._connection)
        self._connection = None
        close_socket(self._server_socket)
        self._server_socket = None
        self._log_path = None
        self._cleanup_log_on_close = False
        if cleanup_log and log_path:
            try:
                Path(log_path).unlink(missing_ok=True)
            except OSError:
                pass

    def ensure_started(self) -> None:
        if self._reader is not None and self._writer is not None and self._connection is not None:
            return

        qrenderdoc_path = resolve_qrenderdoc_path()
        last_log_path: str | None = None

        for attempt in range(2):
            self.close()

            listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listen_socket.bind(("127.0.0.1", 0))
                listen_socket.listen(1)
                listen_socket.settimeout(0.5)
            except Exception:
                listen_socket.close()
                raise
            self._server_socket = listen_socket

            token = secrets.token_urlsafe(24)
            log_path = str(Path(tempfile.gettempdir()) / ("renderdoc_mcp_bridge_{}.log".format(token)))
            last_log_path = log_path
            self._log_path = log_path
            self._cleanup_log_on_close = False
            env = os.environ.copy()
            env.update(
                {
                    "RENDERDOC_MCP_BRIDGE_HOST": "127.0.0.1",
                    "RENDERDOC_MCP_BRIDGE_PORT": str(listen_socket.getsockname()[1]),
                    "RENDERDOC_MCP_BRIDGE_TOKEN": token,
                    "RENDERDOC_MCP_BRIDGE_PROTOCOL": str(BRIDGE_PROTOCOL_VERSION),
                    "RENDERDOC_MCP_BRIDGE_LOG": log_path,
                }
            )

            self._process = subprocess.Popen(
                [str(qrenderdoc_path)],
                cwd=str(qrenderdoc_path.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            deadline = time.monotonic() + self.timeout_seconds
            connection: socket.socket | None = None

            while time.monotonic() < deadline:
                try:
                    connection, _ = listen_socket.accept()
                    break
                except TimeoutError:
                    continue
                except OSError:
                    break

            if connection is None:
                self.close()
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise BridgeHandshakeTimeoutError(self.timeout_seconds, last_log_path)

            connection.settimeout(self.timeout_seconds)
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
            writer = connection.makefile("w", encoding="utf-8", newline="\n")

            try:
                hello = read_message(reader)
                self._accept_hello(hello, token)
            except Exception as exc:
                writer.close()
                reader.close()
                close_socket(connection)
                self.close()
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                if isinstance(exc, ReplayFailureError):
                    raise
                raise BridgeHandshakeTimeoutError(self.timeout_seconds, last_log_path) from exc

            self._connection = connection
            self._reader = reader
            self._writer = writer
            close_socket(self._server_socket)
            self._server_socket = None
            self._cleanup_log_on_close = True
            return

        raise BridgeHandshakeTimeoutError(self.timeout_seconds, last_log_path)

    def _accept_hello(self, hello: dict[str, Any], token: str) -> None:
        if (
            hello.get("type") != "hello"
            or hello.get("token") != token
            or hello.get("protocol_version") != BRIDGE_PROTOCOL_VERSION
        ):
            self.renderdoc_version = None
            raise ReplayFailureError(
                "Received an invalid bridge handshake from qrenderdoc.",
                {"hello": hello, "log_path": self._log_path},
            )

        renderdoc_version = hello.get("renderdoc_version")
        if isinstance(renderdoc_version, str):
            normalized = renderdoc_version.strip()
            self.renderdoc_version = normalized or None
        else:
            self.renderdoc_version = None

    def _call_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from renderdoc_mcp.errors import BridgeDisconnectedError

        if self._reader is None or self._writer is None:
            raise BridgeDisconnectedError()

        request_id = self._new_request_id()
        try:
            send_message(
                self._writer,
                {
                    "type": "request",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            response = read_message(self._reader)
        except (OSError, ConnectionError, ValueError) as exc:
            self._cleanup_log_on_close = False
            self.close()
            raise BridgeDisconnectedError() from exc

        if response.get("type") != "response" or response.get("id") != request_id:
            self._cleanup_log_on_close = False
            self.close()
            raise ReplayFailureError("Received an invalid bridge response.", {"response": response})

        error = response.get("error")
        if error:
            self._raise_mapped_error(error)

        result = response.get("result")
        if not isinstance(result, dict):
            self._cleanup_log_on_close = False
            self.close()
            raise ReplayFailureError("Bridge response did not include a JSON object result.")
        return result


def create_default_bridge() -> RenderDocBridge:
    backend = current_backend_name()
    if backend == NATIVE_PYTHON_BACKEND:
        from renderdoc_mcp.native_bridge import NativePythonBridge

        return NativePythonBridge()

    return QRenderDocBridge()
