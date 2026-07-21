from __future__ import annotations

import logging
import math
import os
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterator

from renderdoc_mcp.bridge import RenderDocBridge, create_default_bridge
from renderdoc_mcp.uri import create_capture_id

logger = logging.getLogger(__name__)
DEFAULT_MAX_SESSIONS = 8


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class CaptureSession:
    capture_id: str
    capture_path: str
    bridge: RenderDocBridge
    last_used_monotonic: float
    in_use_count: int = 0


def _close_session_snapshot(sessions: list[CaptureSession]) -> None:
    for session in sessions:
        try:
            session.bridge.close()
        except Exception:
            logger.debug("Failed to close bridge for session %s", session.capture_id, exc_info=True)


def _finalize_session_pool(
    sessions: dict[str, CaptureSession],
    lock: threading.RLock,
    janitor_stop: threading.Event,
) -> None:
    janitor_stop.set()
    with lock:
        snapshot = list(sessions.values())
        sessions.clear()
    _close_session_snapshot(snapshot)


def _session_pool_janitor(
    pool_ref: weakref.ReferenceType["CaptureSessionPool"],
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    while not stop_event.wait(interval_seconds):
        pool = pool_ref()
        if pool is None:
            return
        pool.evict_idle_sessions()
        del pool


class CaptureSessionPool:
    def __init__(
        self,
        idle_timeout_seconds: float | None = None,
        max_sessions: int | None = None,
        bridge_factory: Callable[[], RenderDocBridge] | None = None,
        monotonic: Callable[[], float] | None = None,
        enable_janitor: bool = False,
    ) -> None:
        self.idle_timeout_seconds = (
            idle_timeout_seconds
            if idle_timeout_seconds is not None
            else _env_float("RENDERDOC_CAPTURE_SESSION_IDLE_SECONDS", 300.0)
        )
        self.max_sessions = max_sessions if max_sessions is not None else _env_int(
            "RENDERDOC_CAPTURE_MAX_SESSIONS",
            DEFAULT_MAX_SESSIONS,
        )
        self._bridge_factory = bridge_factory or create_default_bridge
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._sessions: dict[str, CaptureSession] = {}
        self._enable_janitor = bool(enable_janitor and self.idle_timeout_seconds > 0)
        self._janitor_stop = threading.Event()
        self._janitor_thread: threading.Thread | None = None
        self._finalizer = weakref.finalize(
            self,
            _finalize_session_pool,
            self._sessions,
            self._lock,
            self._janitor_stop,
        )

    def open(self, capture_path: str) -> CaptureSession:
        return self._open(capture_path, initial_in_use_count=0)

    @contextmanager
    def open_lease(self, capture_path: str) -> Iterator[CaptureSession]:
        session = self._open(capture_path, initial_in_use_count=1)
        try:
            yield session
        finally:
            self.release(session.capture_id)

    def _open(self, capture_path: str, initial_in_use_count: int) -> CaptureSession:
        bridge = self._bridge_factory()
        now = self._monotonic()
        to_close: list[CaptureSession] = []
        added = False
        try:
            with self._lock:
                to_close.extend(self._pop_expired_locked(now))
                session = CaptureSession(
                    capture_id=create_capture_id(),
                    capture_path=capture_path,
                    bridge=bridge,
                    last_used_monotonic=now,
                    in_use_count=initial_in_use_count,
                )
                self._sessions[session.capture_id] = session
                added = True
                to_close.extend(self._pop_excess_idle_locked(exclude_ids={session.capture_id}))
        except Exception:
            if not added:
                try:
                    bridge.close()
                except Exception:
                    logger.debug("Failed to close an unregistered bridge.", exc_info=True)
            raise
        finally:
            self._close_sessions(to_close)
        self._start_janitor_if_needed()
        return session

    @contextmanager
    def lease(self, capture_id: str) -> Iterator[CaptureSession]:
        session = self._acquire(capture_id)
        try:
            yield session
        finally:
            self.release(capture_id)

    def get(self, capture_id: str) -> CaptureSession | None:
        with self._lock:
            return self._sessions.get(capture_id)

    def release(self, capture_id: str) -> None:
        now = self._monotonic()
        with self._lock:
            session = self._sessions.get(capture_id)
            if session is not None:
                if session.in_use_count > 0:
                    session.in_use_count -= 1
                session.last_used_monotonic = now
            expired = self._pop_expired_locked(now)
            expired.extend(self._pop_excess_idle_locked())
        self._close_sessions(expired)

    def close(self, capture_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(capture_id, None)
        self._close_sessions([session] if session is not None else [])
        return session is not None

    def evict_idle_sessions(self) -> list[str]:
        with self._lock:
            expired = self._pop_expired_locked(self._monotonic())
            expired.extend(self._pop_excess_idle_locked())
        self._close_sessions(expired)
        return [session.capture_id for session in expired]

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        self._close_sessions(sessions)

    def shutdown(self) -> None:
        self._janitor_stop.set()
        thread = self._janitor_thread
        self._janitor_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.close_all()
        if self._finalizer.alive:
            self._finalizer.detach()

    def _acquire(self, capture_id: str) -> CaptureSession:
        now = self._monotonic()
        expired: list[CaptureSession] = []
        try:
            with self._lock:
                expired = self._pop_expired_locked(now)
                session = self._sessions.get(capture_id)
                if session is None:
                    raise KeyError(capture_id)
                session.in_use_count += 1
                session.last_used_monotonic = now
        finally:
            self._close_sessions(expired)
        return session

    def _pop_expired_locked(self, now: float) -> list[CaptureSession]:
        if self.idle_timeout_seconds <= 0:
            return []

        expired_ids = [
            capture_id
            for capture_id, session in self._sessions.items()
            if session.in_use_count == 0 and (now - session.last_used_monotonic) > self.idle_timeout_seconds
        ]
        return [self._sessions.pop(capture_id) for capture_id in expired_ids]

    def _pop_excess_idle_locked(self, exclude_ids: set[str] | None = None) -> list[CaptureSession]:
        if self.max_sessions <= 0 or len(self._sessions) <= self.max_sessions:
            return []

        excluded = exclude_ids or set()
        candidates = sorted(
            (
                session
                for session in self._sessions.values()
                if session.in_use_count == 0 and session.capture_id not in excluded
            ),
            key=lambda session: (session.last_used_monotonic, session.capture_id),
        )
        remove_count = min(len(candidates), len(self._sessions) - self.max_sessions)
        return [self._sessions.pop(session.capture_id) for session in candidates[:remove_count]]

    def _close_sessions(self, sessions: list[CaptureSession]) -> None:
        _close_session_snapshot(sessions)

    def _start_janitor_if_needed(self) -> None:
        if not self._enable_janitor or self._janitor_stop.is_set():
            return
        with self._lock:
            if self._janitor_thread is not None and self._janitor_thread.is_alive():
                return
            interval = min(30.0, max(1.0, self.idle_timeout_seconds / 2.0))
            thread = threading.Thread(
                target=_session_pool_janitor,
                args=(weakref.ref(self), self._janitor_stop, interval),
                name="renderdoc_session_janitor",
                daemon=True,
            )
            self._janitor_thread = thread
            thread.start()


@lru_cache(maxsize=1)
def get_capture_session_pool() -> CaptureSessionPool:
    return CaptureSessionPool(enable_janitor=True)
