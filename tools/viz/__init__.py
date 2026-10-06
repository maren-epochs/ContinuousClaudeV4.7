"""tools.viz - chart tokens and helpers for the dataviz suite.

Kept minimal on purpose: importing this package (or ``tools.viz.palette``)
must not pull in matplotlib/plotly. Submodules load lazily via attribute
access, e.g. ``from tools.viz import palette``.
"""
import importlib

__version__ = "0.1.0"

_SUBMODULES = ("palette", "validate_palette", "style", "recommend", "export")


def __getattr__(name):
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_SUBMODULES))
