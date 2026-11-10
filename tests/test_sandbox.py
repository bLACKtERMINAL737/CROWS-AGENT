from pathlib import Path
import pytest

from crows.sandbox import DockerTestSandbox, SandboxError


def test_snapshot_copies_workspace_and_skips_excluded_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("print('hello')\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "secret").write_text("ignore me", encoding="utf-8")
    (workspace / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (workspace / "private.pem").write_text("private key", encoding="utf-8")
    destination = tmp_path / "snapshot"
    destination.mkdir()

    sandbox = DockerTestSandbox(workspace)
    sandbox._snapshot(destination)

    assert (destination / "sample.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert not (destination / ".git").exists()
    assert not (destination / ".env").exists()
    assert not (destination / "private.pem").exists()


def test_snapshot_rejects_symlinks(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (workspace / "linked.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    destination = tmp_path / "snapshot"
    destination.mkdir()

    with pytest.raises(SandboxError, match="symlink"):
        DockerTestSandbox(workspace)._snapshot(destination)


def test_test_runner_has_network_disabled_and_read_only_flags(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tests").mkdir()
    sandbox = DockerTestSandbox(workspace)
    captured = {}

    import io

    class Process:
        stdout = io.BytesIO(b"ok")
        stderr = io.BytesIO(b"")

        def wait(self, timeout=None):
            return 0

    def fake_which(name):
        return "/usr/bin/docker"

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        workspace_mount = command[command.index("-v") + 1]
        snapshot_path = Path(workspace_mount.split(":/workspace:", 1)[0])
        pytest_tmp = snapshot_path / ".pytest-tmp"
        captured["pytest_tmp_is_dir"] = pytest_tmp.is_dir()
        captured["pytest_tmp_mode"] = pytest_tmp.stat().st_mode & 0o777
        return Process()

    monkeypatch.setattr("crows.sandbox.shutil.which", fake_which)
    monkeypatch.setattr("crows.sandbox.subprocess.Popen", fake_popen)
    result = sandbox.run_pytest()

    assert result["passed"] is True
    assert "--network=none" in captured["command"]
    assert "--read-only" in captured["command"]
    assert "--cap-drop=ALL" in captured["command"]
    assert captured["command"][captured["command"].index("-e") + 1] == "TMPDIR=/workspace/.pytest-tmp"
    # Capture these while fake_popen runs: TemporaryDirectory is cleaned up
    # as soon as run_pytest returns, before assertions below execute.
    assert captured["pytest_tmp_is_dir"] is True
    assert captured["pytest_tmp_mode"] == 0o777
    assert result["sandboxed"] is True


def test_snapshot_skips_common_secret_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".netrc").write_text("secret", encoding="utf-8")
    (workspace / "credentials.json").write_text('{"token":"secret"}', encoding="utf-8")
    (workspace / "service-secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "config.py").write_text("SAFE = True", encoding="utf-8")
    destination = tmp_path / "snapshot"
    destination.mkdir()

    DockerTestSandbox(workspace)._snapshot(destination)

    assert not (destination / ".netrc").exists()
    assert not (destination / "credentials.json").exists()
    assert not (destination / "service-secret.txt").exists()
    assert (destination / "config.py").exists()



@pytest.mark.parametrize(
    "image",
    [
        "--privileged",
        "--pid=host",
        "--volume=/:/host",
        "-v",
        "",
        " image:latest",
        "image:latest ",
        "image:latest\n--privileged",
        "image;--privileged",
    ],
)
def test_run_pytest_rejects_invalid_or_option_like_image_reference(
    tmp_path: Path, monkeypatch, image: str
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerTestSandbox(workspace, image=image)
    monkeypatch.setattr("crows.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    with pytest.raises(SandboxError, match="Invalid sandbox image reference"):
        sandbox.run_pytest()


def test_test_runner_bounds_captured_output_without_truncating_pipe_drain(
    tmp_path: Path, monkeypatch
):
    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerTestSandbox(workspace, timeout_seconds=10)
    monkeypatch.setattr("crows.sandbox.shutil.which", lambda _: "/usr/bin/docker")

    real_popen = subprocess.Popen

    def fake_popen(_command, **kwargs):
        return real_popen(
            [
                sys.executable, "-c",
                "import sys; sys.stdout.write('x' * 100000 + 'END'); "
                "sys.stderr.write('y' * 90000 + 'ERR')",
            ],
            **kwargs,
        )

    monkeypatch.setattr("crows.sandbox.subprocess.Popen", fake_popen)
    result = sandbox.run_pytest()

    assert result["stdout"].endswith("END")
    assert result["stderr"].endswith("ERR")
    assert len(result["stdout"].encode("utf-8")) <= 12_000
    assert len(result["stderr"].encode("utf-8")) <= 12_000
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
