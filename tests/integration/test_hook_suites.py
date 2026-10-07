"""Every .claude/hooks/test_*.sh suite, run as `bash <suite>` from the repo root.

Pass = exit 0 and a summary line reporting >0 passed and 0 failed. Suites print
either `RESULT: N passed, M failed` or `PASS=N FAIL=M`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
Runner = Callable[..., subprocess.CompletedProcess[str]]

SUITES = sorted((REPO / ".claude" / "hooks").glob("test_*.sh"))
SUMMARY = re.compile(
    r"RESULT:\s*(?P<p1>\d+)\s+passed,\s*(?P<f1>\d+)\s+failed"
    r"|PASS=(?P<p2>\d+)\s+FAIL=(?P<f2>\d+)"
)


def test_suites_discovered() -> None:
    assert SUITES, "no .claude/hooks/test_*.sh found"


@pytest.mark.parametrize("suite", SUITES, ids=[s.stem for s in SUITES])
def test_hook_suite(suite: Path, bash: str, run: Runner) -> None:
    p = run([bash, suite.relative_to(REPO).as_posix()], cwd=REPO)
    out = p.stdout + p.stderr
    tail = out[-4000:]
    assert p.returncode == 0, f"{suite.name} exit {p.returncode}\n{tail}"
    matches = list(SUMMARY.finditer(out))
    assert matches, f"{suite.name}: no RESULT/PASS summary line\n{tail}"
    m = matches[-1]
    passed = int(m.group("p1") or m.group("p2"))
    failed = int(m.group("f1") or m.group("f2"))
    assert failed == 0, f"{suite.name}: {failed} failed\n{tail}"
    assert passed > 0, f"{suite.name}: 0 passed\n{tail}"
