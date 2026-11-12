#!/usr/bin/env python3
"""Fail a Crows evaluation run when it regresses against a baseline.

Example:
  python scripts/agent_eval_gate.py baseline=before.json candidate=after.json \\
    --max-pass-rate-drop 0 --max-latency-increase-percent 25
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read report {path.name}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"Report {path.name} must use schema_version 1")
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError(f"Report {path.name} must contain a non-empty results list")
    indexed: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise ValueError(f"Report {path.name} result {index} must be an object")
        name = item.get("name")
        passed = item.get("passed")
        latency = item.get("latency_ms")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError(f"Report {path.name} result {index} has an invalid case name")
        if name in indexed:
            raise ValueError(f"Report {path.name} contains duplicate case names")
        if type(passed) is not bool:
            raise ValueError(f"Report {path.name} result {index} needs a boolean passed field")
        if type(latency) is not int or latency < 0:
            raise ValueError(f"Report {path.name} result {index} needs non-negative integer latency_ms")
        indexed[name] = {"passed": passed, "latency_ms": latency}
    return indexed


def evaluate_gate(
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
    *,
    max_pass_rate_drop: float = 0.0,
    max_latency_increase_percent: float = 25.0,
) -> dict[str, Any]:
    if not baseline or not candidate:
        raise ValueError("Baseline and candidate reports must both contain cases")
    if set(baseline) != set(candidate):
        missing = len(set(baseline) - set(candidate))
        added = len(set(candidate) - set(baseline))
        raise ValueError(
            f"Case sets differ (missing from candidate: {missing}; added in candidate: {added})"
        )
    count = len(baseline)
    baseline_passed = sum(item["passed"] for item in baseline.values())
    candidate_passed = sum(item["passed"] for item in candidate.values())
    baseline_rate = baseline_passed / count
    candidate_rate = candidate_passed / count
    baseline_median = statistics.median(item["latency_ms"] for item in baseline.values())
    candidate_median = statistics.median(item["latency_ms"] for item in candidate.values())
    latency_increase = None
    if baseline_median > 0:
        latency_increase = (candidate_median - baseline_median) / baseline_median * 100
    regressions = sum(
        baseline[name]["passed"] and not candidate[name]["passed"] for name in baseline
    )
    improvements = sum(
        not baseline[name]["passed"] and candidate[name]["passed"] for name in baseline
    )
    pass_rate_drop = baseline_rate - candidate_rate
    failures = []
    if pass_rate_drop > max_pass_rate_drop:
        failures.append("pass_rate_regression")
    if (
        latency_increase is not None
        and latency_increase > max_latency_increase_percent
    ):
        failures.append("latency_regression")
    return {
        "schema_version": 1,
        "passed": not failures,
        "case_count": count,
        "baseline": {
            "passed": baseline_passed,
            "pass_rate": round(baseline_rate, 4),
            "median_latency_ms": baseline_median,
        },
        "candidate": {
            "passed": candidate_passed,
            "pass_rate": round(candidate_rate, 4),
            "median_latency_ms": candidate_median,
        },
        "pass_rate_drop": round(pass_rate_drop, 4),
        "max_pass_rate_drop": max_pass_rate_drop,
        "median_latency_increase_percent": (
            round(latency_increase, 2) if latency_increase is not None else None
        ),
        "max_latency_increase_percent": max_latency_increase_percent,
        "case_regressions": regressions,
        "case_improvements": improvements,
        "failures": failures,
        "privacy_note": "Only aggregate counts and timing are emitted; prompts, answers, tool data, and credentials are excluded.",
    }


def parse_named_paths(values: list[str]) -> tuple[Path, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("Reports must use baseline=path and candidate=path syntax")
        label, raw_path = value.split("=", 1)
        if label not in {"baseline", "candidate"} or not raw_path.strip() or label in parsed:
            raise ValueError("Provide exactly one baseline=path and one candidate=path")
        parsed[label] = Path(raw_path)
    if set(parsed) != {"baseline", "candidate"}:
        raise ValueError("Provide exactly one baseline=path and one candidate=path")
    return parsed["baseline"], parsed["candidate"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", help="baseline=report.json candidate=report.json")
    parser.add_argument("--max-pass-rate-drop", type=float, default=0.0)
    parser.add_argument("--max-latency-increase-percent", type=float, default=25.0)
    parser.add_argument("--output", type=Path, default=Path("agent-eval-gate.json"))
    args = parser.parse_args()
    if not 0 <= args.max_pass_rate_drop <= 1:
        parser.error("--max-pass-rate-drop must be between 0 and 1")
    if args.max_latency_increase_percent < 0:
        parser.error("--max-latency-increase-percent must be non-negative")
    try:
        baseline_path, candidate_path = parse_named_paths(args.reports)
        report = evaluate_gate(
            load_results(baseline_path),
            load_results(candidate_path),
            max_pass_rate_drop=args.max_pass_rate_drop,
            max_latency_increase_percent=args.max_latency_increase_percent,
        )
        args.output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"Evaluation gate {'PASSED' if report['passed'] else 'FAILED'}: "
        f"{report['candidate']['passed']}/{report['case_count']} passed; "
        f"median latency {report['candidate']['median_latency_ms']} ms"
    )
    if report["failures"]:
        print("Regressions: " + ", ".join(report["failures"]), file=sys.stderr)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
