import pytest

from crows.evaluation import evaluate_workflow


def workflow(*, status="succeeded", task_status="succeeded", qa_status="succeeded", verdict="PASS"):
    return {
        "status": status,
        "plan": [{"title": "private task text"}],
        "results": {
            "tasks": [{"status": task_status, "tool_calls": 2, "tool_attempts": 3}],
            "qa_review": {"status": qa_status, "result": '{"verdict":"' + verdict + '"}'},
            "diagnostics": {"failure_count": 0},
        },
    }


def test_evaluation_passes_only_when_workflow_tasks_and_qa_pass():
    report = evaluate_workflow(workflow())
    assert report["outcome"] == "passed"
    assert report["checks"] == {
        "plan_consistent": True,
        "dependency_graph_valid": True,
        "dependency_outcomes_consistent": True,
        "all_tasks_succeeded": True,
        "qa_passed": True,
        "workflow_succeeded": True,
    }
    assert report["tool_calls"] == 2
    assert report["tool_attempts"] == 3
    assert "private task text" not in str(report)


@pytest.mark.parametrize(
    ("value", "outcome", "qa_verdict"),
    [
        (workflow(status="partial", task_status="failed", verdict="NEEDS_ATTENTION"), "needs_attention", "NEEDS_ATTENTION"),
        (workflow(status="unknown", task_status="unknown", qa_status="not_started"), "incomplete", "NOT_PASSED"),
        (workflow(status="succeeded", verdict="UNSTRUCTURED"), "needs_attention", "UNVERIFIED"),
        (workflow(status="running", task_status="queued", qa_status="not_started"), "incomplete", "NOT_PASSED"),
    ],
)
def test_evaluation_reports_non_passing_workflows_without_false_success(value, outcome, qa_verdict):
    report = evaluate_workflow(value)
    assert report["outcome"] == outcome
    assert report["qa_verdict"] == qa_verdict


def test_evaluation_detects_plan_checkpoint_mismatch():
    value = workflow()
    value["results"]["tasks"] = []
    report = evaluate_workflow(value)
    assert report["checks"]["plan_consistent"] is False
    assert report["checks"]["all_tasks_succeeded"] is False
    assert report["outcome"] == "needs_attention"


def test_evaluation_handles_malformed_checkpoint_data():
    report = evaluate_workflow({"status": "failed", "plan": None, "results": {"tasks": [None], "qa_review": "bad"}})
    assert report["outcome"] == "needs_attention"
    assert report["recorded_tasks"] == 0
    assert report["qa_verdict"] == "NOT_PASSED"


def test_evaluation_rejects_a_cyclic_dependency_graph():
    value = workflow()
    value["plan"] = [
        {"title": "first", "depends_on": [1]},
        {"title": "second", "depends_on": [0]},
    ]
    value["results"]["tasks"] = [
        {"plan_index": 0, "depends_on": [1], "status": "succeeded"},
        {"plan_index": 1, "depends_on": [0], "status": "succeeded"},
    ]

    report = evaluate_workflow(value)

    assert report["checks"]["dependency_graph_valid"] is False
    assert report["checks"]["all_tasks_succeeded"] is False
    assert report["outcome"] == "needs_attention"


def test_evaluation_rejects_a_dependent_task_that_ran_after_failure():
    value = workflow()
    value["plan"] = [
        {"title": "source", "depends_on": []},
        {"title": "dependent", "depends_on": [0]},
    ]
    value["results"]["tasks"] = [
        {"plan_index": 0, "depends_on": [], "status": "failed"},
        {"plan_index": 1, "depends_on": [0], "status": "succeeded"},
    ]
    value["results"]["qa_review"] = {
        "status": "succeeded",
        "result": '{"verdict":"PASS"}',
    }

    report = evaluate_workflow(value)

    assert report["checks"]["dependency_graph_valid"] is True
    assert report["checks"]["dependency_outcomes_consistent"] is False
    assert report["checks"]["all_tasks_succeeded"] is False
    assert report["outcome"] == "needs_attention"


def test_evaluation_accepts_a_dependent_task_blocked_by_failed_prerequisite():
    value = workflow(status="partial", task_status="failed", verdict="NEEDS_ATTENTION")
    value["plan"] = [
        {"title": "source", "depends_on": []},
        {"title": "dependent", "depends_on": [0]},
    ]
    value["results"]["tasks"] = [
        {"plan_index": 0, "depends_on": [], "status": "failed"},
        {"plan_index": 1, "depends_on": [0], "status": "blocked"},
    ]

    report = evaluate_workflow(value)

    assert report["checks"]["dependency_graph_valid"] is True
    assert report["checks"]["dependency_outcomes_consistent"] is True
    assert report["outcome"] == "needs_attention"


def test_evaluation_rejects_checkpoint_plan_index_and_dependency_mismatch():
    value = workflow()
    value["plan"] = [
        {"title": "first", "depends_on": []},
        {"title": "second", "depends_on": [0]},
    ]
    value["results"]["tasks"] = [
        {"plan_index": 0, "depends_on": [], "status": "succeeded"},
        {"plan_index": 0, "depends_on": [0], "status": "succeeded"},
    ]

    report = evaluate_workflow(value)

    assert report["checks"]["dependency_graph_valid"] is False
    assert report["checks"]["all_tasks_succeeded"] is False
