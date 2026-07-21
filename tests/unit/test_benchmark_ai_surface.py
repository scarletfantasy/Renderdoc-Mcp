from __future__ import annotations

from renderdoc_mcp.benchmark_ai_surface import (
    WORKFLOW_VERSION,
    CallMetric,
    build_acceptance,
    build_delta,
    build_ref_comparison,
    build_scores,
    find_previous_entry,
    sanitize_call_args,
    summarize_history_scenarios,
)


def test_build_scores_rewards_smaller_payload_and_lower_latency() -> None:
    compact = build_scores({"approx_tokens": 10_000, "total_elapsed_ms": 500.0})
    heavy = build_scores({"approx_tokens": 100_000, "total_elapsed_ms": 3_000.0})

    assert compact["payload_score"] > heavy["payload_score"]
    assert compact["latency_score"] > heavy["latency_score"]
    assert compact["composite_score"] > heavy["composite_score"]


def test_find_previous_entry_filters_by_workflow_and_capture() -> None:
    history = [
        {"workflow_version": "other", "capture": {"label": "0916"}},
        {"workflow_version": WORKFLOW_VERSION, "capture": {"label": "other"}},
        {"workflow_version": WORKFLOW_VERSION, "capture": {"label": "0916"}, "git": {"commit": "abc123"}},
    ]

    entry = find_previous_entry(history, WORKFLOW_VERSION, "0916")

    assert entry is not None
    assert entry["git"]["commit"] == "abc123"


def test_build_delta_reports_improvement_against_previous_result() -> None:
    previous = {
        "git": {"commit": "old"},
        "scores": {
            "payload_score": 400.0,
            "latency_score": 500.0,
            "composite_score": 415.0,
        },
        "summary": {
            "stages": {
                "interactive": {
                    "total_bytes": 50_000,
                    "approx_tokens": 12_500,
                    "total_elapsed_ms": 600.0,
                }
            }
        },
    }
    current = {
        "scores": {
            "payload_score": 600.0,
            "latency_score": 550.0,
            "composite_score": 592.5,
        },
        "summary": {
            "stages": {
                "interactive": {
                    "total_bytes": 20_000,
                    "approx_tokens": 5_000,
                    "total_elapsed_ms": 450.0,
                }
            }
        },
    }

    delta = build_delta(current, previous)

    assert delta is not None
    assert delta["vs_commit"] == "old"
    assert delta["payload_score_delta"] == 200.0
    assert delta["interactive_bytes_delta"] == -30_000
    assert delta["interactive_tokens_delta"] == -7_500
    assert delta["interactive_elapsed_ms_delta"] == -150.0


def test_build_ref_comparison_reports_percentages_and_startup_delta() -> None:
    baseline = {
        "git": {"commit": "old", "branch": "HEAD^"},
        "scores": {
            "payload_score": 100.0,
            "latency_score": 900.0,
            "composite_score": 220.0,
        },
        "summary": {
            "stages": {
                "interactive": {
                    "total_bytes": 100_000,
                    "approx_tokens": 25_000,
                    "total_elapsed_ms": 500.0,
                }
            }
        },
        "calls": [
            {
                "label": "open_capture",
                "bytes": 500,
                "approx_tokens": 125,
                "elapsed_ms": 4_000.0,
            }
        ],
    }
    current = {
        "scores": {
            "payload_score": 700.0,
            "latency_score": 800.0,
            "composite_score": 715.0,
        },
        "summary": {
            "stages": {
                "interactive": {
                    "total_bytes": 20_000,
                    "approx_tokens": 5_000,
                    "total_elapsed_ms": 650.0,
                }
            }
        },
        "calls": [
            {
                "label": "open_capture",
                "bytes": 650,
                "approx_tokens": 163,
                "elapsed_ms": 3_600.0,
            }
        ],
    }

    comparison = build_ref_comparison(current, baseline)

    assert comparison["baseline_git"]["commit"] == "old"
    assert comparison["score_delta"]["payload_delta"] == 600.0
    assert comparison["interactive_delta"]["bytes_delta"] == -80_000
    assert comparison["interactive_delta"]["bytes_pct"] == -80.0
    assert comparison["interactive_delta"]["elapsed_ms_delta"] == 150.0
    assert comparison["interactive_delta"]["elapsed_ms_pct"] == 30.0
    assert comparison["startup_delta"] is not None
    assert comparison["startup_delta"]["bytes_delta"] == 150
    assert comparison["startup_delta"]["elapsed_ms_delta"] == -400.0


def test_sanitize_call_args_redacts_local_paths() -> None:
    sanitized = sanitize_call_args(
        {
            "capture_path": r"C:\captures\sample.rdc",
            "capture_id": "abc123",
            "event_id": 42,
        }
    )

    assert sanitized["capture_path"] == "<redacted>"
    assert sanitized["capture_id"] == "abc123"
    assert sanitized["event_id"] == 42


def test_history_scenarios_encode_call_budgets_from_real_workflows() -> None:
    metrics = [
        CallMetric("search_actions", "renderdoc_search_actions", {}, 1.0, 100, 25),
        CallMetric("correctness_worklist", "renderdoc_get_analysis_worklist", {}, 1.0, 100, 25),
        CallMetric("event_dossier", "renderdoc_get_event_dossier", {}, 1.0, 100, 25),
        CallMetric("shader_summary", "renderdoc_get_shader_summary", {}, 1.0, 100, 25),
        CallMetric("shader_search", "renderdoc_search_shader_code", {}, 1.0, 100, 25),
    ]

    scenarios = summarize_history_scenarios(metrics)

    assert scenarios["nested_action_discovery"]["within_call_budget"] is True
    assert scenarios["correctness_event_dossier"]["call_count"] == 2
    assert scenarios["shader_root_cause"]["within_call_budget"] is True


def test_acceptance_rejects_a_response_over_256_kib() -> None:
    metrics = [CallMetric("event_dossier", "renderdoc_get_event_dossier", {}, 1.0, 300_000, 75_000)]
    stages = {
        "interactive": {
            "call_count": 1,
            "total_bytes": 300_000,
            "approx_tokens": 75_000,
            "total_elapsed_ms": 1.0,
            "largest_call": {"bytes": 300_000},
        }
    }

    acceptance = build_acceptance(metrics, stages)

    assert acceptance["checks"]["largest_response_within_256_kib"] is False
    assert acceptance["passed"] is False
