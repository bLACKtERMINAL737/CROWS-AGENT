"""Deterministic, content-minimizing evaluation of persisted agent workflows."""
import json
from typing import Any


def _qa_verdict(review: Any) -> str:
    if not isinstance(review, dict) or review.get("status") != "succeeded":
        return "NOT_PASSED"
    value = review.get("result")
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str):
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            return "UNVERIFIED"
    else:
        return "UNVERIFIED"
    if not isinstance(payload, dict):
        return "UNVERIFIED"
    verdict = payload.get("verdict")
    return verdict if verdict in {"PASS", "NEEDS_ATTENTION"} else "UNVERIFIED"


def _dependency_integrity(
    plan: list[Any], tasks: list[Any]
) -> tuple[bool, bool]:
    """Check the saved dependency graph and whether task outcomes respect it."""
    dependencies: list[list[int]] = []
    graph_valid = len(plan) == len(tasks) and all(
        isinstance(item, dict) for item in plan
    ) and all(isinstance(item, dict) for item in tasks)
    if not graph_valid:
        return False, False

    task_by_index: dict[int, dict[str, Any]] = {}
    for position, task in enumerate(tasks):
        index = task.get("plan_index", position)
        if type(index) is not int or index < 0 or index >= len(plan) or index in task_by_index:
            graph_valid = False
            continue
        task_by_index[index] = task
    if len(task_by_index) != len(plan):
        graph_valid = False

    for index, item in enumerate(plan):
        deps = item.get("depends_on", [])
        if (
            not isinstance(deps, list)
            or any(type(dep) is not int or dep < 0 or dep >= len(plan) or dep == index for dep in deps)
            or len(set(deps)) != len(deps)
        ):
            graph_valid = False
            dependencies.append([])
            continue
        dependencies.append(deps)
        task = task_by_index.get(index)
        if task is not None and task.get("depends_on", deps) != deps:
            graph_valid = False

    if graph_valid:
        remaining = {index: set(deps) for index, deps in enumerate(dependencies)}
        completed: set[int] = set()
        while remaining:
            ready = [index for index, deps in remaining.items() if deps <= completed]
            if not ready:
                graph_valid = False
                break
            for index in ready:
                completed.add(index)
                del remaining[index]

    outcomes_consistent = graph_valid
    if graph_valid:
        for index, deps in enumerate(dependencies):
            status = task_by_index[index].get("status")
            prerequisite_statuses = [task_by_index[dep].get("status") for dep in deps]
            if any(value != "succeeded" for value in prerequisite_statuses):
                if status in {"running", "succeeded", "failed"}:
                    outcomes_consistent = False
    return graph_valid, outcomes_consistent


def evaluate_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Evaluate saved checkpoints without model calls or exposing task content."""
    plan = workflow.get("plan")
    results = workflow.get("results")
    plan = plan if isinstance(plan, list) else []
    results = results if isinstance(results, dict) else {}
    tasks = results.get("tasks")
    tasks = tasks if isinstance(tasks, list) else []
    valid_tasks = [item for item in tasks if isinstance(item, dict)]
    counts = {status: 0 for status in ("queued", "running", "succeeded", "failed", "unknown", "blocked")}
    for item in valid_tasks:
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    qa_verdict = _qa_verdict(results.get("qa_review"))
    plan_consistent = len(plan) == len(valid_tasks) and len(tasks) == len(valid_tasks)
    dependency_graph_valid, dependency_outcomes_consistent = _dependency_integrity(plan, tasks)
    all_tasks_succeeded = (
        plan_consistent and dependency_graph_valid and dependency_outcomes_consistent
        and len(plan) > 0 and counts["succeeded"] == len(plan)
    )
    qa_passed = qa_verdict == "PASS"
    workflow_status = workflow.get("status")
    passed = workflow_status == "succeeded" and all_tasks_succeeded and qa_passed
    if passed:
        outcome = "passed"
    elif workflow_status in {"running", "unknown"} or counts["queued"] or counts["running"]:
        outcome = "incomplete"
    else:
        outcome = "needs_attention"
    return {
        "outcome": outcome,
        "workflow_status": workflow_status,
        "planned_tasks": len(plan),
        "recorded_tasks": len(valid_tasks),
        "task_counts": counts,
        "qa_verdict": qa_verdict,
        "checks": {
            "plan_consistent": plan_consistent,
            "dependency_graph_valid": dependency_graph_valid,
            "dependency_outcomes_consistent": dependency_outcomes_consistent,
            "all_tasks_succeeded": all_tasks_succeeded,
            "qa_passed": qa_passed,
            "workflow_succeeded": workflow_status == "succeeded",
        },
        "tool_calls": sum(
            item.get("tool_calls", 0) for item in valid_tasks
            if type(item.get("tool_calls", 0)) is int and item.get("tool_calls", 0) >= 0
        ),
        "tool_attempts": sum(
            item.get("tool_attempts", 0) for item in valid_tasks
            if type(item.get("tool_attempts", 0)) is int and item.get("tool_attempts", 0) >= 0
        ),
        "diagnostics": results.get("diagnostics") if isinstance(results.get("diagnostics"), dict) else {},
    }


def summarize_workflows(workflows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate recent workflow evaluations without exposing workflow content or IDs."""
    outcomes = {"passed": 0, "incomplete": 0, "needs_attention": 0}
    task_counts = {status: 0 for status in ("queued", "running", "succeeded", "failed", "unknown", "blocked")}
    qa_verdicts = {"PASS": 0, "NEEDS_ATTENTION": 0, "NOT_PASSED": 0, "UNVERIFIED": 0}
    tool_calls = 0
    tool_attempts = 0
    for workflow in workflows:
        report = evaluate_workflow(workflow)
        outcomes[report["outcome"]] += 1
        for status, count in report["task_counts"].items():
            if status in task_counts and type(count) is int and count >= 0:
                task_counts[status] += count
        verdict = report["qa_verdict"]
        if verdict in qa_verdicts:
            qa_verdicts[verdict] += 1
        tool_calls += report["tool_calls"]
        tool_attempts += report["tool_attempts"]
    total = len(workflows)
    return {
        "sampled_workflows": total,
        "outcomes": outcomes,
        "pass_rate": round(outcomes["passed"] / total, 4) if total else None,
        "task_counts": task_counts,
        "qa_verdicts": qa_verdicts,
        "tool_calls": tool_calls,
        "tool_attempts": tool_attempts,
    }


def build_workflow_timeline(
    workflow: dict[str, Any],
    task_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a sanitized timeline from durable workflow, task, and tool journals."""
    events: list[tuple[str, int, dict[str, Any]]] = []
    sequence = 0

    def add(at: Any, event: str, status: Any, **metadata: Any) -> None:
        nonlocal sequence
        if not isinstance(at, str) or not at:
            return
        item = {
            "at": at,
            "event": event,
            "status": status if isinstance(status, str) else "unknown",
        }
        for key in ("phase", "plan_index", "role", "tool_name"):
            value = metadata.get(key)
            if value is not None:
                item[key] = value
        events.append((at, sequence, item))
        sequence += 1

    add(workflow.get("created_at"), "workflow.created", "running")
    records = {
        item.get("id"): item
        for item in task_records
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    results = workflow.get("results")
    results = results if isinstance(results, dict) else {}
    outcomes = results.get("tasks")
    outcomes = outcomes if isinstance(outcomes, list) else []

    def add_task_events(outcome: dict[str, Any], *, phase: str, plan_index: int | None = None) -> None:
        task_id = outcome.get("task_id")
        task = records.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            return
        role = outcome.get("role") if isinstance(outcome.get("role"), str) else (
            "qa_security" if phase == "qa" else None
        )
        metadata = {"phase": phase, "plan_index": plan_index, "role": role}
        add(task.get("created_at"), "task.created", "queued", **metadata)
        add(task.get("updated_at"), "task.status", task.get("status"), **metadata)
        executions = task.get("tool_executions")
        if not isinstance(executions, list):
            return
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            tool_name = execution.get("tool_name")
            tool_metadata = {**metadata, "tool_name": tool_name[:100] if isinstance(tool_name, str) else None}
            add(execution.get("created_at"), "tool.started", "running", **tool_metadata)
            add(execution.get("updated_at"), "tool.status", execution.get("status"), **tool_metadata)

    for index, outcome in enumerate(outcomes):
        if isinstance(outcome, dict):
            plan_index = outcome.get("plan_index", index)
            if not isinstance(plan_index, int) or isinstance(plan_index, bool):
                plan_index = index
            add_task_events(outcome, phase="specialist", plan_index=plan_index)

    qa_review = results.get("qa_review")
    if isinstance(qa_review, dict):
        add_task_events(qa_review, phase="qa")

    add(workflow.get("updated_at"), "workflow.status", workflow.get("status"))
    events.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in events]
