import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agent_eval_compare.py"
SPEC = importlib.util.spec_from_file_location("agent_eval_compare", MODULE_PATH)
compare = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(compare)


def write_report(path, results):
    path.write_text(json.dumps({"schema_version": 1, "results": results}), encoding="utf-8")


def result(name="lookup", *, passed=True, latency=100, mode="auto", route="primary",
           model="provider/model", fallback=False, attempts=1):
    return {
        "name": name, "passed": passed, "latency_ms": latency, "mode": mode,
        "route": route, "model": model, "fallback_used": fallback, "attempts": attempts,
    }


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


def test_compare_reports_aggregates_by_actual_mode_route_and_model(tmp_path):
    auto = tmp_path / "auto.json"
    manual = tmp_path / "manual.json"
    write_report(auto, [
        result(latency=100) | {
            "prompt": "PRIVATE_PROMPT_SENTINEL",
            "answer": "PRIVATE_ANSWER_SENTINEL",
            "tool_arguments": {"token": "PRIVATE_TOOL_ARGUMENT_SENTINEL"},
        },
        result("missing", passed=False, latency=300, fallback=True, attempts=2),
    ])
    write_report(manual, [result(mode="manual", route="coding", model="provider/code", latency=200)])

    report = compare.compare_reports([("auto-run", auto), ("manual-run", manual)])

    assert report["group_count"] == 2
    primary = next(item for item in report["groups"] if item["mode"] == "auto")
    assert primary["case_count"] == 2
    assert primary["passed"] == 1
    assert primary["pass_rate"] == 0.5
    assert primary["latency_ms_mean"] == 200
    assert primary["latency_ms_median"] == 200
    assert primary["fallback_observations"] == 2
    assert primary["fallback_count"] == 1
    assert primary["attempts_mean"] == 1.5
    serialized = json.dumps(report)
    assert "PRIVATE_PROMPT_SENTINEL" not in serialized
    assert "PRIVATE_ANSWER_SENTINEL" not in serialized
    assert "PRIVATE_TOOL_ARGUMENT_SENTINEL" not in serialized
    assert not {"prompt", "answer", "tool_arguments"}.intersection(nested_keys(report))


def test_compare_omits_rates_when_metadata_was_not_reported(tmp_path):
    path = tmp_path / "minimal.json"
    write_report(path, [{"name": "simple", "passed": True, "latency_ms": 12}])

    report = compare.compare_reports([("minimal", path)])

    assert report["groups"][0]["fallback_rate"] is None
    assert report["groups"][0]["attempts_mean"] is None
    assert report["groups"][0]["route"] is None


@pytest.mark.parametrize("bad", [
    {"name": "x", "passed": 1, "latency_ms": 1},
    {"name": "x", "passed": True, "latency_ms": -1},
    {"name": "x", "passed": True, "latency_ms": 1, "mode": "automatic"},
    {"name": "x", "passed": True, "latency_ms": 1, "fallback_used": "yes"},
    {"name": "x", "passed": True, "latency_ms": 1, "attempts": True},
])
def test_load_report_rejects_invalid_result_metadata(tmp_path, bad):
    path = tmp_path / "bad.json"
    write_report(path, [bad])

    with pytest.raises(ValueError):
        compare.load_report(path)


def test_load_report_rejects_unknown_schema(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"schema_version": 2, "results": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        compare.load_report(path)


def test_parse_named_paths_requires_unique_labels_and_equals():
    assert compare.parse_named_paths(["first=a.json"]) == [("first", Path("a.json"))]
    with pytest.raises(ValueError):
        compare.parse_named_paths(["missing-separator"])
    with pytest.raises(ValueError):
        compare.parse_named_paths(["same=a.json", "same=b.json"])
