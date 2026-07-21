from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

WORKFLOW_VERSION = "ai_surface_v1"
TOKEN_BUDGET = 25_000
LATENCY_BUDGET_MS = 5_000.0
PAYLOAD_WEIGHT = 0.85
LATENCY_WEIGHT = 0.15
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 300.0
DEFAULT_HISTORY_PATH = Path("benchmarks") / "ai_surface_history.jsonl"
INTERACTIVE_LABELS = {
    "capture_overview",
    "analysis_worklist",
    "list_passes",
    "pass_summary",
    "timing_events",
    "list_actions",
    "action_summary",
    "pipeline_overview",
    "pipeline_bindings",
    "shader_summary",
    "shader_code_chunk",
    "list_resources",
    "resource_summary",
}
STAGE_GROUPS = {
    "overview": {"capture_overview", "analysis_worklist"},
    "pass_drilldown": {"list_passes", "pass_summary", "timing_events"},
    "event_drilldown": {
        "list_actions",
        "action_summary",
        "pipeline_overview",
        "pipeline_bindings",
        "shader_summary",
        "shader_code_chunk",
    },
    "resource_drilldown": {"list_resources", "resource_summary"},
    "interactive": INTERACTIVE_LABELS,
}
LEGACY_INTERACTIVE_LABELS = {
    "capture_summary",
    "analyze_frame",
    "list_passes",
    "pass_details",
    "timing_data",
    "list_actions",
    "action_details",
    "pipeline_state",
    "shader_code",
    "list_resources",
}
SENSITIVE_ARG_KEYS = {
    "capture_path",
    "output_path",
}


@dataclass(slots=True)
class CallMetric:
    label: str
    tool: str
    args: dict[str, Any]
    elapsed_ms: float
    bytes: int
    approx_tokens: int


@dataclass(slots=True)
class BenchmarkConfig:
    capture_path: Path
    capture_label: str
    python_executable: str
    timeout_seconds: float
    history_path: Path
    append_history: bool
    note: str
    repo_root: Path
    compare_ref: str | None


def size_bytes(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def approx_tokens(byte_count: int) -> int:
    return int(round(byte_count / 4.0))


def compute_payload_score(total_tokens: int) -> float:
    return round(1000.0 / (1.0 + (float(total_tokens) / float(TOKEN_BUDGET))), 1)


def compute_latency_score(total_elapsed_ms: float) -> float:
    return round(1000.0 / (1.0 + (float(total_elapsed_ms) / LATENCY_BUDGET_MS)), 1)


def compute_composite_score(payload_score: float, latency_score: float) -> float:
    return round((payload_score * PAYLOAD_WEIGHT) + (latency_score * LATENCY_WEIGHT), 1)


def _largest_call(metrics: list[CallMetric]) -> dict[str, Any] | None:
    if not metrics:
        return None
    item = max(metrics, key=lambda metric: metric.bytes)
    return {
        "label": item.label,
        "tool": item.tool,
        "bytes": item.bytes,
        "approx_tokens": item.approx_tokens,
        "elapsed_ms": item.elapsed_ms,
    }


def summarize_metrics(metrics: list[CallMetric], labels: set[str]) -> dict[str, Any]:
    selected = [metric for metric in metrics if metric.label in labels]
    total_bytes = sum(metric.bytes for metric in selected)
    total_tokens = sum(metric.approx_tokens for metric in selected)
    total_elapsed_ms = round(sum(metric.elapsed_ms for metric in selected), 1)
    return {
        "call_count": len(selected),
        "total_bytes": total_bytes,
        "approx_tokens": total_tokens,
        "total_elapsed_ms": total_elapsed_ms,
        "largest_call": _largest_call(selected),
    }


def build_scores(interactive_summary: dict[str, Any]) -> dict[str, float]:
    payload_score = compute_payload_score(interactive_summary["approx_tokens"])
    latency_score = compute_latency_score(interactive_summary["total_elapsed_ms"])
    composite_score = compute_composite_score(payload_score, latency_score)
    return {
        "payload_score": payload_score,
        "latency_score": latency_score,
        "composite_score": composite_score,
    }


def load_history(history_path: Path) -> list[dict[str, Any]]:
    if not history_path.is_file():
        return []

    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(history_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {history_path} at line {line_number}.") from exc
    return entries


def find_previous_entry(
    history: list[dict[str, Any]],
    workflow_version: str,
    capture_label: str,
) -> dict[str, Any] | None:
    for entry in reversed(history):
        if entry.get("workflow_version") != workflow_version:
            continue
        capture = entry.get("capture") or {}
        if capture.get("label") != capture_label:
            continue
        return entry
    return None


def build_delta(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    if previous is None:
        return None

    current_scores = current.get("scores") or {}
    previous_scores = previous.get("scores") or {}
    current_interactive = (current.get("summary") or {}).get("stages", {}).get("interactive", {})
    previous_interactive = (previous.get("summary") or {}).get("stages", {}).get("interactive", {})
    return {
        "vs_commit": (previous.get("git") or {}).get("commit"),
        "payload_score_delta": round(
            float(current_scores.get("payload_score", 0.0)) - float(previous_scores.get("payload_score", 0.0)),
            1,
        ),
        "latency_score_delta": round(
            float(current_scores.get("latency_score", 0.0)) - float(previous_scores.get("latency_score", 0.0)),
            1,
        ),
        "composite_score_delta": round(
            float(current_scores.get("composite_score", 0.0)) - float(previous_scores.get("composite_score", 0.0)),
            1,
        ),
        "interactive_bytes_delta": int(current_interactive.get("total_bytes", 0)) - int(
            previous_interactive.get("total_bytes", 0)
        ),
        "interactive_tokens_delta": int(current_interactive.get("approx_tokens", 0)) - int(
            previous_interactive.get("approx_tokens", 0)
        ),
        "interactive_elapsed_ms_delta": round(
            float(current_interactive.get("total_elapsed_ms", 0.0))
            - float(previous_interactive.get("total_elapsed_ms", 0.0)),
            1,
        ),
    }


def build_ref_comparison(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    delta = build_delta(current, baseline)
    if delta is None:
        raise RuntimeError("build_ref_comparison requires a baseline entry.")

    current_interactive = (current.get("summary") or {}).get("stages", {}).get("interactive", {})
    baseline_interactive = (baseline.get("summary") or {}).get("stages", {}).get("interactive", {})
    current_startup = startup_call(current)
    baseline_startup = startup_call(baseline)

    startup_delta = None
    if current_startup is not None and baseline_startup is not None:
        startup_delta = {
            "bytes_delta": int(current_startup.get("bytes", 0)) - int(baseline_startup.get("bytes", 0)),
            "tokens_delta": int(current_startup.get("approx_tokens", 0)) - int(baseline_startup.get("approx_tokens", 0)),
            "elapsed_ms_delta": round(
                float(current_startup.get("elapsed_ms", 0.0)) - float(baseline_startup.get("elapsed_ms", 0.0)),
                1,
            ),
            "bytes_pct": percent_change(
                int(current_startup.get("bytes", 0)),
                int(baseline_startup.get("bytes", 0)),
            ),
            "tokens_pct": percent_change(
                int(current_startup.get("approx_tokens", 0)),
                int(baseline_startup.get("approx_tokens", 0)),
            ),
            "elapsed_ms_pct": percent_change(
                float(current_startup.get("elapsed_ms", 0.0)),
                float(baseline_startup.get("elapsed_ms", 0.0)),
            ),
        }

    return {
        "baseline_git": baseline.get("git") or {},
        "baseline_scores": baseline.get("scores") or {},
        "baseline_interactive": baseline_interactive,
        "baseline_startup": baseline_startup,
        "score_delta": {
            "composite_delta": delta["composite_score_delta"],
            "payload_delta": delta["payload_score_delta"],
            "latency_delta": delta["latency_score_delta"],
        },
        "interactive_delta": {
            "bytes_delta": delta["interactive_bytes_delta"],
            "tokens_delta": delta["interactive_tokens_delta"],
            "elapsed_ms_delta": delta["interactive_elapsed_ms_delta"],
            "bytes_pct": percent_change(
                int(current_interactive.get("total_bytes", 0)),
                int(baseline_interactive.get("total_bytes", 0)),
            ),
            "tokens_pct": percent_change(
                int(current_interactive.get("approx_tokens", 0)),
                int(baseline_interactive.get("approx_tokens", 0)),
            ),
            "elapsed_ms_pct": percent_change(
                float(current_interactive.get("total_elapsed_ms", 0.0)),
                float(baseline_interactive.get("total_elapsed_ms", 0.0)),
            ),
        },
        "startup_delta": startup_delta,
    }


def append_history(history_path: Path, entry: dict[str, Any]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def git_info(cwd: Path) -> dict[str, Any]:
    def read_git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    commit = read_git("rev-parse", "HEAD")
    branch = read_git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(read_git("status", "--short"))
    return {
        "commit": commit,
        "branch": branch,
        "dirty": dirty,
    }


def git_ref_info(cwd: Path, ref: str) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Unable to resolve git ref {ref!r}: {result.stderr.strip()}")
    return {
        "commit": result.stdout.strip(),
        "branch": ref,
        "dirty": False,
    }


def extract_git_ref(cwd: Path, ref: str, destination: Path) -> None:
    result = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=str(cwd),
        capture_output=True,
        check=False,
        timeout=30.0,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Unable to archive git ref {ref!r}: {result.stderr.decode('utf-8', errors='replace')}")

    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        archive.extractall(destination)


def capture_info(capture_path: Path, capture_label: str) -> dict[str, Any]:
    stat_result = capture_path.stat()
    return {
        "label": capture_label,
        "size_bytes": int(stat_result.st_size),
    }


def server_env(timeout_seconds: float, source_root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["RENDERDOC_BRIDGE_TIMEOUT_SECONDS"] = str(timeout_seconds)
    if source_root is not None:
        env["PYTHONPATH"] = str(source_root / "src")
    return env


def startup_call(entry: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in entry.get("calls", []) if item.get("label") == "open_capture"), None)


def percent_change(current_value: float | int, baseline_value: float | int) -> float | None:
    if float(baseline_value) == 0.0:
        return None
    return round(((float(current_value) / float(baseline_value)) - 1.0) * 100.0, 1)


def sanitize_call_args(args: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in args.items():
        if key in SENSITIVE_ARG_KEYS:
            sanitized[key] = "<redacted>"
        else:
            sanitized[key] = value
    return sanitized


async def _call_tool(
    session: ClientSession,
    metrics: list[CallMetric],
    tool: str,
    args: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = await session.call_tool(tool, args)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if result.isError:
        raise RuntimeError(f"{tool} failed: {result.content}")

    payload = result.structuredContent
    if payload is None:
        raise RuntimeError(f"{tool} returned no structured content.")

    metrics.append(
        CallMetric(
            label=label,
            tool=tool,
            args=sanitize_call_args(args),
            elapsed_ms=elapsed_ms,
            bytes=size_bytes(payload),
            approx_tokens=approx_tokens(size_bytes(payload)),
        )
    )
    return payload


def _worklist_pass_id(worklist_payload: dict[str, Any]) -> str | None:
    for item in worklist_payload.get("items", []):
        if item.get("kind") == "pass":
            identifier = item.get("id")
            if isinstance(identifier, str) and identifier:
                return identifier
    return None


def _representative_event_id(pass_summary_payload: dict[str, Any]) -> int | None:
    for item in pass_summary_payload.get("representative_events", []):
        flags = set(item.get("flags") or [])
        if flags.intersection({"draw", "dispatch"}):
            try:
                return int(item["event_id"])
            except (KeyError, TypeError, ValueError):
                continue

    event_range = pass_summary_payload.get("event_range") or {}
    try:
        return int(event_range["start_event_id"])
    except (KeyError, TypeError, ValueError):
        return None


def _shader_stage(pipeline_overview_payload: dict[str, Any]) -> str | None:
    shaders = ((pipeline_overview_payload.get("pipeline") or {}).get("shaders")) or []
    if not shaders:
        return None
    stage = shaders[0].get("stage")
    return str(stage) if stage else None


def _resource_id(resource_list_payload: dict[str, Any]) -> str | None:
    for item in resource_list_payload.get("items", []):
        resource_id = item.get("resource_id")
        if resource_id:
            return str(resource_id)
    return None


async def run_workflow(config: BenchmarkConfig) -> dict[str, Any]:
    params = StdioServerParameters(
        command=config.python_executable,
        args=["-m", "renderdoc_mcp"],
        env=server_env(config.timeout_seconds),
    )

    metrics: list[CallMetric] = []
    skipped_steps: list[str] = []
    renderdoc_version = ""
    capture_id = ""
    pass_id: str | None = None
    event_id: int | None = None
    stage: str | None = None

    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            opened = await _call_tool(
                session,
                metrics,
                "renderdoc_open_capture",
                {"capture_path": str(config.capture_path)},
                "open_capture",
            )
            capture_id = str(opened["capture_id"])
            renderdoc_version = str((opened.get("meta") or {}).get("renderdoc_version") or "")

            try:
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_capture_overview",
                    {"capture_id": capture_id},
                    "capture_overview",
                )
                worklist = await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_analysis_worklist",
                    {"capture_id": capture_id, "focus": "performance", "limit": 10},
                    "analysis_worklist",
                )
                pass_id = _worklist_pass_id(worklist)
                if pass_id is None:
                    listed_passes = await _call_tool(
                        session,
                        metrics,
                        "renderdoc_list_passes",
                        {"capture_id": capture_id, "limit": 10, "sort_by": "gpu_time"},
                        "list_passes",
                    )
                    passes = listed_passes.get("passes") or []
                    if not passes:
                        raise RuntimeError("Benchmark workflow found no passes to inspect.")
                    pass_id = str(passes[0]["pass_id"])
                else:
                    await _call_tool(
                        session,
                        metrics,
                        "renderdoc_list_passes",
                        {"capture_id": capture_id, "limit": 10, "sort_by": "gpu_time"},
                        "list_passes",
                    )

                pass_summary = await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_pass_summary",
                    {"capture_id": capture_id, "pass_id": pass_id},
                    "pass_summary",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_timing_events",
                    {
                        "capture_id": capture_id,
                        "pass_id": pass_id,
                        "limit": 100,
                        "sort_by": "gpu_time",
                    },
                    "timing_events",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_actions",
                    {"capture_id": capture_id, "limit": 50},
                    "list_actions",
                )

                event_id = _representative_event_id(pass_summary)
                if event_id is None:
                    raise RuntimeError("Benchmark workflow could not choose a representative event.")

                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_action_summary",
                    {"capture_id": capture_id, "event_id": event_id},
                    "action_summary",
                )
                pipeline_overview = await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_pipeline_overview",
                    {"capture_id": capture_id, "event_id": event_id},
                    "pipeline_overview",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_pipeline_bindings",
                    {
                        "capture_id": capture_id,
                        "event_id": event_id,
                        "binding_kind": "descriptor_accesses",
                        "limit": 50,
                    },
                    "pipeline_bindings",
                )

                stage = _shader_stage(pipeline_overview)
                if stage is None:
                    skipped_steps.extend(["shader_summary", "shader_code_chunk"])
                else:
                    await _call_tool(
                        session,
                        metrics,
                        "renderdoc_get_shader_summary",
                        {"capture_id": capture_id, "event_id": event_id, "stage": stage},
                        "shader_summary",
                    )
                    await _call_tool(
                        session,
                        metrics,
                        "renderdoc_get_shader_code_chunk",
                        {
                            "capture_id": capture_id,
                            "event_id": event_id,
                            "stage": stage,
                            "start_line": 1,
                            "line_count": 200,
                        },
                        "shader_code_chunk",
                    )

                resources = await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_resources",
                    {"capture_id": capture_id, "kind": "all", "limit": 50, "sort_by": "size"},
                    "list_resources",
                )
                resource_id = _resource_id(resources)
                if resource_id is None:
                    skipped_steps.append("resource_summary")
                else:
                    await _call_tool(
                        session,
                        metrics,
                        "renderdoc_get_resource_summary",
                        {"capture_id": capture_id, "resource_id": resource_id},
                        "resource_summary",
                    )
            finally:
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_close_capture",
                    {"capture_id": capture_id},
                    "close_capture",
                )

    stages = {name: summarize_metrics(metrics, labels) for name, labels in STAGE_GROUPS.items()}
    scores = build_scores(stages["interactive"])
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_version": WORKFLOW_VERSION,
        "capture": capture_info(config.capture_path, config.capture_label),
        "git": git_info(config.repo_root),
        "environment": {
            "python_executable_name": Path(config.python_executable).name,
            "renderdoc_version": renderdoc_version,
            "bridge_timeout_seconds": config.timeout_seconds,
        },
        "scores": scores,
        "summary": {
            "stages": stages,
            "skipped_steps": skipped_steps,
        },
        "calls": [asdict(metric) for metric in metrics],
        "selection": {
            "pass_id": pass_id or None,
            "event_id": event_id,
            "stage": stage,
        },
        "note": config.note,
    }


async def run_legacy_workflow(
    config: BenchmarkConfig,
    *,
    source_root: Path,
    ref: str,
    pass_id: str,
    event_id: int,
    stage: str | None,
) -> dict[str, Any]:
    params = StdioServerParameters(
        command=config.python_executable,
        args=["-m", "renderdoc_mcp"],
        env=server_env(config.timeout_seconds, source_root),
    )

    metrics: list[CallMetric] = []
    renderdoc_version = ""
    capture_id = ""
    skipped_steps: list[str] = []

    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            opened = await _call_tool(
                session,
                metrics,
                "renderdoc_open_capture",
                {"capture_path": str(config.capture_path)},
                "open_capture",
            )
            capture_id = str(opened["capture_id"])
            renderdoc_version = str((opened.get("meta") or {}).get("renderdoc_version") or "")

            try:
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_capture_summary",
                    {"capture_id": capture_id},
                    "capture_summary",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_analyze_frame",
                    {"capture_id": capture_id, "include_timing_summary": True},
                    "analyze_frame",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_passes",
                    {"capture_id": capture_id, "limit": 10, "sort_by": "gpu_time"},
                    "list_passes",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_pass_details",
                    {"capture_id": capture_id, "pass_id": pass_id},
                    "pass_details",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_timing_data",
                    {"capture_id": capture_id, "pass_id": pass_id},
                    "timing_data",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_actions",
                    {"capture_id": capture_id, "limit": 50},
                    "list_actions",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_action_details",
                    {"capture_id": capture_id, "event_id": event_id},
                    "action_details",
                )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_get_pipeline_state",
                    {"capture_id": capture_id, "event_id": event_id},
                    "pipeline_state",
                )
                if stage is None:
                    skipped_steps.append("shader_code")
                else:
                    await _call_tool(
                        session,
                        metrics,
                        "renderdoc_get_shader_code",
                        {"capture_id": capture_id, "event_id": event_id, "stage": stage},
                        "shader_code",
                    )
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_list_resources",
                    {"capture_id": capture_id, "kind": "all"},
                    "list_resources",
                )
            finally:
                await _call_tool(
                    session,
                    metrics,
                    "renderdoc_close_capture",
                    {"capture_id": capture_id},
                    "close_capture",
                )

    stages = {
        "interactive": summarize_metrics(metrics, LEGACY_INTERACTIVE_LABELS),
    }
    scores = build_scores(stages["interactive"])
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_version": f"{WORKFLOW_VERSION}:legacy",
        "capture": capture_info(config.capture_path, config.capture_label),
        "git": git_ref_info(config.repo_root, ref),
        "environment": {
            "python_executable_name": Path(config.python_executable).name,
            "renderdoc_version": renderdoc_version,
            "bridge_timeout_seconds": config.timeout_seconds,
        },
        "scores": scores,
        "summary": {
            "stages": stages,
            "skipped_steps": skipped_steps,
        },
        "calls": [asdict(metric) for metric in metrics],
        "selection": {
            "pass_id": pass_id,
            "event_id": event_id,
            "stage": stage,
        },
        "note": f"Comparison baseline for {ref}",
    }


def print_summary(entry: dict[str, Any], delta: dict[str, Any] | None, history_path: Path) -> None:
    git = entry["git"]
    scores = entry["scores"]
    interactive = entry["summary"]["stages"]["interactive"]
    startup = startup_call(entry)

    print(f"Benchmark: {entry['workflow_version']}")
    print(f"Commit: {git['commit']} dirty={git['dirty']}")
    print(f"Capture: {entry['capture']['label']}")
    print(
        "Scores: composite={:.1f} payload={:.1f} latency={:.1f}".format(
            scores["composite_score"],
            scores["payload_score"],
            scores["latency_score"],
        )
    )
    print(
        "Interactive: bytes={} tokens={} elapsed_ms={}".format(
            interactive["total_bytes"],
            interactive["approx_tokens"],
            interactive["total_elapsed_ms"],
        )
    )
    if startup is not None:
        print(
            "Startup: bytes={} tokens={} elapsed_ms={}".format(
                startup["bytes"],
                startup["approx_tokens"],
                startup["elapsed_ms"],
            )
        )
    print(f"History: {history_path}")
    if delta is not None:
        print(
            "Delta: composite={:+.1f} payload={:+.1f} latency={:+.1f}".format(
                delta["composite_score_delta"],
                delta["payload_score_delta"],
                delta["latency_score_delta"],
            )
        )
        print(
            "Delta: interactive_bytes={:+d} interactive_tokens={:+d} interactive_elapsed_ms={:+.1f}".format(
                delta["interactive_bytes_delta"],
                delta["interactive_tokens_delta"],
                delta["interactive_elapsed_ms_delta"],
            )
        )


def print_ref_comparison(comparison: dict[str, Any]) -> None:
    baseline_git = comparison["baseline_git"]
    baseline_scores = comparison["baseline_scores"]
    baseline_interactive = comparison["baseline_interactive"]
    baseline_startup = comparison["baseline_startup"]
    score_delta = comparison["score_delta"]
    interactive_delta = comparison["interactive_delta"]
    startup_delta = comparison["startup_delta"]

    print(
        "Baseline: ref={} commit={}".format(
            baseline_git.get("branch", ""),
            baseline_git.get("commit", ""),
        )
    )
    print(
        "Baseline Scores: composite={:.1f} payload={:.1f} latency={:.1f}".format(
            float(baseline_scores.get("composite_score", 0.0)),
            float(baseline_scores.get("payload_score", 0.0)),
            float(baseline_scores.get("latency_score", 0.0)),
        )
    )
    print(
        "Baseline Interactive: bytes={} tokens={} elapsed_ms={}".format(
            baseline_interactive.get("total_bytes", 0),
            baseline_interactive.get("approx_tokens", 0),
            baseline_interactive.get("total_elapsed_ms", 0.0),
        )
    )
    if baseline_startup is not None:
        print(
            "Baseline Startup: bytes={} tokens={} elapsed_ms={}".format(
                baseline_startup.get("bytes", 0),
                baseline_startup.get("approx_tokens", 0),
                baseline_startup.get("elapsed_ms", 0.0),
            )
        )
    print(
        "Vs Baseline: composite={:+.1f} payload={:+.1f} latency={:+.1f}".format(
            score_delta["composite_delta"],
            score_delta["payload_delta"],
            score_delta["latency_delta"],
        )
    )
    print(
        "Vs Baseline: interactive_bytes={:+d} ({:+.1f}%) interactive_tokens={:+d} ({:+.1f}%) interactive_elapsed_ms={:+.1f} ({:+.1f}%)".format(
            interactive_delta["bytes_delta"],
            float(interactive_delta["bytes_pct"] or 0.0),
            interactive_delta["tokens_delta"],
            float(interactive_delta["tokens_pct"] or 0.0),
            interactive_delta["elapsed_ms_delta"],
            float(interactive_delta["elapsed_ms_pct"] or 0.0),
        )
    )
    if startup_delta is not None:
        print(
            "Vs Baseline: startup_bytes={:+d} ({:+.1f}%) startup_tokens={:+d} ({:+.1f}%) startup_elapsed_ms={:+.1f} ({:+.1f}%)".format(
                startup_delta["bytes_delta"],
                float(startup_delta["bytes_pct"] or 0.0),
                startup_delta["tokens_delta"],
                float(startup_delta["tokens_pct"] or 0.0),
                startup_delta["elapsed_ms_delta"],
                float(startup_delta["elapsed_ms_pct"] or 0.0),
            )
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the AI-first RenderDoc MCP workflow and append a score to the local history file. "
            "Timing-driven selections rely on replay-derived GPU timing and may vary across runs."
        )
    )
    parser.add_argument(
        "--capture",
        default=os.environ.get("RENDERDOC_MCP_CAPTURE", ""),
        help="Path to the .rdc capture. Defaults to $RENDERDOC_MCP_CAPTURE.",
    )
    parser.add_argument(
        "--capture-label",
        default="",
        help="Stable label used to group history entries for the same benchmark capture.",
    )
    parser.add_argument(
        "--python",
        dest="python_executable",
        default=sys.executable,
        help="Python executable used to launch `python -m renderdoc_mcp`.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_BRIDGE_TIMEOUT_SECONDS,
        help="Bridge startup timeout passed through to the benchmark server process.",
    )
    parser.add_argument(
        "--history-file",
        default=str(root / DEFAULT_HISTORY_PATH),
        help="JSONL file used to append benchmark runs.",
    )
    parser.add_argument(
        "--note",
        default="",
        help="Optional free-form note stored with the benchmark record.",
    )
    parser.add_argument(
        "--compare-ref",
        default="",
        help="Optional git ref to benchmark as a legacy baseline, for example `HEAD^`. Use deltas as directional signals because timing-driven selections are noisy.",
    )
    parser.add_argument(
        "--no-append",
        action="store_true",
        help="Run the benchmark without writing a history entry.",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> BenchmarkConfig:
    if not args.capture:
        raise SystemExit("Missing --capture and $RENDERDOC_MCP_CAPTURE is not set.")

    capture_path = Path(args.capture).expanduser().resolve()
    if not capture_path.is_file():
        raise SystemExit(f"Capture file does not exist: {capture_path}")

    capture_label = str(args.capture_label or capture_path.stem)
    root = repo_root()
    return BenchmarkConfig(
        capture_path=capture_path,
        capture_label=capture_label,
        python_executable=str(Path(args.python_executable).expanduser()),
        timeout_seconds=float(args.timeout_seconds),
        history_path=Path(args.history_file).expanduser(),
        append_history=not bool(args.no_append),
        note=str(args.note or ""),
        repo_root=root,
        compare_ref=str(args.compare_ref or "").strip() or None,
    )


async def _run(config: BenchmarkConfig) -> int:
    history = load_history(config.history_path)
    previous_entry = find_previous_entry(history, WORKFLOW_VERSION, config.capture_label)
    current_entry = await run_workflow(config)
    delta = build_delta(current_entry, previous_entry)
    ref_comparison = None

    if config.compare_ref:
        selection = current_entry.get("selection") or {}
        pass_id = selection.get("pass_id")
        event_id = selection.get("event_id")
        if not pass_id or event_id is None:
            raise RuntimeError("Current workflow did not produce a comparable pass/event selection.")

        with tempfile.TemporaryDirectory(prefix="renderdoc_mcp_legacy_") as temp_dir:
            legacy_root = Path(temp_dir) / "legacy"
            extract_git_ref(config.repo_root, config.compare_ref, legacy_root)
            legacy_entry = await run_legacy_workflow(
                config,
                source_root=legacy_root,
                ref=config.compare_ref,
                pass_id=str(pass_id),
                event_id=int(event_id),
                stage=selection.get("stage"),
            )
        ref_comparison = build_ref_comparison(current_entry, legacy_entry)

    if config.append_history:
        append_history(config.history_path, current_entry)

    print_summary(current_entry, delta, config.history_path)
    if ref_comparison is not None:
        print_ref_comparison(ref_comparison)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = build_config(args)
    return anyio.run(_run, config)


if __name__ == "__main__":
    raise SystemExit(main())
