"""tools.viz - chart tokens and helpers for the dataviz suite.

Kept minimal on purpose: importing this package (or ``tools.viz.palette``)
must not pull in matplotlib/plotly. Submodules load lazily via attribute
access, e.g. ``from tools.viz import palette``.

Two import names, one package: in the repo it is ``tools.viz``; consumers
(the /visualize PRELUDE) register this directory as ``ccv_viz`` through
importlib, because a project's own regular ``tools`` package shadows the
namespace ``~/.claude/tools``. Intra-package imports are relative, so every
module works under either name.
"""
import importlib

__version__ = "0.1.0"

_SUBMODULES = ("palette", "validate_palette", "style", "recommend", "export", "artifact_page")


def __getattr__(name):
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_SUBMODULES))
