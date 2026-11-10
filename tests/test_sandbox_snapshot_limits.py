from pathlib import Path

import pytest

from crows.sandbox import DockerTestSandbox, SandboxError


def test_snapshot_rejects_more_than_5000_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = tmp_path / "snapshot"

    for index in range(5001):
        (workspace / f"file_{index:04}.txt").write_text("x", encoding="utf-8")

    sandbox = DockerTestSandbox(workspace)

    with pytest.raises(SandboxError, match="snapshot exceeds limits"):
        sandbox._snapshot(destination)


def test_snapshot_rejects_more_than_50_mb_before_copying(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = tmp_path / "snapshot"
    oversized_file = workspace / "large.txt"

    with oversized_file.open("wb") as file:
        file.truncate(50_000_001)

    sandbox = DockerTestSandbox(workspace)

    with pytest.raises(SandboxError, match="snapshot exceeds limits"):
        sandbox._snapshot(destination)

    assert not (destination / "large.txt").exists()
