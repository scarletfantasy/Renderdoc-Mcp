from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from renderdoc_mcp._bridge_base import BaseBridge
from renderdoc_mcp.backend import NATIVE_PYTHON_BACKEND, NativePythonConfig, resolve_native_python_config
from renderdoc_mcp.errors import (
    BridgeDisconnectedError,
    NativeHelperStartupError,
    NativePythonImportError,
    NativePythonModuleNotFoundError,
    NativePythonNotConfiguredError,
    RenderDocMCPError,
)
from renderdoc_mcp.protocol import BRIDGE_PROTOCOL_VERSION, decode_message, send_message


class NativePythonBridge(BaseBridge):
    backend_name = NATIVE_PYTHON_BACKEND

    def __init__(
        self,
        config: NativePythonConfig | None = None,
        timeout_seconds: float | None = None,
        helper_module: str = "renderdoc_mcp.native_helper",
    ) -> None:
        super().__init__(timeout_seconds)
        self._config = config
        self._helper_module = helper_module
        self._message_queue: Queue[dict[str, Any] | None] = Queue()
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    def _close_extra_resources(self) -> None:
        stdout_thread = self._stdout_thread
        stderr_thread = self._stderr_thread
        self._stdout_thread = None
        self._stderr_thread = None
        for thread in (stdout_thread, stderr_thread):
            if thread is not None:
                thread.join(timeout=2.0)

    def ensure_started(self) -> None:
        if self._reader is not None and self._writer is not None and self._process is not None:
            if self._process.poll() is None:
                return
            self.close()

        config = self._config or resolve_native_python_config()
        env = os.environ.copy()
        src_root = Path(__file__).resolve().parents[1]
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(src_root) if not existing_pythonpath else str(src_root) + os.pathsep + existing_pythonpath

        try:
            process = subprocess.Popen(
                [
                    config.python_executable,
                    "-m",
                    self._helper_module,
                    "--module-dir",
                    str(config.module_dir),
                    "--dll-dir",
                    str(config.dll_dir),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise NativeHelperStartupError(
                "Failed to launch the native RenderDoc helper process.",
                {"exception_type": type(exc).__name__, "exception": str(exc)},
            ) from exc
        self._process = process
        self._reader = process.stdout
        self._writer = process.stdin
        if self._reader is None or self._writer is None:
            self.close()
            raise NativeHelperStartupError("The native RenderDoc helper did not expose stdio pipes.")
        self._message_queue = Queue()
        self._stderr_lines.clear()

        self._stdout_thread = threading.Thread(target=self._stdout_reader_loop, name="renderdoc_native_stdout", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_reader_loop, name="renderdoc_native_stderr", daemon=True)
        self._stderr_thread.start()

        hello = self._wait_for_message()
        if hello is None:
            self.close()
            raise NativeHelperStartupError(
                "The native RenderDoc helper exited before completing its startup handshake.",
                self._startup_details(),
            )

        if hello.get("type") == "fatal":
            self.close()
            self._raise_mapped_error(hello.get("error", {}))

        if hello.get("type") != "hello":
            self.close()
            raise NativeHelperStartupError(
                "The native RenderDoc helper returned an invalid startup handshake.",
                {"message": hello, **self._startup_details()},
            )
        if hello.get("protocol_version") not in (None, BRIDGE_PROTOCOL_VERSION):
            self.close()
            raise NativeHelperStartupError(
                "The native RenderDoc helper reported an unsupported protocol version.",
                {"message": hello, **self._startup_details()},
            )

        renderdoc_version = hello.get("renderdoc_version")
        if renderdoc_version is None:
            self.renderdoc_version = None
        else:
            self.renderdoc_version = str(renderdoc_version).strip() or None

    def _stdout_reader_loop(self) -> None:
        reader = self._reader
        if reader is None:
            self._message_queue.put(None)
            return

        try:
            while True:
                line = reader.readline()
                if not line:
                    break
                self._message_queue.put(decode_message(line))
        except Exception as exc:
            self._message_queue.put(
                {
                    "type": "fatal",
                    "error": {
                        "code": "native_helper_startup_failed",
                        "message": "The native RenderDoc helper emitted invalid protocol data.",
                        "details": {"exception_type": type(exc).__name__, "exception": str(exc)},
                    },
                }
            )
        finally:
            self._message_queue.put(None)

    def _stderr_reader_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return

        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                self._stderr_lines.append(line.rstrip())
        except Exception:
            return

    def _wait_for_message(self) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeHelperStartupError(
                    "Timed out waiting for the native RenderDoc helper to respond.",
                    self._startup_details(),
                )
            try:
                message = self._message_queue.get(timeout=remaining)
            except Empty as exc:
                raise NativeHelperStartupError(
                    "Timed out waiting for the native RenderDoc helper to respond.",
                    self._startup_details(),
                ) from exc

            if message is None:
                return None
            return message

    def _call_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
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
        except OSError as exc:
            self.close()
            raise BridgeDisconnectedError() from exc

        response = self._wait_for_message()
        if response is None:
            self.close()
            raise BridgeDisconnectedError()

        if response.get("type") == "fatal":
            self.close()
            self._raise_mapped_error(response.get("error", {}))

        if response.get("type") != "response" or response.get("id") != request_id:
            raise RenderDocMCPError("replay_failure", "Received an invalid bridge response.", {"response": response})

        error = response.get("error")
        if error:
            self._raise_mapped_error(error)

        result = response.get("result")
        if not isinstance(result, dict):
            raise RenderDocMCPError("replay_failure", "Bridge response did not include a JSON object result.")
        return result

    def _startup_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if self._stderr_lines:
            details["stderr"] = "\n".join(self._stderr_lines)
        process = self._process
        if process is not None and process.poll() is not None:
            details["returncode"] = process.returncode
        return details

    def _raise_mapped_error(self, error: dict[str, Any]) -> None:
        code = error.get("code")
        message = error.get("message", "RenderDoc bridge request failed.")
        details = error.get("details") or {}

        if code == "native_python_not_configured":
            raise NativePythonNotConfiguredError(str(details.get("missing_env_var", "RENDERDOC_NATIVE_MODULE_DIR")))
        if code == "native_python_module_not_found":
            raise NativePythonModuleNotFoundError(
                str(details.get("checked_path", "")),
                kind=str(details.get("kind", "renderdoc_module")),
            )
        if code == "native_python_import_failed":
            raise NativePythonImportError(message, details)
        if code == "native_helper_startup_failed":
            raise NativeHelperStartupError(message, details)
        super()._raise_mapped_error(error)
