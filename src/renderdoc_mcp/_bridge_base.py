from __future__ import annotations

import math
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import IO, Any

from renderdoc_mcp.errors import (
    BridgeDisconnectedError,
    CapturePathError,
    InvalidEventIDError,
    RenderDocMCPError,
)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _close_text_io(stream: IO[str] | None) -> None:
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def _terminate_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
    except OSError:
        pass


class BaseBridge:
    """Shared logic for all RenderDoc bridge backends."""

    backend_name: str = ""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        resolved_timeout = (
            env_float("RENDERDOC_BRIDGE_TIMEOUT_SECONDS", 30.0)
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if not math.isfinite(resolved_timeout) or resolved_timeout <= 0:
            raise ValueError("timeout_seconds must be a finite number greater than 0.")
        self.timeout_seconds = resolved_timeout
        self._lock = threading.RLock()
        self._process: subprocess.Popen[Any] | None = None
        self._reader: IO[str] | None = None
        self._writer: IO[str] | None = None
        self._current_capture: str | None = None
        self._current_capture_token: tuple[int, int] | None = None
        self.renderdoc_version: str | None = None

    # -- public API (satisfies RenderDocBridge Protocol) ---------------------

    def ensure_capture_loaded(self, capture_path: str) -> dict[str, Any]:
        path = Path(capture_path)
        normalized = str(path)
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise CapturePathError(normalized) from exc
        capture_token = (
            int(stat_result.st_size),
            int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
        )
        with self._lock:
            self.ensure_started()
            if self._current_capture == normalized and self._current_capture_token == capture_token:
                return {"loaded": True, "filename": normalized}
            result = self._call_locked("load_capture", {"capture_path": normalized})
            self._current_capture = normalized
            self._current_capture_token = capture_token
            return result

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self.ensure_started()
            return self._call_locked(method, params or {})

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._current_capture = None
            self._current_capture_token = None
            self.renderdoc_version = None
            _close_text_io(self._writer)
            self._writer = None
            _close_text_io(self._reader)
            self._reader = None
            self._close_extra_resources()
            _terminate_process(process)

    # -- extension points for subclasses ------------------------------------

    def ensure_started(self) -> None:
        raise NotImplementedError

    def _call_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _close_extra_resources(self) -> None:
        """Override to release subclass-specific resources (sockets, threads)."""

    # -- error mapping (override to extend) ---------------------------------

    def _raise_mapped_error(self, error: dict[str, Any]) -> None:
        code = error.get("code")
        message = error.get("message", "RenderDoc bridge request failed.")
        details = error.get("details") or {}

        if code == "capture_path_not_found":
            raise CapturePathError(str(details.get("capture_path", "")))
        if code == "invalid_event_id":
            raise InvalidEventIDError(int(details.get("event_id", 0)))
        if code == "bridge_disconnected":
            self.close()
            raise BridgeDisconnectedError()
        raise RenderDocMCPError(str(code or "replay_failure"), message, details)

    # -- shared helpers for _call_locked ------------------------------------

    def _new_request_id(self) -> str:
        return uuid.uuid4().hex
