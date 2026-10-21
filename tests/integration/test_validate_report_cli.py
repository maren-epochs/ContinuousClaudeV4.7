"""tools/validate_report.py as a CLI on fixture reports: valid -> exit 0, invalid -> exit 1."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

Runner = Callable[..., subprocess.CompletedProcess[str]]
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def _validate(repo: Path, python: str, run: Runner, *paths: Path):
    return run([python, str(repo / "tools" / "validate_report.py"), *map(str, paths)])


def test_valid_report_exits_zero(repo: Path, python: str, run: Runner) -> None:
    p = _validate(repo, python, run, FIXTURES / "report_valid.json")
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert "ERROR" not in p.stdout + p.stderr


def test_invalid_report_exits_nonzero(repo: Path, python: str, run: Runner) -> None:
    p = _validate(repo, python, run, FIXTURES / "report_invalid.json")
    out = p.stdout + p.stderr
    assert p.returncode == 1, f"exit {p.returncode}\n{out}"
    assert "ERROR" in out
    assert "result" in out, out


def test_mixed_batch_fails(repo: Path, python: str, run: Runner) -> None:
    p = _validate(
        repo,
        python,
        run,
        FIXTURES / "report_valid.json",
        FIXTURES / "report_invalid.json",
    )
    assert p.returncode == 1, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
