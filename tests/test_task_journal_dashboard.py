from crows import app as app_module


def test_task_journal_dashboard_is_in_static_ui():
    from pathlib import Path

    html = (Path(app_module.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="tasksHeading"' in html
    assert 'id="refreshTasks"' in html
    assert 'id="createTask"' in html
    assert 'api("/tasks?limit=100")' in html
    assert 'api("/tasks/"+encodeURIComponent(id))' in html


def test_task_api_routes_remain_registered():
    routes = {
        (getattr(route, "path", None), tuple(sorted(getattr(route, "methods", set()) or set())))
        for route in app_module.app.routes
    }
    assert ("/tasks", ("GET",)) in routes
    assert ("/tasks", ("POST",)) in routes
    assert ("/tasks/{task_id}", ("GET",)) in routes
