import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any


MAX_OUTPUT_BYTES = 12_000


def _capture_bounded_output(stream, limit: int = MAX_OUTPUT_BYTES) -> tuple[bytes, int]:
    """Drain a child pipe completely while retaining only its final bounded tail."""
    tail = bytearray()
    total = 0
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        total += len(chunk)
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]
    return bytes(tail), total


class SandboxError(RuntimeError):
    pass


class DockerTestSandbox:
    """Run pytest in a disposable, network-isolated Docker container.

    The image must be built locally ahead of time. No image pulls occur at run time.
    This reduces exposure but is not a substitute for a patched host/container runtime.
    """
    EXCLUDED_DIRS = {
        ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "data",
        ".ssh", ".aws", ".azure", ".gnupg",
    }
    EXCLUDED_FILES = {".env", ".env.local", ".env.production", ".env.development",
                      "id_rsa", "id_ed25519", "credentials", "credentials.json"}
    EXCLUDED_SUFFIXES = {
        ".pem", ".key", ".p12", ".pfx", ".p7b", ".p7c",
        ".p8", ".p10", ".p12", ".pfx", ".der", ".crt",
    }
    EXCLUDED_SECRET_NAMES = {
        ".netrc", ".npmrc", ".pypirc", ".dockercfg",
        "id_rsa", "id_ed25519", "known_hosts",
        "credentials", "credentials.json",
    }

    def __init__(self, workspace: Path, image: str = "crows-sandbox:latest",
                 timeout_seconds: int = 60, memory_limit: str = "512m",
                 cpu_limit: str = "1", pids_limit: int = 128):
        self.workspace = workspace.resolve()
        self.image = image
        self.timeout_seconds = max(5, min(timeout_seconds, 120))
        if not isinstance(memory_limit, str) or not re.fullmatch(r"[1-9][0-9]*[bBkKmMgG]?", memory_limit):
            raise SandboxError("Invalid sandbox memory limit")
        if (
            not isinstance(cpu_limit, str)
            or cpu_limit.count(".") > 1
            or not cpu_limit.replace(".", "", 1).isdigit()
            or float(cpu_limit) <= 0
        ):
            raise SandboxError("Invalid sandbox CPU limit")
        if isinstance(pids_limit, bool) or not isinstance(pids_limit, int):
            raise SandboxError("Invalid sandbox PID limit")
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.pids_limit = max(16, min(pids_limit, 256))

    def _snapshot(self, destination: Path) -> None:
        if not self.workspace.is_dir():
            raise SandboxError("Workspace directory does not exist")
        total_bytes = 0
        file_count = 0
        excluded_dirs = {name.casefold() for name in self.EXCLUDED_DIRS}
        for root, dirs, files in os.walk(self.workspace, followlinks=False):
            root_path = Path(root)
            for name in dirs:
                directory = root_path / name
                if name.casefold() not in excluded_dirs and directory.is_symlink():
                    relative = directory.relative_to(self.workspace)
                    raise SandboxError(
                        f"Workspace contains a symlink directory; remove it before sandbox execution: {relative}"
                    )
            dirs[:] = [name for name in dirs if name.casefold() not in excluded_dirs]
            relative_root = root_path.relative_to(self.workspace)
            target_root = destination / relative_root
            target_root.mkdir(parents=True, exist_ok=True)
            for name in files:
                source = root_path / name
                if source.is_symlink():
                    raise SandboxError(f"Workspace contains a symlink; remove it before sandbox execution: {source.relative_to(self.workspace)}")
                lower_name = name.lower()
                if (
                    name in self.EXCLUDED_FILES
                    or lower_name in self.EXCLUDED_SECRET_NAMES
                    or lower_name == ".env"
                    or lower_name.startswith(".env.")
                    or source.suffix.lower() in self.EXCLUDED_SUFFIXES
                    or "credential" in lower_name
                    or "secret" in lower_name
                ):
                    continue
                if not source.is_file():
                    continue
                file_count += 1
                total_bytes += source.stat().st_size
                if file_count > 5000 or total_bytes > 50_000_000:
                    raise SandboxError("Workspace snapshot exceeds limits (5,000 files / 50 MB)")
                target = target_root / name
                shutil.copyfile(source, target)
                target.chmod(0o666)
            target_root.chmod(0o777)
        # Ensure nested directories are writable by the unprivileged container UID.
        for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                path.chmod(0o777)
            elif path.is_file():
                path.chmod(0o666)

    def run_pytest(self) -> dict[str, Any]:
        if not shutil.which("docker"):
            raise SandboxError("Docker CLI not found. Install Docker and build the local sandbox image first.")
        if (
            not isinstance(self.image, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", self.image)
        ):
            raise SandboxError("Invalid sandbox image reference")
        with tempfile.TemporaryDirectory(prefix="crows-sandbox-") as temp:
            snapshot = Path(temp) / "workspace"
            snapshot.mkdir(mode=0o777)
            self._snapshot(snapshot)
            # Pytest may create substantial temporary databases and files. Keep
            # them on the disposable workspace mount rather than the small /tmp
            # tmpfs, while preserving the real workspace isolation boundary.
            pytest_tmp = snapshot / ".pytest-tmp"
            pytest_tmp.mkdir(mode=0o777)
            pytest_tmp.chmod(0o777)
            cid_file = Path(temp) / "container-id"
            command = [
                "docker", "run", "--rm", "--pull=never",
                "--cidfile", str(cid_file),
                "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit", str(self.pids_limit),
                "--memory", self.memory_limit, "--cpus", self.cpu_limit,
                "--user", "65534:65534",
                "-e", "TMPDIR=/workspace/.pytest-tmp",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "-v", f"{snapshot}:/workspace:rw",
                "-w", "/workspace", self.image,
                "python", "-m", "pytest", "-q",
            ]
            try:
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                )
                assert process.stdout is not None and process.stderr is not None
                stdout_result: list[tuple[bytes, int]] = []
                stderr_result: list[tuple[bytes, int]] = []
                stdout_reader = threading.Thread(
                    target=lambda: stdout_result.append(_capture_bounded_output(process.stdout)),
                    daemon=True,
                )
                stderr_reader = threading.Thread(
                    target=lambda: stderr_result.append(_capture_bounded_output(process.stderr)),
                    daemon=True,
                )
                stdout_reader.start()
                stderr_reader.start()
                timed_out = False
                try:
                    returncode = process.wait(timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    process.wait()
                    # Killing the Docker CLI alone can leave its container running.
                    # Use the cidfile to stop the container so --rm can clean it up.
                    try:
                        container_id = cid_file.read_text(encoding="ascii").strip()
                        if container_id:
                            subprocess.run(
                                ["docker", "kill", container_id],
                                capture_output=True, text=True, timeout=5,
                                check=False,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                            )
                    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
                        pass
                    returncode = None
                stdout_reader.join(timeout=5)
                stderr_reader.join(timeout=5)
                stdout_bytes, stdout_total = stdout_result[0] if stdout_result else (b"", 0)
                stderr_bytes, stderr_total = stderr_result[0] if stderr_result else (b"", 0)
                output = {
                    "command": command,
                    "returncode": returncode,
                    "passed": returncode == 0 and not timed_out,
                    "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                    "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                    "stdout_truncated": stdout_total > MAX_OUTPUT_BYTES,
                    "stderr_truncated": stderr_total > MAX_OUTPUT_BYTES,
                    "timeout_seconds": self.timeout_seconds,
                    "sandboxed": True,
                    "network": "disabled",
                    "workspace_mode": "disposable-copy",
                }
                if timed_out:
                    output["timed_out"] = True
                return output
            except OSError as exc:
                raise SandboxError(f"Could not start Docker: {exc}") from exc
