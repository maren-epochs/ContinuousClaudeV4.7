"""DS-bridge tests for ouros_harness.py.

Covers:
  (a) run_python: host CPython bridge, pandas end-to-end
  (b) write_file binary safety: PNG bytes round-trip
  (c) OUROS_DATA_ROOTS: read-only allowlist extension, deny-by-default preserved
  (d) --session default-load semantics + --reset wipe
  (e) artifact surfacing: new files in sandbox output dir printed with absolute paths

Run: py -3.13 tools/test_ouros_harness_ds.py

Tests go through the CLI via subprocess — same path agents use.
"""

import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT / "tools" / "ouros_harness.py"
# "/tmp/ouros-sandbox-output" is drive-relative on Windows: resolves against
# the current drive (C:\tmp\ouros-sandbox-output when cwd is on C:).
OUTPUT_ROOT = Path("/tmp/ouros-sandbox-output").resolve()


def _posix(p):
    """Forward-slash path string — safe inside ouros string literals."""
    return str(p).replace("\\", "/")


def run_harness(code, *extra_args, env_overrides=None, timeout=180):
    """Run the harness with sandbox `code` via --file; return CompletedProcess."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Deterministic default: no data roots unless the test sets them.
    env["OUROS_DATA_ROOTS"] = ""
    if env_overrides:
        env.update(env_overrides)
    fd, path = tempfile.mkstemp(suffix=".py", prefix="ouros-ds-test-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        return subprocess.run(
            [sys.executable, str(HARNESS), "--file", path, *extra_args],
            cwd=str(PROJECT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    finally:
        os.unlink(path)


def run_harness_raw(*args, env_overrides=None, timeout=60):
    """Run the harness CLI without --file (for --list-vars etc.)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["OUROS_DATA_ROOTS"] = ""
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=str(PROJECT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


class TestRunPython(unittest.TestCase):
    """(a) run_python executes on host CPython with the full DS stack."""

    def test_pandas_end_to_end(self):
        code = (
            'out = run_python("import pandas as pd; '
            "df = pd.DataFrame({'a': [1, 2, 3]}); "
            'print(df.shape)")\n'
            "print(out)\n"
        )
        r = run_harness(code)
        self.assertIn("(3, 1)", r.stdout,
                      f"pandas shape not in output\nstdout: {r.stdout}\nstderr: {r.stderr}")

    def test_error_output_surfaced(self):
        code = (
            'out = run_python("raise ValueError(12345)")\n'
            "print(out)\n"
        )
        r = run_harness(code)
        self.assertIn("ValueError", r.stdout)
        self.assertIn("12345", r.stdout)


class TestBinaryWriteFile(unittest.TestCase):
    """(b) write_file with bytes round-trips a PNG header intact."""

    def test_png_roundtrip(self):
        name = f"roundtrip-{uuid.uuid4().hex}.png"
        target = OUTPUT_ROOT / name
        code = (
            "png = bytes([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 1])\n"
            f"r = write_file('{_posix(target)}', png)\n"
            "print(r)\n"
        )
        try:
            r = run_harness(code)
            self.assertTrue(target.exists(),
                            f"file not written\nstdout: {r.stdout}\nstderr: {r.stderr}")
            data = target.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n",
                             f"PNG magic corrupted: {data[:8]!r}")
            self.assertEqual(len(data), 12)
        finally:
            if target.exists():
                target.unlink()


class TestDataRoots(unittest.TestCase):
    """(c) OUROS_DATA_ROOTS extends read_allow read-only; deny-by-default holds."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ouros-data-root-")
        self.csv = Path(self.dir) / "data.csv"
        self.csv.write_text("x,y\n1,2\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_denied_without_env(self):
        code = f"print(read_file('{_posix(self.csv)}'))\n"
        r = run_harness(code)
        self.assertIn("denied", r.stdout,
                      f"expected denial\nstdout: {r.stdout}\nstderr: {r.stderr}")
        self.assertNotIn("x,y", r.stdout)

    def test_allowed_with_env(self):
        code = f"print(read_file('{_posix(self.csv)}'))\n"
        r = run_harness(code, env_overrides={"OUROS_DATA_ROOTS": self.dir})
        self.assertIn("x,y", r.stdout,
                      f"CSV not readable with OUROS_DATA_ROOTS set\nstdout: {r.stdout}\nstderr: {r.stderr}")

    def test_data_root_is_read_only(self):
        target = Path(self.dir) / "should-not-exist.txt"
        code = f"print(write_file('{_posix(target)}', 'nope'))\n"
        r = run_harness(code, env_overrides={"OUROS_DATA_ROOTS": self.dir})
        self.assertIn("denied", r.stdout)
        self.assertFalse(target.exists())


class TestSessionDefaultLoad(unittest.TestCase):
    """(d) --session NAME on an existing saved session loads; --reset wipes."""

    def setUp(self):
        self.storage = tempfile.mkdtemp(prefix="ouros-sessions-")
        self.session = f"ds-test-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.storage, ignore_errors=True)

    def _args(self):
        return ["--session", self.session, "--storage", self.storage]

    def test_default_load_then_reset(self):
        # Run 1: set a variable, session gets saved.
        r1 = run_harness("marker_var = 'alive-and-well'\nprint('run1-done')\n",
                         *self._args())
        self.assertIn("run1-done", r1.stdout, f"stderr: {r1.stderr}")

        # Run 2: --session only, no --load. Must LOAD, not reset.
        r2 = run_harness("print(marker_var)\n", *self._args())
        self.assertIn("alive-and-well", r2.stdout,
                      f"session state lost without --load\nstdout: {r2.stdout}\nstderr: {r2.stderr}")

        # Run 3: --reset wipes state.
        r3 = run_harness("print('run3-done')\n", *self._args(), "--reset")
        self.assertIn("run3-done", r3.stdout, f"stderr: {r3.stderr}")

        r4 = run_harness_raw("--session", self.session, "--storage", self.storage,
                             "--list-vars")
        self.assertNotIn("marker_var", r4.stdout,
                         f"--reset did not wipe state\nstdout: {r4.stdout}")

    def test_explicit_load_still_works(self):
        r1 = run_harness("keep_me = 41 + 1\nprint('saved')\n", *self._args())
        self.assertIn("saved", r1.stdout, f"stderr: {r1.stderr}")
        r2 = run_harness("print(keep_me)\n", *self._args(), "--load")
        self.assertIn("42", r2.stdout, f"stderr: {r2.stderr}")


class TestArtifactSurfacing(unittest.TestCase):
    """(e) New files in the sandbox output dir are printed with absolute paths."""

    def test_write_file_artifact_listed(self):
        name = f"artifact-{uuid.uuid4().hex}.txt"
        target = OUTPUT_ROOT / name
        code = f"r = write_file('{_posix(target)}', 'hello artifacts')\nprint(r)\n"
        try:
            r = run_harness(code)
            self.assertIn("artifacts:", r.stdout,
                          f"no artifacts section\nstdout: {r.stdout}\nstderr: {r.stderr}")
            # Path printed in the artifacts section must be an absolute host path.
            lines = r.stdout.splitlines()
            start = lines.index("artifacts:")
            artifact_lines = [l.strip() for l in lines[start + 1:] if l.strip()]
            matching = [l for l in artifact_lines if name in l]
            self.assertTrue(matching, f"artifact not listed: {artifact_lines}")
            self.assertTrue(Path(matching[0]).is_absolute(), f"not absolute: {matching[0]}")
        finally:
            if target.exists():
                target.unlink()

    def test_run_python_relative_write_lands_in_work_dir(self):
        name = f"relwrite-{uuid.uuid4().hex}.txt"
        code = (
            f"out = run_python(\"open('{name}', 'w').write('from-host')\")\n"
            "print('wrote-relative')\n"
        )
        work_dir_file = OUTPUT_ROOT / "default" / name
        try:
            r = run_harness(code)
            self.assertIn("wrote-relative", r.stdout, f"stderr: {r.stderr}")
            self.assertTrue(work_dir_file.exists(),
                            f"relative write did not land in work dir\nstdout: {r.stdout}")
            self.assertIn("artifacts:", r.stdout)
            self.assertIn(name, r.stdout)
        finally:
            if work_dir_file.exists():
                work_dir_file.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
