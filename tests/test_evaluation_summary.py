from crows.evaluation import summarize_workflows


def _workflow(status="succeeded", task_status="succeeded", verdict="PASS"):
    return {
        "status": status,
        "plan": [{"title": "private goal"}],
        "results": {
            "tasks": [{"status": task_status, "tool_calls": 2, "tool_attempts": 3}],
            "qa_review": {
                "status": "succeeded",
                "result": '{"verdict":"' + verdict + '"}',
            },
        },
    }


def test_summary_aggregates_outcomes_without_workflow_content():
    report = summarize_workflows([
        _workflow(),
        _workflow(status="partial", task_status="failed", verdict="NEEDS_ATTENTION"),
        _workflow(status="unknown", task_status="unknown", verdict="PASS"),
    ])

    assert report == {
        "sampled_workflows": 3,
        "outcomes": {"passed": 1, "incomplete": 1, "needs_attention": 1},
        "pass_rate": 0.3333,
        "task_counts": {
            "queued": 0, "running": 0, "succeeded": 1,
            "failed": 1, "unknown": 1, "blocked": 0,
        },
        "qa_verdicts": {
            "PASS": 2, "NEEDS_ATTENTION": 1, "NOT_PASSED": 0, "UNVERIFIED": 0,
        },
        "tool_calls": 6,
        "tool_attempts": 9,
    }
    assert "private goal" not in str(report)


def test_summary_handles_empty_history():
    report = summarize_workflows([])
    assert report["sampled_workflows"] == 0
    assert report["pass_rate"] is None
    assert report["tool_calls"] == 0
