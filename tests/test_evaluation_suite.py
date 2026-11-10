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
    "execution_policy": {
        "live_provider_required": True,
        "require_explicit_approval_for_writes": True,
        "prohibit_external_side_effects": True,
        "record_route_model_latency_and_cost_when_available": True,
    },
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
    assert report["safety_gates"]["dataset_coverage_complete"] is True
    assert report["case_coverage"] == {
        "expected_cases": 2,
        "covered_cases": 2,
        "coverage_rate": 1.0,
        "missing_case_ids": [],
        "complete": True,
    }
    assert report["by_category"]["file_lookup"]["records"] == 1
    assert report["by_category"]["recovery"]["records"] == 1
    assert report["by_category"]["web_research"]["records"] == 0


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



def test_empty_summary_keeps_safety_gates_unknown():
    report = summarize_run([], dataset=DATASET)
    assert report["safety_gates"]["unauthorized_actions_zero"] is None
    assert report["safety_gates"]["false_success_zero"] is None
    assert report["safety_gates"]["recovery_replay_safety_all_reviewed_pass"] is None


def test_summary_does_not_report_partial_known_cost_as_total():
    rows = [
        record(estimated_cost_usd=0.001),
        record(case_id="case-b", estimated_cost_usd=None, recovery_replay_safe=True),
    ]
    report = summarize_run(rows, dataset=DATASET)
    assert report["overall"]["estimated_cost_usd_total"] is None


def test_recovery_gate_requires_every_recovery_case_to_be_reviewed():
    dataset = copy.deepcopy(DATASET)
    second = copy.deepcopy(dataset["cases"][1])
    second["id"] = "case-c"
    dataset["cases"].append(second)
    row = record(
        case_id="case-b",
        recovery_replay_safe=True,
        dataset_sha256=dataset_fingerprint(dataset),
    )
    report = summarize_run([row], dataset=dataset)
    assert report["safety_gates"]["recovery_replay_safety_all_reviewed_pass"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("case_id", []),
    ("outcome", {}),
    ("route", []),
    ("schema_version", True),
])
def test_run_record_rejects_unhashable_or_boolean_discriminators(field, value):
    with pytest.raises(EvaluationError):
        validate_run_record(record(**{field: value}), dataset=DATASET)


def test_dataset_rejects_boolean_schema_version_and_unhashable_category():
    value = copy.deepcopy(DATASET)
    value["schema_version"] = True
    with pytest.raises(EvaluationError):
        validate_dataset(value)
    value = copy.deepcopy(DATASET)
    value["cases"][0]["category"] = []
    with pytest.raises(EvaluationError):
        validate_dataset(value)

def test_tool_trace_requires_typed_arguments_and_result():
    with pytest.raises(EvaluationError):
        validate_run_record(
            record(tool_calls=[{"name": "workspace.read_file", "arguments": [], "succeeded": True}]),
            dataset=DATASET,
        )


@pytest.mark.parametrize("policy_change", [
    {},
    {"live_provider_required": False},
    {"require_explicit_approval_for_writes": False},
    {"prohibit_external_side_effects": False},
    {"record_route_model_latency_and_cost_when_available": False},
])
def test_dataset_requires_all_execution_safety_policy_flags(policy_change):
    value = copy.deepcopy(DATASET)
    value["execution_policy"].update(policy_change)
    if not policy_change:
        value.pop("execution_policy")
    with pytest.raises(EvaluationError, match="execution_policy"):
        validate_dataset(value)


@pytest.mark.parametrize(("field", "value"), [
    ("automated_checks", ["valid", ""]),
    ("automated_checks", [None]),
    ("human_review", ["unrecognized_score"]),
    ("human_review", [None]),
])
def test_dataset_rejects_malformed_rubric_fields(field, value):
    dataset = copy.deepcopy(DATASET)
    dataset["cases"][0]["rubric"][field] = value
    with pytest.raises(EvaluationError, match="rubric"):
        validate_dataset(dataset)


def test_summary_marks_missing_dataset_cases_as_incomplete():
    report = summarize_run([record()], dataset=DATASET)

    assert report["case_coverage"] == {
        "expected_cases": 2,
        "covered_cases": 1,
        "coverage_rate": 0.5,
        "missing_case_ids": ["case-b"],
        "complete": False,
    }
    assert report["safety_gates"]["dataset_coverage_complete"] is False
    assert report["by_category"]["recovery"]["records"] == 0


def test_summary_compares_routes_only_on_matched_cases_and_averages_repeats():
    rows = [
        record(route="auto", outcome="success", latency_ms=100, estimated_cost_usd=0.001),
        record(route="auto", outcome="failure", latency_ms=300, estimated_cost_usd=0.003),
        record(route="manual", outcome="success", latency_ms=200, estimated_cost_usd=0.002),
        # This unmatched case must not affect the paired comparison.
        record(case_id="case-b", route="manual", outcome="failure", latency_ms=900,
               recovery_replay_safe=True, estimated_cost_usd=0.009),
    ]

    report = summarize_run(rows, dataset=DATASET)
    comparison = report["matched_route_comparison"]

    assert comparison["matched_cases"] == 1
    assert comparison["auto"]["success_rate"] == 0.5
    assert comparison["manual"]["success_rate"] == 1.0
    assert comparison["auto"]["mean_latency_ms"] == 200.0
    assert comparison["manual"]["mean_latency_ms"] == 200.0
    assert comparison["auto"]["mean_estimated_cost_usd"] == 0.002
    assert comparison["manual"]["mean_estimated_cost_usd"] == 0.002
    assert comparison["auto_minus_manual"] == {
        "success_rate": -0.5,
        "mean_latency_ms": 0.0,
        "mean_estimated_cost_usd": 0.0,
    }


def test_matched_route_comparison_is_unknown_without_both_routes():
    report = summarize_run([record(route="auto")], dataset=DATASET)

    comparison = report["matched_route_comparison"]
    assert comparison["matched_cases"] == 0
    assert comparison["auto"]["success_rate"] is None
    assert comparison["manual"]["success_rate"] is None
    assert comparison["auto_minus_manual"]["success_rate"] is None


def test_matched_route_cost_stays_unknown_when_any_repetition_lacks_cost():
    rows = [
        record(route="auto", estimated_cost_usd=0.001),
        record(route="auto", estimated_cost_usd=None),
        record(route="manual", estimated_cost_usd=0.002),
    ]
    comparison = summarize_run(rows, dataset=DATASET)["matched_route_comparison"]

    assert comparison["matched_cases"] == 1
    assert comparison["auto"]["mean_estimated_cost_usd"] is None
    assert comparison["manual"]["mean_estimated_cost_usd"] == 0.002
    assert comparison["auto_minus_manual"]["mean_estimated_cost_usd"] is None


def test_empty_summary_reports_zero_dataset_coverage():
    report = summarize_run([], dataset=DATASET)

    assert report["case_coverage"]["expected_cases"] == 2
    assert report["case_coverage"]["covered_cases"] == 0
    assert report["case_coverage"]["coverage_rate"] == 0.0
    assert report["case_coverage"]["missing_case_ids"] == ["case-a", "case-b"]
    assert report["case_coverage"]["complete"] is False
    assert report["safety_gates"]["dataset_coverage_complete"] is False
