import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_eval_examples_match_documented_crows_fixture():
    cases = json.loads((ROOT / "examples" / "agent_eval_cases.json").read_text(encoding="utf-8"))
    docs = (ROOT / "docs" / "AGENT_EVALUATION.md").read_text(encoding="utf-8")
    by_name = {case["name"]: case for case in cases}

    assert "CROWS_EVAL_READY" in by_name["basic answer"]["expected_contains"]
    assert "CROWS_EVAL_MARKER_9241" in by_name["read known workspace file"]["expected_contains"]
    assert "CROWS_EVAL_MARKER_9241" in docs
    assert "AETHER" not in json.dumps(cases)
