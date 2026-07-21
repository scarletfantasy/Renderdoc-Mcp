from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import IO, Any

BRIDGE_PROTOCOL_VERSION = 1
MAX_BRIDGE_MESSAGE_CHARS = 8 * 1024 * 1024


@dataclass(slots=True)
class BridgeRequest:
    request_id: str
    method: str
    params: dict[str, Any]

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "request",
            "id": self.request_id,
            "method": self.method,
            "params": self.params,
        }


@dataclass(slots=True)
class BridgeResponse:
    request_id: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def encode_message(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message(line: str) -> dict[str, Any]:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("Bridge message must be a JSON object.")
    return payload


def send_message(stream: IO[str], message: dict[str, Any]) -> None:
    encoded = json.dumps(message, separators=(",", ":"))
    if len(encoded) > MAX_BRIDGE_MESSAGE_CHARS:
        raise ValueError("Bridge message exceeded the maximum supported size.")
    stream.write(encoded)
    stream.write("\n")
    stream.flush()


def read_message(stream: IO[str]) -> dict[str, Any]:
    line = stream.readline(MAX_BRIDGE_MESSAGE_CHARS + 1)
    if not line:
        raise ConnectionError("Bridge stream closed")
    if len(line) > MAX_BRIDGE_MESSAGE_CHARS:
        raise ValueError("Bridge message exceeded the maximum supported size.")
    return decode_message(line)


def close_socket(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass
