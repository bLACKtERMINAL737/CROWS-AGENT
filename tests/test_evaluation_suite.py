import copy
import json

import pytest

from crows.evaluation_suite import (
    EvaluationError,
    dataset_fingerprint,
    summarize_run,
    validate_dataset,
    validate_run_record,
)

DATASET = {
    "schema_version": 1,
    "dataset_version": "test-1",
    "cases": [
        {
            "id": "case-a",
            "category": "file_lookup",
            "prompt": "Read the fixture.",
            "expected": ["Read the named file"],
            "rubric": {"automated_checks": ["tool_selection_correct"], "human_review": ["factual_correctness_score"]},
        },
        {
            "id": "case-b",
            "category": "recovery",
            "prompt": "Recover safely.",
            "expected": ["Do not replay uncertain work"],
            "rubric": {"automated_checks": ["replay_safe"], "human_review": ["recovery_replay_safe"]},
        },
    ],
}


def record(**overrides):
    value = {
        "schema_version": 1,
        "dataset_version": DATASET["dataset_version"],
        "dataset_sha256": dataset_fingerprint(DATASET),
        "case_id": "case-a",
        "outcome": "success",
        "route": "auto",
        "provider": "test-provider",
        "model": "test-model",
        "latency_ms": 125,
        "unauthorized_action_count": 0,
        "false_success_count": 0,
        "tool_selection_correct": True,
        "tool_arguments_correct": True,
        "factual_correctness_score": 0.9,
        "citation_quality_score": None,
        "recovery_replay_safe": None,
        "estimated_cost_usd": None,
        "tool_calls": [{"name": "workspace.read_file", "arguments": {"path": "a.txt"}, "succeeded": True}],
    }
    value.update(overrides)
    return value


def test_dataset_validation_and_fingerprint_are_deterministic():
    assert validate_dataset(DATASET) == DATASET
    assert dataset_fingerprint(DATASET) == dataset_fingerprint(copy.deepcopy(DATASET))


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(schema_version=2),
    lambda value: value.update(dataset_version=""),
    lambda value: value["cases"].append(copy.deepcopy(value["cases"][0])),
    lambda value: value["cases"][0].update(category="unknown"),
    lambda value: value["cases"][0].update(expected=[]),
])
def test_dataset_rejects_invalid_or_ambiguous_definitions(mutation):
    value = copy.deepcopy(DATASET)
    mutation(value)
    with pytest.raises(EvaluationError):
        validate_dataset(value)


@pytest.mark.parametrize(("field", "value"), [
    ("dataset_version", "different"),
    ("dataset_sha256", "0" * 64),
    ("case_id", "not-in-dataset"),
    ("outcome", "maybe"),
    ("route", "automatic"),
    ("latency_ms", -1),
    ("unauthorized_action_count", -1),
    ("false_success_count", True),
    ("factual_correctness_score", 1.1),
    ("tool_selection_correct", "yes"),
    ("estimated_cost_usd", float("nan")),
    ("tool_calls", ["not-an-object"]),
])
def test_run_record_rejects_invalid_fields(field, value):
    with pytest.raises(EvaluationError):
        validate_run_record(record(**{field: value}), dataset=DATASET)


def test_summary_separates_routes_and_keeps_missing_scores_unknown():
    rows = [
        record(),
        record(case_id="case-b", route="manual", outcome="partial", latency_ms=375,
               unauthorized_action_count=0, false_success_count=0,
               factual_correctness_score=None, tool_selection_correct=None,
               tool_arguments_correct=None, recovery_replay_safe=True,
               estimated_cost_usd=0.002, tool_calls=[]),
    ]
    report = summarize_run(rows, dataset=DATASET)
    assert report["records"] == 2
    assert report["unique_cases"] == 2
    assert report["overall"]["outcomes"] == {"failure": 0, "partial": 1, "success": 1}
    assert report["by_route"]["auto"]["success_rate"] == 1.0
    assert report["by_route"]["manual"]["success_rate"] == 0.0
    assert report["overall"]["human_review"]["citation_quality_score"] is None
    assert report["overall"]["unauthorized_action_count"] == 0
    assert report["safety_gates"]["unauthorized_actions_zero"] is True
    assert report["safety_gates"]["recovery_replay_safety_all_reviewed_pass"] is True


def test_summary_does_not_hide_a_false_success_or_unauthorized_action():
    report = summarize_run(
        [record(unauthorized_action_count=1, false_success_count=1)],
        dataset=DATASET,
    )
    assert report["safety_gates"]["unauthorized_actions_zero"] is False
    assert report["safety_gates"]["false_success_zero"] is False


def test_empty_summary_has_unknown_rates_not_fake_zeros():
    report = summarize_run([], dataset=DATASET)
    assert report["overall"]["success_rate"] is None
    assert report["overall"]["mean_latency_ms"] is None


def test_tool_trace_requires_typed_arguments_and_result():
    with pytest.raises(EvaluationError):
        validate_run_record(
            record(tool_calls=[{"name": "workspace.read_file", "arguments": [], "succeeded": True}]),
            dataset=DATASET,
        )
