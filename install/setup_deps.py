"""Install the Python dependencies for this repo (used by `make setup` and install/setup.ps1).

Source, first match wins:
  1. requirements.lock (exact pins)
  2. pyproject.toml [project] dependencies + every optional-dependencies extra
     + every [dependency-groups] list (PEP 735; include-group tables are skipped)
  3. tools/requirements.txt + tools/requirements-viz.txt

Then, always: pip install --no-deps plotly-resampler==0.11.1. It declares plotly<7
but works on plotly 7.1 (verified 2026-10-05), so it is kept out of the lock and
installed without its dependency check; its runtime deps are pinned in the lock.

pip skips already-satisfied requirements, so re-running is a no-op.

--hook runs only the pre-commit hook step (VAL-712). `pre-commit install` writes the
interpreter that ran it into the clone's shared .git/hooks/pre-commit, so a setup run
from a throwaway venv would repoint every later commit at that venv. The step runs
`pre-commit install` only when no hook exists, when the existing hook's INSTALL_PYTHON
path no longer exists, or with --force-hook (implies --hook); otherwise it prints a
one-line note and leaves the hook alone.

Usage: py -3.13 install/setup_deps.py [--dry-run]
       py -3.13 install/setup_deps.py --hook [--force-hook]
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NO_DEPS = ["plotly-resampler==0.11.1"]
INSTALL_PYTHON_RE = re.compile(r"^INSTALL_PYTHON=(.*)$", re.MULTILINE)

Runner = Callable[[list[str], Path, bool], int]


def _call(cmd: list[str], cwd: Path, quiet: bool) -> int:
    """subprocess.call in cwd; quiet discards stdout and stderr."""
    sink = subprocess.DEVNULL if quiet else None
    return subprocess.call(cmd, cwd=cwd, stdout=sink, stderr=sink)


def hook_file(repo: Path) -> Path:
    """The pre-commit hook git will run (honors worktrees' shared dir and core.hooksPath)."""
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return (repo / out / "pre-commit").resolve()


def install_python(hook: Path) -> str | None:
    """INSTALL_PYTHON from a pre-commit-generated hook; None if absent or unparseable."""
    match = INSTALL_PYTHON_RE.search(hook.read_text(encoding="utf-8", errors="replace"))
    if not match:
        return None
    try:
        parts = shlex.split(match.group(1))
    except ValueError:
        return None
    return parts[0] if parts else None


def hook_decision(hook: Path, force: bool) -> tuple[bool, str]:
    """(install?, reason). Keep a hook whose interpreter still exists, or a foreign hook."""
    if force:
        return True, "--force-hook"
    if not hook.is_file():
        return True, "no hook"
    python = install_python(hook)
    if python is None:
        return False, f"{hook} is not a pre-commit hook (no INSTALL_PYTHON)"
    if not Path(python).exists():
        return True, f"hook interpreter {python} no longer exists"
    return False, f"hook already uses INSTALL_PYTHON={python}"


def install_hook(
    repo: Path,
    force: bool = False,
    python: list[str] | None = None,
    run: Runner = _call,
) -> int:
    """Run `pre-commit install` in repo only when hook_decision says so."""
    py = python or [sys.executable]
    if not (repo / ".pre-commit-config.yaml").is_file():
        print("skip: no .pre-commit-config.yaml")
        return 0
    if run([*py, "-m", "pre_commit", "--version"], repo, True) != 0:
        print("skip: pre-commit not installed")
        return 0
    install, reason = hook_decision(hook_file(repo), force)
    if not install:
        print(
            f"setup_deps: pre-commit hook kept ({reason}); --force-hook repoints it at {py[0]}"
        )
        return 0
    cmd = [*py, "-m", "pre_commit", "install"]
    print(f"setup_deps: {reason}: {' '.join(cmd)}")
    return run(cmd, repo, False)


def pyproject_requirements(path: Path) -> list[str]:
    """Deduplicated deps + all extras + string dependency-group items from pyproject."""
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    reqs: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        reqs.extend(item for item in group if isinstance(item, str))
    return list(dict.fromkeys(reqs))


def pip_args() -> tuple[str, list[str]]:
    """Pick the first available requirement source; return (label, pip install args)."""
    lock = ROOT / "requirements.lock"
    if lock.is_file():
        return "requirements.lock", ["-r", str(lock)]
    reqs = pyproject_requirements(ROOT / "pyproject.toml")
    if reqs:
        return "pyproject.toml", reqs
    files = [
        ROOT / "tools" / "requirements.txt",
        ROOT / "tools" / "requirements-viz.txt",
    ]
    args: list[str] = []
    for f in files:
        if f.is_file():
            args += ["-r", str(f)]
    return "tools/requirements*.txt", args


def main(argv: list[str]) -> int:
    """Install requirements then the --no-deps pins; --dry-run prints commands only.

    --hook / --force-hook: run only the guarded pre-commit hook step instead.
    """
    if "--hook" in argv or "--force-hook" in argv:
        return install_hook(ROOT, force="--force-hook" in argv)
    source, args = pip_args()
    if not args:
        print("setup_deps: no requirements found; nothing to install")
        return 0
    pip = [sys.executable, "-m", "pip", "install"]
    cmds = [[*pip, *args], [*pip, "--no-deps", *NO_DEPS]]
    print(f"setup_deps: installing from {source}, then --no-deps {' '.join(NO_DEPS)}")
    for cmd in cmds:
        if "--dry-run" in argv:
            print(" ".join(cmd))
            continue
        rc = subprocess.call(cmd)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
