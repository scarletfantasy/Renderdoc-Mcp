from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from renderdoc_mcp.bridge import QRenderDocBridge
from renderdoc_mcp.session_pool import CaptureSessionPool


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBridge:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _capture(tmp_path: Path, name: str) -> str:
    capture_path = tmp_path / name
    capture_path.write_text("x", encoding="utf-8")
    return str(capture_path.resolve())


def test_session_pool_opens_and_reuses_capture_ids(tmp_path: Path) -> None:
    capture_path = _capture(tmp_path, "sample.rdc")
    created: list[FakeBridge] = []
    pool = CaptureSessionPool(bridge_factory=lambda: created.append(FakeBridge()) or created[-1])

    session = pool.open(capture_path)
    with pool.lease(session.capture_id) as leased:
        assert leased.capture_id == session.capture_id
        assert leased.capture_path == capture_path

    assert len(created) == 1
    assert pool.get(session.capture_id) is not None


def test_session_pool_reuses_existing_session_for_same_capture_path(tmp_path: Path) -> None:
    capture_path = _capture(tmp_path, "sample.rdc")
    created: list[FakeBridge] = []
    pool = CaptureSessionPool(bridge_factory=lambda: created.append(FakeBridge()) or created[-1])

    first = pool.open(capture_path)
    second = pool.open(capture_path)

    assert first.capture_id == second.capture_id
    assert pool.session_count() == 1
    assert len(created) == 1


def test_session_pool_creates_distinct_sessions_for_distinct_capture_paths(tmp_path: Path) -> None:
    first_capture = _capture(tmp_path, "first.rdc")
    second_capture = _capture(tmp_path, "second.rdc")
    created: list[FakeBridge] = []
    pool = CaptureSessionPool(bridge_factory=lambda: created.append(FakeBridge()) or created[-1])

    first = pool.open(first_capture)
    second = pool.open(second_capture)

    assert first.capture_id != second.capture_id
    assert pool.session_count() == 2
    assert len(created) == 2


def test_session_pool_evicts_only_expired_idle_sessions(tmp_path: Path) -> None:
    first_capture = _capture(tmp_path, "first.rdc")
    second_capture = _capture(tmp_path, "second.rdc")
    clock = FakeClock()
    created: list[FakeBridge] = []
    pool = CaptureSessionPool(
        idle_timeout_seconds=10.0,
        bridge_factory=lambda: created.append(FakeBridge()) or created[-1],
        monotonic=clock,
    )

    first = pool.open(first_capture)
    clock.advance(5.0)
    second = pool.open(second_capture)

    clock.advance(6.0)
    evicted = pool.evict_idle_sessions()

    assert evicted == [first.capture_id]
    assert pool.get(first.capture_id) is None
    assert pool.get(second.capture_id) is not None
    assert created[0].closed == 1
    assert created[1].closed == 0


def test_session_pool_keeps_in_use_sessions_during_cleanup(tmp_path: Path) -> None:
    capture_path = _capture(tmp_path, "sample.rdc")
    clock = FakeClock()
    created: list[FakeBridge] = []
    pool = CaptureSessionPool(
        idle_timeout_seconds=10.0,
        bridge_factory=lambda: created.append(FakeBridge()) or created[-1],
        monotonic=clock,
    )

    session = pool.open(capture_path)
    with pool.lease(session.capture_id):
        clock.advance(11.0)
        evicted = pool.evict_idle_sessions()
        assert evicted == []
        assert created[0].closed == 0

    clock.advance(11.0)
    evicted = pool.evict_idle_sessions()

    assert evicted == [session.capture_id]
    assert created[0].closed == 1


def test_explicit_close_closes_bridge_and_removes_session(tmp_path: Path) -> None:
    capture_path = _capture(tmp_path, "sample.rdc")
    created: list[FakeBridge] = []
    pool = CaptureSessionPool(bridge_factory=lambda: created.append(FakeBridge()) or created[-1])

    session = pool.open(capture_path)

    assert pool.close(session.capture_id) is True
    assert pool.get(session.capture_id) is None
    assert created[0].closed == 1


def test_bridge_close_terminates_child_process_and_is_idempotent() -> None:
    bridge = QRenderDocBridge()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bridge._process = process

    bridge.close()

    assert process.poll() is not None
    assert bridge._process is None

    bridge.close()
    assert process.poll() is not None
