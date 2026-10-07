"""Shared fixtures for the end-to-end suite (VAL-605).

Run from the repo root:  py -3.13 -m pytest tests/integration -q
Every test here carries the `integration` marker (registered in pyproject.toml);
deselect with `-m "not integration"`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TIMEOUT = 300

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _git_bash() -> str | None:
    """Git for Windows' bash, derived from `git --exec-path`.

    On Windows a bare `bash` handed to CreateProcess resolves System32 first,
    which is the WSL launcher, not Git Bash; the hook suites need Git Bash.
    """
    try:
        exec_path = subprocess.run(
            ["git", "--exec-path"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
    except OSError:
        return None
    if not exec_path:
        return None
    # <git>/mingw64/libexec/git-core -> <git>/bin/bash.exe or <git>/usr/bin/bash.exe
    git_root = Path(exec_path).parents[2]
    for cand in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
        if cand.is_file():
            return str(cand)
    return None


def _find_bash() -> str | None:
    override = os.environ.get("CCV_BASH")
    if override:
        return override
    if sys.platform == "win32":
        found = _git_bash()
        if found:
            return found
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            cand = Path(entry) / "bash.exe"
            low = str(cand).lower()
            if cand.is_file() and "system32" not in low and "windowsapps" not in low:
                return str(cand)
        return None
    return shutil.which("bash")


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def python() -> str:
    """The interpreter running pytest (py -3.13 under the documented command)."""
    return sys.executable


@pytest.fixture(scope="session")
def bash() -> str:
    path = _find_bash()
    if not path:
        pytest.skip("no Git Bash / POSIX bash found (set CCV_BASH)")
    return path


@pytest.fixture(scope="session")
def run() -> Runner:
    """subprocess.run with captured UTF-8 text output and a timeout; never raises on exit code."""

    def _run(
        args: list[str],
        cwd: Path = REPO,
        env: dict[str, str] | None = None,
        timeout: int = TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    return _run


@pytest.fixture(scope="session")
def simulated_install(
    tmp_path_factory: pytest.TempPathFactory, python: str, run: Runner
) -> dict[str, Path | dict[str, str]]:
    """home/.claude built by `install/sync_global.py --apply --target`, plus a sibling
    project dir; env has HOME/USERPROFILE -> home and no PYTHONPATH."""
    root = tmp_path_factory.mktemp("ccv47-install")
    home = root / "home"
    install = home / ".claude"
    proj = root / "proj"
    proj.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(USERPROFILE=str(home), HOME=str(home), PYTHONIOENCODING="utf-8")
    p = run(
        [
            python,
            str(REPO / "install" / "sync_global.py"),
            "--apply",
            "--target",
            str(install),
        ],
        env=env,
    )
    assert p.returncode == 0, (
        f"sync_global --apply --target failed ({p.returncode})\n"
        f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    return {"home": home, "install": install, "proj": proj, "env": env}
