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

Usage: py -3.13 install/setup_deps.py [--dry-run]
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NO_DEPS = ["plotly-resampler==0.11.1"]


def pyproject_requirements(path: Path) -> list[str]:
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
