"""Build and exercise Crows from a fresh wheel installation.

This smoke test deliberately runs the installed package from outside the
repository checkout so the source tree cannot hide packaging mistakes.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]
SMOKE_CODE = r"""
from importlib.metadata import version
from pathlib import Path

import crows
from fastapi.testclient import TestClient
from crows.app import app

package_path = Path(crows.__file__).resolve()
assert "site-packages" in str(package_path).lower(), package_path
assert version("crows") == "0.4.0"

with TestClient(app, client=("127.0.0.1", 50000)) as client:
    health = client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json()["status"] == "ok", health.json()
    assert health.json()["project"] == "crows", health.json()

    database = client.get("/health/db")
    assert database.status_code == 200, database.text
    assert database.json() == {
        "status": "ok",
        "database": "reachable",
    }, database.json()

print(f"Clean-install smoke passed: {package_path}")
"""


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="crows-clean-install-") as temporary:
        temp = Path(temporary)
        wheelhouse = temp / "wheelhouse"
        wheelhouse.mkdir()
        environment = temp / "venv"
        isolated_cwd = temp / "outside-checkout"
        isolated_cwd.mkdir()
        data = temp / "data"
        workspace = temp / "workspace"
        data.mkdir()
        workspace.mkdir()

        run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheelhouse),
                str(ROOT),
            ],
            cwd=isolated_cwd,
        )

        wheels = list(wheelhouse.glob("crows-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected exactly one Crows wheel, found: {wheels}")

        venv.EnvBuilder(with_pip=True).create(environment)
        venv_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(
            [str(venv_python), "-m", "pip", "install", str(wheels[0])],
            cwd=isolated_cwd,
        )

        smoke_env = os.environ.copy()
        for name in ("CROWS_API_TOKEN", "AETHER_API_TOKEN", "PYTHONPATH"):
            smoke_env.pop(name, None)
        smoke_env.update(
            {
                "CROWS_DB_PATH": str(data / "crows.sqlite3"),
                "CROWS_WORKSPACE": str(workspace),
                "CROWS_MESSAGING_WORKER_ENABLED": "false",
                "CROWS_INBOUND_AGENT_WORKER_ENABLED": "false",
                "CROWS_SCHEDULER_WORKER_ENABLED": "false",
            }
        )
        run(
            [str(venv_python), "-c", SMOKE_CODE],
            cwd=isolated_cwd,
            env=smoke_env,
        )


if __name__ == "__main__":
    main()
