import pytest

from crows.agents import list_agent_roles, plan_subtasks


def test_specialist_agent_registry_has_unique_roles():
    roles = list_agent_roles()
    ids = [role["id"] for role in roles]
    assert len(ids) >= 6
    assert len(ids) == len(set(ids))
    assert {"planner", "coding", "qa_security", "research", "memory", "monitor"} <= set(ids)


def test_registry_roles_do_not_claim_direct_file_mutation():
    for role in list_agent_roles():
        assert role["can_modify_files"] is False
        assert role["requires_approval_for_external_actions"] is True


def test_registry_returns_defensive_copies():
    first = list_agent_roles()
    first[0]["capabilities"].append("injected_capability")
    assert "injected_capability" not in list_agent_roles()[0]["capabilities"]


def test_plan_validation_accepts_valid_bounded_tasks():
    result = plan_subtasks([{"title":"Inspect code","input":"Review current implementation","role":"qa_security"}])
    assert result == [{"title":"Inspect code","input":"Review current implementation","role":"qa_security","depends_on":[]}]


@pytest.mark.parametrize("value", [None, [], [{}], [{"title":"x","input":"y","role":"root"}],
                                  [{"title":"x"*121,"input":"y","role":"coding"}],
                                  [{"title":"x","input":"y"*2001,"role":"coding"}],
                                  [{"title":"x","input":"y","role":"coding"}]*9])
def test_plan_validation_rejects_invalid_or_oversized_tasks(value):
    with pytest.raises(ValueError):
        plan_subtasks(value)


def test_plan_subtasks_validates_dependencies_and_topological_order():
    from crows.agents import dependency_order

    plan = plan_subtasks([
        {"title": "Final report", "input": "Summarize results", "role": "qa_security", "depends_on": [2]},
        {"title": "Independent scan", "input": "Inspect independently", "role": "research"},
        {"title": "Collect evidence", "input": "Gather evidence first", "role": "research"},
    ])
    order = dependency_order(plan)
    assert order.index(2) < order.index(0)
    assert order.index(1) < order.index(0)
    assert plan[0]["depends_on"] == [2]


@pytest.mark.parametrize("dependencies", [[0], [3], [-1], [1, 1], ["1"]])
def test_plan_subtasks_rejects_invalid_dependency_indexes(dependencies):
    plan = [
        {"title": "First", "input": "First step", "role": "research", "depends_on": dependencies},
        {"title": "Second", "input": "Second step", "role": "monitor"},
    ]
    with pytest.raises(ValueError):
        plan_subtasks(plan)


def test_plan_subtasks_rejects_dependency_cycles():
    plan = [
        {"title": "First", "input": "First step", "role": "research", "depends_on": [1]},
        {"title": "Second", "input": "Second step", "role": "monitor", "depends_on": [0]},
    ]
    with pytest.raises(ValueError, match="acyclic"):
        plan_subtasks(plan)
