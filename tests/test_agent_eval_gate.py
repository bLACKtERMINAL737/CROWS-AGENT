import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agent_eval_gate.py"
SPEC = importlib.util.spec_from_file_location("agent_eval_gate", MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)


def result(passed=True, latency=100):
    return {"passed": passed, "latency_ms": latency}


def write_report(path, results):
    path.write_text(json.dumps({
        "schema_version": 1,
        "results": [
            {"name": name, **item} for name, item in results.items()
        ],
    }), encoding="utf-8")


def test_gate_passes_when_metrics_are_within_thresholds():
    baseline = {"a": result(True, 100), "b": result(False, 200)}
    candidate = {"a": result(True, 120), "b": result(True, 220)}

    report = gate.evaluate_gate(
        baseline, candidate, max_pass_rate_drop=0, max_latency_increase_percent=25
    )

    assert report["passed"] is True
    assert report["case_improvements"] == 1
    assert report["case_regressions"] == 0
    assert report["failures"] == []


def test_gate_fails_on_pass_rate_regression():
    baseline = {"a": result(True), "b": result(True)}
    candidate = {"a": result(False), "b": result(True)}

    report = gate.evaluate_gate(baseline, candidate)

    assert report["passed"] is False
    assert report["pass_rate_drop"] == 0.5
    assert "pass_rate_regression" in report["failures"]


def test_gate_fails_on_latency_regression():
    baseline = {"a": result(True, 100), "b": result(True, 100)}
    candidate = {"a": result(True, 140), "b": result(True, 140)}

    report = gate.evaluate_gate(
        baseline, candidate, max_latency_increase_percent=25
    )

    assert report["passed"] is False
    assert report["median_latency_increase_percent"] == 40
    assert "latency_regression" in report["failures"]


def test_gate_allows_configured_pass_rate_tolerance():
    baseline = {"a": result(True), "b": result(True), "c": result(True), "d": result(True)}
    candidate = {"a": result(False), "b": result(True), "c": result(True), "d": result(True)}

    report = gate.evaluate_gate(baseline, candidate, max_pass_rate_drop=0.25)

    assert report["passed"] is True


def test_gate_rejects_different_case_sets():
    with pytest.raises(ValueError, match="Case sets differ"):
        gate.evaluate_gate({"baseline": result()}, {"candidate": result()})


def test_load_results_rejects_duplicate_names_and_bad_metrics(tmp_path):
    path = tmp_path / "bad.json"
    write_report(path, {"one": result()})
    payload = json.loads(path.read_text())
    payload["results"].append(payload["results"][0])
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate"):
        gate.load_results(path)

    write_report(path, {"one": {"passed": 1, "latency_ms": 1}})
    with pytest.raises(ValueError, match="boolean"):
        gate.load_results(path)


def test_gate_report_excludes_case_level_content(tmp_path):
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    write_report(baseline_path, {"private-case-label": result(True)})
    write_report(candidate_path, {"private-case-label": result(True)})
    baseline = gate.load_results(baseline_path)
    candidate = gate.load_results(candidate_path)
    report = gate.evaluate_gate(baseline, candidate)
    serialized = json.dumps(report)

    assert "private-case-label" not in serialized
    assert "prompt" not in serialized
    assert "answer" not in serialized
