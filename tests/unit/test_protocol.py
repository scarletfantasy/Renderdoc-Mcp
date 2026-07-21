from io import StringIO

import pytest

from renderdoc_mcp import protocol
from renderdoc_mcp.protocol import decode_message, encode_message, read_message, send_message


def test_protocol_message_round_trip() -> None:
    payload = {"type": "request", "id": "abc", "method": "ping", "params": {"x": 1}}
    text = encode_message(payload).decode("utf-8")
    assert decode_message(text) == payload


def test_protocol_rejects_non_object_messages() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        decode_message("[]")


def test_protocol_enforces_message_size_limit(monkeypatch) -> None:
    monkeypatch.setattr(protocol, "MAX_BRIDGE_MESSAGE_CHARS", 8)

    with pytest.raises(ValueError, match="maximum supported size"):
        read_message(StringIO("123456789\n"))
    with pytest.raises(ValueError, match="maximum supported size"):
        send_message(StringIO(), {"value": "123456789"})
