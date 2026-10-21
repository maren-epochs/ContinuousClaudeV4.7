"""Generate requirements.lock from pyproject.toml against the installed host env.

The lock is the transitive closure of every requirement in pyproject.toml
([project] dependencies + every [project.optional-dependencies] group), pinned
with `==` to the version installed in the running interpreter. Nothing is
downloaded or resolved: what is pinned is what was verified on this host.

    py -3.13 tools/lock_requirements.py            # rewrite requirements.lock
    py -3.13 tools/lock_requirements.py --check    # exit 1 if the lock is stale

Edges whose marker depends on the platform or Python version (for example
`colorama; sys_platform == "win32"`) keep that marker in the lock when every
path to the package is conditional, so the file stays installable elsewhere.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import platform
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# packaging is in the locked set (pytest, matplotlib and others require it).
from packaging.markers import Marker
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "requirements.lock"

# Marker variables whose value differs between hosts (everything but `extra`).
_ENV_VARS = re.compile(
    r"\b(sys_platform|platform_system|os_name|platform_machine|platform_release|"
    r"platform_version|platform_python_implementation|implementation_name|"
    r"implementation_version|python_version|python_full_version)\b"
)

# Reason comments emitted above specific pins (mirrors tools/requirements-viz.txt).
NOTES: dict[str, list[str]] = {
    "bokeh": [
        "bokeh pinned below 3.10: panel 1.9.4 fails on import with bokeh 3.10.0, which",
        "breaks holoviews/hvplot (verified 2026-10-05; re-test before raising).",
    ],
    "vl-convert-python": ["keep <2: 2.0 is still rc-only."],
}


def normalize(name: str) -> str:
    """PEP 503 project key: runs of -_. collapsed to '-', lowercased."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Node:
    """One installed package in the closure: pin, extras walked, edges, markers."""

    name: str
    version: str
    extras: set[str] = field(default_factory=set)
    direct: bool = False
    # (child key, host-dependent edge marker or None)
    edges: list[tuple[str, str | None]] = field(default_factory=list)
    unconditional: bool = False
    markers: set[str] = field(default_factory=set)


def _classify(marker: Marker | None, new_extras: set[str]) -> tuple[bool, str | None]:
    """(edge active for the newly requested extras, host-dependent marker or None).

    `new_extras` holds "" when the package's base requirements are being walked.
    Extra-gated edges are unconditional once the extra is chosen; mixed
    extra + platform edges are pinned unconditionally (documented trade-off).
    """
    base = "" in new_extras
    if marker is None:
        return base, None
    text = str(marker)
    if "extra" in text:
        named = [e for e in new_extras if e]
        return any(marker.evaluate({"extra": e}) for e in named), None
    if not base or not marker.evaluate({"extra": ""}):
        return False, None
    return True, (text if _ENV_VARS.search(text) else None)


def collect(roots: list[str]) -> dict[str, Node]:
    """Breadth-first closure of `roots` over installed metadata, markers propagated.

    Exits (SystemExit) when a required distribution is not installed.
    """
    nodes: dict[str, Node] = {}
    queue: list[tuple[str, set[str], bool]] = []
    for text in roots:
        req = Requirement(text)
        queue.append((req.name, set(req.extras), True))
    while queue:
        name, extras, direct = queue.pop(0)
        key = normalize(name)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            raise SystemExit(
                f"error: {name} is required but not installed in {sys.executable}"
            )
        node = nodes.get(key)
        new_extras = {normalize(e) for e in extras}
        if node is None:
            node = Node(name=key, version=dist.version)
            nodes[key] = node
            new_extras.add("")
        new_extras -= node.extras
        node.direct |= direct
        if not new_extras:
            continue
        node.extras |= new_extras
        for spec in dist.requires or []:
            req = Requirement(spec)
            active, edge_marker = _classify(req.marker, new_extras)
            if active:
                node.edges.append((normalize(req.name), edge_marker))
                queue.append((req.name, set(req.extras), False))
    _propagate(nodes)
    return nodes


def _propagate(nodes: dict[str, Node]) -> None:
    """Fixpoint: a package is unconditional if some unconditional path reaches it;
    otherwise its marker is the union of the conditional edge markers on its paths.
    Marker sets only draw from the finite set of edge markers, so this terminates."""
    for node in nodes.values():
        node.unconditional = node.direct
    changed = True
    while changed:
        changed = False
        for parent in nodes.values():
            for child_key, marker in parent.edges:
                changed |= _relax(parent, nodes[child_key], marker)


def _relax(parent: Node, child: Node, marker: str | None) -> bool:
    """Apply one parent -> child edge to the child's reachability; True if it changed."""
    if child.unconditional:
        return False
    if parent.unconditional and marker is None:
        child.unconditional = True
        child.markers.clear()
        return True
    add = {marker} if marker is not None else set(parent.markers)
    if add <= child.markers:
        return False
    child.markers |= add
    return True


def _line(node: Node) -> str:
    """`name==version`, plus ` ; marker` (OR-joined) for a conditional package."""
    line = f"{node.name}=={node.version}"
    if not node.unconditional and node.markers:
        markers = sorted(node.markers)
        joined = (
            markers[0] if len(markers) == 1 else " or ".join(f"({m})" for m in markers)
        )
        line += f" ; {joined}"
    return line


def render(nodes: dict[str, Node]) -> str:
    """Full lock text: header comments, direct pins, then transitive pins, by name."""
    py = platform.python_version()
    out = [
        "# requirements.lock - exact pins for every package declared in pyproject.toml",
        "# ([project] dependencies + optional groups research, viz, dev) and their full",
        "# transitive closure, as installed on the host that generated it",
        f"# (CPython {py}, {platform.system()} {platform.machine()}).",
        "#",
        "# Regenerate (after installing/upgrading in the host env, then re-run the tests):",
        "#   py -3.13 tools/lock_requirements.py",
        "#   py -3.13 tools/lock_requirements.py --check   # exit 1 when stale",
        "#   py -3.13 tools/test_deps_lock.py",
        "#",
        "# Install:",
        "#   py -3.13 -m pip install -r requirements.lock",
        "#   py -3.13 -m pip install --no-deps plotly-resampler==0.11.1",
        "#   py -3.13 -m playwright install chromium",
        "#",
        "# plotly-resampler is deliberately NOT a requirement line: 0.11.1 declares plotly<7,",
        "# so pip cannot resolve it together with plotly 7.1.0 (it works at runtime, verified",
        "# 2026-10-05). Its runtime deps (dash, orjson, tsdownsample, numpy, pandas, plotly)",
        "# are pinned below; install the package itself with --no-deps as shown above.",
        "#",
        "# Host-dependent markers are kept where every path to a package is conditional;",
        "# packages behind mixed extra+platform markers are pinned unconditionally.",
        "",
        "# --- direct: named in pyproject.toml ---",
    ]
    ordered = sorted(nodes.values(), key=lambda n: n.name)
    for group, wanted in (("direct", True), ("transitive", False)):
        if group == "transitive":
            out += ["", "# --- transitive ---"]
        for node in ordered:
            if node.direct is not wanted:
                continue
            out += [f"# {note}" for note in NOTES.get(node.name, [])]
            out.append(_line(node))
    return "\n".join(out) + "\n"


def roots_from_pyproject(path: Path = PYPROJECT) -> list[str]:
    """[project] dependencies followed by every optional-dependencies group."""
    with path.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    roots = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        roots.extend(group)
    return roots


def main(argv: list[str] | None = None) -> int:
    """Write the lock (exit 0) or, with --check, exit 1 when it differs."""
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    ap.add_argument(
        "--check", action="store_true", help="exit 1 if requirements.lock is stale"
    )
    ap.add_argument("--output", type=Path, default=LOCK)
    args = ap.parse_args(argv)
    text = render(collect(roots_from_pyproject()))
    if args.check:
        current = (
            args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        )
        if current != text:
            print(
                f"{args.output.name} is stale; run: py -3.13 tools/lock_requirements.py"
            )
            return 1
        print(f"{args.output.name} is up to date")
        return 0
    args.output.write_text(text, encoding="utf-8", newline="\n")
    pins = sum(1 for ln in text.splitlines() if ln and not ln.startswith("#"))
    print(f"wrote {args.output} ({pins} pins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
