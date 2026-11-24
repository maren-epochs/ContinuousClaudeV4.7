#!/usr/bin/env python3
"""Build the stand-alone install zips (no git or GitHub needed) from a commit.

    py -3.13 install/build_zips.py [--ref HEAD] [--out DIR] [--only mac|windows]

Writes epoch-harness-mac.zip (Install-Mac.command, INSTALL-ON-MAC.txt) and epoch-harness-windows.zip
(Install-Windows.cmd) to DIR (default ~/Downloads), each holding the tracked tree of
REF under epoch-harness/, without the git-clone installer, .github/ and continuum/.
File modes (the executable bit on .command and .sh) and line endings come from
`git archive`, so .gitattributes decides them. Prints one line per zip.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREFIX = "epoch-harness/"
COMMON_DROP = ("Install-Epoch-Harness.cmd", ".github/", "continuum/")
PLANS = {
    "mac": ("Install-Windows.cmd", "Create-Epoch-Shortcut.cmd"),
    "windows": ("Install-Mac.command", "INSTALL-ON-MAC.txt"),
}


def archive(ref: str, repo: Path = REPO) -> bytes:
    """The tracked tree of ref as a zip, from git archive."""
    return subprocess.run(
        ["git", "archive", "--format=zip", f"--prefix={PREFIX}", ref],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout


def filtered(src: zipfile.ZipFile, drop: tuple[str, ...], out: Path) -> int:
    """Copy src to out without entries under PREFIX + any of drop; entry count."""
    skip = tuple(PREFIX + d for d in drop)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename.startswith(skip):
                continue
            entry = zipfile.ZipInfo(info.filename, info.date_time)
            entry.external_attr = info.external_attr
            entry.create_system = 3  # unix, so macOS keeps the executable bit
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, src.read(info))
            n += 1
    return n


def build(ref: str, out_dir: Path, only: str | None = None) -> list[Path]:
    """Write the zips for ref into out_dir; their paths."""
    src = zipfile.ZipFile(io.BytesIO(archive(ref)))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, extra in PLANS.items():
        if only and name != only:
            continue
        path = out_dir / f"epoch-harness-{name}.zip"
        n = filtered(src, COMMON_DROP + extra, path)
        print(f"{path} ({n} entries, {path.stat().st_size / 1e6:.2f} MB)")
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    """CLI entry; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--out", default=str(Path.home() / "Downloads"))
    ap.add_argument("--only", choices=sorted(PLANS))
    args = ap.parse_args(argv)
    try:
        build(args.ref, Path(args.out), args.only)
    except subprocess.CalledProcessError as exc:
        print(
            f"build_zips: git archive failed: {exc.stderr.decode().strip()}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
