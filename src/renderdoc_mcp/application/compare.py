from __future__ import annotations

import hashlib
import json
from typing import Any

_VOLATILE_KEYS = {
    "capture_id",
    "capture_path",
    "action_id",
    "event_id",
    "start_event_id",
    "end_event_id",
    "pass_id",
    "parent_pass_id",
    "shader_id",
    "resource_id",
    "secondary_resource_id",
    "view_id",
    "descriptor_store_id",
    "graphics_pipeline_object",
    "compute_pipeline_object",
    "parent_event_id",
}


def build_event_snapshot(dossier: dict[str, Any]) -> dict[str, Any]:
    return _canonicalize(
        {
            "api": dossier.get("api", ""),
            "action": dossier.get("action") or {},
            "pass": dossier.get("pass"),
            "pipeline": dossier.get("pipeline") or {},
            "bindings": dossier.get("bindings") or {},
        }
    )


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compare_event_dossiers(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    max_changes: int = 200,
) -> dict[str, Any]:
    baseline_snapshot = build_event_snapshot(baseline)
    candidate_snapshot = build_event_snapshot(candidate)
    bounded_limit = max(1, max_changes)
    changes: list[dict[str, Any]] = []
    _collect_changes(baseline_snapshot, candidate_snapshot, "$", changes, bounded_limit + 1)
    changes_truncated = len(changes) > bounded_limit
    if changes_truncated:
        changes = changes[:bounded_limit]
    baseline_fingerprint = fingerprint(baseline_snapshot)
    candidate_fingerprint = fingerprint(candidate_snapshot)
    return {
        "equivalent": baseline_fingerprint == candidate_fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "candidate_fingerprint": candidate_fingerprint,
        "changes": changes,
        "change_count": len(changes),
        "changes_truncated": changes_truncated,
        "baseline_snapshot": baseline_snapshot,
        "candidate_snapshot": candidate_snapshot,
    }


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_KEYS and str(key) != "meta"
        }
    if isinstance(value, list):
        canonical_items = [_canonicalize(item) for item in value]
        if all(isinstance(item, dict) for item in canonical_items):
            return sorted(
                canonical_items,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        return canonical_items
    return value


def _collect_changes(before: Any, after: Any, path: str, output: list[dict[str, Any]], limit: int) -> None:
    if len(output) >= limit:
        return
    if type(before) is not type(after):
        output.append({"path": path, "kind": "type_changed", "before": _preview(before), "after": _preview(after)})
        return
    if isinstance(before, dict):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}"
            if key not in before:
                output.append({"path": child_path, "kind": "added", "after": _preview(after[key])})
            elif key not in after:
                output.append({"path": child_path, "kind": "removed", "before": _preview(before[key])})
            else:
                _collect_changes(before[key], after[key], child_path, output, limit)
            if len(output) >= limit:
                return
        return
    if isinstance(before, list):
        for index in range(max(len(before), len(after))):
            child_path = f"{path}[{index}]"
            if index >= len(before):
                output.append({"path": child_path, "kind": "added", "after": _preview(after[index])})
            elif index >= len(after):
                output.append({"path": child_path, "kind": "removed", "before": _preview(before[index])})
            else:
                _collect_changes(before[index], after[index], child_path, output, limit)
            if len(output) >= limit:
                return
        return
    if before != after:
        output.append({"path": path, "kind": "changed", "before": _preview(before), "after": _preview(after)})


def _preview(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    if depth >= 3:
        if isinstance(value, dict):
            return {"summary": "object", "key_count": len(value)}
        if isinstance(value, list):
            return ["...", {"item_count": len(value)}]
        return value
    if isinstance(value, dict):
        keys = sorted(value, key=str)
        dict_preview: dict[str, Any] = {str(key): _preview(value[key], depth + 1) for key in keys[:20]}
        if len(keys) > 20:
            dict_preview["..."] = {"omitted_key_count": len(keys) - 20}
        return dict_preview
    if isinstance(value, list):
        list_preview: list[Any] = [_preview(item, depth + 1) for item in value[:20]]
        if len(value) > 20:
            list_preview.append({"omitted_item_count": len(value) - 20})
        return list_preview
    return value
