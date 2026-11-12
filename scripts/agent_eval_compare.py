#!/usr/bin/env python3
"""Compare privacy-conscious Crows agent-evaluation reports.

Example:
  python scripts/agent_eval_compare.py run-auto=auto.json run-manual=manual.json \
    --output agent-eval-comparison.json

Only aggregate evaluation metadata is emitted; prompts and answers are never read
or copied into the comparison report.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_report(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read report {path.name}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"Report {path.name} must use schema_version 1")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"Report {path.name} must contain a results list")
    validated = []
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise ValueError(f"Report {path.name} result {index} must be an object")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError(f"Report {path.name} result {index} needs a case name")
        if type(item.get("passed")) is not bool:
            raise ValueError(f"Report {path.name} result {index} needs a boolean passed field")
        latency = item.get("latency_ms")
        if type(latency) is not int or latency < 0:
            raise ValueError(f"Report {path.name} result {index} needs non-negative integer latency_ms")
        mode = item.get("mode", "auto")
        if mode not in {"auto", "manual"}:
            raise ValueError(f"Report {path.name} result {index} has invalid mode")
        route = item.get("route")
        model = item.get("model")
        if route is not None and (not isinstance(route, str) or len(route) > 32):
            raise ValueError(f"Report {path.name} result {index} has invalid route")
        if model is not None and (not isinstance(model, str) or len(model) > 128):
            raise ValueError(f"Report {path.name} result {index} has invalid model")
        fallback = item.get("fallback_used")
        if fallback is not None and type(fallback) is not bool:
            raise ValueError(f"Report {path.name} result {index} has invalid fallback_used")
        attempts = item.get("attempts")
        if attempts is not None and (type(attempts) is not int or not 1 <= attempts <= 100):
            raise ValueError(f"Report {path.name} result {index} has invalid attempts")
        validated.append({
            "name": item["name"],
            "passed": item["passed"],
            "latency_ms": latency,
            "mode": mode,
            "route": route,
            "model": model,
            "fallback_used": fallback,
            "attempts": attempts,
        })
    return validated


def compare_reports(named_paths: list[tuple[str, Path]]) -> dict[str, Any]:
    groups: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
    sources = []
    for label, path in named_paths:
        results = load_report(path)
        sources.append({"label": label, "case_count": len(results)})
        for item in results:
            groups[(item["mode"], item["route"], item["model"])].append(item)

    summaries = []
    for (mode, route, model), items in sorted(
        groups.items(), key=lambda entry: (entry[0][0], entry[0][1] or "", entry[0][2] or "")
    ):
        passed = sum(item["passed"] for item in items)
        latencies = [item["latency_ms"] for item in items]
        fallback_values = [item["fallback_used"] for item in items if item["fallback_used"] is not None]
        attempt_values = [item["attempts"] for item in items if item["attempts"] is not None]
        summaries.append({
            "mode": mode,
            "route": route,
            "model": model,
            "case_count": len(items),
            "passed": passed,
            "failed": len(items) - passed,
            "pass_rate": round(passed / len(items), 4),
            "latency_ms_mean": round(statistics.fmean(latencies), 2),
            "latency_ms_median": statistics.median(latencies),
            "fallback_observations": len(fallback_values),
            "fallback_count": sum(fallback_values),
            "fallback_rate": round(sum(fallback_values) / len(fallback_values), 4) if fallback_values else None,
            "attempt_observations": len(attempt_values),
            "attempts_mean": round(statistics.fmean(attempt_values), 2) if attempt_values else None,
        })
    return {
        "schema_version": 1,
        "source_reports": sources,
        "group_count": len(summaries),
        "groups": summaries,
        "interpretation_note": (
            "Descriptive results only. Compare identical case sets and repeat runs; "
            "differences do not establish statistical significance or general model superiority."
        ),
        "privacy_note": "Case prompts, answers, tool arguments, and credentials are excluded.",
    }


def parse_named_paths(values: list[str]) -> list[tuple[str, Path]]:
    parsed = []
    labels = set()
    for value in values:
        if "=" not in value:
            raise ValueError("Each report must use label=path syntax")
        label, raw_path = value.split("=", 1)
        if not label.strip() or not raw_path.strip() or label in labels:
            raise ValueError("Report labels and paths must be non-empty; labels must be unique")
        labels.add(label)
        parsed.append((label, Path(raw_path)))
    if not parsed:
        raise ValueError("At least one report is required")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", help="One or more label=report.json inputs")
    parser.add_argument("--output", type=Path, default=Path("agent-eval-comparison.json"))
    args = parser.parse_args()
    try:
        named_paths = parse_named_paths(args.reports)
        comparison = compare_reports(named_paths)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.output.write_text(json.dumps(comparison, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Compared {len(named_paths)} reports across {comparison['group_count']} route/mode groups; output: {args.output}")
    for item in comparison["groups"]:
        route = item["route"] or "unknown"
        model = item["model"] or "unknown"
        print(
            f"{item['mode']} / {route} / {model}: "
            f"{item['passed']}/{item['case_count']} passed, "
            f"mean {item['latency_ms_mean']} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
