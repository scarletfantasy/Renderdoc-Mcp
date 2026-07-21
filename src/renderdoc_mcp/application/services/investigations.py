from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from renderdoc_mcp.errors import InvalidInvestigationIDError


class InvestigationRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}

    def create(self, name: str | None = None) -> dict[str, Any]:
        now = time.time()
        investigation_id = uuid.uuid4().hex
        item: dict[str, Any] = {
            "investigation_id": investigation_id,
            "name": name or f"RenderDoc investigation {investigation_id[:8]}",
            "created_at_unix": now,
            "updated_at_unix": now,
            "captures": {},
            "focus": {},
            "evidence": {},
        }
        with self._lock:
            self._items[investigation_id] = item
        return _copy_item(item)

    def get(self, investigation_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.get(investigation_id)
            if item is None:
                raise InvalidInvestigationIDError(investigation_id, list(self._items))
            return _copy_item(item)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(
                self._items.values(),
                key=lambda item: (-float(item["updated_at_unix"]), str(item["investigation_id"])),
            )
            return [_copy_item(item) for item in items]

    def add_capture(self, investigation_id: str, label: str, capture_id: str, capture_path: str) -> dict[str, Any]:
        with self._lock:
            item = self._require(investigation_id)
            item["captures"][label] = {"capture_id": capture_id, "capture_path": capture_path}
            item["updated_at_unix"] = time.time()
            return _copy_item(item)

    def set_focus(self, investigation_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = self._require(investigation_id)
            item["focus"].update(values)
            item["updated_at_unix"] = time.time()
            return _copy_item(item)

    def pin_evidence(self, investigation_id: str, name: str, evidence: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = self._require(investigation_id)
            item["evidence"][name] = dict(evidence)
            item["updated_at_unix"] = time.time()
            return _copy_item(item)

    def close(self, investigation_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.pop(investigation_id, None)
            if item is None:
                raise InvalidInvestigationIDError(investigation_id, list(self._items))
            return _copy_item(item)

    def _require(self, investigation_id: str) -> dict[str, Any]:
        item = self._items.get(investigation_id)
        if item is None:
            raise InvalidInvestigationIDError(investigation_id, list(self._items))
        return item


def _copy_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "investigation_id": item["investigation_id"],
        "name": item["name"],
        "created_at_unix": item["created_at_unix"],
        "updated_at_unix": item["updated_at_unix"],
        "captures": {key: dict(value) for key, value in item["captures"].items()},
        "focus": dict(item["focus"]),
        "evidence": {key: dict(value) for key, value in item["evidence"].items()},
    }
