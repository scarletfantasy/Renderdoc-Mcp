from __future__ import annotations

import ast
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_ROOT = REPO_ROOT / "src" / "renderdoc_mcp" / "qrenderdoc_extension" / "renderdoc_mcp_bridge"
SHARED_ANALYSIS_ROOT = REPO_ROOT / "src" / "renderdoc_mcp" / "analysis"


@pytest.mark.parametrize(
    "source_path",
    sorted(EXTENSION_ROOT.rglob("*.py")) + sorted(SHARED_ANALYSIS_ROOT.rglob("*.py")),
    ids=lambda path: str(path.relative_to(REPO_ROOT)),
)
def test_embedded_extension_source_is_python_36_syntax_compatible(source_path: Path) -> None:
    ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path), feature_version=(3, 6))


@pytest.mark.skipif(os.name != "nt", reason="The qrenderdoc transport directly uses WinSock.")
def test_winsock_transport_round_trip() -> None:
    from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge.transport import _WinSockClient

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received: list[bytes] = []

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            received.append(connection.recv(1024))
            connection.sendall(b'{"type":"hello"}\n')

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    client = _WinSockClient()
    try:
        client.connect("127.0.0.1", port)
        client.send_text('{"type":"ping"}\n')
        assert client.recv_line() == '{"type":"hello"}'
    finally:
        client.close()
        server.close()
        server_thread.join(timeout=2)

    assert received == [b'{"type":"ping"}\n']
    assert not server_thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="The qrenderdoc transport directly uses WinSock.")
def test_winsock_transport_rejects_oversized_send() -> None:
    from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge.transport import (
        MAX_MESSAGE_BYTES,
        _WinSockClient,
    )

    client = object.__new__(_WinSockClient)

    with pytest.raises(RuntimeError, match="maximum supported size"):
        client.send_text("x" * (MAX_MESSAGE_BYTES + 1))


@pytest.mark.skipif(os.name != "nt", reason="The qrenderdoc runtime imports its WinSock transport.")
def test_bridge_runtime_closes_socket_after_failed_handshake(monkeypatch) -> None:
    from renderdoc_mcp.qrenderdoc_extension.renderdoc_mcp_bridge import runtime as runtime_module

    stop_event = threading.Event()
    fake_client = SimpleNamespace(
        PROTOCOL_VERSION=1,
        CONNECT_RETRY_SECONDS=1.0,
        renderdoc_version="1.43",
        stop_event=stop_event,
        sock=None,
        thread=None,
    )

    class FailingSocket:
        def __init__(self) -> None:
            self.closed = False

        def connect(self, host: str, port: int) -> None:
            return None

        def send_text(self, text: str) -> None:
            raise RuntimeError("simulated handshake failure")

        def close(self) -> None:
            self.closed = True
            stop_event.set()

    sockets: list[FailingSocket] = []

    def create_socket() -> FailingSocket:
        sock = FailingSocket()
        sockets.append(sock)
        return sock

    monkeypatch.setenv("RENDERDOC_MCP_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("RENDERDOC_MCP_BRIDGE_PORT", "12345")
    monkeypatch.setenv("RENDERDOC_MCP_BRIDGE_TOKEN", "token")
    monkeypatch.setenv("RENDERDOC_MCP_BRIDGE_PROTOCOL", "1")
    monkeypatch.setattr(runtime_module, "_WinSockClient", create_socket)
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _seconds: None)

    bridge_runtime = runtime_module.BridgeRuntime(fake_client)

    assert bridge_runtime.start() is False
    assert len(sockets) == 1
    assert sockets[0].closed is True
    assert fake_client.sock is None
