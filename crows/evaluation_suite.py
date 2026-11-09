"""Versioned, reproducible evaluation records for Crows agent runs.

This module deliberately separates execution from scoring: adapters may run tasks,
but this layer validates immutable dataset identity and aggregates recorded evidence.
Subjective quality fields remain human-review scores rather than model-generated truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DATASET_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1
OUTCOMES = {"success", "partial", "failure"}
ROUTES = {"auto", "manual"}
CATEGORIES = {
    "file_lookup",
    "multi_file_investigation",
    "web_research",
    "tool_failure",
    "dependent_subtasks",
    "coding_proposal",
    "recovery",
}
SCORE_FIELDS = (
    "factual_correctness_score",
    "citation_quality_score",
)
BOOLEAN_FIELDS = (
    "tool_selection_correct",
    "tool_arguments_correct",
    "recovery_replay_safe",
)


class EvaluationError(ValueError):
    """Raised when a dataset or run record is malformed or inconsistent."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dataset_fingerprint(dataset: dict[str, Any]) -> str:
    """Return a stable SHA-256 over the complete dataset, excluding no fields."""
    return hashlib.sha256(canonical_json(dataset).encode("utf-8")).hexdigest()


def validate_dataset(dataset: Any) -> dict[str, Any]:
    if not isinstance(dataset, dict):
        raise EvaluationError("Dataset must be a JSON object")
    if dataset.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise EvaluationError(f"schema_version must be {DATASET_SCHEMA_VERSION}")
    version = dataset.get("dataset_version")
    if not isinstance(version, str) or not version.strip():
        raise EvaluationError("dataset_version must be a non-empty string")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("cases must be a non-empty array")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            raise EvaluationError(f"{prefix} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise EvaluationError(f"{prefix}.id must be a non-empty string")
        if case_id in seen:
            raise EvaluationError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        if case.get("category") not in CATEGORIES:
            raise EvaluationError(f"{prefix}.category is unsupported")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise EvaluationError(f"{prefix}.prompt must be non-empty")
        expected = case.get("expected")
        if not isinstance(expected, list) or not expected or not all(
            isinstance(item, str) and item.strip() for item in expected
        ):
            raise EvaluationError(f"{prefix}.expected must contain non-empty strings")
        rubric = case.get("rubric")
        if not isinstance(rubric, dict) or not isinstance(rubric.get("automated_checks"), list):
            raise EvaluationError(f"{prefix}.rubric.automated_checks must be an array")
        if not isinstance(rubric.get("human_review"), list):
            raise EvaluationError(f"{prefix}.rubric.human_review must be an array")
    return dataset


def _non_negative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{field} must be a non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise EvaluationError(f"{field} must be a finite non-negative number")
    return number


def validate_run_record(record: Any, *, dataset: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise EvaluationError("Each run record must be a JSON object")
    if record.get("schema_version") != RUN_SCHEMA_VERSION:
        raise EvaluationError(f"run schema_version must be {RUN_SCHEMA_VERSION}")
    if record.get("dataset_version") != dataset["dataset_version"]:
        raise EvaluationError("run record dataset_version does not match dataset")
    expected_hash = dataset_fingerprint(dataset)
    if record.get("dataset_sha256") != expected_hash:
        raise EvaluationError("run record dataset_sha256 does not match dataset")
    case_ids = {case["id"] for case in dataset["cases"]}
    if record.get("case_id") not in case_ids:
        raise EvaluationError(f"unknown case_id: {record.get('case_id')}")
    if record.get("outcome") not in OUTCOMES:
        raise EvaluationError("outcome must be success, partial, or failure")
    if record.get("route") not in ROUTES:
        raise EvaluationError("route must be auto or manual")
    for field in ("provider", "model"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise EvaluationError(f"{field} must be a non-empty string")
    _non_negative_number(record.get("latency_ms"), "latency_ms")
    for field in ("unauthorized_action_count", "false_success_count"):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvaluationError(f"{field} must be a non-negative integer")
    for field in SCORE_FIELDS:
        value = record.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or not 0 <= value <= 1
        ):
            raise EvaluationError(f"{field} must be null or a score between 0 and 1")
    for field in BOOLEAN_FIELDS:
        value = record.get(field)
        if value is not None and not isinstance(value, bool):
            raise EvaluationError(f"{field} must be null or boolean")
    cost = record.get("estimated_cost_usd")
    if cost is not None:
        _non_negative_number(cost, "estimated_cost_usd")
    tool_calls = record.get("tool_calls", [])
    if not isinstance(tool_calls, list) or not all(isinstance(item, dict) for item in tool_calls):
        raise EvaluationError("tool_calls must be an array of objects")
    for index, call in enumerate(tool_calls):
        if not isinstance(call.get("name"), str) or not call["name"]:
            raise EvaluationError(f"tool_calls[{index}].name must be non-empty")
        if not isinstance(call.get("arguments"), dict):
            raise EvaluationError(f"tool_calls[{index}].arguments must be an object")
        if not isinstance(call.get("succeeded"), bool):
            raise EvaluationError(f"tool_calls[{index}].succeeded must be boolean")
    return record


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize_run(records: list[dict[str, Any]], *, dataset: dict[str, Any]) -> dict[str, Any]:
    """Aggregate validated records; missing human scores stay missing, never zero."""
    validate_dataset(dataset)
    checked = [validate_run_record(item, dataset=dataset) for item in records]
    outcomes = Counter(item["outcome"] for item in checked)
    routes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in checked:
        routes[item["route"]].append(item)

    def metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "records": len(items),
            "outcomes": {key: sum(row["outcome"] == key for row in items) for key in sorted(OUTCOMES)},
            "success_rate": round(sum(row["outcome"] == "success" for row in items) / len(items), 4) if items else None,
            "mean_latency_ms": _mean([float(row["latency_ms"]) for row in items]),
            "estimated_cost_usd_total": round(sum(float(row["estimated_cost_usd"]) for row in items if row.get("estimated_cost_usd") is not None), 8)
                if any(row.get("estimated_cost_usd") is not None for row in items) else None,
            "unauthorized_action_count": sum(row["unauthorized_action_count"] for row in items),
            "false_success_count": sum(row["false_success_count"] for row in items),
            "tool_selection_accuracy": _mean([float(row["tool_selection_correct"]) for row in items if row.get("tool_selection_correct") is not None]),
            "tool_argument_accuracy": _mean([float(row["tool_arguments_correct"]) for row in items if row.get("tool_arguments_correct") is not None]),
            "recovery_replay_safety_rate": _mean([float(row["recovery_replay_safe"]) for row in items if row.get("recovery_replay_safe") is not None]),
            "human_review": {
                field: _mean([float(row[field]) for row in items if row.get(field) is not None])
                for field in SCORE_FIELDS
            },
        }

    model_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in checked:
        model_groups[f'{item["provider"]}/{item["model"]}'].append(item)
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_fingerprint(dataset),
        "records": len(checked),
        "unique_cases": len({item["case_id"] for item in checked}),
        "outcomes": {key: outcomes.get(key, 0) for key in sorted(OUTCOMES)},
        "overall": metrics(checked),
        "by_route": {route: metrics(routes[route]) for route in sorted(routes)},
        "by_model": {model: metrics(items) for model, items in sorted(model_groups.items())},
        "safety_gates": {
            "unauthorized_actions_zero": all(item["unauthorized_action_count"] == 0 for item in checked),
            "false_success_zero": all(item["false_success_count"] == 0 for item in checked),
            "recovery_replay_safety_all_reviewed_pass": all(
                item["recovery_replay_safe"] is True
                for item in checked if item.get("recovery_replay_safe") is not None
            ) and any(item.get("recovery_replay_safe") is not None for item in checked),
        },
        "limitations": [
            "This report aggregates supplied run evidence; it does not execute an agent.",
            "Factual correctness and citation quality are human-review scores when supplied.",
            "A zero cost total is not inferred when cost data is absent.",
            "Route/model comparisons are descriptive and require matched cases and repeated runs.",
        ],
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvaluationError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Crows evaluation datasets and summarize JSONL runs.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate a versioned dataset")
    validate_parser.add_argument("dataset", type=Path)
    summary_parser = subparsers.add_parser("summarize", help="summarize recorded evaluation runs")
    summary_parser.add_argument("dataset", type=Path)
    summary_parser.add_argument("records", type=Path, help="JSONL run records")
    summary_parser.add_argument("--output", type=Path, help="write report JSON to this path")
    args = parser.parse_args(argv)
    try:
        dataset = validate_dataset(json.loads(args.dataset.read_text(encoding="utf-8")))
        if args.command == "validate":
            result = {"valid": True, "dataset_version": dataset["dataset_version"],
                      "case_count": len(dataset["cases"]), "dataset_sha256": dataset_fingerprint(dataset)}
        else:
            result = summarize_run(load_jsonl(args.records), dataset=dataset)
        rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if getattr(args, "output", None):
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (OSError, json.JSONDecodeError, EvaluationError) as exc:
        parser.exit(2, f"evaluation error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
