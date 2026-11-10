from pathlib import Path

import pytest

from crows.sandbox import DockerTestSandbox, SandboxError


@pytest.mark.parametrize("memory_limit", ["512m", "1g", "1024", "64k", "1B"])
def test_accepts_valid_memory_limits(tmp_path: Path, memory_limit: str):
    sandbox = DockerTestSandbox(tmp_path, memory_limit=memory_limit)
    assert sandbox.memory_limit == memory_limit


@pytest.mark.parametrize("memory_limit", ["", "0", "0m", "-1g", "512 m", "512m --privileged", "1.5g"])
def test_rejects_invalid_memory_limits(tmp_path: Path, memory_limit: str):
    with pytest.raises(SandboxError, match="memory limit"):
        DockerTestSandbox(tmp_path, memory_limit=memory_limit)


@pytest.mark.parametrize("cpu_limit", ["1", "0.5", "2.25", ".5", "1."])
def test_accepts_valid_cpu_limits(tmp_path: Path, cpu_limit: str):
    sandbox = DockerTestSandbox(tmp_path, cpu_limit=cpu_limit)
    assert sandbox.cpu_limit == cpu_limit


@pytest.mark.parametrize("cpu_limit", ["", "0", "0.0", "-1", "1e2", "1 cpu", "0.5 --privileged"])
def test_rejects_invalid_cpu_limits(tmp_path: Path, cpu_limit: str):
    with pytest.raises(SandboxError, match="CPU limit"):
        DockerTestSandbox(tmp_path, cpu_limit=cpu_limit)


@pytest.mark.parametrize("pids_limit", [True, False, "128", 128.0])
def test_rejects_non_integer_pid_limits(tmp_path: Path, pids_limit):
    with pytest.raises(SandboxError, match="PID limit"):
        DockerTestSandbox(tmp_path, pids_limit=pids_limit)


def test_clamps_pid_limits_to_existing_safe_bounds(tmp_path: Path):
    assert DockerTestSandbox(tmp_path, pids_limit=1).pids_limit == 16
    assert DockerTestSandbox(tmp_path, pids_limit=1000).pids_limit == 256
