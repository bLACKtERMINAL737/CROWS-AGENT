from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.sandbox import SandboxError
from crows.store import TaskStore


class FakeRouter:
    async def chat(self, message, task_type="general"):
        return {
            "answer": "{}",
            "route": "coding",
            "model": "fake",
            "fallback_used": False,
            "attempts": 1,
        }


def make_engine(tmp_path: Path, *, test_project: Path | None = None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    engine = CodingEngine(
        workspace,
        TaskStore(tmp_path / "crows.sqlite3"),
        FakeRouter(),
        enable_test_runner=True,
        test_project=test_project,
    )
    return engine, workspace


def test_run_tests_rejects_missing_configured_test_project(tmp_path: Path):
    missing_project = tmp_path / "missing-tests"
    engine, _ = make_engine(tmp_path, test_project=missing_project)

    with pytest.raises(CodingError, match="test project directory does not exist"):
        engine.run_tests()


def test_run_tests_rejects_workspace_without_test_project_markers(tmp_path: Path):
    engine, _ = make_engine(tmp_path)

    with pytest.raises(CodingError, match="requires a project pyproject.toml or tests/"):
        engine.run_tests()


def test_run_tests_translates_sandbox_errors_to_coding_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, workspace = make_engine(tmp_path)
    (workspace / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )

    def fail_sandbox(*args, **kwargs):
        raise SandboxError("Docker is unavailable")

    monkeypatch.setattr("crows.coding.DockerTestSandbox", fail_sandbox)

    with pytest.raises(CodingError, match="Docker is unavailable") as exc_info:
        engine.run_tests()

    assert isinstance(exc_info.value.__cause__, SandboxError)
