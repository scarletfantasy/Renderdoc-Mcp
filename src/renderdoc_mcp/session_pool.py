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
    open_count: int = 1


def _capture_path_key(capture_path: str) -> str:
    return os.path.normcase(os.path.abspath(capture_path))


def _close_session_snapshot(sessions: list[CaptureSession]) -> None:
    for session in sessions:
        try:
            session.bridge.close()
        except Exception:
            logger.debug("Failed to close bridge for session %s", session.capture_id, exc_info=True)


def _finalize_session_pool(
    sessions: dict[str, CaptureSession],
    path_index: dict[str, str],
    lock: threading.RLock,
    janitor_stop: threading.Event,
) -> None:
    janitor_stop.set()
    with lock:
        snapshot = list(sessions.values())
        sessions.clear()
        path_index.clear()
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
        self._path_index: dict[str, str] = {}
        self._enable_janitor = bool(enable_janitor and self.idle_timeout_seconds > 0)
        self._janitor_stop = threading.Event()
        self._janitor_thread: threading.Thread | None = None
        self._finalizer = weakref.finalize(
            self,
            _finalize_session_pool,
            self._sessions,
            self._path_index,
            self._lock,
            self._janitor_stop,
        )

    def open(self, capture_path: str) -> CaptureSession:
        session, _ = self._open(capture_path, initial_in_use_count=0)
        return session

    def open_with_status(self, capture_path: str) -> tuple[CaptureSession, bool]:
        return self._open(capture_path, initial_in_use_count=0)

    @contextmanager
    def open_lease(self, capture_path: str) -> Iterator[CaptureSession]:
        with self.open_lease_with_status(capture_path) as (session, _):
            yield session

    @contextmanager
    def open_lease_with_status(self, capture_path: str) -> Iterator[tuple[CaptureSession, bool]]:
        session, reused = self._open(capture_path, initial_in_use_count=1)
        try:
            yield session, reused
        finally:
            self.release(session.capture_id)

    def _open(self, capture_path: str, initial_in_use_count: int) -> tuple[CaptureSession, bool]:
        now = self._monotonic()
        to_close: list[CaptureSession] = []
        try:
            with self._lock:
                to_close.extend(self._pop_expired_locked(now))
                path_key = _capture_path_key(capture_path)
                existing_id = self._path_index.get(path_key)
                existing = self._sessions.get(existing_id) if existing_id is not None else None
                if existing is not None:
                    existing.in_use_count += initial_in_use_count
                    existing.open_count += 1
                    existing.last_used_monotonic = now
                    session = existing
                    reused = True
                else:
                    bridge = self._bridge_factory()
                    session = CaptureSession(
                        capture_id=create_capture_id(),
                        capture_path=capture_path,
                        bridge=bridge,
                        last_used_monotonic=now,
                        in_use_count=initial_in_use_count,
                    )
                    self._sessions[session.capture_id] = session
                    self._path_index[path_key] = session.capture_id
                    reused = False
                    to_close.extend(self._pop_excess_idle_locked(exclude_ids={session.capture_id}))
        finally:
            self._close_sessions(to_close)
        self._start_janitor_if_needed()
        return session, reused

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

    def find_by_path(self, capture_path: str) -> CaptureSession | None:
        with self._lock:
            capture_id = self._path_index.get(_capture_path_key(capture_path))
            return self._sessions.get(capture_id) if capture_id is not None else None

    def list_sessions(self) -> list[dict[str, object]]:
        now = self._monotonic()
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda item: (item.capture_path, item.capture_id))
            return [
                {
                    "capture_id": session.capture_id,
                    "capture_path": session.capture_path,
                    "in_use_count": session.in_use_count,
                    "open_count": session.open_count,
                    "idle_seconds": max(0.0, now - session.last_used_monotonic),
                }
                for session in sessions
            ]

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
            session = self._pop_session_locked(capture_id)
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
            self._path_index.clear()
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
        return [session for capture_id in expired_ids if (session := self._pop_session_locked(capture_id)) is not None]

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
        removed = []
        for session in candidates[:remove_count]:
            popped = self._pop_session_locked(session.capture_id)
            if popped is not None:
                removed.append(popped)
        return removed

    def _pop_session_locked(self, capture_id: str) -> CaptureSession | None:
        session = self._sessions.pop(capture_id, None)
        if session is not None:
            path_key = _capture_path_key(session.capture_path)
            if self._path_index.get(path_key) == capture_id:
                self._path_index.pop(path_key, None)
        return session

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
